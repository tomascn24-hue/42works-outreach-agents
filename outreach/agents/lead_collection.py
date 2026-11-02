"""Lead Collection Agent (T3): compiles prospect emails and company details."""
import csv
import re
import secrets
import socket
from html import unescape

from ..context import valid_timezone
from ..email_verification import VerificationError
from ..web import html_to_text

ACTOR = "lead_collection_agent"
EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})$")
_FIND_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
NEVER_CONTACT = re.compile(
    r"^(no-?reply|do-?not-?reply|postmaster|abuse|mailer-daemon|privacy|legal|dpo|gdpr|security|billing|careers|jobs)[@+.]",
    re.I)
CONTACT_PAGES = ("", "/contact", "/contact-us", "/about", "/about-us", "/team")


def domain_resolves(domain):
    try:
        socket.getaddrinfo(domain, None)
        return True
    except (socket.gaierror, UnicodeError):
        return False


class LeadCollectionAgent:
    def __init__(self, ctx, fetcher=None, domain_check=domain_resolves, verifier=None):
        self.ctx = ctx
        self.fetcher = fetcher
        self.domain_check = domain_check
        self.verifier = verifier

    def _verify(self, email):
        if self.verifier is None:
            return None
        try:
            accepted, status = self.verifier.check(email)
        except VerificationError as exc:
            return f"mailbox verification unavailable: {exc}"
        return None if accepted else f"mailbox verification: {status}"

    def add_lead(self, email, source, first_name="", last_name="", title="", company="", segment="default",
                 timezone=""):
        """Validate and store one lead. Returns the id, or None when it was refused."""
        email = (email or "").strip().lower()
        match = EMAIL.match(email)
        reason = None
        if not match:
            reason = "invalid email syntax"
        elif NEVER_CONTACT.match(email):
            reason = "non-contactable mailbox"
        elif self.ctx.is_suppressed(email):
            reason = "suppressed"
        elif self.ctx.db.one("SELECT 1 FROM leads WHERE email=?", (email,)):
            reason = "duplicate"
        elif not self.domain_check(match.group(1)):
            reason = "domain does not resolve"
        else:
            reason = self._verify(email)
        if reason:
            self.ctx.audit(ACTOR, "lead_refused", "lead", email, {"reason": reason, "source": source})
            return None
        timezone = (timezone or "").strip()
        if timezone and not valid_timezone(timezone):
            self.ctx.audit(ACTOR, "timezone_ignored", "lead", email, {"timezone": timezone})
            timezone = ""
        with self.ctx.db.tx():
            cur = self.ctx.db.execute(
                "INSERT INTO leads(email, first_name, last_name, title, company, domain, segment, timezone,"
                " source, unsubscribe_token, booking_ref, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (email, first_name.strip(), last_name.strip(), title.strip(),
                 company.strip() or match.group(1), match.group(1), segment.strip() or "default",
                 timezone or None, source, secrets.token_urlsafe(24), secrets.token_urlsafe(9), self.ctx.now_iso()),
            )
            self.ctx.audit(ACTOR, "lead_added", "lead", cur.lastrowid, {"source": source})
        return cur.lastrowid

    def collect_csv(self, path):
        """Columns: email, first_name, last_name, title, company, segment, timezone (e.g. America/Vancouver)."""
        added = 0
        with open(path, newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                added += self.add_lead(
                    row.get("email"), f"csv:{path}", row.get("first_name") or "", row.get("last_name") or "",
                    row.get("title") or "", row.get("company") or "", row.get("segment") or "default",
                    row.get("timezone") or "",
                ) is not None
        return added

    def collect_website(self, domain, segment="default", company=""):
        """Find addresses a company publishes on its own site, on its own domain."""
        if self.fetcher is None:
            raise ValueError("a fetcher is required")
        domain = domain.strip().lower()
        added, found = 0, set()
        for path in CONTACT_PAGES:
            url = f"https://{domain}{path}"
            html = self.fetcher.get(url)
            if not html:
                continue
            if not company:
                title = _TITLE.search(html)
                if title:
                    company = re.split(r"\s[|\-–—]\s", unescape(title.group(1)).strip())[0].strip()
            for email in _FIND_EMAIL.findall(html_to_text(html) + " " + unescape(html)):
                email = email.lower()
                if (email.endswith("@" + domain) or email.endswith("." + domain)) and email not in found:
                    found.add(email)
                    added += self.add_lead(email, url, company=company, segment=segment) is not None
        self.ctx.audit(ACTOR, "website_scanned", "domain", domain, {"found": len(found), "added": added})
        return added
