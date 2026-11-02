"""Adapters for the web endpoints that T9/T12 host: they check the request and translate it.

Each function takes the raw request body (bytes) and headers, and returns an HTTP status code.
The endpoint only has to pass the request in and send the status back. The signature
schemes follow the providers' documentation (Resend signs with Svix; Calendly signs with its
own header); check them against the current docs when wiring up a real account.
"""
import base64
import hashlib
import hmac
import json
import time

from .agents.reply_handler import ReplyAgent
from .agents.sending import SendingAgent
from .mailer import DryRunMailer

SIGNATURE_TOLERANCE_SECONDS = 300
RESEND_EVENTS = {
    "email.delivered": "delivered",
    "email.opened": "opened",
    "email.bounced": "bounced",
    "email.complained": "complained",
}


def _header(headers, name):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def verify_resend_signature(secret, headers, body, now=None):
    """Svix scheme: base64 HMAC-SHA256 of "id.timestamp.body", keyed with the whsec_ secret."""
    msg_id, stamp, signatures = (_header(headers, h) for h in ("svix-id", "svix-timestamp", "svix-signature"))
    if not (secret and msg_id and stamp and signatures):
        return False
    try:
        if abs((now or time.time()) - int(stamp)) > SIGNATURE_TOLERANCE_SECONDS:
            return False
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except ValueError:
        return False
    signed = f"{msg_id}.{stamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(expected, part.split(",", 1)[-1]) for part in signatures.split())


def verify_calendly_signature(signing_key, headers, body, now=None):
    """Calendly-Webhook-Signature: "t=<unix time>,v1=<hex HMAC-SHA256 of 't.body'>"."""
    header = _header(headers, "Calendly-Webhook-Signature")
    if not (signing_key and header):
        return False
    fields = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
    stamp, signature = fields.get("t"), fields.get("v1")
    if not (stamp and signature) or not stamp.isdigit():
        return False
    if abs((now or time.time()) - int(stamp)) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    expected = hmac.new(signing_key.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def resend_webhook(ctx, secret, headers, body, now=None):
    """POST /webhooks/resend. 401 for a bad signature; 200 for everything accepted or ignored."""
    if not verify_resend_signature(secret, headers, body, now):
        return 401
    try:
        payload = json.loads(body)
        event = RESEND_EVENTS.get(payload.get("type"))
        email_id = (payload.get("data") or {}).get("email_id")
    except (ValueError, AttributeError):
        return 400
    if event and email_id:
        SendingAgent(ctx, DryRunMailer()).ingest_event(email_id, event)
    return 200  # other event types (sent, clicked, delayed) are not used


def calendly_webhook(ctx, signing_key, headers, body, now=None):
    """POST /webhooks/calendly, subscribed to invitee.created."""
    if not verify_calendly_signature(signing_key, headers, body, now):
        return 401
    try:
        payload = json.loads(body)
        inner = payload.get("payload") or {}
        tracking = inner.get("tracking") or {}
    except (ValueError, AttributeError):
        return 400
    if payload.get("event") == "invitee.created":
        ReplyAgent(ctx).record_booking(tracking.get("utm_content"), inner.get("email"))
    return 200


def unsubscribe(ctx, token):
    """POST to the unsubscribe URL (the button on the page, or the mail client's one-click).

    Always 200, so the page never reveals whether a token exists.
    """
    ReplyAgent(ctx).unsubscribe(token)
    return 200
