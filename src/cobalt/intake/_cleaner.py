"""Structural cleaner — strips formatting noise before normalization."""

import re

_TRIGGER_KEYWORDS = [
    "statement of work",
    "service agreement",
    "master agreement",
    "order form",
    "amendment",
    "addendum",
    "schedule",
    "exhibit",
    "annex",
    "attachment",
    "sow",
    "nda",
    "baa",
]

# Pre-compiled for the reference-number pattern.
# Token must start with a letter, contain at least one digit, consist only of
# alphanumeric/hyphen chars, and be followed by a space: "PO-99812 ", "INV-001 ".
_REF_NUMBER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9\-]*\d[A-Za-z0-9\-]*\s+")


def structural_clean(raw: str) -> str:
    """Remove structural noise from a raw vendor name string.

    Steps applied in order:
    1. Strip surrounding whitespace.
    2. Strip surrounding quotes (single or double).
    3. Extract vendor name from document-title patterns.
    4. Strip leading reference numbers (PO-99812, INV-001 …).
    5. Strip trailing punctuation (, . ; :).
    6. Strip whitespace again.
    7. If result is empty return original raw unchanged.
    """
    s = raw.strip()

    # Step 2 — strip surrounding quotes
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()

    # Step 3 — extract from document title patterns
    lower = s.lower()
    for kw in _TRIGGER_KEYWORDS:
        if lower.startswith(kw):
            # Find the last occurrence of " – " or " - "
            last_em = s.rfind(" – ")   # en-dash
            last_hyp = s.rfind(" - ")
            pos = max(last_em, last_hyp)
            if pos != -1:
                sep_len = 3  # " X " is always 3 chars
                s = s[pos + sep_len:]
            break

    # Step 4 — strip leading reference numbers
    s = _REF_NUMBER_RE.sub("", s)

    # Step 5 — strip trailing punctuation
    s = s.rstrip(",.;:")

    # Step 6 — strip whitespace
    s = s.strip()

    # Step 7 — guard against empty result
    if not s:
        return raw

    return s
