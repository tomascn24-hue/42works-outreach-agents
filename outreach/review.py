"""Human-in-the-loop service. The HITL UI (T6) calls these functions; the CLI does too."""
import json

from .context import content_hash
from .guardrails import FOOTER_MARK


class ReviewError(ValueError):
    pass


class ReviewService:
    def __init__(self, ctx):
        self.ctx = ctx

    def pending_drafts(self):
        """Drafts with the source of every sentence, so a reviewer can check them at a glance."""
        out = []
        for m in self.ctx.db.all(
                "SELECT m.*, l.email, l.company FROM messages m JOIN leads l ON l.id=m.lead_id"
                " WHERE m.status='pending_review' ORDER BY m.id"):
            verification = json.loads(m["verification"])
            sources = {}
            for sentence in verification["kept"]:
                for cite in sentence["cites"]:
                    sources[cite] = self._source(cite)
            out.append({
                "id": m["id"], "to": m["email"], "company": m["company"], "touch": m["touch"],
                "variant": m["variant"], "subject": m["subject"], "body": m["body"],
                "sentences": verification["kept"], "dropped": verification["dropped"], "sources": sources,
            })
        return out

    def _source(self, cite):
        if cite.startswith("F"):
            fact = self.ctx.db.one("SELECT text, source_url, fetched_at FROM facts WHERE id=?", (cite[1:],))
            return dict(fact) if fact else None
        claims = {c["id"]: c["text"] for c in self.ctx.settings["approved_claims"]}
        return {"text": claims.get(cite), "source_url": "client-approved claims"}

    def _pending(self, message_id):
        message = self.ctx.db.one("SELECT * FROM messages WHERE id=?", (message_id,))
        if message is None or message["status"] != "pending_review":
            raise ReviewError(f"message {message_id} is not pending review")
        return message

    def approve(self, message_id, reviewer, feedback=None):
        if not reviewer:
            raise ReviewError("a named reviewer is required")
        ctx = self.ctx
        with ctx.db.tx():
            message = self._pending(message_id)
            ctx.db.execute(
                "UPDATE messages SET status='approved', reviewer=?, reviewed_at=?, approved_sha256=?,"
                " review_feedback=? WHERE id=?",
                (reviewer, ctx.now_iso(), message["content_sha256"], feedback, message_id))
            ctx.audit(reviewer, "draft_approved", "message", message_id, {"sha256": message["content_sha256"]})

    def approve_batch(self, message_ids, reviewer):
        """One-click digest approval (SoW risk 3 mitigation). Each id is logged separately."""
        for message_id in message_ids:
            self.approve(message_id, reviewer)
        return len(message_ids)

    def edit_and_approve(self, message_id, reviewer, new_text):
        """A human rewrites the main text. Code keeps the greeting, signature and footer."""
        if not reviewer:
            raise ReviewError("a named reviewer is required")
        ctx = self.ctx
        with ctx.db.tx():
            message = self._pending(message_id)
            main, footer = message["body"].split(FOOTER_MARK, 1)
            parts = main.split("\n\n")
            if len(parts) < 3:
                raise ReviewError("unexpected draft layout")
            body = "\n\n".join([parts[0], " ".join(new_text.split()), *parts[2:]]) + FOOTER_MARK + footer
            sha = content_hash(message["subject"], body)
            ctx.db.execute(
                "UPDATE messages SET body=?, content_sha256=?, approved_sha256=?, status='approved', reviewer=?,"
                " reviewed_at=?, human_edited=1 WHERE id=?",
                (body, sha, sha, reviewer, ctx.now_iso(), message_id))
            ctx.audit(reviewer, "draft_edited_and_approved", "message", message_id,
                      {"old_text": parts[1], "new_text": new_text, "sha256": sha})

    def reject(self, message_id, reviewer, feedback, redraft=True):
        """Feedback goes to the next draft for this lead and into the learning loop."""
        if not reviewer:
            raise ReviewError("a named reviewer is required")
        ctx = self.ctx
        with ctx.db.tx():
            message = self._pending(message_id)
            ctx.db.execute(
                "UPDATE messages SET status='rejected', reviewer=?, reviewed_at=?, review_feedback=? WHERE id=?",
                (reviewer, ctx.now_iso(), feedback, message_id))
            if not redraft:
                ctx.db.execute("UPDATE leads SET status='needs_manual' WHERE id=?", (message["lead_id"],))
            elif message["touch"] == 0:
                ctx.db.execute("UPDATE leads SET status='researched' WHERE id=?", (message["lead_id"],))
            ctx.audit(reviewer, "draft_rejected", "message", message_id, {"feedback": feedback, "redraft": redraft})

    def pending_replies(self):
        return [dict(r) for r in self.ctx.db.all(
            "SELECT * FROM replies WHERE needs_human=1 AND resolved_at IS NULL ORDER BY id")]

    def resolve_reply(self, reply_id, reviewer, resolution):
        """resolution: opt_out, continue, interested or meeting_booked."""
        if not reviewer:
            raise ReviewError("a named reviewer is required")
        if resolution not in ("opt_out", "continue", "interested", "meeting_booked"):
            raise ReviewError(f"unknown resolution {resolution}")
        ctx = self.ctx
        reply = ctx.db.one("SELECT * FROM replies WHERE id=?", (reply_id,))
        if reply is None or reply["resolved_at"]:
            raise ReviewError(f"reply {reply_id} is not awaiting a decision")
        with ctx.db.tx():
            ctx.db.execute("UPDATE replies SET resolved_by=?, resolved_at=?, resolution=? WHERE id=?",
                           (reviewer, ctx.now_iso(), resolution, reply_id))
            if resolution == "opt_out":
                ctx.suppress(reply["from_email"], "opt-out confirmed by human", reviewer)
            if reply["lead_id"]:
                status = {"opt_out": "opted_out", "continue": "in_sequence"}.get(resolution, "goal_met")
                ctx.db.execute("UPDATE leads SET status=? WHERE id=? AND (? = 'opted_out' OR status != 'opted_out')",
                               (status, reply["lead_id"], status))
            ctx.audit(reviewer, "reply_resolved", "reply", reply_id, {"resolution": resolution})

    def requeue_lead(self, lead_id, reviewer):
        """Return a needs_manual lead to the flow once a person has dealt with the cause."""
        if not reviewer:
            raise ReviewError("a named reviewer is required")
        ctx = self.ctx
        lead = ctx.db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        if lead is None or lead["status"] != "needs_manual":
            raise ReviewError(f"lead {lead_id} does not need manual handling")
        if ctx.is_suppressed(lead["email"]):
            raise ReviewError(f"lead {lead_id} is suppressed")
        contacted = ctx.db.one("SELECT 1 FROM messages WHERE lead_id=? AND status='sent'", (lead_id,))
        status = "in_sequence" if contacted else "researched"
        with ctx.db.tx():
            ctx.db.execute("UPDATE leads SET status=? WHERE id=?", (status, lead_id))
            ctx.audit(reviewer, "lead_requeued", "lead", lead_id, {"status": status})
        return status
