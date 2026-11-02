"""Reads replies from a sending mailbox over IMAP and hands each one to the Reply Handler.

Messages are fetched without being marked read, and flagged as read only after they are
stored, so a crash never loses a reply. A reply already stored (same Message-ID) is skipped.
"""
import email
import imaplib
import os
from email import policy
from email.utils import getaddresses, parseaddr

from .agents.reply_handler import ReplyAgent
from .web import html_to_text

ACTOR = "inbox_reader"
_SYSTEM_SENDERS = ("mailer-daemon@", "postmaster@")


def _text(message):
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, ValueError):
        content = part.get_payload(decode=True).decode("utf-8", errors="replace")
    return html_to_text(content) if part.get_content_type() == "text/html" else content


def _thread_id(message):
    """The id of the message being answered: In-Reply-To, or the last References entry."""
    value = (message.get("In-Reply-To") or "").strip()
    if not value:
        refs = (message.get("References") or "").split()
        value = refs[-1] if refs else ""
    return value or None


class ImapReplyReader:
    def __init__(self, ctx, host, username, password=None, folder="INBOX", port=993,
                 imap_factory=imaplib.IMAP4_SSL):
        self.ctx = ctx
        self.host, self.port, self.folder = host, port, folder
        self.username = username
        self.password = password or os.environ.get("IMAP_PASSWORD")
        if not self.password:
            raise ValueError("IMAP_PASSWORD is not set")
        self.imap_factory = imap_factory
        self.replies = ReplyAgent(ctx)

    def poll(self, limit=100):
        summary = {"handled": 0, "skipped": 0, "unreadable": 0}
        own = {i["address"].lower() for i in self.ctx.settings["inboxes"]}
        conn = self.imap_factory(self.host, self.port)
        try:
            conn.login(self.username, self.password)
            conn.select(self.folder)
            _, data = conn.search(None, "UNSEEN")
            for number in (data[0].split() if data and data[0] else [])[:limit]:
                summary[self._one(conn, number, own)] += 1
        finally:
            try:
                conn.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
        self.ctx.audit(ACTOR, "inbox_polled", "mailbox", self.username, summary)
        return summary

    def _one(self, conn, number, own):
        ctx = self.ctx
        _, parts = conn.fetch(number, "(BODY.PEEK[])")
        raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
        try:
            message = email.message_from_bytes(raw, policy=policy.default)
            sender = parseaddr(str(message.get("From", "")))[1].lower()
            source_id = (message.get("Message-ID") or "").strip() or None
            body = _text(message)
        except Exception as exc:  # noqa: BLE001 - one bad message must not stop the others
            ctx.audit(ACTOR, "inbound_unreadable", "imap", str(number), {"error": str(exc)})
            ctx.notify(f"A message in {self.username} could not be read; please check it by hand.")
            conn.store(number, "+FLAGS", "\\Seen")
            return "unreadable"

        recipients = {a.lower() for _, a in getaddresses(message.get_all("To", []) + message.get_all("Cc", []))}
        is_system = sender.startswith(_SYSTEM_SENDERS) or message.get_content_type() == "multipart/report"
        duplicate = source_id and ctx.db.one("SELECT 1 FROM replies WHERE source_message_id=?", (source_id,))
        if is_system or sender in own or not sender or duplicate:
            # Bounces arrive as provider events; copies of our own mail are not replies.
            reason = "duplicate" if duplicate else "system or own message"
            ctx.audit(ACTOR, "inbound_skipped", "imap", source_id or str(number),
                      {"reason": reason, "to_us": bool(recipients & own)})
            conn.store(number, "+FLAGS", "\\Seen")
            return "skipped"

        auto = str(message.get("Auto-Submitted", "no")).lower() not in ("", "no")
        self.replies.handle(sender, body, _thread_id(message), auto_submitted=auto, source_message_id=source_id)
        conn.store(number, "+FLAGS", "\\Seen")
        return "handled"
