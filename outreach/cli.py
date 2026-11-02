"""Command line for operators and reviewers, until the HITL UI (T6) is connected."""
import argparse
import imaplib
import json
import os
import sys
from pathlib import Path

from .agents.learning import LearningAgent
from .agents.lead_collection import LeadCollectionAgent
from .agents.reply_handler import ReplyAgent
from .agents.reporting import ReportingAgent
from .agents.sending import SendingAgent
from .context import Context, SettingsError, validate_live
from .db import DB
from .email_verification import ZeroBounceVerifier
from .evaluation import export_sheets, score
from .inbox_reader import ImapReplyReader
from .llm import OfflineLLM, OpenAILLM
from .mailer import DryRunMailer, ResendMailer
from .notify import SlackNotifier
from .orchestrator import Orchestrator
from .review import ReviewError, ReviewService
from .web import HttpFetcher


NEEDS_MODEL = {"research", "draft", "followups", "reply", "cycle", "poll-inbox"}


def _parser():
    p = argparse.ArgumentParser(prog="outreach")
    p.add_argument("--db", default="outreach.sqlite3")
    p.add_argument("--offline", action="store_true", help="use the deterministic offline model")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="load the campaign settings")
    s.add_argument("--config", required=True)
    s.add_argument("--approved-by", required=True)
    s = sub.add_parser("collect-csv")
    s.add_argument("path")
    s = sub.add_parser("collect-site")
    s.add_argument("domain")
    s.add_argument("--segment", default="default")
    sub.add_parser("research")
    sub.add_parser("draft")
    sub.add_parser("followups")

    sub.add_parser("review-list")
    s = sub.add_parser("approve")
    s.add_argument("ids", type=int, nargs="+")
    s.add_argument("--reviewer", required=True)
    s = sub.add_parser("edit", help="rewrite a draft's main text and approve it")
    s.add_argument("id", type=int)
    s.add_argument("text")
    s.add_argument("--reviewer", required=True)
    s = sub.add_parser("requeue", help="return a needs_manual lead to the flow")
    s.add_argument("id", type=int)
    s.add_argument("--reviewer", required=True)
    s = sub.add_parser("reject")
    s.add_argument("id", type=int)
    s.add_argument("--reviewer", required=True)
    s.add_argument("--feedback", required=True)
    s.add_argument("--no-redraft", action="store_true")

    s = sub.add_parser("send", help="show what would be sent; sends only with --live")
    s.add_argument("--live", action="store_true")
    s = sub.add_parser("cycle", help="run one full pass; sends only with --live")
    s.add_argument("--live", action="store_true")
    s = sub.add_parser("event")
    s.add_argument("provider_id")
    s.add_argument("event", choices=["delivered", "opened", "bounced", "complained"])
    s = sub.add_parser("reply")
    s.add_argument("from_email")
    s.add_argument("body")
    s.add_argument("--in-reply-to", help="the reply's In-Reply-To header, to match the thread")
    s = sub.add_parser("booking", help="record a meeting booked through the calendar link")
    s.add_argument("--ref", help="utm_content value from the booking")
    s.add_argument("--email")
    s = sub.add_parser("poll-inbox", help="read new replies from a sending mailbox (password: IMAP_PASSWORD)")
    s.add_argument("--host", required=True)
    s.add_argument("--user", required=True)
    s.add_argument("--folder", default="INBOX")
    s.add_argument("--port", type=int, default=993)
    sub.add_parser("stuck-list", help="emails whose provider call never finished")
    s = sub.add_parser("stuck-resolve")
    s.add_argument("id", type=int)
    s.add_argument("outcome", choices=["sent", "not_sent"])
    s.add_argument("--reviewer", required=True)
    s.add_argument("--provider-id")
    s = sub.add_parser("eval-export", help="write sheets for a person to rate real drafts")
    s.add_argument("--out", default="evaluation")
    s.add_argument("--limit", type=int, default=50)
    s = sub.add_parser("eval-score", help="score the rated sheets")
    s.add_argument("--dir", default="evaluation")
    s = sub.add_parser("unsubscribe")
    s.add_argument("token")
    sub.add_parser("replies-list")
    s = sub.add_parser("resolve-reply")
    s.add_argument("id", type=int)
    s.add_argument("resolution", choices=["opt_out", "continue", "interested", "meeting_booked"])
    s.add_argument("--reviewer", required=True)

    s = sub.add_parser("report")
    s.add_argument("--out", default="reports")
    sub.add_parser("learn")
    sub.add_parser("proposals")
    s = sub.add_parser("decide")
    s.add_argument("id", type=int)
    s.add_argument("decision", choices=["approve", "reject"])
    s.add_argument("--approver", required=True)
    s = sub.add_parser("set-paused", help="kill switch")
    s.add_argument("state", choices=["on", "off"])
    s.add_argument("--approved-by", required=True)
    sub.add_parser("verify-audit")
    return p


def _llm(args):
    if args.offline:
        return OfflineLLM()
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAILLM()
    if args.command in NEEDS_MODEL:
        raise SettingsError("OPENAI_API_KEY is not set (use --offline only for tests and demos)")
    return None


def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        llm = _llm(args)
        if getattr(args, "live", False) and args.offline:
            raise SettingsError("--live cannot be combined with --offline")
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    notifier = SlackNotifier() if os.environ.get("SLACK_WEBHOOK_URL") else None
    ctx = Context(DB(args.db), llm=llm, notifier=notifier)
    verifier = ZeroBounceVerifier() if os.environ.get("ZEROBOUNCE_API_KEY") else None
    review = ReviewService(ctx)
    c = args.command
    try:
        if c == "init":
            with open(args.config, encoding="utf-8") as handle:
                ctx.save_settings(json.load(handle), args.approved_by, f"loaded {args.config}")
            print("campaign initialised")
        elif c == "collect-csv":
            print(LeadCollectionAgent(ctx, verifier=verifier).collect_csv(args.path), "leads added")
        elif c == "collect-site":
            agent = LeadCollectionAgent(ctx, HttpFetcher(), verifier=verifier)
            print(agent.collect_website(args.domain, args.segment), "leads added")
        elif c == "research":
            print(Orchestrator(ctx, HttpFetcher(), DryRunMailer()).research_new(), "leads researched")
        elif c == "draft":
            print(Orchestrator(ctx, HttpFetcher(), DryRunMailer()).draft_researched(), "drafts queued for review")
        elif c == "followups":
            print(Orchestrator(ctx, HttpFetcher(), DryRunMailer()).writer.schedule_followups(), "follow-ups queued")
        elif c == "review-list":
            _print(review.pending_drafts())
        elif c == "approve":
            print(review.approve_batch(args.ids, args.reviewer), "approved")
        elif c == "edit":
            review.edit_and_approve(args.id, args.reviewer, args.text)
            print("edited and approved")
        elif c == "requeue":
            print(review.requeue_lead(args.id, args.reviewer))
        elif c == "reject":
            review.reject(args.id, args.reviewer, args.feedback, redraft=not args.no_redraft)
            print("rejected")
        elif c == "send":
            if args.live:
                validate_live(ctx.settings)
                _print(SendingAgent(ctx, ResendMailer()).send_due())
            else:
                _print(SendingAgent(ctx, DryRunMailer()).preview())
        elif c == "cycle":
            if args.live:
                validate_live(ctx.settings)
            mailer = ResendMailer() if args.live else DryRunMailer()
            _print(Orchestrator(ctx, HttpFetcher(), mailer).run_cycle(send=args.live))
        elif c == "event":
            print(SendingAgent(ctx, DryRunMailer()).ingest_event(args.provider_id, args.event))
        elif c == "reply":
            result = ReplyAgent(ctx).handle(args.from_email, args.body, args.in_reply_to)
            _print(dict(zip(("reply_id", "intent"), result)))
        elif c == "booking":
            print(ReplyAgent(ctx).record_booking(args.ref, args.email) or "no matching lead")
        elif c == "poll-inbox":
            _print(ImapReplyReader(ctx, args.host, args.user, folder=args.folder, port=args.port).poll())
        elif c == "stuck-list":
            _print(SendingAgent(ctx, DryRunMailer()).stuck())
        elif c == "stuck-resolve":
            SendingAgent(ctx, DryRunMailer()).resolve_stuck(args.id, args.reviewer, args.outcome, args.provider_id)
            print("resolved")
        elif c == "eval-export":
            _print(export_sheets(ctx, args.out, args.limit))
        elif c == "eval-score":
            _print(score(ctx, args.dir, Path(args.dir) / "evaluation_results.md"))
        elif c == "unsubscribe":
            print(ReplyAgent(ctx).unsubscribe(args.token))
        elif c == "replies-list":
            _print(review.pending_replies())
        elif c == "resolve-reply":
            review.resolve_reply(args.id, args.reviewer, args.resolution)
            print("resolved")
        elif c == "report":
            m = ReportingAgent(ctx).write_report(args.out)
            print(f"open rate {m['open_rate']}, response rate {m['response_rate']}, breaches {len(m['breaches'])}")
        elif c == "learn":
            print(LearningAgent(ctx).analyse(), "new proposals")
        elif c == "proposals":
            _print([dict(r) for r in ctx.db.all("SELECT * FROM proposals ORDER BY id")])
        elif c == "decide":
            LearningAgent(ctx).decide(args.id, args.approver, args.decision == "approve")
            print("recorded")
        elif c == "set-paused":
            settings = ctx.settings
            settings["paused"] = args.state == "on"
            ctx.save_settings(settings, args.approved_by, f"paused={args.state}")
            print("paused" if settings["paused"] else "running")
        elif c == "verify-audit":
            bad = ctx.verify_audit_chain()
            print("audit chain intact" if bad is None else f"TAMPERED at row {bad}")
            return 0 if bad is None else 1
    except (SettingsError, ReviewError, ValueError, OSError, imaplib.IMAP4.error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        ctx.db.close()
    return 0
