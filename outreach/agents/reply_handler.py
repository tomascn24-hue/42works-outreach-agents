"""Reply Handler Agent (T7). Fail-safe by design (SoW risk 4):

rules run first and the model can never override them. Only a confident, clearly
positive reply avoids review; everything negative or unclear becomes "Opt-Out Pending",
which blocks all further email until a human decides. No reply is answered automatically.
"""
import re
from datetime import timedelta

from ..context import iso
from ..llm import LLMError

ACTOR = "reply_agent"
OOO_HOLD_DAYS = 7
MIN_CONFIDENCE = 0.8

OPT_OUT = re.compile(
    r"unsubscrib|\bunsub\b|\bremove (me|us|my|our)\b|\btake (me|us) off\b|\bopt(ing)?[- ]?out\b|"
    r"\b(do not|don'?t|don’t) (contact|email|e-mail|message|write)\b|"
    r"\bremove (this|the|that) (email|e-mail|address)\b|\bdelete (me|us|my|our)\b|\berase (me|us|my|our)\b|"
    r"\boff (of )?your (mailing |e-?mail )?list\b|\bno longer (contact|email|e-mail|send|write)\b|"
    r"\bstop (emailing|e-mailing|contacting|sending|writing|messaging)\b|\bno more emails?\b|"
    r"cancelar (a )?subscri|remov(a|e|er)[- ]me|n[ãa]o me (contact|envie)|deixem? de (me )?(enviar|contactar)|"
    r"d[ée]sabonn|d[ée]sinscri|retirez[- ]moi|ne plus recevoir", re.I)
NEGATIVE = re.compile(
    r"\bnot\b|\bno\b|\bnope\b|\bnever\b|n't\b|n’t\b|\bstop\b|\bspam\b|annoy|complain|\blegal\b|lawyer|"
    r"\bwrong person\b|leave (me|us) alone|\bpass\b|sem interesse|\bn[ãa]o\b|pas int[ée]ress|non merci|\bnon\b", re.I)
AUTO_REPLY = re.compile(
    r"out[- ]of[- ](the )?office|\bon (vacation|holiday|leave)\b|automatic reply|auto[- ]?reply|"
    r"fora do escrit[óo]rio|absent du bureau|r[ée]ponse automatique", re.I)

_QUOTE_INTRO = re.compile(r"(wrote|escreveu|a [ée]crit)\s?:\s*$", re.I)
_QUOTE_START = re.compile(
    r"^\s*(-{2,}\s*(original message|mensagem original|message d'origine)\s*-{2,}|"
    r"(from|de|sent|enviado|envoy[ée])\s?:\s.+|--\s?)\s*$", re.I)

CLASSIFY_SYSTEM = (
    "Classify a reply to a B2B outreach email. intent is one of: interested, meeting_booked, question, "
    'negative, ambiguous. Reply as {"intent": "...", "confidence": 0.0-1.0}. When unsure use ambiguous.'
)


def strip_quoted(body):
    """Keep what the person wrote. Our own email, footer included, must not be classified."""
    kept = []
    for line in body.splitlines():
        if _QUOTE_START.match(line):
            break
        if line.lstrip().startswith(">") or _QUOTE_INTRO.search(line):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


class ReplyAgent:
    def __init__(self, ctx):
        self.ctx = ctx

    def classify(self, body):
        """Return (intent, decided_by)."""
        body = strip_quoted(body)
        if OPT_OUT.search(body):
            return "opt_out", "rule"
        if NEGATIVE.search(body):
            return "opt_out_pending", "rule"
        if AUTO_REPLY.search(body):
            return "out_of_office", "rule"
        if not body.strip():
            return "opt_out_pending", "rule"
        try:
            result = self.ctx.llm.complete_json("classify_reply", CLASSIFY_SYSTEM, {"reply": body[:4000]})
            intent, confidence = result.get("intent"), result.get("confidence")
        except (LLMError, AttributeError):
            return "opt_out_pending", "fail_safe"
        if intent in ("interested", "meeting_booked") and isinstance(confidence, (int, float)) \
                and confidence >= MIN_CONFIDENCE:
            return intent, "llm"
        return "opt_out_pending", "llm"

    def _match(self, from_email, in_reply_to):
        """The thread header identifies the lead even when a colleague or an alias replies."""
        if in_reply_to:
            row = self.ctx.db.one("SELECT lead_id FROM messages WHERE message_id_header=?", (in_reply_to.strip(),))
            if row:
                return self.ctx.db.one("SELECT * FROM leads WHERE id=?", (row["lead_id"],))
        return self.ctx.db.one("SELECT * FROM leads WHERE email=?", (from_email,))

    def handle(self, from_email, body, in_reply_to=None, auto_submitted=False, source_message_id=None):
        ctx = self.ctx
        from_email = from_email.strip().lower()
        lead = self._match(from_email, in_reply_to)
        intent, decided_by = self.classify(body)
        if auto_submitted and intent != "opt_out":
            # Marked by the sending server as automatic (RFC 3834): not a person's answer.
            intent, decided_by = "out_of_office", "header"
        third_party = lead is not None and lead["email"] != from_email
        # Someone else asking to be removed does not opt the lead out, but a person must decide.
        lead_intent = "opt_out_pending" if third_party and intent == "opt_out" else intent
        needs_human = intent != "out_of_office" and (lead_intent != "opt_out" or lead is None)
        now = ctx.now_iso()

        with ctx.db.tx():
            cur = ctx.db.execute(
                "INSERT INTO replies(lead_id, from_email, body, received_at, intent, decided_by, needs_human,"
                " source_message_id) VALUES (?,?,?,?,?,?,?,?)",
                (lead["id"] if lead else None, from_email, body, now, lead_intent, decided_by, int(needs_human),
                 source_message_id),
            )
            reply_id = cur.lastrowid
            ctx.audit(ACTOR, "reply_classified", "reply", reply_id,
                      {"intent": intent, "decided_by": decided_by, "matched_lead": bool(lead),
                       "third_party": third_party})
            if intent == "opt_out":
                ctx.suppress(from_email, "opt-out reply", ACTOR)
            if lead is not None and intent == "out_of_office":
                hold = iso(ctx.now() + timedelta(days=OOO_HOLD_DAYS))
                ctx.db.execute("UPDATE leads SET hold_until=? WHERE id=?", (hold, lead["id"]))
            elif lead is not None:
                # Automatic replies aside, any reply ends the automated sequence.
                ctx.cancel_open_messages(lead["id"], f"reply:{lead_intent}", ACTOR)
                ctx.db.execute(
                    "UPDATE messages SET replied_at=COALESCE(replied_at, ?) WHERE id=(SELECT id FROM messages"
                    " WHERE lead_id=? AND status='sent' ORDER BY sent_at DESC, id DESC LIMIT 1)", (now, lead["id"]))
                status = {"opt_out": "opted_out", "opt_out_pending": "opt_out_pending"}.get(lead_intent, "goal_met")
                ctx.db.execute("UPDATE leads SET status=? WHERE id=? AND (? = 'opted_out' OR status != 'opted_out')",
                               (status, lead["id"], status))
                ctx.audit(ACTOR, "lead_status_changed", "lead", lead["id"], {"status": status, "reply": reply_id})

        who = lead["company"] if lead else from_email.rsplit("@", 1)[-1]
        if lead_intent in ("interested", "meeting_booked"):
            ctx.notify(f"Positive reply from {who} (reply {reply_id}). Follow up quickly.")
        elif needs_human:
            ctx.notify(f"A reply from {who} needs a decision (reply {reply_id}, {lead_intent}).")
        return reply_id, lead_intent

    def record_booking(self, booking_ref=None, email=None):
        """A meeting booked through the calendar link: the campaign goal for this lead."""
        ctx = self.ctx
        lead = None
        if booking_ref:
            lead = ctx.db.one("SELECT * FROM leads WHERE booking_ref=?", (booking_ref,))
        if lead is None and email:
            lead = ctx.db.one("SELECT * FROM leads WHERE email=?", (email.strip().lower(),))
        if lead is None:
            ctx.audit(ACTOR, "booking_unmatched", "booking", booking_ref or email or "-", {})
            return None
        now = ctx.now_iso()
        with ctx.db.tx():
            cur = ctx.db.execute(
                "INSERT INTO replies(lead_id, from_email, body, received_at, intent, decided_by, needs_human)"
                " VALUES (?,?,?,?,'meeting_booked','calendar',1)", (lead["id"], lead["email"], "(calendar booking)", now))
            ctx.cancel_open_messages(lead["id"], "meeting booked", ACTOR)
            ctx.db.execute(
                "UPDATE messages SET replied_at=COALESCE(replied_at, ?) WHERE id=(SELECT id FROM messages"
                " WHERE lead_id=? AND status='sent' ORDER BY sent_at DESC, id DESC LIMIT 1)", (now, lead["id"]))
            ctx.db.execute("UPDATE leads SET status='goal_met' WHERE id=? AND status != 'opted_out'", (lead["id"],))
            ctx.audit(ACTOR, "meeting_booked", "lead", lead["id"], {"reply": cur.lastrowid})
        ctx.notify(f"Meeting booked with {lead['company']} (reply {cur.lastrowid}).")
        return cur.lastrowid

    def unsubscribe(self, token):
        """One-click link from the email footer."""
        ctx = self.ctx
        lead = ctx.db.one("SELECT * FROM leads WHERE unsubscribe_token=?", (token,))
        if lead is None:
            return False
        with ctx.db.tx():
            ctx.suppress(lead["email"], "unsubscribe link", ACTOR)
            ctx.cancel_open_messages(lead["id"], "unsubscribe", ACTOR)
            ctx.db.execute("UPDATE leads SET status='opted_out' WHERE id=?", (lead["id"],))
            ctx.audit(ACTOR, "lead_status_changed", "lead", lead["id"], {"status": "opted_out"})
        return True
