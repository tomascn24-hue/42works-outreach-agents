"""Orchestrator: moves leads through the agents. Each step is safe to run repeatedly.

new -> researched -> drafted -> (human approves) -> in_sequence -> goal_met | exhausted
Any time: opt_out_pending (a human decides), opted_out, bounced, needs_manual.
"""
from .agents.learning import LearningAgent
from .agents.reporting import ReportingAgent
from .agents.research import ResearchAgent
from .agents.sending import SendingAgent
from .agents.writer import WriterAgent
from .context import canonical

ACTOR = "orchestrator"


class Orchestrator:
    def __init__(self, ctx, fetcher, mailer):
        self.ctx = ctx
        self.research = ResearchAgent(ctx, fetcher)
        self.writer = WriterAgent(ctx)
        self.sender = SendingAgent(ctx, mailer)
        self.reporter = ReportingAgent(ctx)
        self.learner = LearningAgent(ctx)

    def research_new(self):
        rows = self.ctx.db.all("SELECT * FROM leads WHERE status='new' ORDER BY id")
        for lead in rows:
            self.research.research(lead)
        return len(rows)

    def draft_researched(self):
        rows = self.ctx.db.all("SELECT * FROM leads WHERE status='researched' ORDER BY id")
        return sum(self.writer.draft(lead) is not None for lead in rows)

    def run_cycle(self, send=True):
        """One pass. Drafts stop at the review queue: only approved messages can be sent."""
        summary = {
            "researched": self.research_new(),
            "drafted": self.draft_researched(),
            "send": self.sender.send_due() if send else None,
            "followups_drafted": self.writer.schedule_followups(),
            "proposals": self.learner.analyse(),
        }
        self.ctx.audit(ACTOR, "cycle_completed", "campaign", "-", summary)
        self.review_digest()
        return summary

    def review_digest(self):
        """Tell reviewers what is waiting, once per change, so approvals do not stall (SoW risk 3)."""
        db = self.ctx.db
        counts = {
            "drafts": db.scalar("SELECT COUNT(*) FROM messages WHERE status='pending_review'"),
            "replies": db.scalar("SELECT COUNT(*) FROM replies WHERE needs_human=1 AND resolved_at IS NULL"),
            "stuck": len(self.sender.stuck()),
        }
        last = db.scalar("SELECT details FROM audit_log WHERE action='review_digest' ORDER BY id DESC LIMIT 1")
        if not any(counts.values()) or canonical(counts) == last:
            return False
        text = f"Waiting for review: {counts['drafts']} drafts, {counts['replies']} replies"
        if counts["stuck"]:
            text += f", {counts['stuck']} emails stuck in sending"
        self.ctx.notify(text + ".")
        self.ctx.audit(ACTOR, "review_digest", "campaign", "-", counts)
        return True
