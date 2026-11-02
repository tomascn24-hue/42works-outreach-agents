"""Alerts to the team (SoW risk 3). Messages name the company and the reply id, never its text."""
import http.client
import json
import os
import urllib.request


class SlackNotifier:
    def __init__(self, webhook_url=None, timeout=10):
        self.webhook_url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL")
        if not self.webhook_url:
            raise ValueError("SLACK_WEBHOOK_URL is not set")
        self.timeout = timeout

    def send(self, text):
        request = urllib.request.Request(
            self.webhook_url, data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return 200 <= resp.status < 300
        except (OSError, http.client.HTTPException):
            return False


class ListNotifier:
    """Keeps alerts in memory; used by the tests and the demo."""

    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True
