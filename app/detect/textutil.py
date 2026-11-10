"""Small text helpers: sentence splitting and negation checks for visible page text."""
from __future__ import annotations

import re

_SPLIT_RE = re.compile(r"(?<=[.?!])\s+|\n+")
_NEG_RE = re.compile(r"\b(?:not|no|never|don't|doesn't|do not|does not|isn't|aren't|unsupported|"
                     r"no longer|cannot|can't)\b|n’t\b", re.I)
_ANSWER_NO_RE = re.compile(r"^\s*(?:no|not|nope|unfortunately)\b", re.I)
_DECLARE_RE = re.compile(
    r"\b(?:we use|we accept|we support|we partner with|powered by|processed (?:securely )?(?:by|through|via|with)|"
    r"handled (?:securely )?by|secured by|payments? (?:is|are) (?:handled|processed)|billed (?:via|through)|"
    r"checkout (?:by|via|through))\b", re.I)


def sentences(text: str) -> list[str]:
    """Split visible text into rough sentences (line breaks also split)."""
    return [s.strip() for s in _SPLIT_RE.split(text) if s and s.strip()]


def is_negated(sents: list[str], i: int) -> bool:
    """True if sentence i is negative, or is a question answered with 'No / Not ...'."""
    s = sents[i]
    if _NEG_RE.search(s):
        return True
    if s.endswith("?") and i + 1 < len(sents) and _ANSWER_NO_RE.match(sents[i + 1]):
        return True
    return False


def is_declaration(sentence: str) -> bool:
    """Sentence states a fact about the site's own payments ('We use Stripe ...')."""
    return not sentence.endswith("?") and bool(_DECLARE_RE.search(sentence))
