"""fnv1a32-6gram-v1 normalization (build-plan 9.4): word runs with code-like tokens masked out."""
from __future__ import annotations

import re
import unicodedata

ALGO = "fnv1a32-6gram-v1"
N = 6
_STRIP = "\"'()[]{}<>,.;:!?*`"
_MASK_CHARS = set("/_.:#@=()[]{}<>\\`")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'",
                         "“": '"', "”": '"', "„": '"', "‟": '"'})


def clean(text: str) -> str:
    """NFKC with curly quotes made straight. Offsets below are into this string (NFKC can change lengths, so
    callers that need offsets into the original pass text that is already NFKC, as typed text nearly is)."""
    return unicodedata.normalize("NFKC", text or "").translate(_QUOTES)


def _masked(raw: str, tok: str) -> bool:
    if "`" in raw:
        return True
    if any(c in _MASK_CHARS for c in tok):
        return True
    if re.search(r"[a-z][A-Z]", tok) or re.search(r"\d", tok):
        return True
    if tok.lower().startswith("http"):
        return True
    return len(tok) >= 3 and tok.isupper()


def sequences(text: str) -> list[list[tuple[str, int, int]]]:
    """Maximal runs of unmasked tokens: [[(word, start, end), ...], ...] with offsets into clean(text)."""
    s = clean(text)
    out: list[list[tuple[str, int, int]]] = []
    cur: list[tuple[str, int, int]] = []
    for m in re.finditer(r"\S+", s):
        raw = m.group(0)
        lead = len(raw) - len(raw.lstrip(_STRIP))
        tok = raw.strip(_STRIP)
        if not tok:
            continue
        if _masked(raw, tok):
            if cur:
                out.append(cur)
            cur = []
            continue
        start = m.start() + lead
        cur.append((tok.lower(), start, start + len(tok)))
    if cur:
        out.append(cur)
    return out


def grams(seq: list[tuple[str, int, int]], n: int = N) -> list[str]:
    return [" ".join(w for w, _, _ in seq[i:i + n]) for i in range(len(seq) - n + 1)]


def fnv1a32(s: str) -> int:
    h = 0x811C9DC5
    for b in s.encode():
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h
