"""Shared runtime: database, clock, audit log and the versioned campaign settings."""
import hashlib
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Enforced in code. No settings version, proposal or prompt can raise these.
HARD_LIMITS = {
    "daily_cap_per_inbox": 30,  # SoW risk 2 mitigation
    "max_touches": 4,
    "per_domain_daily_cap": 5,
    "min_gap_days": 2,
}

REQUIRED_SETTINGS = (
    "inboxes", "physical_address", "unsubscribe_base_url", "meeting_link",
    "approved_claims", "approved_phrases", "subject_variants", "cadence_days",
    "daily_cap_per_inbox", "warmup", "per_domain_daily_cap", "campaign_total_cap",
    "max_words", "send_window_local", "default_timezone", "send_weekdays", "banned_phrases",
    "allowed_link_domains", "writer_guidance", "min_overlap",
    "min_sample_per_variant", "best_practices",
)


class SettingsError(ValueError):
    pass


def valid_timezone(name):
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False


def validate_settings(s):
    missing = [k for k in REQUIRED_SETTINGS if k not in s]
    if missing:
        raise SettingsError(f"missing settings: {missing}")
    if s["daily_cap_per_inbox"] > HARD_LIMITS["daily_cap_per_inbox"]:
        raise SettingsError("daily_cap_per_inbox exceeds the hard limit")
    if s["per_domain_daily_cap"] > HARD_LIMITS["per_domain_daily_cap"]:
        raise SettingsError("per_domain_daily_cap exceeds the hard limit")
    if len(s["cadence_days"]) + 1 > HARD_LIMITS["max_touches"]:
        raise SettingsError("cadence_days exceeds the maximum number of touches")
    if any(d < HARD_LIMITS["min_gap_days"] for d in s["cadence_days"]):
        raise SettingsError("a cadence gap is below the minimum")
    if not s["subject_variants"]:
        raise SettingsError("at least one subject variant is required")
    if not 0.85 <= s["min_overlap"] <= 1:
        raise SettingsError("min_overlap must be between 0.85 and 1")
    inboxes = s["inboxes"]
    if not inboxes or any(not {"address", "from_name", "signature"} <= set(i) for i in inboxes):
        raise SettingsError("each inbox needs an address, from_name and signature")
    if any("@" not in i["address"] for i in inboxes) or \
            len({i["address"].lower() for i in inboxes}) != len(inboxes):
        raise SettingsError("inbox addresses must be valid and distinct")
    if not valid_timezone(s["default_timezone"]):
        raise SettingsError(f"unknown time zone {s['default_timezone']}")
    start, end = s["send_window_local"]
    if not 0 <= start < end <= 24:
        raise SettingsError("send_window_local must be [start_hour, end_hour]")
    if s["meeting_link"] and not s["meeting_link"].startswith("https://"):
        raise SettingsError("meeting_link must be https")
    if s.get("test_recipient") and "@" not in s["test_recipient"]:
        raise SettingsError("test_recipient must be an email address")
    if not s["approved_phrases"].get("cta"):
        raise SettingsError("at least one approved call to action is required")


def validate_live(s):
    """Refuse real sends while the campaign still has placeholder content."""
    blob = json.dumps(s)
    if "REPLACE" in blob:
        raise SettingsError("settings still contain REPLACE placeholders")
    if not s["approved_claims"]:
        raise SettingsError("no client-approved claims configured")
    if not s["unsubscribe_base_url"].startswith("https://"):
        raise SettingsError("unsubscribe_base_url must be https")


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(text):
    return datetime.fromisoformat(text)


def canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(subject, body):
    return sha256(subject + "\0" + body)


class Context:
    def __init__(self, db, llm=None, clock=None, notifier=None):
        self.db = db
        self.llm = llm
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.notifier = notifier

    def notify(self, text):
        """Alerts are best effort: a failed alert never stops the campaign."""
        if self.notifier is None:
            return False
        try:
            return bool(self.notifier.send(text))
        except Exception:  # noqa: BLE001
            return False

    def now(self):
        return self.clock()

    def now_iso(self):
        return iso(self.clock())

    # ----- settings -----
    @property
    def settings(self):
        row = self.db.one("SELECT data FROM settings_versions ORDER BY id DESC LIMIT 1")
        if row is None:
            raise SettingsError("campaign not initialised")
        return json.loads(row["data"])

    @property
    def settings_version(self):
        return self.db.scalar("SELECT MAX(id) FROM settings_versions")

    def save_settings(self, data, approved_by, reason):
        validate_settings(data)
        with self.db.tx():
            cur = self.db.execute(
                "INSERT INTO settings_versions(data, created_at, approved_by, reason) VALUES (?,?,?,?)",
                (canonical(data), self.now_iso(), approved_by, reason),
            )
            self.audit(approved_by, "settings_changed", "settings", cur.lastrowid, {"reason": reason})
        return cur.lastrowid

    # ----- audit log: append-only (DB triggers) and hash-chained (tamper-evident) -----
    def audit(self, actor, action, entity, entity_id, details=None):
        details = canonical(details or {})
        ts = self.now_iso()
        with self.db.tx():
            prev = self.db.scalar("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1") or "GENESIS"
            digest = sha256(canonical([prev, ts, actor, action, entity, str(entity_id), details]))
            self.db.execute(
                "INSERT INTO audit_log(ts, actor, action, entity, entity_id, details, prev_hash, hash)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (ts, actor, action, entity, str(entity_id), details, prev, digest),
            )

    def verify_audit_chain(self):
        """Return the id of the first tampered audit row, or None when intact."""
        prev = "GENESIS"
        for r in self.db.all("SELECT * FROM audit_log ORDER BY id"):
            expected = sha256(canonical(
                [prev, r["ts"], r["actor"], r["action"], r["entity"], r["entity_id"], r["details"]]))
            if r["prev_hash"] != prev or r["hash"] != expected:
                return r["id"]
            prev = r["hash"]
        return None

    # ----- suppression list -----
    def suppress(self, value, reason, actor):
        value = value.strip().lower()
        with self.db.tx():
            self.db.execute(
                "INSERT OR IGNORE INTO suppression(value, reason, created_at) VALUES (?,?,?)",
                (value, reason, self.now_iso()),
            )
            self.audit(actor, "suppressed", "suppression", value, {"reason": reason})

    def is_suppressed(self, email):
        email = email.strip().lower()
        domain = email.rsplit("@", 1)[-1]
        return self.db.one("SELECT 1 FROM suppression WHERE value IN (?, ?)", (email, domain)) is not None

    def cancel_open_messages(self, lead_id, reason, actor):
        with self.db.tx():
            rows = self.db.all(
                "SELECT id FROM messages WHERE lead_id=? AND status IN ('pending_review','approved')",
                (lead_id,),
            )
            for r in rows:
                self.db.execute("UPDATE messages SET status='cancelled' WHERE id=?", (r["id"],))
                self.audit(actor, "message_cancelled", "message", r["id"], {"reason": reason})


def render_phrase(phrase, lead):
    first = (lead["first_name"] or "there")
    return phrase.replace("{first_name}", first).replace("{company}", lead["company"] or "your team")
