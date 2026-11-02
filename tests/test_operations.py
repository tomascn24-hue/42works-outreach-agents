"""Inboxes, time zones, threading, bookings, alerts, stuck sends, mailbox checks and evaluation."""
import csv
import json
import sqlite3
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from test_agents import FACT, MONDAY, PAGE, Base

from outreach.agents.lead_collection import LeadCollectionAgent
from outreach.agents.reporting import ReportingAgent, find_breaches
from outreach.agents.sending import SendingAgent
from outreach.context import SettingsError
from outreach.db import DB, SCHEMA
from outreach.email_verification import VerificationError, ZeroBounceVerifier
from outreach.evaluation import export_sheets, score
from outreach.mailer import DryRunMailer, MailerError
from outreach.notify import ListNotifier
from outreach.web import FakeFetcher

SECOND_INBOX = {"address": "second@demo.example.test", "from_name": "Second Sender",
                "signature": "Second Sender\n42works (demo)"}


def serve(handler_cls, test):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return f"http://127.0.0.1:{server.server_port}"


class Ops(Base):
    def setUp(self):
        super().setUp()
        self.alerts = ListNotifier()
        self.ctx.notifier = self.alerts

    def change(self, **values):
        settings = self.ctx.settings
        settings.update(values)
        self.ctx.save_settings(settings, "rita", "test")

    def many_leads(self, n, **extra):
        pages = {}
        for i in range(n):
            pages[f"https://c{i}.example.test"] = PAGE
            self.collector.add_lead(f"a@c{i}.example.test", "t", first_name="Ana", company="Acme", **extra)
        self.orch.research.fetcher = FakeFetcher(pages)
        self.orch.research_new()
        self.orch.draft_researched()
        self.review.approve_batch([d["id"] for d in self.review.pending_drafts()], "rita")


class InboxTests(Ops):
    def test_each_inbox_has_its_own_cap(self):
        self.change(inboxes=self.ctx.settings["inboxes"] + [SECOND_INBOX])
        self.many_leads(40)
        self.assertEqual(self.orch.sender.send_due()["sent"], 10)  # warm-up day 1: 5 per inbox
        per_inbox = {r[0]: r[1] for r in self.ctx.db.all(
            "SELECT inbox, COUNT(*) FROM messages WHERE status='sent' GROUP BY inbox")}
        self.assertEqual(sorted(per_inbox.values()), [5, 5])

    def test_followup_comes_from_the_same_inbox(self):
        self.change(inboxes=self.ctx.settings["inboxes"] + [SECOND_INBOX])
        _, first = self.approved_message()
        self.orch.sender.send_due()
        self.advance(3)
        self.orch.writer.schedule_followups()
        follow = self.review.pending_drafts()[0]
        self.assertEqual(self.get_message(follow["id"])["inbox"], self.get_message(first)["inbox"])

    def test_lead_keeps_its_inbox_when_inboxes_are_added(self):
        _, first = self.approved_message()
        self.orch.sender.send_due()
        extra = [{"address": f"{n}@demo.example.test", "from_name": n, "signature": n} for n in "abcdefgh"]
        self.change(inboxes=self.ctx.settings["inboxes"] + extra)
        self.advance(3)
        self.orch.writer.schedule_followups()
        follow = self.review.pending_drafts()[0]
        self.assertEqual(self.get_message(follow["id"])["inbox"], "outreach@demo.example.test")

    def test_signature_matches_the_inbox(self):
        self.change(inboxes=[SECOND_INBOX])
        self.approved_message()
        self.assertIn("Second Sender", self.get_message(1)["body"])

    def test_removed_inbox_blocks_its_messages(self):
        _, message_id = self.approved_message()
        self.change(inboxes=[SECOND_INBOX])
        self.assertEqual(self.orch.sender.send_one(message_id), "blocked")

    def test_inbox_settings_are_validated(self):
        for bad in ([], [{"address": "x"}], [SECOND_INBOX, SECOND_INBOX]):
            with self.subTest(bad=bad), self.assertRaises(SettingsError):
                self.change(inboxes=bad)


class TimezoneTests(Ops):
    def test_window_uses_the_recipient_time_zone(self):
        self.collector.add_lead("v@van.example.test", "t", first_name="Vi", company="Acme",
                                timezone="America/Vancouver")
        self.orch.research.fetcher = FakeFetcher({"https://van.example.test": PAGE})
        self.orch.research_new()
        self.orch.draft_researched()
        message_id = self.review.pending_drafts()[0]["id"]
        self.review.approve(message_id, "rita")
        self.assertEqual(self.orch.sender.send_one(message_id), "deferred")  # 07:00 in Vancouver
        self.time["now"] = MONDAY.replace(hour=18)  # 10:00 in Vancouver
        self.assertEqual(self.orch.sender.send_one(message_id), "sent")

    def test_unknown_time_zone_falls_back_to_default(self):
        lead_id = self.collector.add_lead("x@y.example.test", "t", timezone="Mars/Olympus")
        self.assertIsNone(self.get_lead(lead_id)["timezone"])

    def test_csv_time_zone_column(self):
        path = Path(self.tmp.name) / "l.csv"
        path.write_text("email,timezone\nx@y.example.test,America/Vancouver\n", encoding="utf-8")
        self.collector.collect_csv(path)
        self.assertEqual(self.get_lead()["timezone"], "America/Vancouver")

    def test_bad_default_time_zone_rejected(self):
        with self.assertRaises(SettingsError):
            self.change(default_timezone="Nowhere/Here")


class ThreadTests(Ops):
    def sent_twice(self):
        self.approved_message()
        self.orch.sender.send_due()
        self.advance(3)
        self.orch.writer.schedule_followups()
        self.review.approve(self.review.pending_drafts()[0]["id"], "rita")
        self.orch.sender.send_due()

    def test_followup_replies_to_the_first_email(self):
        self.sent_twice()
        first, second = self.mailer.outbox
        self.assertTrue(first["headers"]["Message-ID"].endswith("@demo.example.test>"))
        self.assertNotIn("In-Reply-To", first["headers"])
        self.assertEqual(second["headers"]["In-Reply-To"], first["headers"]["Message-ID"])
        self.assertEqual(second["headers"]["References"], first["headers"]["Message-ID"])
        self.assertNotEqual(second["headers"]["Message-ID"], first["headers"]["Message-ID"])
        self.assertEqual(first["idempotency_key"], "outreach-message-1")

    def test_reply_matched_by_thread_from_another_address(self):
        self.approved_message()
        self.orch.sender.send_due()
        thread = self.mailer.outbox[0]["headers"]["Message-ID"]
        _, intent = self.replies.handle("ana.personal@gmail.example.test", "Yes, happy to chat.", thread)
        self.assertEqual(intent, "interested")
        self.assertEqual(self.get_lead()["status"], "goal_met")

    def test_colleague_opt_out_in_thread_holds_lead_and_suppresses_sender(self):
        self.approved_message()
        self.orch.sender.send_due()
        thread = self.mailer.outbox[0]["headers"]["Message-ID"]
        _, intent = self.replies.handle("boss@acme.example.test", "Please remove me from this.", thread)
        self.assertEqual(intent, "opt_out_pending")
        self.assertEqual(self.get_lead()["status"], "opt_out_pending")
        self.assertTrue(self.ctx.is_suppressed("boss@acme.example.test"))
        self.assertFalse(self.ctx.is_suppressed("ana@acme.example.test"))
        self.assertEqual(len(self.review.pending_replies()), 1)


class BookingTests(Ops):
    def test_booking_link_in_email_and_allowed_by_the_gate(self):
        self.change(meeting_link="https://cal.example.test/42works/intro")
        _, message_id = self.approved_message()
        body = self.get_message(message_id)["body"]
        self.assertIn(f"https://cal.example.test/42works/intro?utm_content={self.get_lead()['booking_ref']}", body)
        self.assertEqual(self.orch.sender.send_one(message_id), "sent")

    def test_link_already_with_query(self):
        self.change(meeting_link="https://cal.example.test/x?month=11")
        self.approved_message()
        self.assertIn("?month=11&utm_content=", self.get_message(1)["body"])

    def test_booking_meets_goal_counts_as_meeting_and_stops_sequence(self):
        self.approved_message()
        self.orch.sender.send_due()
        self.orch.sender.ingest_event(self.get_message(1)["provider_id"], "delivered")
        self.assertIsNotNone(self.replies.record_booking(self.get_lead()["booking_ref"]))
        self.assertEqual(self.get_lead()["status"], "goal_met")
        metrics = ReportingAgent(self.ctx).metrics()
        self.assertEqual((metrics["meetings"], metrics["replies"]), (1, 1))
        self.advance(3)
        self.assertEqual(self.orch.writer.schedule_followups(), 0)
        self.assertTrue(any("Meeting booked" in a for a in self.alerts.sent))

    def test_booking_by_email_and_unmatched(self):
        self.lead()
        self.assertIsNotNone(self.replies.record_booking(email="ANA@acme.example.test"))
        self.assertIsNone(self.replies.record_booking("nope"))

    def test_booking_does_not_revive_an_opt_out(self):
        self.lead()
        self.replies.handle("ana@acme.example.test", "Please remove me")
        self.replies.record_booking(email="ana@acme.example.test")
        self.assertEqual(self.get_lead()["status"], "opted_out")

    def test_meeting_link_must_be_https(self):
        with self.assertRaises(SettingsError):
            self.change(meeting_link="http://cal.example.test")


class AlertTests(Ops):
    def test_positive_and_held_replies_alert_without_the_reply_text(self):
        self.lead()
        self.lead("b@b.example.test")
        self.replies.handle("ana@acme.example.test", "Yes, happy to chat, my number is 555 0100.")
        self.replies.handle("b@b.example.test", "Not now")
        self.assertEqual(len(self.alerts.sent), 2)
        self.assertIn("Positive reply from Acme", self.alerts.sent[0])
        self.assertNotIn("555", " ".join(self.alerts.sent))

    def test_explicit_opt_out_and_out_of_office_do_not_alert(self):
        self.lead()
        self.replies.handle("ana@acme.example.test", "Automatic reply: out of office")
        self.replies.handle("ana@acme.example.test", "unsubscribe")
        self.assertEqual(self.alerts.sent, [])

    def test_digest_only_when_the_queue_changes(self):
        self.lead()
        self.orch.run_cycle(send=False)
        self.orch.run_cycle(send=False)
        self.assertEqual(len(self.alerts.sent), 1)
        self.assertIn("1 drafts", self.alerts.sent[0])

    def test_broken_notifier_never_stops_the_flow(self):
        class Broken:
            def send(self, text):
                raise RuntimeError("down")
        self.ctx.notifier = Broken()
        self.lead()
        self.assertEqual(self.replies.handle("ana@acme.example.test", "Yes, happy to chat.")[1], "interested")


class StuckTests(Ops):
    def stuck_message(self):
        class Dies(DryRunMailer):
            def send(self, *args):
                raise KeyboardInterrupt  # the process dies mid-call
        _, message_id = self.approved_message()
        self.orch.sender.mailer = Dies()
        with self.assertRaises(KeyboardInterrupt):
            self.orch.sender.send_one(message_id)
        self.orch.sender.mailer = self.mailer
        return message_id

    def test_stuck_listed_only_after_a_delay(self):
        message_id = self.stuck_message()
        self.assertEqual(self.orch.sender.stuck(), [])
        self.time["now"] += timedelta(minutes=20)
        self.assertEqual([s["id"] for s in self.orch.sender.stuck()], [message_id])

    def test_not_sent_is_retried_with_the_same_ids(self):
        message_id = self.stuck_message()
        header = self.get_message(message_id)["message_id_header"]
        self.time["now"] += timedelta(minutes=20)
        self.orch.sender.resolve_stuck(message_id, "rita", "not_sent")
        self.assertEqual(self.orch.sender.send_due()["sent"], 1)
        self.assertEqual(self.mailer.outbox[0]["headers"]["Message-ID"], header)
        self.assertEqual(self.mailer.outbox[0]["idempotency_key"], f"outreach-message-{message_id}")

    def test_sent_is_recorded(self):
        message_id = self.stuck_message()
        self.orch.sender.resolve_stuck(message_id, "rita", "sent", "provider-1")
        self.assertEqual(self.get_message(message_id)["status"], "sent")
        self.assertEqual(self.get_lead()["status"], "in_sequence")
        self.assertEqual(find_breaches(self.ctx), [])

    def test_resolution_needs_a_person_and_a_stuck_message(self):
        message_id = self.stuck_message()
        with self.assertRaises(ValueError):
            self.orch.sender.resolve_stuck(message_id, "", "sent")
        self.orch.sender.resolve_stuck(message_id, "rita", "not_sent")
        with self.assertRaises(ValueError):
            self.orch.sender.resolve_stuck(message_id, "rita", "sent")

    def test_stuck_shows_in_digest(self):
        self.stuck_message()
        self.time["now"] += timedelta(minutes=20)
        self.orch.review_digest()
        self.assertIn("1 emails stuck", self.alerts.sent[-1])


class VerificationServiceTests(Ops):
    def zerobounce(self, statuses):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status = statuses.pop(0)
                self.send_response(500 if status is None else 200)
                self.end_headers()
                self.wfile.write(json.dumps({"status": status}).encode())
            def log_message(self, *args):
                pass
        return ZeroBounceVerifier(api_key="k", base_url=serve(Handler, self))

    def test_only_valid_mailboxes_are_kept(self):
        verifier = self.zerobounce(["valid", "invalid", "catch-all", "unknown", None])
        agent = LeadCollectionAgent(self.ctx, domain_check=lambda d: True, verifier=verifier)
        kept = [agent.add_lead(f"p{i}@x.example.test", "t") is not None for i in range(5)]
        self.assertEqual(kept, [True, False, False, False, False])
        reasons = [json.loads(r["details"])["reason"] for r in self.ctx.db.all(
            "SELECT details FROM audit_log WHERE action='lead_refused'")]
        self.assertIn("mailbox verification: invalid", reasons)
        self.assertTrue(reasons[-1].startswith("mailbox verification unavailable"))

    def test_catch_all_can_be_accepted(self):
        verifier = self.zerobounce(["catch-all"])
        verifier.accepted.add("catch-all")
        self.assertEqual(verifier.check("a@b.test"), (True, "catch-all"))

    def test_missing_key(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZEROBOUNCE_API_KEY", None)
            with self.assertRaises(VerificationError):
                ZeroBounceVerifier()


class EvaluationTests(Ops):
    def test_export_rate_and_score(self):
        self.many_leads(3)
        out = Path(self.tmp.name) / "eval"
        self.assertEqual(export_sheets(self.ctx, out), {"emails": 3, "sentences": 6})
        for name, column, answers in (("sentences.csv", "faithful_y_n", ["s", "y", "n", "y", "y", ""]),
                                      ("emails.csv", "would_send_y_n", ["sim", "não", "y"])):
            with open(out / name, newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle, delimiter=";"))
            for row, answer in zip(rows, answers):
                row[column] = answer
            with open(out / name, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys(), delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
        result = score(self.ctx, out, out / "results.md")
        self.assertEqual((result["sentences_rated"], result["unfaithful_sentences"]), (5, 1))
        self.assertAlmostEqual(result["hallucination_rate"], 0.2)
        self.assertEqual((result["emails_rated"], result["emails_acceptable"]), (3, 2))
        self.assertIn("Not supported by their source", (out / "results.md").read_text(encoding="utf-8"))

    def test_sources_are_in_the_sheet(self):
        self.many_leads(1)
        out = Path(self.tmp.name) / "eval"
        export_sheets(self.ctx, out)
        text = (out / "sentences.csv").read_text(encoding="utf-8-sig")
        self.assertIn(FACT, text)
        self.assertIn("https://c0.example.test", text)


class MigrationTests(Ops):
    def test_old_database_gains_new_columns(self):
        path = Path(self.tmp.name) / "old.sqlite3"
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)  # the first version's tables, without the later columns
        conn.close()
        db = DB(path)
        columns = {r[1] for r in db.all("PRAGMA table_info(messages)")}
        lead_columns = {r[1] for r in db.all("PRAGMA table_info(leads)")}
        db.close()
        self.assertTrue({"inbox", "message_id_header", "in_reply_to"} <= columns)
        self.assertTrue({"timezone", "booking_ref"} <= lead_columns)


class ResendHeaderTests(Ops):
    def test_thread_headers_and_idempotency_reach_resend(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.headers.get("Idempotency-Key"),
                             json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"id": "r1"}')
            def log_message(self, *args):
                pass
        from outreach.mailer import ResendMailer
        mailer = ResendMailer(api_key="k", base_url=serve(Handler, self))
        mailer.send("a@b.test", "S <s@b.test>", "x", "y", "https://u.test/1",
                    {"Message-ID": "<m@b.test>", "In-Reply-To": "<p@b.test>"}, "outreach-message-9")
        key, payload = seen[0]
        self.assertEqual(key, "outreach-message-9")
        self.assertEqual(payload["headers"]["In-Reply-To"], "<p@b.test>")
        self.assertEqual(payload["headers"]["List-Unsubscribe"], "<https://u.test/1>")
        with self.assertRaises(MailerError):
            ResendMailer(api_key="k", base_url="http://127.0.0.1:1").send("a", "b", "c", "d", "e")


class ReportInboxTests(Ops):
    def test_report_has_inbox_table(self):
        self.approved_message()
        self.orch.sender.send_due()
        ReportingAgent(self.ctx).write_report(Path(self.tmp.name) / "r")
        report = (Path(self.tmp.name) / "r" / "campaign_report.md").read_text(encoding="utf-8")
        self.assertIn("## By inbox", report)
        self.assertIn("outreach@demo.example.test", report)


del Base  # keep unittest from running the imported base tests twice
