# Integration guide for T6, T9 and T12

The AI agents (T3, T4, T5, T7, T8) are in this repository and tested. This guide lists what the review interface and the infrastructure need to call, so that each group can build its part without changing the agents.

If something you need is missing, open an issue instead of editing the agent code. Changes to `guardrails.py`, `verification.py`, `agents/` and `review.py` need a review from the agents team, because the safety rules live there.

## Common setup

Everything goes through a `Context`, built on one database file.

```python
from outreach.context import Context
from outreach.db import DB

ctx = Context(DB("campaign.sqlite3"))
```

- **One connection per request or thread:** create a `Context` (and its `DB`) for each one, and call `ctx.db.close()` when done. Do not share one connection between threads.
- **Several processes at once:** the database uses WAL mode with a 30-second lock timeout, so the UI, the endpoints and the scheduler can use the same file at the same time.
- **Tests:** `python -m unittest discover -s tests` must pass before any merge. GitHub runs it on every push.
- **Practice data:** `python demo.py` fills nothing permanent. To get a database with drafts and replies to build against, run the commands in "Running a real campaign" in the README with `--offline` and `--db practice.sqlite3`.

## T6: review interface

All the logic exists in `outreach/review.py` and the agents. The UI only shows data and calls these functions. Every action needs the reviewer's name, which is stored in the audit log, so the UI must know who is logged in.

```python
from outreach.review import ReviewService, ReviewError

review = ReviewService(ctx)
```

### Screens and the calls behind them

| Screen | Read | Actions |
|---|---|---|
| Drafts to review | `review.pending_drafts()` | `review.approve(id, reviewer)`, `review.approve_batch(ids, reviewer)`, `review.edit_and_approve(id, reviewer, new_text)`, `review.reject(id, reviewer, feedback)` |
| Replies needing a decision | `review.pending_replies()` | `review.resolve_reply(id, reviewer, resolution)`, where `resolution` is `opt_out`, `continue`, `interested` or `meeting_booked` |
| Learning proposals | `ctx.db.all("SELECT * FROM proposals WHERE status='pending'")` | `LearningAgent(ctx).decide(id, approver, True or False)` |
| Leads needing attention | `ctx.db.all("SELECT * FROM leads WHERE status='needs_manual'")` | `review.requeue_lead(id, reviewer)` |
| Stuck sends | `SendingAgent(ctx, DryRunMailer()).stuck()` | `.resolve_stuck(id, reviewer, "sent" or "not_sent", provider_id)` |
| Campaign results | `ReportingAgent(ctx).metrics()` | None |
| Emergency stop | `ctx.settings.get("paused")` | Save settings with `paused` set to true or false: `ctx.save_settings(settings, name, reason)` |

Imports: `from outreach.agents.learning import LearningAgent`, `from outreach.agents.sending import SendingAgent`, `from outreach.mailer import DryRunMailer`, `from outreach.agents.reporting import ReportingAgent`.

Any action can raise `ReviewError` or `ValueError`, for example when two reviewers act on the same draft. Show the message and reload the list.

### What a draft looks like

`pending_drafts()` returns a list of these:

```python
{
    "id": 12, "to": "ana@acme.com", "company": "Acme", "touch": 0, "variant": "A",
    "subject": "A question for Acme",
    "body": "Hi Ana,\n\n...full email, as it will be sent...",
    "sentences": [{"text": "...", "cites": ["F3"], "kind": "cited"},
                  {"text": "Would a 15-minute call next week be useful?", "cites": [], "kind": "cta"}],
    "dropped": [{"text": "...", "reason": "numbers not in source: ['300']"}],
    "sources": {"F3": {"text": "...", "source_url": "https://acme.com/about", "fetched_at": "..."},
                "C1": {"text": "...", "source_url": "client-approved claims"}},
}
```

The key screen for the "no hallucination" requirement shows each sentence next to its source, with a link to `source_url`, so the reviewer can check it at a glance. Showing the `dropped` sentences and their reasons helps reviewers trust the system.

`touch` is 0 for the first email and 1 to 3 for follow-ups. `edit_and_approve` replaces only the main paragraph: the greeting, booking link, signature and footer are kept by code.

### What a reply looks like

`pending_replies()` returns rows with `id`, `lead_id` (may be empty for an unknown sender), `from_email`, `body`, `received_at` and `intent` (`opt_out_pending`, `interested`, `meeting_booked` or `opt_out`).

`body` is the full email, including any quoted text. Show it as plain text, never as HTML: it comes from outside and may contain anything.

### Metrics

`ReportingAgent(ctx).metrics()` returns a dictionary. The keys a dashboard needs:

| Group | Keys |
|---|---|
| Volume | `sent`, `delivered`, `opened`, `bounced` |
| Responses | `replies`, `positive_replies`, `meetings` |
| Rates | `open_rate`, `response_rate` (a fraction, or `None` before any delivery), `open_target_met`, `response_target_met` |
| Safety | `breaches` (a list: it must be empty), `blocked_attempts`, `sentences_dropped`, `audit_chain_intact` |
| Waiting | `pending_review`, `pending_replies` |
| Data quality | `simulated`, `test_sends` |

If `simulated` is true, show a clear banner: the figures are not real campaign results.

## T9 and T12: endpoints

The parsing, signature checks and calls are already written in `outreach/integration.py` and covered by tests. Each endpoint only reads the raw body (bytes) and headers, calls one function, and returns the status code it gives back.

| Method and path | Call | Notes |
|---|---|---|
| `GET /u/<token>` | None | Show a page "Unsubscribe from 42works emails?" with a button that POSTs to the same URL. Do not unsubscribe on GET: link scanners open links automatically |
| `POST /u/<token>` | `integration.unsubscribe(ctx, token)` | Then show "You have been unsubscribed". Mail clients' one-click button also POSTs here. **Legally required and the most urgent piece.** No login, and it must keep working for at least 60 days after the last email |
| `POST /webhooks/resend` | `integration.resend_webhook(ctx, RESEND_WEBHOOK_SECRET, headers, body)` | In Resend, subscribe to delivered, opened, bounced and complained. The secret starts with `whsec_` |
| `POST /webhooks/calendly` | `integration.calendly_webhook(ctx, CALENDLY_SIGNING_KEY, headers, body)` | Subscribe to `invitee.created` |

The path in `unsubscribe_base_url` (campaign settings) must match the deployed URL, for example `https://outreach.example.com/u/`. The token is everything after it.

A minimal example with the standard library, to show the shape. Use your own framework in production:

```python
from http.server import BaseHTTPRequestHandler
from outreach import integration
from outreach.context import Context
from outreach.db import DB

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        headers = dict(self.headers)
        ctx = Context(DB("campaign.sqlite3"))
        try:
            if self.path.startswith("/u/"):
                status = integration.unsubscribe(ctx, self.path[3:])
            elif self.path == "/webhooks/resend":
                status = integration.resend_webhook(ctx, RESEND_SECRET, headers, body)
            elif self.path == "/webhooks/calendly":
                status = integration.calendly_webhook(ctx, CALENDLY_KEY, headers, body)
            else:
                status = 404
        finally:
            ctx.db.close()
        self.send_response(status)
        self.end_headers()
```

The Resend (Svix) and Calendly signature formats were written from their documentation and tested with signatures we generated. Check the first real webhook from each one. If it returns 401, compare the headers with the provider's current docs and tell the agents team.

Replies need no endpoint: the scheduler reads them from each mailbox over IMAP (below).

## T12: scheduler

| Every | Command | Purpose |
|---|---|---|
| Hour | `python -m outreach --db campaign.sqlite3 cycle --live` | Research, drafting, sending, follow-ups, learning |
| 5 minutes, per inbox | `python -m outreach --db campaign.sqlite3 poll-inbox --host <imap host> --user <mailbox>` | Read replies |
| Day | `python -m outreach --db campaign.sqlite3 report --out reports` | Performance record |

- **No overlapping runs:** never start a run while the previous one of the same command is still going. On Linux, wrap each command in `flock`; in the Windows Task Scheduler, set "Do not start a new instance".
- **Exit codes:** a non-zero exit code means an error, with the reason on stderr. Alert someone when that happens.
- **Before the first live run:** the campaign must be loaded with `python -m outreach --db campaign.sqlite3 init --config config/campaign.json --approved-by "<name>"`.

## T12: environment, database and DNS

### Environment variables

| Variable | Used by |
|---|---|
| `OPENAI_API_KEY`, optionally `OPENAI_MODEL` | `cycle` and `poll-inbox` |
| `RESEND_API_KEY` | `cycle --live` |
| `IMAP_PASSWORD` | `poll-inbox` (one per mailbox if they differ: set it for each scheduled command) |
| `SLACK_WEBHOOK_URL` | Optional alerts |
| `ZEROBOUNCE_API_KEY` | Optional mailbox verification when collecting leads |
| `RESEND_WEBHOOK_SECRET`, `CALENDLY_SIGNING_KEY` | Your endpoints, passed to the `integration` functions |

Keep them in the hosting platform's secret store, never in the repository.

### Database

SQLite on a persistent disk is enough for the pilot.

- **Daily backup:** a copy taken while the system is running (`python -c "import sqlite3; s=sqlite3.connect('campaign.sqlite3'); d=sqlite3.connect('backup.sqlite3'); s.backup(d)"`), stored off the server.
- **Restore test:** restore a backup once before the pilot, and check that `verify-audit` says the chain is intact.
- **PostgreSQL** (SoW plan) means replacing `outreach/db.py` and a few SQLite-specific queries. That is agent code, so plan it with the agents team.

### DNS for the sending domain

Once 42works gives access, add the SPF and DKIM records that Resend shows for the domain, plus a DMARC record. Start with `p=none` and a reporting address. Then follow the warm-up in the settings (`warmup`), which the code already enforces.

## Checklist before the first real email

- [ ] The unsubscribe page works from a phone, with no login.
- [ ] A test email's open shows up in the report (Resend webhook working).
- [ ] A reply sent from the team inbox appears in `replies-list` after the next `poll-inbox`.
- [ ] A test booking through the link in the email marks the lead as `goal_met`.
- [ ] Test mode (`test_recipient`) was used for the quality test, in a separate database.
- [ ] The backup has been restored once.
- [ ] `verify-audit` passes, and the report shows 0 breaches.
