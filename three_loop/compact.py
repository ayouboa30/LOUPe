"""Token-budget compaction for context carried between roles and cycles.

Why this exists
---------------
CPU inference is prefill-bound. Measured on a Ryzen 7 5825U with
Qwen2.5-Coder-3B Q4_K_M: prefill runs at ~70 tok/s (14.3 ms per prompt
token) while decode sits at 22.4 tok/s, which is already the DDR4-3200
dual-channel bandwidth ceiling. On a realistic 1500-token prompt that makes
prefill ~94% of wall-clock time. So the only lever that matters is *how many
prompt tokens* get re-fed into the model - not how fast it generates.

What does NOT work
------------------
Stripping vowels ("Bonjour" -> "Bnjr") looks like it should help: it cuts
character count by ~38%. Measured against this model's tokenizer it does the
opposite - token count goes *up* 17% overall and up to 89% on English prose.
BPE merges are learned over real words, so "heuristic" costs 1-2 tokens while
"hrstc" fragments into one token per consonant. Fewer characters, more tokens,
and unreadable context. ``strip_vowels`` below is kept only so that result
stays reproducible; it is not used by the pipeline.

What does work
--------------
Removing whole redundant *tokens*: collapsed whitespace, dropped boilerplate,
and a hard budget that keeps the most recent content intact rather than
mangling all of it.
"""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")

#: Filler that carries no information for the next role but costs real tokens.
_FILLER = re.compile(
    r"\b(?:"
    r"il est important de noter que|il convient de (?:noter|souligner) que|"
    r"en d'?autres termes|c'?est-a-dire|"
    r"as an ai(?: language model)?|"
    r"it is important to note that|it should be noted that|"
    r"in other words|that is to say|"
    r"bien s[uû]r|of course|certainly"
    r")\b[,:]?\s*",
    re.IGNORECASE,
)

#: Rough chars-per-token for this tokenizer family, used only to turn a token
#: budget into a character budget without paying for a real tokenize() call.
_CHARS_PER_TOKEN = 3.6


def strip_vowels(text: str) -> str:
    """Consonants and digits only. Measured harmful - kept for reproducibility.

    Retained so the "+17% tokens" measurement in this module's docstring can
    be re-run, not because anything should call it.
    """

    no_punctuation = re.sub(r"[^0-9A-Za-z\s]", " ", text)
    consonants = re.sub(
        "[aeiouyAEIOUYÀÂÄÉÈÊËÎÏÔÖÙÛÜàâäéèêëîïôöùûü]", "", no_punctuation
    )
    return re.sub(r"\s+", " ", consonants).strip()


def compact_text(text: str, *, max_tokens: int | None = None) -> str:
    """Drop redundant tokens while leaving the wording itself readable.

    Whitespace is collapsed and known filler phrases are removed. When
    ``max_tokens`` is given the tail is kept and the head is dropped whole,
    since the most recent context is what the next role actually needs.
    """

    cleaned = _FILLER.sub("", text)
    cleaned = _WHITESPACE.sub(" ", cleaned)
    cleaned = _TRAILING_SPACE.sub("\n", cleaned)
    cleaned = _BLANK_LINES.sub("\n\n", cleaned).strip()

    if max_tokens is None:
        return cleaned
    budget = int(max_tokens * _CHARS_PER_TOKEN)
    if len(cleaned) <= budget:
        return cleaned
    # Cut on a paragraph boundary when one is close, so the kept text starts
    # at something coherent instead of mid-sentence.
    tail = cleaned[-budget:]
    boundary = tail.find("\n\n")
    if 0 <= boundary < budget // 4:
        tail = tail[boundary + 2 :]
    return "[...contexte ancien omis...]\n" + tail
