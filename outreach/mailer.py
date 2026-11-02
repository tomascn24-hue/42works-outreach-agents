"""Email delivery back ends. Dry-run is the default; real sends need an explicit opt-in."""
import html
import http.client
import json
import os
import re
import urllib.request
import uuid


class MailerError(RuntimeError):
    pass


def to_html(body):
    """Plain layout with clickable links. Open tracking needs an HTML part to carry its pixel."""
    escaped = html.escape(body)
    linked = re.sub(r"https?://[^\s<]+", lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped)
    return "<p>" + linked.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"


class DryRunMailer:
    """Sends nothing. Reports mark campaigns using it as simulated."""

    name = "dry-run"

    def __init__(self):
        self.outbox = []

    def send(self, to, from_addr, subject, body, unsubscribe_url, headers=None, idempotency_key=None):
        provider_id = "dry-" + uuid.uuid4().hex
        self.outbox.append({"to": to, "from": from_addr, "subject": subject, "body": body, "id": provider_id,
                            "headers": dict(headers or {}), "idempotency_key": idempotency_key})
        return provider_id


class ResendMailer:
    name = "resend"

    def __init__(self, api_key=None, timeout=30, base_url="https://api.resend.com"):
        self.api_key = api_key or os.environ.get("RESEND_API_KEY")
        if not self.api_key:
            raise MailerError("RESEND_API_KEY is not set")
        self.timeout = timeout
        self.base_url = base_url

    def send(self, to, from_addr, subject, body, unsubscribe_url, headers=None, idempotency_key=None):
        payload = {
            "from": from_addr,
            "to": [to],
            "subject": subject,
            "text": body,
            "html": to_html(body),
            "headers": {
                **(headers or {}),
                "List-Unsubscribe": f"<{unsubscribe_url}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
        }
        request_headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if idempotency_key:
            request_headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            self.base_url + "/emails", data=json.dumps(payload).encode("utf-8"), headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))["id"]
        except (OSError, http.client.HTTPException, KeyError, json.JSONDecodeError) as exc:
            raise MailerError(str(exc)) from exc
