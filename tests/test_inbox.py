"""IMAP reply reading and test-mode sending."""
from email.message import EmailMessage
from pathlib import Path

from test_agents import Base

from outreach.agents.reporting import ReportingAgent
from outreach.inbox_reader import ImapReplyReader
from outreach.notify import ListNotifier

TEAM = "team-inbox@uni.example.test"


class FakeImap:
    """Just enough of imaplib.IMAP4 for the reader."""

    def __init__(self, messages):
        self.messages = {str(i + 1).encode(): m for i, m in enumerate(messages)}
        self.seen = set()
        self.logged_in = self.logged_out = False

    def __call__(self, host, port):
        return self

    def login(self, user, password):
        self.logged_in = True

    def select(self, folder):
        return "OK", [b"1"]

    def search(self, charset, criterion):
        return "OK", [b" ".join(n for n in self.messages if n not in self.seen)]

    def fetch(self, number, spec):
        assert "PEEK" in spec  # reading must not mark the message as read
        message = self.messages[number]
        raw = None if message is None else message.as_bytes()  # None: the server returned no body
        return "OK", [(number + b" (BODY[] {1})", raw), b")"]

    def store(self, number, op, flags):
        self.seen.add(number)

    def logout(self):
        self.logged_out = True


def mail(sender, body, in_reply_to=None, message_id=None, html=False, **headers):
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "outreach@demo.example.test"
    m["Subject"] = "Re: A question for Acme"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    if message_id:
        m["Message-ID"] = message_id
    for key, value in headers.items():
        m[key.replace("_", "-")] = value
    if html:
        m.set_content("fallback")
        m.add_alternative(f"<p>{body}</p>", subtype="html")
        m.get_body(("plain",)).set_content(body)
    else:
        m.set_content(body)
    return m


class InboxTests(Base):
    def setUp(self):
        super().setUp()
        self.approved_message()
        self.orch.sender.send_due()
        self.thread = self.mailer.outbox[0]["headers"]["Message-ID"]

    def poll(self, *messages):
        imap = FakeImap(messages)
        reader = ImapReplyReader(self.ctx, "imap.example.test", "u", "p", imap_factory=imap)
        return reader.poll(), imap

    def test_reply_is_matched_by_thread_and_marked_read(self):
        summary, imap = self.poll(mail("Ana <ana.other@gmail.example.test>", "Yes, happy to chat.", self.thread))
        self.assertEqual(summary, {"handled": 1, "skipped": 0, "unreadable": 0})
        self.assertEqual(self.get_lead()["status"], "goal_met")
        self.assertEqual(imap.seen, {b"1"})
        self.assertTrue(imap.logged_out)

    def test_quoted_reply_with_unsubscribe_footer_is_not_an_opt_out(self):
        body = ("Sounds good, happy to meet.\n\nOn Mon, Demo wrote:\n> Hi Ana,\n> -- \n"
                "> Unsubscribe: https://demo.example.test/u/x\n")
        self.poll(mail("ana@acme.example.test", body, self.thread))
        self.assertEqual(self.get_lead()["status"], "goal_met")
        self.assertFalse(self.ctx.is_suppressed("ana@acme.example.test"))

    def test_opt_out_by_email_suppresses(self):
        self.poll(mail("ana@acme.example.test", "Please remove me from your list."))
        self.assertTrue(self.ctx.is_suppressed("ana@acme.example.test"))

    def test_html_only_reply_is_read(self):
        m = EmailMessage()
        m["From"] = "ana@acme.example.test"
        m.set_content("<p>Not interested, thanks.</p>", subtype="html")
        self.poll(m)
        self.assertEqual(self.get_lead()["status"], "opt_out_pending")

    def test_automatic_reply_header_is_out_of_office(self):
        self.poll(mail("ana@acme.example.test", "Thank you for your message. I will not be able to reply.",
                       self.thread, Auto_Submitted="auto-replied"))
        self.assertEqual(self.get_lead()["status"], "in_sequence")
        self.assertIsNotNone(self.get_lead()["hold_until"])

    def test_automatic_reply_still_honours_an_opt_out(self):
        self.poll(mail("ana@acme.example.test", "Automatic reply. To unsubscribe, reply STOP.",
                       Auto_Submitted="auto-replied"))
        self.assertEqual(self.get_lead()["status"], "opted_out")

    def test_bounce_notices_and_own_copies_are_skipped(self):
        bounce = mail("Mail Delivery System <MAILER-DAEMON@mx.example.test>", "Delivery failed")
        own = mail("outreach@demo.example.test", "copy of our own email")
        summary, imap = self.poll(bounce, own)
        self.assertEqual(summary["skipped"], 2)
        self.assertEqual(self.ctx.db.scalar("SELECT COUNT(*) FROM replies"), 0)
        self.assertEqual(imap.seen, {b"1", b"2"})

    def test_same_message_is_never_handled_twice(self):
        first = mail("ana@acme.example.test", "Not now", message_id="<r1@acme.example.test>")
        self.poll(first)
        summary, _ = self.poll(mail("ana@acme.example.test", "Not now", message_id="<r1@acme.example.test>"))
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(self.ctx.db.scalar("SELECT COUNT(*) FROM replies"), 1)

    def test_unreadable_message_is_reported_and_others_continue(self):
        alerts = ListNotifier()
        self.ctx.notifier = alerts
        summary, _ = self.poll(None, mail("ana@acme.example.test", "Yes, happy to chat."))
        self.assertEqual((summary["unreadable"], summary["handled"]), (1, 1))
        self.assertTrue(any("could not be read" in a for a in alerts.sent))

    def test_references_header_used_when_in_reply_to_missing(self):
        self.poll(mail("other@acme.example.test", "Yes, happy to chat.", References=f"<a@x.test> {self.thread}"))
        self.assertEqual(self.get_lead()["status"], "goal_met")

    def test_password_required(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IMAP_PASSWORD", None)
            with self.assertRaises(ValueError):
                ImapReplyReader(self.ctx, "h", "u")


class TestModeTests(Base):
    def setUp(self):
        super().setUp()
        settings = self.ctx.settings
        settings["test_recipient"] = TEAM
        self.ctx.save_settings(settings, "rita", "test mode")

    def test_every_email_goes_to_the_team_inbox(self):
        self.approved_message()
        self.orch.sender.send_due()
        sent = self.mailer.outbox[0]
        self.assertEqual(sent["to"], TEAM)
        self.assertTrue(sent["subject"].startswith("[TEST for ana@acme.example.test] "))
        self.assertEqual(self.get_message(1)["test_recipient"], TEAM)

    def test_followups_also_go_to_the_team_inbox(self):
        self.approved_message()
        self.orch.sender.send_due()
        self.advance(3)
        self.orch.writer.schedule_followups()
        self.review.approve(self.review.pending_drafts()[0]["id"], "rita")
        self.orch.sender.send_due()
        self.assertEqual([m["to"] for m in self.mailer.outbox], [TEAM, TEAM])

    def test_test_database_cannot_send_for_real(self):
        self.approved_message()
        self.orch.sender.send_due()
        self.advance(3)
        self.orch.writer.schedule_followups()
        message_id = self.review.pending_drafts()[0]["id"]
        self.review.approve(message_id, "rita")
        settings = self.ctx.settings
        settings["test_recipient"] = ""
        self.ctx.save_settings(settings, "rita", "real")
        self.assertEqual(self.orch.sender.send_one(message_id), "blocked")
        self.assertEqual(len(self.mailer.outbox), 1)

    def test_report_marks_test_sends(self):
        self.approved_message()
        self.orch.sender.send_due()
        metrics = ReportingAgent(self.ctx).write_report(Path(self.tmp.name) / "r")
        self.assertTrue(metrics["simulated"])
        report = (Path(self.tmp.name) / "r" / "campaign_report.md").read_text(encoding="utf-8")
        self.assertIn("TEST SENDS", report)

    def test_test_recipient_must_be_an_address(self):
        from outreach.context import SettingsError
        settings = self.ctx.settings
        settings["test_recipient"] = "team"
        with self.assertRaises(SettingsError):
            self.ctx.save_settings(settings, "rita", "bad")


del Base
