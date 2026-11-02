"""Email Writing & Follow-up Agent (T5): drafts each touch and queues it for human review."""
import hashlib
from datetime import timedelta

from ..context import canonical, content_hash, parse_iso, render_phrase
from ..guardrails import FOOTER_MARK, check_content
from ..llm import LLMError
from ..verification import verify_draft

ACTOR = "writer_agent"
MAX_ATTEMPTS = 2

WRITER_SYSTEM = """You write short, plain B2B outreach emails for 42works.
Return {"sentences": [{"text": "...", "cites": ["F1"]}]}. Do not include a greeting, subject,
signature or links: code adds them.
Rules:
- Use only what is in `facts` (about the prospect) and `claims` (about 42works). Every sentence
  stating anything about either must list the ids it relies on in `cites`.
- Reuse the words of the cited text. Add no numbers, names, results, offers or adjectives: any word
  that is not in the cited text, apart from short connecting words, gets the sentence removed.
- Sentences with empty `cites` must be copied exactly from `approved_ctas` or `approved_follow_ups`.
- First touch: one sentence from a fact, one from a claim, then one approved CTA. Follow-up: an
  approved follow-up line, optionally one cited sentence, then one approved CTA.
- Follow `guidance` and `reviewer_feedback`. Fix anything listed in `rejected_sentences`.
Unverifiable sentences are removed automatically, so never guess."""


def _bucket(lead, salt, size):
    digest = hashlib.sha256((salt + lead["email"]).encode("utf-8")).digest()
    return digest[0] % size


def choose_variant(lead, settings):
    """Stable assignment so a lead keeps its variant and results stay comparable."""
    variants = settings["subject_variants"]
    names = sorted(n for n, v in variants.items() if lead["first_name"] or "{first_name}" not in v) \
        or sorted(variants)
    return names[_bucket(lead, "", len(names))]


def choose_inbox(db, lead, settings):
    """A lead keeps the inbox that first wrote to it, so the whole sequence is one conversation."""
    addresses = sorted(i["address"] for i in settings["inboxes"])
    row = db.one(
        "SELECT inbox FROM messages WHERE lead_id=? AND inbox IS NOT NULL AND status NOT IN ('cancelled','rejected')"
        " ORDER BY status='sent' DESC, id LIMIT 1", (lead["id"],))
    if row and row["inbox"] in addresses:
        return row["inbox"]
    return addresses[_bucket(lead, "inbox:", len(addresses))]


def booking_url(lead, settings):
    link = settings["meeting_link"]
    if not link or not lead["booking_ref"]:
        return None
    return f"{link}{'&' if '?' in link else '?'}utm_content={lead['booking_ref']}"


def render_body(lead, kept, settings, inbox):
    signature = next(i["signature"] for i in settings["inboxes"] if i["address"] == inbox)
    url = settings["unsubscribe_base_url"] + lead["unsubscribe_token"]
    greeting = render_phrase(settings["approved_phrases"]["greeting"], lead)
    footer = f"{settings['physical_address']}\nUnsubscribe: {url}"
    paragraphs = [greeting, " ".join(k["text"] for k in kept)]
    booking = booking_url(lead, settings)
    if booking:
        paragraphs.append(f"Book a time: {booking}")
    paragraphs.append(signature)
    return "\n\n".join(paragraphs) + FOOTER_MARK + footer


class WriterAgent:
    def __init__(self, ctx):
        self.ctx = ctx

    def _payload(self, lead, touch, facts, settings):
        feedback = self.ctx.db.all(
            "SELECT review_feedback FROM messages WHERE lead_id=? AND status='rejected'"
            " AND review_feedback IS NOT NULL", (lead["id"],))
        phrases = settings["approved_phrases"]
        return {
            "lead": {"first_name": lead["first_name"], "company": lead["company"], "title": lead["title"]},
            "touch": touch,
            "facts": [{"id": k, "text": v} for k, v in facts.items()],
            "claims": settings["approved_claims"],
            "approved_ctas": [render_phrase(p, lead) for p in phrases["cta"]],
            "approved_follow_ups": [render_phrase(p, lead) for p in phrases.get("follow_up", [])],
            "guidance": settings["writer_guidance"],
            "reviewer_feedback": [f["review_feedback"] for f in feedback],
        }

    def draft(self, lead, touch=0):
        """Create a pending_review message, or return None and flag the lead for a human."""
        ctx = self.ctx
        settings = ctx.settings
        facts = {f"F{r['id']}": r["text"]
                 for r in ctx.db.all("SELECT id, text FROM facts WHERE lead_id=? ORDER BY id", (lead["id"],))}
        payload = self._payload(lead, touch, facts, settings)
        all_dropped, result = [], None
        for _ in range(MAX_ATTEMPTS):
            try:
                draft = ctx.llm.complete_json("write_email", WRITER_SYSTEM, payload)
            except LLMError as exc:
                ctx.audit(ACTOR, "llm_error", "lead", lead["id"], {"error": str(exc)})
                continue
            result = verify_draft(draft, lead, facts, settings, llm=ctx.llm, require_fact=touch == 0)
            all_dropped.extend(result["dropped"])
            if result["ok"]:
                break
            payload["rejected_sentences"] = result["dropped"]

        if result is None:
            return None  # the model was unavailable: the lead keeps its status and is retried

        if not result["ok"]:
            with ctx.db.tx():
                ctx.db.execute("UPDATE leads SET status='needs_manual' WHERE id=?", (lead["id"],))
                ctx.audit(ACTOR, "draft_failed", "lead", lead["id"], {"touch": touch, "dropped": all_dropped})
            return None

        variant = choose_variant(lead, settings)
        subject = render_phrase(settings["subject_variants"][variant], lead)
        if touch > 0:
            subject = "Re: " + subject
        inbox = choose_inbox(ctx.db, lead, settings)
        body = render_body(lead, result["kept"], settings, inbox)
        problems = check_content(subject, body, lead, settings)
        if problems:
            with ctx.db.tx():
                ctx.db.execute("UPDATE leads SET status='needs_manual' WHERE id=?", (lead["id"],))
                ctx.audit(ACTOR, "draft_failed", "lead", lead["id"], {"touch": touch, "content": problems})
            return None

        verification = {"ok": True, "kept": result["kept"], "dropped": all_dropped}
        with ctx.db.tx():
            cur = ctx.db.execute(
                "INSERT INTO messages(lead_id, touch, variant, inbox, subject, body, content_sha256, verification,"
                " settings_version, scheduled_for, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (lead["id"], touch, variant, inbox, subject, body, content_hash(subject, body),
                 canonical(verification), ctx.settings_version, ctx.now_iso(), ctx.now_iso()),
            )
            if touch == 0:
                ctx.db.execute("UPDATE leads SET status='drafted' WHERE id=?", (lead["id"],))
            ctx.audit(ACTOR, "draft_created", "message", cur.lastrowid,
                      {"lead": lead["id"], "touch": touch, "variant": variant, "inbox": inbox,
                       "dropped": all_dropped})
        return cur.lastrowid

    def schedule_followups(self):
        """Draft the next touch for every lead whose cadence gap has elapsed."""
        ctx = self.ctx
        cadence = ctx.settings["cadence_days"]
        now = ctx.now()
        created = 0
        rows = ctx.db.all(
            "SELECT l.*, MAX(m.touch) AS last_touch, MAX(m.sent_at) AS last_sent"
            " FROM leads l JOIN messages m ON m.lead_id=l.id AND m.status='sent'"
            " WHERE l.status='in_sequence' GROUP BY l.id")
        for lead in rows:
            if ctx.db.one("SELECT 1 FROM messages WHERE lead_id=? AND status IN ('pending_review','approved','sending')",
                          (lead["id"],)):
                continue
            next_touch = lead["last_touch"] + 1
            if next_touch > len(cadence):
                with ctx.db.tx():
                    ctx.db.execute("UPDATE leads SET status='exhausted' WHERE id=?", (lead["id"],))
                    ctx.audit(ACTOR, "sequence_exhausted", "lead", lead["id"], {"touches": next_touch})
                continue
            due = parse_iso(lead["last_sent"]) + timedelta(days=cadence[next_touch - 1])
            on_hold = lead["hold_until"] and parse_iso(lead["hold_until"]) > now
            if due <= now and not on_hold:
                created += self.draft(lead, next_touch) is not None
        return created
