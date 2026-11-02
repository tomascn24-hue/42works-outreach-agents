"""The endpoint adapters, and the examples given in INTEGRATION.md."""
import base64
import hashlib
import hmac
import json
import threading
import time
from pathlib import Path

from test_agents import Base

from outreach.context import Context
from outreach.db import DB
from outreach.integration import (calendly_webhook, resend_webhook, unsubscribe, verify_calendly_signature,
                                  verify_resend_signature)

RESEND_SECRET = "whsec_" + base64.b64encode(b"resend-test-key").decode()
CALENDLY_KEY = "calendly-test-key"


def resend_headers(body, stamp=None, secret=RESEND_SECRET):
    stamp = str(int(stamp or time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"msg_1.{stamp}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"svix-id": "msg_1", "svix-timestamp": stamp, "svix-signature": f"v1,{sig}"}


def calendly_headers(body, stamp=None, key=CALENDLY_KEY):
    stamp = str(int(stamp or time.time()))
    sig = hmac.new(key.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return {"Calendly-Webhook-Signature": f"t={stamp},v1={sig}"}


class AdapterTests(Base):
    def sent(self):
        self.approved_message()
        self.orch.sender.send_due()
        return self.get_message(1)["provider_id"]

    def test_resend_events_update_the_campaign(self):
        provider_id = self.sent()
        for kind in ("email.delivered", "email.opened"):
            body = json.dumps({"type": kind, "data": {"email_id": provider_id}}).encode()
            self.assertEqual(resend_webhook(self.ctx, RESEND_SECRET, resend_headers(body), body), 200)
        message = self.get_message(1)
        self.assertIsNotNone(message["delivered_at"])
        self.assertIsNotNone(message["opened_at"])

    def test_resend_bounce_suppresses(self):
        provider_id = self.sent()
        body = json.dumps({"type": "email.bounced", "data": {"email_id": provider_id}}).encode()
        resend_webhook(self.ctx, RESEND_SECRET, resend_headers(body), body)
        self.assertTrue(self.ctx.is_suppressed("ana@acme.example.test"))

    def test_resend_rejects_forged_old_or_wrong_key_requests(self):
        provider_id = self.sent()
        body = json.dumps({"type": "email.complained", "data": {"email_id": provider_id}}).encode()
        tampered = body.replace(b"complained", b"delivered")
        other_secret = "whsec_" + base64.b64encode(b"other").decode()
        cases = [
            (resend_headers(body), tampered),
            (resend_headers(body, time.time() - 3600), body),
            (resend_headers(body, secret=other_secret), body),
            ({}, body),
        ]
        for headers, sent_body in cases:
            self.assertEqual(resend_webhook(self.ctx, RESEND_SECRET, headers, sent_body), 401)
        self.assertFalse(self.ctx.is_suppressed("ana@acme.example.test"))

    def test_resend_ignores_unused_events_and_rejects_bad_json(self):
        body = b'{"type": "email.clicked", "data": {"email_id": "x"}}'
        self.assertEqual(resend_webhook(self.ctx, RESEND_SECRET, resend_headers(body), body), 200)
        body = b"not json"
        self.assertEqual(resend_webhook(self.ctx, RESEND_SECRET, resend_headers(body), body), 400)

    def test_header_names_are_case_insensitive(self):
        body = b"{}"
        headers = {k.upper(): v for k, v in resend_headers(body).items()}
        self.assertTrue(verify_resend_signature(RESEND_SECRET, headers, body))

    def test_calendly_booking_by_reference(self):
        self.sent()
        ref = self.get_lead()["booking_ref"]
        body = json.dumps({"event": "invitee.created",
                           "payload": {"email": "someone@else.test", "tracking": {"utm_content": ref}}}).encode()
        self.assertEqual(calendly_webhook(self.ctx, CALENDLY_KEY, calendly_headers(body), body), 200)
        self.assertEqual(self.get_lead()["status"], "goal_met")

    def test_calendly_booking_by_email_when_no_reference(self):
        self.sent()
        body = json.dumps({"event": "invitee.created",
                           "payload": {"email": "ana@acme.example.test", "tracking": {}}}).encode()
        calendly_webhook(self.ctx, CALENDLY_KEY, calendly_headers(body), body)
        self.assertEqual(self.get_lead()["status"], "goal_met")

    def test_calendly_rejects_bad_signatures_and_ignores_cancellations(self):
        self.sent()
        body = json.dumps({"event": "invitee.created", "payload": {"email": "ana@acme.example.test"}}).encode()
        self.assertEqual(calendly_webhook(self.ctx, CALENDLY_KEY, calendly_headers(body, key="x"), body), 401)
        self.assertEqual(calendly_webhook(self.ctx, CALENDLY_KEY, {}, body), 401)
        self.assertFalse(verify_calendly_signature(CALENDLY_KEY, {"Calendly-Webhook-Signature": "t=abc,v1=1"}, body))
        cancel = json.dumps({"event": "invitee.canceled", "payload": {"email": "ana@acme.example.test"}}).encode()
        self.assertEqual(calendly_webhook(self.ctx, CALENDLY_KEY, calendly_headers(cancel), cancel), 200)
        self.assertEqual(self.get_lead()["status"], "in_sequence")

    def test_unsubscribe_always_200(self):
        self.sent()
        self.assertEqual(unsubscribe(self.ctx, self.get_lead()["unsubscribe_token"]), 200)
        self.assertEqual(self.get_lead()["status"], "opted_out")
        self.assertEqual(unsubscribe(self.ctx, "unknown-token"), 200)


class SharedDatabaseTests(Base):
    def test_two_connections_write_without_errors(self):
        """The review UI and the scheduler each open their own connection to the same file."""
        path = Path(self.tmp.name) / "t.sqlite3"
        errors = []

        def worker(n):
            try:
                ctx = Context(DB(path))
                for i in range(30):
                    ctx.audit(f"worker{n}", "ping", "test", i)
                ctx.db.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertIsNone(self.ctx.verify_audit_chain())
        self.assertEqual(self.ctx.db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='ping'"), 90)

    def test_examples_in_the_integration_guide(self):
        """The review-UI example from INTEGRATION.md, run as written."""
        self.approved_message()
        from outreach.agents.learning import LearningAgent
        from outreach.agents.reporting import ReportingAgent
        from outreach.review import ReviewService
        ctx = Context(DB(Path(self.tmp.name) / "t.sqlite3"))
        review = ReviewService(ctx)
        self.assertEqual(review.pending_drafts(), [])
        self.assertEqual(review.pending_replies(), [])
        self.assertIn("open_rate", ReportingAgent(ctx).metrics())
        self.assertEqual(ctx.db.all("SELECT * FROM proposals WHERE status='pending'"), [])
        self.assertTrue(callable(LearningAgent(ctx).decide))
        ctx.db.close()


del Base
