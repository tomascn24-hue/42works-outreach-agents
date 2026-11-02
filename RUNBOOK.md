# Runbook: operating the outreach agents

For the people who run campaigns at 42works and the team that supports them. Commands assume `python -m outreach --db <database file>`; `--db` is left out below.

## Daily routine

| When | Who | What |
|---|---|---|
| Morning | Reviewer | `review-list`: check each draft against its sources, then `approve`, `edit` or `reject --feedback` |
| Morning | Reviewer | `replies-list`: decide every held reply with `resolve-reply` (`opt_out`, `continue`, `interested`, `meeting_booked`) |
| Every hour (scheduler) | System | `cycle --live`: research, draft, send, follow-ups and learning |
| Every 5 minutes (scheduler) | System | `poll-inbox` for each sending mailbox: replies, opt-outs and automatic replies |
| After the last cycle of the day | Operator | `stuck-list` must be empty; resolve anything listed (see below) |
| Weekly | Campaign owner | `report --out reports`, then `proposals` and `decide` for each pending proposal |
| Weekly | Operator | `verify-audit` must say the chain is intact |

When checking a draft:

- Does each sentence say only what its source says?
- Would you send it under your own name?
- Is the subject right for this person?

If anything is wrong, reject it with feedback. Feedback that repeats becomes a learning proposal.

## Incidents

### Something is wrong and sending must stop now

```bash
python -m outreach set-paused on --approved-by "Name"
```

Every send is deferred until `set-paused off`. Drafting and reply handling continue. Then find the cause in the audit log and the report.

### An email went out that should not have

1. Pause the campaign.
2. Run `report` and read the Guardrails section: any breach is listed with its message number.
3. Find the message in `performance_record.csv`, and its history in `audit_log` (`entity='message'`, `entity_id=<id>`).
4. If the recipient complained or asked to stop, run `unsubscribe <token>`, or `resolve-reply <id> opt_out`.
5. Write down the cause and the fix before you resume. A breach fails the project's success criteria, so tell the project manager.

### `stuck-list` shows a message

The process stopped while the email was being handed to the provider, so it may or may not have gone.

1. Look it up in the Resend dashboard (Emails), by recipient and time.
2. If it was sent: `stuck-resolve <id> sent --reviewer "Name" --provider-id <Resend id>`.
3. If it was not sent: `stuck-resolve <id> not_sent --reviewer "Name"`. It is sent on the next cycle with the same Message-ID and idempotency key.

### Bounces rise above about 2% of deliveries

Pause, and check the latest lead source: bounces usually come from one list. Turn on mailbox verification (`ZEROBOUNCE_API_KEY`) before collecting more leads. Bounced addresses are already suppressed.

### Opens fall suddenly, or Resend or Google Postmaster warns about reputation

The domain may be landing in spam. Pause, check SPF, DKIM and DMARC, lower `daily_cap_per_inbox` with a new settings version, and restart the warm-up.

### OpenAI is unavailable

Nothing is lost. Leads waiting for research stay `new` and drafts are retried on the next cycle. Reply classification falls back to "Opt-Out Pending", so replies wait for a person.

### A lead shows `needs_manual`

The audit log says why: no verifiable facts, content rule or failed send. Fix the cause (for example, approve a missing claim), then run `requeue <lead id> --reviewer "Name"`.

### Someone asks what data is held about them, or for it to be deleted

Their address stays on the suppression list, which is needed so they are never contacted again. Lead details, facts and replies are in the `leads`, `facts` and `replies` tables. The audit log cannot be edited, by design. **There is no deletion command yet:** the team must agree with 42works how to handle such requests before the pilot.

### `poll-inbox` fails to log in

Check the password, and whether the provider needs an app password and IMAP turned on (Google Workspace and Microsoft 365 do). Until it works, replies pile up unread in the mailbox; nothing is lost, and they are handled on the next successful poll. Meanwhile follow-ups can still go to people who already answered, so pause the campaign if it stays down for more than a few hours.

## Testing with real emails before the pilot

1. Use a separate database: add `--db test.sqlite3` to every command.
2. Set `test_recipient` to the team inbox in a copy of the campaign file, then `init` with it.
3. Collect 20 to 50 real leads, `research`, `draft`, review, then `send --live`. Every email reaches the team inbox, with the subject `[TEST for <prospect>]`.
4. Reply from the team inbox to try the reply handling, then run `poll-inbox`.
5. Rate the drafts with `eval-export` and `eval-score`.
6. Start the real campaign in a new database. The test database cannot send for real.

## Changing the campaign

Settings are versioned, and each change records who approved it and why.

- **Content or limits:** edit a copy of the campaign file, then run `init --config <file> --approved-by "Name"`. This creates a new version, and the hard limits in the code still apply.
- **New inbox:** add it to `inboxes`. It starts its own warm-up, and existing leads keep the inbox that first wrote to them.
- **Removing an inbox:** unsent messages from it are blocked. A lead whose first email was blocked goes to `needs_manual`; requeue it to redraft from another inbox. Follow-ups are redrafted from another inbox automatically, but they no longer appear in the same thread as the first email.

## Before every release

```bash
python -m unittest discover -s tests
python demo.py
```

Both must finish without errors. In `demo_output/summary.json`, `breaches` must be empty. The GitHub workflow runs the tests on every push.
