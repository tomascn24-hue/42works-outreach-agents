"""Zero-hallucination gate (SoW risk 1): unverifiable sentences are dropped by code.

A sentence survives only if it is an approved phrase, or cites known sources and all
of its numbers, names and negations appear in them and its content words come from them.
When a real model is configured it runs as a second, fail-closed check; it can only
drop a sentence, never rescue one.
"""
import re

from .context import render_phrase
from .llm import LLMError

_WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", re.UNICODE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_CAPITALISED = re.compile(r"\b[A-ZÀ-ÖØ-Þ][^\W\d_]*(?:['’-][^\W\d_]+)*", re.UNICODE)
_LINK = re.compile(r"https?://|www\.|\S+@\S+\.\S+", re.I)
_NEGATIONS = {"not", "no", "never", "without", "cannot", "none", "nor", "neither"}

# Framing words a writer may add around a sourced fact without changing what it says.
_FRAMING = set("""
a about after also an and are as at be because been but by can could did do does
for from had has have he her his how i if in into is it its just like may me might
my of on or our she should so such than that the their them then there these they
this those to too us was we were what when where which while who why will with would you your
yours congratulations noticed saw read see seen came across found looked website site page
pages mention mentions mentioned says said share shared shares note noted team company
business
""".split())


def _normalise(word):
    return word.lower().replace("’", "'").removesuffix("'s")


def _stem(word):
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _words(text):
    return [_normalise(w) for w in _WORD.findall(text)]


def _has_negation(text):
    words = _words(text)
    return any(w in _NEGATIONS or w.endswith("n't") or w.endswith("n’t") for w in words)


def _numbers(text):
    return {n.rstrip(".,").replace(",", "") for n in _NUMBER.findall(text)}


def _clean(text):
    return " ".join(text.split()).casefold()


def _check_cited(text, sources, lead, min_overlap):
    source_text = " ".join(sources)
    source_words = set(_words(source_text))
    lead_words = set(_words(" ".join([lead["first_name"], lead["last_name"], lead["company"]])))

    missing_numbers = _numbers(text) - _numbers(source_text)
    if missing_numbers:
        return f"numbers not in source: {sorted(missing_numbers)}"

    allowed_names = source_words | lead_words | _FRAMING
    for name in _CAPITALISED.findall(text):
        if _normalise(name) not in allowed_names:
            return f"name not in source: {name}"

    if _has_negation(text) and not _has_negation(source_text):
        return "negation not in source"

    known = {_stem(w) for w in source_words | lead_words}
    content = {_stem(w) for w in _words(text) if len(w) >= 3 and w not in _FRAMING}
    if not content:
        return "cited sentence has no substantive content"
    overlap = len(content & known) / len(content)
    if overlap < min_overlap:
        return f"content overlap {overlap:.2f} below {min_overlap}"
    return None


SECOND_CHECK_SYSTEM = (
    "You are a strict fact checker. Decide whether the sentence is fully supported by the sources, "
    'with nothing added. Reply as {"supported": true} or {"supported": false}.'
)


def _second_check(llm, text, sources):
    if llm is None or not getattr(llm, "can_verify", False):
        return None
    try:
        result = llm.complete_json("verify_sentence", SECOND_CHECK_SYSTEM, {"sentence": text, "sources": sources})
    except LLMError as exc:
        return f"secondary check unavailable: {exc}"
    return None if result.get("supported") is True else "secondary check: not supported"


def verify_draft(draft, lead, facts, settings, llm=None, require_fact=True):
    """Return {ok, kept, dropped}. `facts` maps ids like 'F3' to the stored source sentence."""
    claims = {c["id"]: c["text"] for c in settings["approved_claims"]}
    phrases = settings["approved_phrases"]
    cta = {_clean(render_phrase(p, lead)) for p in phrases["cta"]}
    other = {_clean(render_phrase(p, lead)) for p in phrases.get("follow_up", []) + phrases.get("opener", [])}

    kept, dropped, seen = [], [], set()
    items = draft.get("sentences") if isinstance(draft, dict) else None
    for item in items if isinstance(items, list) else []:
        text = item.get("text") if isinstance(item, dict) else None
        cites = item.get("cites") if isinstance(item, dict) else None
        if not isinstance(text, str) or not text.strip():
            dropped.append({"text": str(text), "reason": "malformed sentence"})
            continue
        text = " ".join(text.split())
        cites = cites if isinstance(cites, list) else []
        reason, kind = None, "cited"

        if _clean(text) in seen:
            reason = "repeated sentence"
        elif _LINK.search(text):
            reason = "links and addresses are added by code only"
        elif not cites:
            if _clean(text) in cta:
                kind = "cta"
            elif _clean(text) in other:
                kind = "approved_phrase"
            else:
                reason = "no citation and not an approved phrase"
        else:
            unknown = [c for c in cites if c not in facts and c not in claims]
            if unknown:
                reason = f"unknown citations: {unknown}"
            else:
                sources = [facts.get(c) or claims[c] for c in cites]
                reason = _check_cited(text, sources, lead, settings["min_overlap"]) \
                    or _second_check(llm, text, sources)

        if reason:
            dropped.append({"text": text, "reason": reason})
        else:
            seen.add(_clean(text))
            kept.append({"text": text, "cites": cites, "kind": kind})

    has_cta = any(k["kind"] == "cta" for k in kept)
    has_fact = any(c in facts for k in kept for c in k["cites"])
    ok = has_cta and (has_fact or not require_fact)
    return {"ok": ok, "kept": kept, "dropped": dropped}
