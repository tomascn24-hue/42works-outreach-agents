"""Research & Fact-Check Agent (T4): builds the cited evidence base for each lead.

The model may only choose among sentences by index. Stored fact text is always the
verbatim page sentence, so it cannot introduce anything that is not in the source.
"""
import re

from ..context import sha256
from ..llm import LLMError
from ..web import html_to_text

ACTOR = "research_agent"
RESEARCH_PAGES = ("", "/about", "/about-us", "/company", "/news", "/blog")
MAX_FACTS = 5
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-Ö0-9\"“])")
_INJECTION = re.compile(
    r"ignore (all |any )?(previous|prior|above)|system prompt|you are (an?|now)|as an ai|"
    r"instructions?:|assistant:|language model", re.I)
_BOILERPLATE = re.compile(
    r"cookie|privacy policy|terms (of|and)|all rights reserved|subscribe|sign up|log ?in|click here|"
    r"javascript|copyright|©", re.I)

SELECT_SYSTEM = (
    "You choose sentences from a company's website that are specific, factual and useful for "
    "personalising a B2B email (products, customers, markets, news). Avoid slogans. "
    'Reply as {"indices": [..]} with at most 5 indices into candidates, best first.'
)


def extract_candidates(text):
    seen, out = set(), []
    for block in text.split("\n"):
        for sentence in _SENTENCE_END.split(block.strip()):
            sentence = " ".join(sentence.split())
            if not 40 <= len(sentence) <= 240 or len(sentence.split()) < 6:
                continue
            if not (sentence[0].isupper() or sentence[0].isdigit()):
                continue  # a fragment can change the meaning of what the source says
            if sentence[-1] not in ".!?" or _INJECTION.search(sentence) or _BOILERPLATE.search(sentence):
                continue
            if sentence.lower() not in seen:
                seen.add(sentence.lower())
                out.append(sentence)
    return out


class ResearchAgent:
    def __init__(self, ctx, fetcher):
        self.ctx = ctx
        self.fetcher = fetcher

    def research(self, lead):
        candidates = []  # (sentence, url, page hash)
        fetched_at = self.ctx.now_iso()
        for path in RESEARCH_PAGES:
            url = f"https://{lead['domain']}{path}"
            html = self.fetcher.get(url)
            if not html:
                continue
            page_hash = sha256(html)
            known = {c[0].lower() for c in candidates}
            for sentence in extract_candidates(html_to_text(html)):
                if sentence.lower() not in known:
                    candidates.append((sentence, url, page_hash))

        try:
            chosen = self._select(lead, candidates)
        except LLMError as exc:
            self.ctx.audit(ACTOR, "fact_selection_failed", "lead", lead["id"], {"error": str(exc)})
            return 0  # the lead stays new and is retried
        with self.ctx.db.tx():
            for index in chosen:
                sentence, url, page_hash = candidates[index]
                cur = self.ctx.db.execute(
                    "INSERT INTO facts(lead_id, text, source_url, page_sha256, fetched_at) VALUES (?,?,?,?,?)",
                    (lead["id"], sentence, url, page_hash, fetched_at),
                )
                self.ctx.audit(ACTOR, "fact_stored", "fact", cur.lastrowid, {"lead": lead["id"], "url": url})
            status = "researched" if chosen else "research_failed"
            self.ctx.db.execute("UPDATE leads SET status=? WHERE id=?", (status, lead["id"]))
            self.ctx.audit(ACTOR, "research_done", "lead", lead["id"],
                           {"candidates": len(candidates), "facts": len(chosen), "status": status})
        return len(chosen)

    def _select(self, lead, candidates):
        if not candidates:
            return []
        payload = {"company": lead["company"], "candidates": [c[0] for c in candidates[:60]]}
        raw = self.ctx.llm.complete_json("select_facts", SELECT_SYSTEM, payload).get("indices", [])
        chosen = []
        for i in raw if isinstance(raw, list) else []:
            if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(payload["candidates"]) \
                    and i not in chosen:
                chosen.append(i)
        return chosen[:MAX_FACTS]
