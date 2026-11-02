"""The single gate every send must pass. Rules live in code, not in a prompt."""
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from zoneinfo import ZoneInfo

from .context import HARD_LIMITS, content_hash, parse_iso, render_phrase, valid_timezone

FOOTER_MARK = "\n-- \n"
_URL = re.compile(r"https?://([^/\s?#]+)[^\s]*", re.I)
SENDABLE_LEAD_STATUS = {"drafted", "in_sequence"}


@dataclass
class Decision:
    hard: list = field(default_factory=list)  # never send this message
    soft: list = field(default_factory=list)  # not now, may pass later

    @property
    def allowed(self):
        return not self.hard and not self.soft


def effective_daily_cap(ctx, inbox):
    """Warm-up ramp per inbox: the cap follows the number of earlier days that inbox sent on."""
    s = ctx.settings
    today = ctx.now_iso()[:10]
    earlier = ctx.db.scalar(
        "SELECT COUNT(DISTINCT substr(sent_at,1,10)) FROM messages"
        " WHERE status IN ('sending','sent') AND inbox=? AND substr(sent_at,1,10) < ?", (inbox, today))
    ramp = s["warmup"][min(earlier, len(s["warmup"]) - 1)] if s["warmup"] else s["daily_cap_per_inbox"]
    return min(ramp, s["daily_cap_per_inbox"], HARD_LIMITS["daily_cap_per_inbox"])


def lead_timezone(lead, settings):
    return lead["timezone"] if lead["timezone"] and valid_timezone(lead["timezone"]) else settings["default_timezone"]


def allowed_link_hosts(settings):
    hosts = {d.lower() for d in settings["allowed_link_domains"]}
    for url in (settings["unsubscribe_base_url"], settings["meeting_link"]):
        match = _URL.match(url or "")
        if match:
            hosts.add(match.group(1).lower())
    return hosts


def check_content(subject, body, lead, settings):
    problems = []
    text = subject + "\n" + body
    for phrase in settings["banned_phrases"]:
        if re.search(phrase, text, re.I):
            problems.append(f"banned content: {phrase}")
    if "{" in text or "}" in text:
        problems.append("unresolved template placeholder")
    allowed_subjects = {render_phrase(v, lead) for v in settings["subject_variants"].values()}
    if subject.removeprefix("Re: ") not in allowed_subjects:
        problems.append("subject is not an approved variant")
    if FOOTER_MARK not in body:
        problems.append("compliance footer missing")
    else:
        main, footer = body.split(FOOTER_MARK, 1)
        if len(main.split()) > settings["max_words"]:
            problems.append("email longer than max_words")
        if settings["physical_address"] not in footer:
            problems.append("physical address missing")
        if settings["unsubscribe_base_url"] + lead["unsubscribe_token"] not in footer:
            problems.append("unsubscribe link missing")
    hosts = allowed_link_hosts(settings)
    for host in _URL.findall(body):
        if host.lower() not in hosts:
            problems.append(f"link to unapproved domain: {host}")
    return problems


def check_send(ctx, message, lead):
    s = ctx.settings
    db = ctx.db
    now = ctx.now()
    d = Decision()

    # authorisation: only the exact content a human approved may leave
    if message["status"] != "approved" or not message["reviewer"]:
        d.hard.append("not approved by a human")
    current = content_hash(message["subject"], message["body"])
    if message["approved_sha256"] != current or message["content_sha256"] != current:
        d.hard.append("content differs from what was approved")
    if not json.loads(message["verification"]).get("ok"):
        d.hard.append("failed fact verification")
    inbox = message["inbox"]
    if inbox not in {i["address"] for i in s["inboxes"]}:
        d.hard.append("sending inbox is not configured")
    if not s.get("test_recipient") and db.scalar("SELECT 1 FROM messages WHERE test_recipient IS NOT NULL LIMIT 1"):
        # Leads here already "received" test emails; real follow-ups would reach them with no first email.
        d.hard.append("this database was used for test sends: start the real campaign in a new database")

    # contact rules
    if ctx.is_suppressed(lead["email"]):
        d.hard.append("recipient is suppressed")
    if lead["status"] not in SENDABLE_LEAD_STATUS:
        d.hard.append(f"lead status is {lead['status']}")
    if db.scalar("SELECT COUNT(*) FROM replies WHERE lead_id=? AND needs_human=1 AND resolved_at IS NULL",
                 (lead["id"],)):
        d.hard.append("unresolved reply awaiting a human")
    if db.scalar("SELECT COUNT(*) FROM replies WHERE lead_id IS NULL AND needs_human=1 AND resolved_at IS NULL"
                 " AND substr(from_email, instr(from_email,'@')+1)=?", (lead["domain"],)):
        d.soft.append("reply from the same company awaiting a human")
    if message["touch"] + 1 > HARD_LIMITS["max_touches"]:
        d.hard.append("touch limit reached")

    # content
    d.hard.extend(check_content(message["subject"], message["body"], lead, s))

    # timing, in the recipient's local time
    if s.get("paused"):
        d.soft.append("campaign paused")
    if parse_iso(message["scheduled_for"]) > now:
        d.soft.append("not due yet")
    if lead["hold_until"] and parse_iso(lead["hold_until"]) > now:
        d.soft.append("lead on hold")
    local = now.astimezone(ZoneInfo(lead_timezone(lead, s)))
    start, end = s["send_window_local"]
    if local.hour < start or local.hour >= end or local.weekday() not in s["send_weekdays"]:
        d.soft.append("outside send window")

    # volume
    today = ctx.now_iso()[:10]
    sent_today = db.scalar(
        "SELECT COUNT(*) FROM messages WHERE status IN ('sending','sent') AND inbox=? AND substr(sent_at,1,10)=?",
        (inbox, today))
    if sent_today >= effective_daily_cap(ctx, inbox):
        d.soft.append("daily inbox cap reached")
    domain_today = db.scalar(
        "SELECT COUNT(*) FROM messages m JOIN leads l ON l.id=m.lead_id"
        " WHERE m.status IN ('sending','sent') AND substr(m.sent_at,1,10)=? AND l.domain=?",
        (today, lead["domain"]))
    if domain_today >= min(s["per_domain_daily_cap"], HARD_LIMITS["per_domain_daily_cap"]):
        d.soft.append("daily domain cap reached")
    total = db.scalar("SELECT COUNT(*) FROM messages WHERE status IN ('sending','sent')")
    if total >= s["campaign_total_cap"]:
        d.soft.append("campaign total cap reached")
    last = db.scalar(
        "SELECT MAX(sent_at) FROM messages WHERE lead_id=? AND status IN ('sending','sent')", (lead["id"],))
    if last and parse_iso(last) + timedelta(days=HARD_LIMITS["min_gap_days"]) > now:
        d.soft.append("minimum gap between touches")
    return d
