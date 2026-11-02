"""Learning Agent (T8): proposes improvements from results. A human applies them; it never does."""
import json
import math

from ..context import canonical, sha256

ACTOR = "learning_agent"
_Z = 1.96


def wilson(successes, n):
    """95% Wilson interval, so small samples do not look like winners."""
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    denominator = 1 + _Z ** 2 / n
    centre = (p + _Z ** 2 / (2 * n)) / denominator
    margin = _Z * math.sqrt(p * (1 - p) / n + _Z ** 2 / (4 * n ** 2)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


class LearningAgent:
    def __init__(self, ctx):
        self.ctx = ctx

    def _propose(self, kind, rationale, evidence, change):
        key = sha256(canonical([kind, change]))
        ctx = self.ctx
        if ctx.db.one("SELECT 1 FROM proposals WHERE dedupe_key=?", (key,)):
            return None
        with ctx.db.tx():
            cur = ctx.db.execute(
                "INSERT INTO proposals(dedupe_key, kind, rationale, evidence, change, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (key, kind, rationale, canonical(evidence), canonical(change), ctx.now_iso()),
            )
            ctx.audit(ACTOR, "proposal_created", "proposal", cur.lastrowid, {"kind": kind})
        return cur.lastrowid

    def analyse(self):
        created = [self._variants(), self._segments(), self._cadence(), self._feedback(), *self._best_practices()]
        return [p for p in created if p]

    def _variants(self):
        s = self.ctx.settings
        rows = self.ctx.db.all(
            "SELECT variant, SUM(delivered_at IS NOT NULL) AS d, SUM(opened_at IS NOT NULL) AS o,"
            " SUM(replied_at IS NOT NULL) AS r FROM messages WHERE status='sent' GROUP BY variant")
        active = [r for r in rows if r["variant"] in s["subject_variants"] and r["d"] >= s["min_sample_per_variant"]]
        if len(active) < 2:
            return None
        ranked = sorted(active, key=lambda r: r["o"] / r["d"], reverse=True)
        best, worst = ranked[0], ranked[-1]
        if wilson(best["o"], best["d"])[0] <= wilson(worst["o"], worst["d"])[1]:
            return None  # intervals overlap: no real difference yet
        return self._propose(
            "retire_variant",
            f"Subject variant {worst['variant']} opens clearly below variant {best['variant']}.",
            {r["variant"]: {"delivered": r["d"], "opened": r["o"], "replied": r["r"]} for r in rows},
            {"op": "retire_variant", "variant": worst["variant"]},
        )

    def _segments(self):
        s = self.ctx.settings
        rows = self.ctx.db.all(
            "SELECT l.segment AS segment, SUM(m.delivered_at IS NOT NULL) AS d, SUM(m.opened_at IS NOT NULL) AS o,"
            " SUM(m.replied_at IS NOT NULL) AS r FROM messages m JOIN leads l ON l.id=m.lead_id"
            " WHERE m.status='sent' GROUP BY l.segment")
        active = [r for r in rows if r["d"] >= s["min_sample_per_variant"]]
        if len(active) < 2:
            return None
        ranked = sorted(active, key=lambda r: r["o"] / r["d"], reverse=True)
        best, worst = ranked[0], ranked[-1]
        if wilson(best["o"], best["d"])[0] <= wilson(worst["o"], worst["d"])[1]:
            return None
        return self._propose(
            "review_segment",
            f"Segment {worst['segment']} opens clearly below segment {best['segment']}.",
            {r["segment"]: {"delivered": r["d"], "opened": r["o"], "replied": r["r"]} for r in rows},
            {"op": "note", "text": f"Review targeting or messaging for segment {worst['segment']}."},
        )

    def _cadence(self):
        s = self.ctx.settings
        if len(s["cadence_days"]) < 2:
            return None
        last = len(s["cadence_days"])
        r = self.ctx.db.one(
            "SELECT SUM(delivered_at IS NOT NULL) AS d, SUM(replied_at IS NOT NULL) AS replied"
            " FROM messages WHERE status='sent' AND touch=?", (last,))
        if not r["d"] or r["d"] < s["min_sample_per_variant"] or r["replied"]:
            return None
        return self._propose(
            "set_cadence",
            f"The last follow-up (touch {last}) had no replies in {r['d']} deliveries; dropping it reduces contact.",
            {"touch": last, "delivered": r["d"], "replied": r["replied"]},
            {"op": "set_cadence", "cadence_days": s["cadence_days"][:-1]},
        )

    def _feedback(self):
        rows = self.ctx.db.all(
            "SELECT review_feedback AS f, COUNT(*) AS n FROM messages WHERE status='rejected'"
            " AND review_feedback IS NOT NULL GROUP BY f ORDER BY n DESC")
        if not rows or rows[0]["n"] < 2:
            return None
        top = rows[0]
        return self._propose(
            "add_guidance",
            f"Reviewers gave the same feedback on {top['n']} rejected drafts.",
            {"feedback": top["f"], "count": top["n"]},
            {"op": "add_guidance", "text": top["f"]},
        )

    def _best_practices(self):
        """Compare drafts with the curated, human-maintained best_practices in the settings."""
        s = self.ctx.settings
        practices = s["best_practices"]
        rows = self.ctx.db.all(
            "SELECT verification, subject FROM messages WHERE status IN ('sent','approved','pending_review')"
            " AND settings_version=?", (self.ctx.settings_version,))
        if len(rows) < 10:
            return []
        out = []
        words = [sum(len(k["text"].split()) for k in json.loads(r["verification"])["kept"]) for r in rows]
        average = sum(words) / len(words)
        if "max_body_words" in practices and average > practices["max_body_words"]:
            out.append(self._propose(
                "add_guidance",
                f"Average draft is {average:.0f} words; the best-practice target is {practices['max_body_words']}.",
                {"average_words": round(average, 1), "drafts": len(rows)},
                {"op": "add_guidance", "text": f"Keep the email under {practices['max_body_words']} words."},
            ))
        if "max_subject_chars" in practices:
            for name, subject in s["subject_variants"].items():
                if len(subject) > practices["max_subject_chars"]:
                    out.append(self._propose(
                        "review_subject",
                        f"Subject variant {name} is over {practices['max_subject_chars']} characters.",
                        {"variant": name, "subject": subject},
                        {"op": "note", "text": f"Rewrite subject variant {name} more briefly."},
                    ))
        return out

    def _cancel_variant(self, variant, approver):
        """Unsent drafts of a retired subject are redrafted; the send gate would block them anyway."""
        ctx = self.ctx
        for m in ctx.db.all("SELECT id, lead_id, touch FROM messages WHERE variant=?"
                            " AND status IN ('pending_review','approved')", (variant,)):
            ctx.db.execute("UPDATE messages SET status='cancelled' WHERE id=?", (m["id"],))
            if m["touch"] == 0:
                ctx.db.execute("UPDATE leads SET status='researched' WHERE id=?", (m["lead_id"],))
            ctx.audit(approver, "message_cancelled", "message", m["id"], {"reason": "variant retired"})

    def decide(self, proposal_id, approver, approved):
        """Human decision. Applying creates a new settings version, validated against hard limits."""
        ctx = self.ctx
        if not approver:
            raise ValueError("a named human approver is required")
        proposal = ctx.db.one("SELECT * FROM proposals WHERE id=?", (proposal_id,))
        if proposal is None or proposal["status"] != "pending":
            raise ValueError("proposal is not pending")
        with ctx.db.tx():
            if approved:
                settings = ctx.settings
                change = json.loads(proposal["change"])
                op = change["op"]
                if op == "retire_variant":
                    settings["subject_variants"].pop(change["variant"], None)
                    self._cancel_variant(change["variant"], approver)
                elif op == "set_cadence":
                    settings["cadence_days"] = change["cadence_days"]
                elif op == "add_guidance":
                    if change["text"] not in settings["writer_guidance"]:
                        settings["writer_guidance"].append(change["text"])
                if op != "note":
                    ctx.save_settings(settings, approver, f"proposal {proposal_id}: {proposal['rationale']}")
            status = "approved" if approved else "rejected"
            ctx.db.execute("UPDATE proposals SET status=?, decided_by=?, decided_at=? WHERE id=?",
                           (status, approver, ctx.now_iso(), proposal_id))
            ctx.audit(approver, f"proposal_{status}", "proposal", proposal_id, {})
