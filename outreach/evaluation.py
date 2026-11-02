"""Human evaluation of real drafts: the evidence for the no-hallucination requirement.

export: writes two sheets for reviewers to fill in (one row per cited sentence, one per email).
score:  reads them back and reports error rates with 95% confidence bounds.
"""
import csv
import json
from pathlib import Path

from .agents.learning import wilson

SENTENCE_COLUMNS = ["message_id", "company", "sentence", "sources", "source_urls", "faithful_y_n", "notes"]
EMAIL_COLUMNS = ["message_id", "company", "subject", "body", "would_send_y_n", "notes"]
YES = {"y", "yes", "s", "sim", "1", "true"}
NO = {"n", "no", "não", "nao", "0", "false"}


def _source(ctx, cite, claims):
    if cite.startswith("F"):
        row = ctx.db.one("SELECT text, source_url FROM facts WHERE id=?", (cite[1:],))
        return (row["text"], row["source_url"]) if row else ("(missing)", "")
    return claims.get(cite, "(missing)"), "client-approved claims"


def export_sheets(ctx, out_dir, limit=50):
    """Most recent drafts that were not human-edited. Semicolons so Excel in Portugal opens them directly."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    claims = {c["id"]: c["text"] for c in ctx.settings["approved_claims"]}
    messages = ctx.db.all(
        "SELECT m.*, l.company FROM messages m JOIN leads l ON l.id=m.lead_id"
        " WHERE m.status IN ('pending_review','approved','sent') AND m.human_edited=0 ORDER BY m.id DESC LIMIT ?",
        (limit,))
    sentences = 0
    with open(out / "sentences.csv", "w", newline="", encoding="utf-8-sig") as s_file, \
            open(out / "emails.csv", "w", newline="", encoding="utf-8-sig") as e_file:
        s_writer = csv.writer(s_file, delimiter=";")
        e_writer = csv.writer(e_file, delimiter=";")
        s_writer.writerow(SENTENCE_COLUMNS)
        e_writer.writerow(EMAIL_COLUMNS)
        for m in messages:
            e_writer.writerow([m["id"], m["company"], m["subject"], m["body"], "", ""])
            for k in json.loads(m["verification"])["kept"]:
                if not k["cites"]:
                    continue  # approved phrases need no fact check
                found = [_source(ctx, c, claims) for c in k["cites"]]
                s_writer.writerow([m["id"], m["company"], k["text"], " | ".join(f[0] for f in found),
                                   " | ".join(f[1] for f in found), "", ""])
                sentences += 1
    return {"emails": len(messages), "sentences": sentences}


def _read(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
        return list(csv.DictReader(handle, delimiter=delimiter))


def _tally(rows, column):
    yes = no = 0
    for row in rows:
        value = (row.get(column) or "").strip().lower()
        yes += value in YES
        no += value in NO
    return yes, no


def score(ctx, sheet_dir, out_file=None):
    sheet_dir = Path(sheet_dir)
    faithful, unfaithful = _tally(_read(sheet_dir / "sentences.csv"), "faithful_y_n")
    good, bad = _tally(_read(sheet_dir / "emails.csv"), "would_send_y_n")
    rated_sentences, rated_emails = faithful + unfaithful, good + bad
    kept = ctx.db.scalar("SELECT COALESCE(SUM(json_array_length(verification, '$.kept')),0) FROM messages")
    dropped = ctx.db.scalar("SELECT COALESCE(SUM(json_array_length(verification, '$.dropped')),0) FROM messages")
    failed = ctx.db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='draft_failed'")
    drafted = ctx.db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='draft_created'")
    result = {
        "sentences_rated": rated_sentences,
        "unfaithful_sentences": unfaithful,
        "hallucination_rate": unfaithful / rated_sentences if rated_sentences else None,
        "hallucination_rate_upper_95": wilson(unfaithful, rated_sentences)[1] if rated_sentences else None,
        "emails_rated": rated_emails,
        "emails_acceptable": good,
        "acceptance_rate": good / rated_emails if rated_emails else None,
        "acceptance_rate_lower_95": wilson(good, rated_emails)[0] if rated_emails else None,
        "sentences_dropped_by_code": dropped,
        "drop_share": dropped / (kept + dropped) if kept + dropped else None,
        "drafts_created": drafted,
        "drafts_failed": failed,
    }
    if out_file:
        pct = lambda v: "n/a" if v is None else f"{v * 100:.1f}%"  # noqa: E731
        lines = [
            "# Draft quality evaluation", "",
            f"Generated: {ctx.now_iso()}", "",
            "| Measure | Result |", "|---|---|",
            f"| Cited sentences checked by a person | {rated_sentences} |",
            f"| Not supported by their source | {unfaithful} ({pct(result['hallucination_rate'])}, "
            f"at most {pct(result['hallucination_rate_upper_95'])} with 95% confidence) |",
            f"| Emails a reviewer would send as they are | {good} of {rated_emails} "
            f"({pct(result['acceptance_rate'])}, at least {pct(result['acceptance_rate_lower_95'])} with 95% confidence) |",
            f"| Sentences removed by the code check | {dropped} ({pct(result['drop_share'])} of all proposed) |",
            f"| Drafts that could not be written | {failed} of {failed + drafted} |",
            "",
            "Any unsupported sentence that was not removed is a failure of the requirement: "
            "look at each one and fix the cause before the pilot.",
        ]
        Path(out_file).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result
