# Agentic AI Marketing Outreach System: AI agents

The agent layer of the 42works project: SoW tasks T3, T4, T5, T7 and T8. It collects leads, researches them, drafts cited emails, sends only what a person approved, follows up in the same thread, handles replies and bookings, reports results and proposes improvements.

It needs Python 3.10 or later and uses only the standard library. On Windows, time zones also need the `tzdata` package (`requirements.txt`; Anaconda already includes it).

Day-to-day operation and incidents are covered in [RUNBOOK.md](RUNBOOK.md). What the review interface (T6) and the infrastructure (T9, T12) need to call is in [INTEGRATION.md](INTEGRATION.md).

## Run it

```bash
python -m unittest discover -s tests
python demo.py
```

On a Windows PC where `python` does not start, use the full path, for example `& "$env:USERPROFILE\anaconda3\python.exe" demo.py` in PowerShell.

`demo.py` simulates a full campaign with fictional companies. It needs no network or API key and sends no email. Results go to `demo_output/`:

| File | Contents |
|---|---|
| `campaign_report.md` | Performance record, marked as simulated |
| `performance_record.csv` | One row per email sent |
| `sample_review_card.json` | What a reviewer sees: the draft, each source and the dropped sentences |
| `summary.json` | Totals from the run |

## Architecture

```text
 Lead Collection -> Research & Fact-Check -> Writer -> Verification gate (code)
                                                              |
                                                     Human review queue  --> alerts
                                                              |
                                                     Guardrail gate (code)
                                                              |
                                                     Sending -> Mail provider
                                                              |
 Learning (proposals) <- Reporting <- Reply Handler <- Replies, bookings and provider events
        |
 Human approves -> new settings version -> Writer
```

| Agent | File | What it does | What the model can do |
|---|---|---|---|
| Lead Collection (T3) | `agents/lead_collection.py` | Imports CSV files and scans a company's own site. Validates, deduplicates, checks suppression and, with a key, verifies the mailbox | Nothing: no model used |
| Research & Fact-Check (T4) | `agents/research.py` | Stores verbatim page sentences with URL, page hash and date | Pick sentences by index only |
| Writer and Follow-up (T5) | `agents/writer.py` | Drafts each touch on the cadence, from the lead's inbox, with the booking link | Propose sentences with citations |
| Verification (T4) | `verification.py` | Drops any sentence it cannot verify | Second check that can only drop |
| Sending | `agents/sending.py`, `guardrails.py` | The only path to a mail provider. Threads follow-ups and recovers stuck sends | Nothing: no model used |
| Reply Handler (T7) | `agents/reply_handler.py` | Sorts replies, matches them by thread, records bookings, handles opt-outs and stops sequences | Classify only after the rules find nothing negative |
| Reporting | `agents/reporting.py` | Performance record and independent breach audit | Nothing: no model used |
| Learning (T8) | `agents/learning.py` | Compares results and creates proposals | Nothing: no model used |

Supporting modules:

| Module | Purpose |
|---|---|
| `orchestrator.py` | Runs one cycle of all agents and sends the review digest |
| `review.py` | Human review service for the HITL UI (T6) to call |
| `notify.py` | Slack alerts |
| `email_verification.py` | Mailbox check (ZeroBounce) |
| `evaluation.py` | Human rating of real drafts |
| `inbox_reader.py` | Reads replies from each sending mailbox over IMAP |
| `integration.py` | Checks and translates requests for the web endpoints (unsubscribe, Resend, Calendly) |

## How the four design requirements are met

### No hallucination

A sentence stays in a draft only if one of these is true:

- It is an exact copy of an approved phrase.
- It cites known sources, and its numbers, names, negations and all of its content words appear in them (`min_overlap` is 1.0 and settings cannot take it below 0.85). Only a short list of connecting words may be added.

Everything else is dropped. A first email without a verified prospect fact and a call to action is not created, and the lead goes to `needs_manual`. Subjects come only from approved variants. Links, signature and footer are added by code.

**Known limit:** the code check compares words, not meaning, so the source's own words rearranged into a false statement can pass. The OpenAI second check and human review cover this case, and offline mode has no second check. The strict rule may also drop good paraphrases; `eval-export` and `eval-score` measure both effects on real drafts.

### Guardrails enforced by code

`guardrails.check_send` runs before every send:

| Area | Rule |
|---|---|
| Authorisation | A named person approved it, and the content hash still matches what they approved |
| Contact | Not suppressed (address or domain), no opt-out, no reply awaiting a person (including one from a colleague at the same company), at most 4 touches, at least 2 days between touches |
| Volume | At most 30 per inbox per day with a warm-up ramp for each inbox, a per-domain cap and a campaign cap |
| Content | No banned phrases, approved subject, word limit, approved links only (plus the unsubscribe and booking links), address and unsubscribe link present |
| Timing | Within the send window in the recipient's own time zone, on allowed weekdays |
| Control | Configured inbox and kill switch (`set-paused`) |

Settings can never go above the limits in `HARD_LIMITS`, even through an approved proposal. A failed send is not retried automatically. A send interrupted mid-call is held for a person to check (`stuck-list`, `stuck-resolve`). A retry reuses the message's Message-ID and idempotency key.

### Human in the loop

Every email, including follow-ups, needs approval. Reviewers can approve (singly or in batches), edit or reject with feedback. Replies that are negative or unclear wait for a person as "Opt-Out Pending", as the SoW requires for risk 4. Only the text the person wrote is classified, not the quoted original email. A colleague who asks to be removed is suppressed, and the lead is held for a decision. Nothing replies to a prospect automatically.

Slack alerts name the company and reply number, never the reply's text. They cover positive replies, bookings and replies needing a decision. A digest of the review queue is sent whenever it changes (SoW risk 3).

### Self-learning loop

`LearningAgent.analyse()` runs in every cycle and looks at results per subject variant, segment, follow-up step, reviewer feedback and best-practice targets. Differences must be clear on a 95% interval, so small samples do not produce proposals. A proposal changes nothing until a named person approves it, which creates a new settings version. Each email records the version that produced it.

### Audit log

Every action is written to `audit_log`. Database triggers prevent updates and deletes, and a hash chain reveals tampering (`verify-audit`). `find_breaches` separately re-checks every sent email against the rules, including whether its text matches the verified sentences. Its count is the breach figure in the report.

## Running a real campaign

1. Copy `config/campaign.example.json` to `config/campaign.json` and replace every `REPLACE` value. Claims must come from 42works. Live sending refuses to start while any placeholder remains. Add one entry per sending inbox to `inboxes`, and set `meeting_link` to the 42works booking page if there is one.
2. Set the environment variables below.
3. Run the steps below. `send` without `--live` only shows what would happen and changes nothing. Add `--live` when you are ready to send real email.

| Variable | Needed for |
|---|---|
| `OPENAI_API_KEY` (optional `OPENAI_MODEL`, default `gpt-4o-mini`) | Research, drafting and reply classification |
| `RESEND_API_KEY` | `send --live` and `cycle --live` |
| `ZEROBOUNCE_API_KEY` | Optional: mailbox verification when collecting leads |
| `SLACK_WEBHOOK_URL` | Optional: alerts and review digests |
| `IMAP_PASSWORD` | `poll-inbox`: the sending mailbox's password or app password |

```bash
python -m outreach init --config config/campaign.json --approved-by "Name"
python -m outreach collect-csv leads.csv
python -m outreach research
python -m outreach draft
python -m outreach review-list
python -m outreach approve 1 2 3 --reviewer "Name"
python -m outreach edit 4 "New main text" --reviewer "Name"
python -m outreach send
python -m outreach poll-inbox --host imap.example.com --user outreach@example.com
python -m outreach booking --ref <utm_content value>
python -m outreach report --out reports
```

`poll-inbox` reads new replies from one sending mailbox: run it for each inbox, every few minutes. It matches each reply to its lead through the thread headers, ignores bounce notices and copies of our own mail, treats messages marked `Auto-Submitted` as automatic replies, never handles the same message twice, and marks a message as read only once it is stored. `reply` does the same for one reply typed by hand.

`leads.csv` columns: `email,first_name,last_name,title,company,segment,timezone`. Only `email` is required. `timezone` is an IANA name such as `America/Vancouver`; without one, `default_timezone` applies.

`python -m outreach cycle` runs one full pass. `python -m outreach --help` lists every command. Without `OPENAI_API_KEY`, commands that need the model stop with an error; `--offline` is for tests and demos and cannot be combined with `--live`.

## Test mode: real emails to the team only

Set `test_recipient` in the campaign file to a team address. Every email, including follow-ups, is then really sent through Resend, but to that address, with the subject starting `[TEST for <prospect>]`. Everything else works as in a real campaign: reading replies (answer from the team inbox), alerts and reports. Reports are marked as test sends.

Use a separate database for test mode (`--db test.sqlite3`). The leads in it have "received" emails, so the send gate refuses to send for real from a database that was used for test sends.

## Measuring draft quality before the pilot

```bash
python -m outreach eval-export --out evaluation --limit 50
python -m outreach eval-score --dir evaluation
```

`eval-export` writes `sentences.csv` (each cited sentence next to its source) and `emails.csv` (each full email). Both use semicolons, so Excel in Portugal opens them directly. A person fills in `faithful_y_n` and `would_send_y_n` (`y`/`n` or `s`/`n`). `eval-score` writes `evaluation_results.md` with the share of unsupported sentences and acceptable emails, each with a 95% bound, and how much the code check removed.

## What has and has not been verified

| Item | Status |
|---|---|
| Agent logic and guardrails | 165 automated tests pass |
| Whether the tests catch faults | 46 rules were disabled one at a time, and the tests failed each time |
| Full campaign flow | Simulated with 2 inboxes and 2 time zones: 500 emails, 0 breaches, never above 30 per inbox per day, every follow-up in the same thread |
| Fetching a real website | Checked on one public page |
| OpenAI client | Tested against a local stand-in server only. **Never run against the real API, so real email quality is unknown** |
| Resend client | Tested against a local stand-in server only. **Never run against the real service.** Still to confirm with a real account: that it keeps the Message-ID we set (threading and reply matching depend on it) and that it honours the `Idempotency-Key` header |
| ZeroBounce and Slack clients | Tested against local stand-ins only |
| IMAP reader | Tested with a simulated mailbox only. **Never run against a real mailbox** |
| Success criteria (3% open rate, 0.01% response rate) | **Not yet shown.** The demo rates are simulated; only a real pilot can show them |

## Needed before the pilot

| Item | Owner |
|---|---|
| Approved claims, postal address, sending domain and booking page | 42works |
| Check that outreach complies with CASL and other applicable law (the system enforces opt-outs but does not establish a legal basis for contact) | 42works and the team |
| HITL interface (T6) connected to `review.py` | T6 |
| Public HTTPS endpoints that call these functions. **The unsubscribe endpoint is mandatory: the link in each email does nothing until it exists** | T9, T12 |
| SPF, DKIM and DMARC on the sending domain, and 2 to 4 weeks of warm-up | T12 and 42works |
| Scheduler to run `cycle --live` every hour and `poll-inbox` every few minutes for each inbox, PostgreSQL and backups | T12 |
| Sending mailboxes with IMAP access | 42works |
| Real draft-quality evaluation with the OpenAI key (`eval-export`) | This team |
| Real best-practice sources: the targets in the settings are team placeholders and are not updated automatically | This team |

The endpoints must call:

| Endpoint | Function |
|---|---|
| Unsubscribe link | `ReplyAgent.unsubscribe(token)` |
| Provider events | `SendingAgent.ingest_event(provider_id, event)` |
| Calendar bookings, passing the `utm_content` value | `ReplyAgent.record_booking(ref)` |

Replies need no endpoint: `poll-inbox` reads them from the mailbox.

Lead collection needs a starting list: it reads CSV files and sites the team supplies, and does not discover companies by itself. Discovery needs a data provider that the team and client must choose.

## Ownership

Each part has one person who answers for it and reviews changes to it. Fill in the names.

| Area | Files | Owner |
|---|---|---|
| Fact checking | `verification.py`, `agents/research.py` | |
| Send gate and sending | `guardrails.py`, `agents/sending.py`, `mailer.py` | |
| Replies and bookings | `agents/reply_handler.py` | |
| Writing, learning and reports | `agents/writer.py`, `agents/learning.py`, `agents/reporting.py`, `evaluation.py` | |
| Leads and integrations | `agents/lead_collection.py`, `email_verification.py`, `notify.py`, `inbox_reader.py`, `cli.py` | |

## Differences from the SoW stack

These should be recorded under SoW section 3.3 if the team keeps them.

| SoW | Here | Reason |
|---|---|---|
| LangChain / AutoGen | Own orchestrator | Keeps the flow deterministic, so the model cannot bypass a guardrail |
| PostgreSQL | SQLite | Runs with no setup. Moving to PostgreSQL means replacing `db.py` and some SQLite-specific SQL |
| SendGrid / Resend | Resend | The SoW accepts either |
