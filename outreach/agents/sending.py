"""Sending Agent: the only code path that calls a mailer, always through the guardrail gate."""
import uuid
from datetime import timedelta

from ..context import iso
from ..guardrails import check_send
from ..mailer import MailerError

ACTOR = "sending_agent"
STUCK_AFTER_MINUTES = 15


class SendingAgent:
    def __init__(self, ctx, mailer):
        self.ctx = ctx
        self.mailer = mailer

    def send_due(self):
        ctx = self.ctx
        summary = {"sent": 0, "blocked": 0, "deferred": 0, "failed": 0}
        ids = [r["id"] for r in ctx.db.all(
            "SELECT id FROM messages WHERE status='approved' ORDER BY scheduled_for, id")]
        for message_id in ids:
            summary[self.send_one(message_id)] += 1
        return summary

    def preview(self):
        """What the gate would do now. Changes nothing, so approvals are not used up."""
        out = []
        for message in self.ctx.db.all("SELECT * FROM messages WHERE status='approved' ORDER BY scheduled_for, id"):
            lead = self.ctx.db.one("SELECT * FROM leads WHERE id=?", (message["lead_id"],))
            decision = check_send(self.ctx, message, lead)
            out.append({"id": message["id"], "to": lead["email"], "from": message["inbox"],
                        "subject": message["subject"], "would_send": decision.allowed,
                        "blocked": decision.hard, "deferred": decision.soft})
        return out

    def _flag_stuck_lead(self, message):
        """A first email that will not leave would otherwise strand the lead without notice."""
        if message["touch"] == 0:
            self.ctx.db.execute("UPDATE leads SET status='needs_manual' WHERE id=? AND status='drafted'",
                                (message["lead_id"],))

    def send_one(self, message_id):
        ctx = self.ctx
        settings = ctx.settings
        with ctx.db.tx():
            message = ctx.db.one("SELECT * FROM messages WHERE id=?", (message_id,))
            lead = ctx.db.one("SELECT * FROM leads WHERE id=?", (message["lead_id"],))
            decision = check_send(ctx, message, lead)
            if decision.hard:
                # A message that never entered the approved state stays as it is.
                if message["status"] == "approved":
                    ctx.db.execute("UPDATE messages SET status='blocked' WHERE id=?", (message_id,))
                    self._flag_stuck_lead(message)
                ctx.audit(ACTOR, "send_blocked", "message", message_id, {"reasons": decision.hard})
                return "blocked"
            if decision.soft:
                ctx.audit(ACTOR, "send_deferred", "message", message_id, {"reasons": decision.soft})
                return "deferred"

            inbox = next(i for i in settings["inboxes"] if i["address"] == message["inbox"])
            # Kept across a retry so the provider and the recipient see one message, not two.
            message_id_header = message["message_id_header"] or \
                f"<{uuid.uuid4().hex}@{inbox['address'].rsplit('@', 1)[1]}>"
            thread = None
            if message["touch"] > 0:
                thread = ctx.db.scalar(
                    "SELECT message_id_header FROM messages WHERE lead_id=? AND touch=0 AND status='sent'",
                    (lead["id"],))
            test_recipient = settings.get("test_recipient") or None
            # Reserved before the provider call so a crash can never cause a double send.
            ctx.db.execute(
                "UPDATE messages SET status='sending', sent_at=?, provider=?, message_id_header=?, in_reply_to=?,"
                " test_recipient=? WHERE id=?",
                (ctx.now_iso(), self.mailer.name, message_id_header, thread, test_recipient, message_id))
            ctx.audit(ACTOR, "send_authorised", "message", message_id,
                      {"approved_by": message["reviewer"], "inbox": inbox["address"]})

        headers = {"Message-ID": message_id_header}
        if thread:
            headers.update({"In-Reply-To": thread, "References": thread})
        to, subject = lead["email"], message["subject"]
        if test_recipient:
            to, subject = test_recipient, f"[TEST for {lead['email']}] {subject}"
        try:
            provider_id = self.mailer.send(
                to, f"{inbox['from_name']} <{inbox['address']}>", subject, message["body"],
                settings["unsubscribe_base_url"] + lead["unsubscribe_token"],
                headers, f"outreach-message-{message_id}")
        except MailerError as exc:
            with ctx.db.tx():
                ctx.db.execute("UPDATE messages SET status='failed' WHERE id=?", (message_id,))
                self._flag_stuck_lead(message)
                ctx.audit(ACTOR, "send_failed", "message", message_id, {"error": str(exc)})
            return "failed"

        self._mark_sent(message, provider_id)
        return "sent"

    def _mark_sent(self, message, provider_id, actor=ACTOR):
        ctx = self.ctx
        with ctx.db.tx():
            ctx.db.execute("UPDATE messages SET status='sent', provider_id=? WHERE id=?", (provider_id, message["id"]))
            ctx.db.execute("UPDATE leads SET status='in_sequence' WHERE id=? AND status='drafted'",
                           (message["lead_id"],))
            ctx.audit(actor, "email_sent", "message", message["id"],
                      {"provider": self.mailer.name, "provider_id": provider_id, "touch": message["touch"]})

    def stuck(self, minutes=STUCK_AFTER_MINUTES):
        """Messages whose provider call never finished, for example because the process died."""
        cutoff = iso(self.ctx.now() - timedelta(minutes=minutes))
        return [dict(r) for r in self.ctx.db.all(
            "SELECT m.id, l.email, m.inbox, m.subject, m.sent_at, m.message_id_header FROM messages m"
            " JOIN leads l ON l.id=m.lead_id WHERE m.status='sending' AND m.sent_at <= ? ORDER BY m.id", (cutoff,))]

    def resolve_stuck(self, message_id, reviewer, outcome, provider_id=None):
        """After checking the provider's log: 'sent' records it; 'not_sent' releases it for the next run.

        A retry reuses the message's idempotency key, so a provider that honours it will not
        deliver twice even if the first attempt did go out.
        """
        if not reviewer:
            raise ValueError("a named reviewer is required")
        if outcome not in ("sent", "not_sent"):
            raise ValueError("outcome must be sent or not_sent")
        ctx = self.ctx
        message = ctx.db.one("SELECT * FROM messages WHERE id=?", (message_id,))
        if message is None or message["status"] != "sending":
            raise ValueError(f"message {message_id} is not stuck in sending")
        if outcome == "sent":
            self._mark_sent(message, provider_id, actor=reviewer)
        else:
            with ctx.db.tx():
                ctx.db.execute("UPDATE messages SET status='approved', sent_at=NULL WHERE id=?", (message_id,))
                ctx.audit(reviewer, "stuck_released", "message", message_id, {})

    def ingest_event(self, provider_id, event):
        """Provider webhook events: delivered, opened, bounced, complained."""
        ctx = self.ctx
        message = ctx.db.one("SELECT * FROM messages WHERE provider_id=?", (provider_id,))
        if message is None:
            ctx.audit(ACTOR, "event_unmatched", "event", provider_id, {"event": event})
            return False
        now = ctx.now_iso()
        lead = ctx.db.one("SELECT * FROM leads WHERE id=?", (message["lead_id"],))
        with ctx.db.tx():
            if event == "delivered":
                ctx.db.execute("UPDATE messages SET delivered_at=COALESCE(delivered_at, ?)"
                               " WHERE id=? AND bounced_at IS NULL", (now, message["id"]))
            elif event == "opened":
                ctx.db.execute(
                    "UPDATE messages SET opened_at=COALESCE(opened_at, ?), delivered_at=COALESCE(delivered_at, ?)"
                    " WHERE id=? AND bounced_at IS NULL", (now, now, message["id"]))
            elif event in ("bounced", "complained"):
                if event == "bounced":
                    ctx.db.execute("UPDATE messages SET bounced_at=?, delivered_at=NULL WHERE id=?",
                                   (now, message["id"]))
                status = "bounced" if event == "bounced" else "opted_out"
                ctx.db.execute("UPDATE leads SET status=? WHERE id=?", (status, lead["id"]))
                ctx.suppress(lead["email"], event, ACTOR)
                ctx.cancel_open_messages(lead["id"], event, ACTOR)
            else:
                ctx.audit(ACTOR, "event_ignored", "message", message["id"], {"event": event})
                return False
            ctx.audit(ACTOR, f"event_{event}", "message", message["id"], {})
        return True
