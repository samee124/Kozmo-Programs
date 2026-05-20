"""Fuzzy vendor name matching utilities for Process 3.

Extracts and centralises the Jaro-Winkler matching logic. Shared by
structured_data_collector (spreadsheet row matching) and document_intelligence
(candidate deduplication).

Also used by external_source_collector (replaces its private _strip_suffixes
and _strip_corporate_suffixes functions).
"""

from __future__ import annotations

import logging
import re
import unicodedata

from jellyfish import jaro_winkler_similarity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Legal suffixes stripped by normalise_for_match (recursive)
# ---------------------------------------------------------------------------

_LEGAL_SUFFIXES: tuple[str, ...] = (
    "ltd", "limited", "inc", "incorporated", "corp", "corporation",
    "llc", "llp", "plc", "gmbh", "sa", "sas", "srl", "bv", "nv",
    "co", "and co", "& co", "holdings", "group", "international",
    "worldwide", "global", "services",
)

# Punctuation to remove (keep hyphens between words)
_PUNCT_RE = re.compile(r"[^\w\s-]")
_MULTI_SPACE_RE = re.compile(r"\s+")


def normalise_for_match(name: str) -> str:
    """Prepare a vendor name for fuzzy matching.

    Steps:
    1. Lowercase
    2. Remove punctuation (except hyphens between word characters)
    3. Recursively strip legal suffixes
    4. Collapse whitespace
    """
    if not name:
        return ""

    # Lowercase + unicode normalise
    n = unicodedata.normalize("NFKD", name.lower())

    # Remove punctuation except hyphens between word characters
    n = _PUNCT_RE.sub(" ", n)
    n = _MULTI_SPACE_RE.sub(" ", n).strip()

    # Recursively strip legal suffixes
    changed = True
    while changed:
        changed = False
        tokens = n.split()
        for suffix in _LEGAL_SUFFIXES:
            suffix_tokens = suffix.split()
            if len(tokens) >= len(suffix_tokens) and tokens[-len(suffix_tokens):] == suffix_tokens:
                tokens = tokens[: -len(suffix_tokens)]
                n = " ".join(tokens).strip()
                changed = True
                break

    return n.strip()


def fuzzy_match(name_a: str, name_b: str) -> float:
    """Jaro-Winkler similarity between two vendor name strings.

    Returns 0.0–1.0. Case-insensitive. Both inputs are normalised before
    comparison.
    """
    a = normalise_for_match(name_a)
    b = normalise_for_match(name_b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return float(jaro_winkler_similarity(a, b))


def best_match(
    query: str,
    candidates: list[str],
    threshold: float = 0.85,
) -> tuple[str, float] | None:
    """Find the best matching candidate for the query string.

    Both query and candidates are normalised before comparison. Returns the
    original (un-normalised) candidate string with its score.

    Returns:
        (original_candidate, score) tuple if best score >= threshold, else None.
        When multiple candidates tie, the earliest in the list is returned.
        Returns None if candidates is empty.
    """
    if not candidates:
        return None

    best_candidate: str | None = None
    best_score: float = -1.0

    for candidate in candidates:
        score = fuzzy_match(query, candidate)
        if score > best_score:
            best_score = score
            best_candidate = candidate

    if best_score >= threshold and best_candidate is not None:
        logger.info("fuzzy_score: '%s' -> '%s' (%.2f)", query[:40], best_candidate[:40], best_score)
        return (best_candidate, best_score)
    logger.info("fuzzy_score: no match for '%s' above %.2f", query[:40], threshold)
    return None
