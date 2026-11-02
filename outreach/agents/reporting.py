"""Reporting Agent: the campaign performance record and an independent breach audit."""
import csv
import json
from pathlib import Path

from ..context import HARD_LIMITS, content_hash
from ..guardrails import FOOTER_MARK

ACTOR = "reporting_agent"
OPEN_TARGET = 0.03
RESPONSE_TARGET = 0.0001


def _rate(part, whole):
    return part / whole if whole else None


def _pct(value, digits=2):
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def _group(ctx, label_sql):
    rows = ctx.db.all(
        f"SELECT {label_sql} AS label, COUNT(*) AS sent,"
        " SUM(m.delivered_at IS NOT NULL) AS delivered, SUM(m.opened_at IS NOT NULL) AS opened,"
        " SUM(m.replied_at IS NOT NULL) AS replied, SUM(m.bounced_at IS NOT NULL) AS bounced"
        " FROM messages m JOIN leads l ON l.id=m.lead_id WHERE m.status='sent' GROUP BY label ORDER BY label")
    return [dict(r) for r in rows]


def find_breaches(ctx):
    """Re-check every sent email against the rules, independently of the send gate."""
    db = ctx.db
    breaches = []
    for m in db.all("SELECT m.*, l.email FROM messages m JOIN leads l ON l.id=m.lead_id WHERE m.sent_at IS NOT NULL"):
        tag = f"message {m['id']}"
        if not m["reviewer"] or not m["reviewed_at"] or m["reviewed_at"] > m["sent_at"]:
            breaches.append(("unauthorised_send", f"{tag}: no prior human approval"))
        sha = content_hash(m["subject"], m["body"])
        if m["approved_sha256"] != sha:
            breaches.append(("unauthorised_send", f"{tag}: content differs from approved version"))
        if not m["human_edited"]:
            verification = json.loads(m["verification"])
            verified = " ".join(k["text"] for k in verification.get("kept", []))
            parts = m["body"].split(FOOTER_MARK, 1)[0].split("\n\n")
            if not verification.get("ok") or len(parts) < 2 or parts[1] != verified:
                breaches.append(("hallucination", f"{tag}: email text differs from the verified sentences"))
        domain = m["email"].rsplit("@", 1)[-1]
        opted = db.scalar("SELECT MIN(created_at) FROM suppression WHERE value IN (?,?)", (m["email"], domain))
        if opted and m["sent_at"] > opted:
            breaches.append(("missed_opt_out", f"{tag}: sent after suppression"))
        pending = db.scalar(
            "SELECT MIN(received_at) FROM replies WHERE lead_id=? AND intent IN ('opt_out','opt_out_pending')"
            " AND (resolution IS NULL OR resolution='opt_out' OR resolved_at > ?)", (m["lead_id"], m["sent_at"]))
        if pending and m["sent_at"] > pending:
            breaches.append(("missed_opt_out", f"{tag}: sent after an opt-out or pending reply"))
    for r in db.all("SELECT inbox, substr(sent_at,1,10) AS day, COUNT(*) AS n FROM messages"
                    " WHERE status IN ('sending','sent') GROUP BY inbox, day"):
        if r["n"] > HARD_LIMITS["daily_cap_per_inbox"]:
            breaches.append(("volume", f"{r['inbox']} on {r['day']}: {r['n']} sends above the hard cap"))
    return breaches


class ReportingAgent:
    def __init__(self, ctx):
        self.ctx = ctx

    def metrics(self):
        db = self.ctx.db
        totals = dict(db.one(
            "SELECT COUNT(*) AS sent, COALESCE(SUM(delivered_at IS NOT NULL),0) AS delivered,"
            " COALESCE(SUM(opened_at IS NOT NULL),0) AS opened, COALESCE(SUM(bounced_at IS NOT NULL),0) AS bounced"
            " FROM messages WHERE status='sent'"))
        # Automatic replies are excluded: they are not responses.
        replies = db.scalar("SELECT COUNT(DISTINCT lead_id) FROM replies WHERE lead_id IS NOT NULL"
                            " AND intent != 'out_of_office'")
        meetings = db.scalar("SELECT COUNT(DISTINCT lead_id) FROM replies WHERE lead_id IS NOT NULL"
                             " AND (intent='meeting_booked' OR resolution='meeting_booked')")
        positive = db.scalar("SELECT COUNT(DISTINCT lead_id) FROM replies WHERE lead_id IS NOT NULL"
                             " AND intent IN ('interested','meeting_booked')")
        opt_outs = db.scalar("SELECT COUNT(*) FROM suppression WHERE reason NOT IN ('bounced')")
        breaches = find_breaches(self.ctx)
        providers = {r["provider"] for r in db.all(
            "SELECT DISTINCT provider FROM messages WHERE status='sent'")}
        open_rate = _rate(totals["opened"], totals["delivered"])
        response_rate = _rate(replies, totals["delivered"])
        return {
            **totals,
            "replies": replies, "positive_replies": positive, "meetings": meetings, "opt_outs": opt_outs,
            "open_rate": open_rate, "response_rate": response_rate,
            "open_target_met": open_rate is not None and open_rate >= OPEN_TARGET,
            "response_target_met": response_rate is not None and response_rate >= RESPONSE_TARGET,
            "breaches": breaches,
            "test_sends": db.scalar("SELECT COUNT(*) FROM messages WHERE test_recipient IS NOT NULL"),
            "simulated": not providers or "dry-run" in providers
            or bool(db.scalar("SELECT 1 FROM messages WHERE test_recipient IS NOT NULL LIMIT 1")),
            "audit_chain_intact": self.ctx.verify_audit_chain() is None,
            "blocked_attempts": db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='send_blocked'"),
            "sentences_dropped": db.scalar(
                "SELECT COALESCE(SUM(json_array_length(verification, '$.dropped')),0) FROM messages"),
            "pending_review": db.scalar("SELECT COUNT(*) FROM messages WHERE status='pending_review'"),
            "pending_replies": db.scalar(
                "SELECT COUNT(*) FROM replies WHERE needs_human=1 AND resolved_at IS NULL"),
        }

    def write_report(self, out_dir):
        ctx = self.ctx
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        m = self.metrics()
        by = {
            "Subject variant": _group(ctx, "m.variant"),
            "Segment": _group(ctx, "l.segment"),
            "Touch": _group(ctx, "m.touch"),
            "Inbox": _group(ctx, "COALESCE(m.inbox, '-')"),
        }
        lines = ["# Campaign performance record", "", f"Generated: {ctx.now_iso()}", ""]
        if m["test_sends"]:
            lines += [f"> **TEST SENDS.** {m['test_sends']} emails went to the team's test address, not to "
                      "prospects. These figures are not evidence for the success criteria.", ""]
        elif m["simulated"]:
            lines += ["> **SIMULATED DATA.** These figures come from a dry run with no real emails. "
                      "They show the system works, and are not evidence for the success criteria.", ""]
        lines += [
            "## Success criteria", "",
            "| Criterion | Target | Result | Status |", "|---|---|---|---|",
            f"| Open rate (opened / delivered) | at least 3% | {_pct(m['open_rate'])} | {'met' if m['open_target_met'] else 'NOT met'} |",
            f"| Response rate (replies or meetings / delivered) | at least 0.01% | {_pct(m['response_rate'])} | {'met' if m['response_target_met'] else 'NOT met'} |",
            f"| Guardrail breaches | 0 | {len(m['breaches'])} | {'met' if not m['breaches'] else 'NOT met'} |",
            "",
            "Open tracking is unreliable: email clients block or pre-load pixels. Replies and meetings are the firmer signal.",
            "",
            "## Totals", "",
            f"- Sent: {m['sent']}; delivered: {m['delivered']}; bounced: {m['bounced']}; opened: {m['opened']}",
            f"- Replying leads: {m['replies']} (positive: {m['positive_replies']}; meetings: {m['meetings']})",
            f"- Suppressed addresses from opt-outs and complaints: {m['opt_outs']}",
            f"- Waiting for a human: {m['pending_review']} drafts, {m['pending_replies']} replies",
            "",
            "## Guardrails", "",
            f"- Send attempts blocked by the gate: {m['blocked_attempts']}",
            f"- Unverifiable sentences dropped from drafts: {m['sentences_dropped']}",
            f"- Audit chain: {'intact' if m['audit_chain_intact'] else 'TAMPERED'}",
        ]
        lines += [f"- BREACH ({kind}): {text}" for kind, text in m["breaches"]]
        for title, rows in by.items():
            lines += ["", f"## By {title.lower()}", "",
                      f"| {title} | Sent | Delivered | Opened | Open rate | Replied | Response rate |",
                      "|---|---|---|---|---|---|---|"]
            for r in rows:
                lines.append(
                    f"| {r['label']} | {r['sent']} | {r['delivered']} | {r['opened']} | "
                    f"{_pct(_rate(r['opened'], r['delivered']))} | {r['replied']} | "
                    f"{_pct(_rate(r['replied'], r['delivered']))} |")
        report = out / "campaign_report.md"
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with open(out / "performance_record.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            columns = ["id", "email", "segment", "touch", "variant", "subject", "provider", "reviewer",
                       "sent_at", "delivered_at", "opened_at", "bounced_at", "replied_at"]
            writer.writerow(columns)
            for r in ctx.db.all(
                    "SELECT m.id, l.email, l.segment, m.touch, m.variant, m.subject, m.provider, m.reviewer,"
                    " m.sent_at, m.delivered_at, m.opened_at, m.bounced_at, m.replied_at"
                    " FROM messages m JOIN leads l ON l.id=m.lead_id WHERE m.status='sent' ORDER BY m.id"):
                writer.writerow([r[c] for c in columns])
        ctx.audit(ACTOR, "report_written", "report", str(report), {"breaches": len(m["breaches"])})
        return m
