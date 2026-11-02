"""Mailbox verification before a lead is stored, to keep bounces low and protect the domain."""
import http.client
import json
import os
import urllib.parse
import urllib.request


class VerificationError(RuntimeError):
    pass


class ZeroBounceVerifier:
    """ZeroBounce v2 single-address check. Anything but a clear result is refused."""

    def __init__(self, api_key=None, base_url="https://api.zerobounce.net/v2", timeout=20, accept_catch_all=False):
        self.api_key = api_key or os.environ.get("ZEROBOUNCE_API_KEY")
        if not self.api_key:
            raise VerificationError("ZEROBOUNCE_API_KEY is not set")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.accepted = {"valid", "catch-all"} if accept_catch_all else {"valid"}

    def check(self, email):
        """Return (accepted, provider_status)."""
        query = urllib.parse.urlencode({"api_key": self.api_key, "email": email, "ip_address": ""})
        try:
            with urllib.request.urlopen(f"{self.base_url}/validate?{query}", timeout=self.timeout) as resp:
                status = str(json.loads(resp.read().decode("utf-8"))["status"]).lower()
        except (OSError, http.client.HTTPException, KeyError, ValueError) as exc:
            raise VerificationError(str(exc)) from exc
        return status in self.accepted, status
