"""Name normalizer — produces (normalized_name, comparison_key) from a cleaned string."""

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Suffix tables
# ---------------------------------------------------------------------------

# Anglo default — applied when no country_code matches a specific table.
# Split into tier-1 (legal designators) and tier-2 (descriptors).
_TIER1_UNIVERSAL = [
    "incorporated", "corporation", "company",
    "limited", "inc", "corp", "co",
    "llc", "llp", "lp", "plc",
    "gmbh", "ag", "kg",
    "sarl", "sas",
    "pvt", "pty", "pte",
    "bv", "nv",
    "ltda", "fze", "fzc",
    "株式会社", "有限会社", "合同会社",
]

_TIER2_DESCRIPTORS = [
    "technologies", "technology",
    "solutions", "services",
    "systems", "consulting",
    "international", "enterprises",
    "partners", "associates",
    "worldwide", "ventures",
    "holdings", "global",
    "intl", "group",
]

# Country-specific suffix tables (all treated as tier-1 within their country).
_COUNTRY_SUFFIXES: dict[str, list[str]] = {
    "DE": ["gmbh", "kgaa", "ohg", "kg", "ug", "ag"],
    "FR": ["sarl", "eurl", "snc", "sci", "sas", "sa"],
    "IN": ["pvt ltd", "private limited", "public limited", "limited", "pvt"],
    "AU": ["pty limited", "pty ltd", "pty"],
    "SG": ["pte limited", "pte ltd", "pte"],
    "BR": ["eireli", "ltda", "sa"],
    "NL": ["vof", "cv", "bv", "nv"],
    "AE": ["fze", "fzc", "llc"],
    "JP": ["株式会社", "有限会社", "合同会社"],
    # Anglo default
    None: [
        "incorporated", "corporation", "private", "public", "company",
        "limited", "holdings", "group", "inc", "corp", "ltd", "llc",
        "llp", "lp", "plc", "pvt", "co",
    ],
}

_JP_SUFFIXES = {"株式会社", "有限会社", "合同会社"}

# Pre-compile abbreviation-dot pattern.
_ABBREV_DOT_RE = re.compile(r"\b(\w)\.")
# Punctuation to strip (everything except hyphens between word chars).
_PUNCT_RE = re.compile(r"[,.()/\\'\";:]")
# Hyphen between word characters — keep these.
_INNER_HYPHEN_RE = re.compile(r"(\w)-(\w)")


def _collapse_abbrev_dots(s: str) -> str:
    """Collapse 'I.B.M.' → 'ibm' by removing dots after single-char words."""
    prev = None
    while prev != s:
        prev = s
        s = _ABBREV_DOT_RE.sub(r"\1", s)
    return s


def _get_suffixes_for_country(country_code: Optional[str]) -> tuple[list[str], list[str]]:
    """Return (tier1_list, tier2_list) sorted longest-first."""
    if country_code is not None:
        country_specific = _COUNTRY_SUFFIXES.get(country_code)
        if country_specific is not None:
            # Country-specific entries are all tier-1; no tier-2 outside Anglo.
            return sorted(country_specific, key=len, reverse=True), []

    # Anglo default (country_code is None or not in the country table).
    tier1 = sorted(_COUNTRY_SUFFIXES[None], key=len, reverse=True)
    tier2 = sorted(_TIER2_DESCRIPTORS, key=len, reverse=True)
    return tier1, tier2


def _strip_jp_prefix(s: str) -> tuple[str, bool]:
    """Strip JP company-type designators from the START of the string."""
    stripped = False
    for jp in sorted(_JP_SUFFIXES, key=len, reverse=True):
        if s.startswith(jp):
            s = s[len(jp):].strip()
            stripped = True
            break
    return s, stripped


def _strip_suffix_once(s: str, suffix: str) -> tuple[str, bool]:
    """Try to strip `suffix` from end of `s` (word boundary, case-insensitive)."""
    # Match suffix at end, preceded by whitespace or start-of-string.
    pattern = re.compile(
        r"(?:^|\s+)" + re.escape(suffix) + r"\s*$",
        re.IGNORECASE,
    )
    m = pattern.search(s)
    if m:
        result = s[: m.start()].strip()
        return result, True
    return s, False


def _apply_suffix_strip(s: str, country_code: Optional[str]) -> str:
    """Apply the two-tier suffix stripping rule.

    Speculatively strips ALL trailing tier-1 and tier-2 tokens from the end,
    then commits only if at least one tier-1 token was in the stripped block.
    This handles "Aramark Corp Services" → "aramark" correctly even though
    "corp" (tier-1) precedes "services" (tier-2) at the end.
    """
    if not s:
        return s

    # JP: strip from START instead.
    if country_code == "JP" or any(jp in s for jp in _JP_SUFFIXES):
        s, _ = _strip_jp_prefix(s)
        return s

    tier1, tier2 = _get_suffixes_for_country(country_code)
    tier1_set = set(tier1)
    all_suffixes = sorted(set(tier1) | set(tier2), key=len, reverse=True)

    changed = True
    while changed:
        changed = False
        # Speculatively strip the entire trailing suffix block.
        temp = s
        block_had_tier1 = False
        block_changed = True
        while block_changed:
            block_changed = False
            for suffix in all_suffixes:
                candidate, hit = _strip_suffix_once(temp, suffix)
                if hit and candidate:
                    if suffix in tier1_set:
                        block_had_tier1 = True
                    temp = candidate
                    block_changed = True
                    break
        # Commit only when tier-1 was present in the block we stripped.
        if block_had_tier1 and temp != s:
            s = temp
            changed = True

    return s.strip()


def _collapse_spaces(s: str) -> str:
    """All tokens 1-3 chars → remove spaces; otherwise keep single spaces."""
    tokens = s.split()
    if not tokens:
        return s
    if all(len(t) <= 3 for t in tokens):
        return "".join(tokens)
    return " ".join(tokens)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_country_hint(cleaned: str) -> Optional[str]:
    """Infer a two-letter country code from legal suffix patterns."""
    lower = cleaned.lower().strip()

    # AE first (fze/fzc are unambiguous)
    if lower.endswith("fze") or lower.endswith("fzc"):
        return "AE"

    # AU
    if lower.endswith("pty ltd") or lower.endswith("pty limited") or lower.endswith("pty. ltd."):
        return "AU"

    # SG
    if lower.endswith("pte ltd") or lower.endswith("pte. ltd.") or lower.endswith("pte limited"):
        return "SG"

    # IN
    if (lower.endswith("pvt ltd") or lower.endswith("pvt. ltd.")
            or lower.endswith("private limited")):
        return "IN"

    # BR
    if lower.endswith("ltda"):
        return "BR"

    # NL
    if " bv" in lower or " nv" in lower or " vof" in lower:
        return "NL"

    # FR
    if lower.endswith("sarl") or lower.endswith(" sas") or lower.endswith(" sci"):
        return "FR"

    # DE
    if lower.endswith("gmbh") or lower.endswith(" ag") or lower.endswith(" kg"):
        return "DE"

    # JP
    if "株式会社" in lower or "有限会社" in lower or "合同会社" in lower:
        return "JP"

    return None


def normalize(
    cleaned: str,
    country_code: Optional[str] = None,
) -> tuple[str, str]:
    """Return (normalized_name, comparison_key) for a cleaned vendor name.

    normalized_name — step 1 (lowercase) + step 3 (suffix strip) + title-case.
    comparison_key  — all seven steps applied.
    """
    # --- normalized_name (steps 1 + 3 only, then title-case) ---
    nm = cleaned.lower()
    nm = _apply_suffix_strip(nm, country_code)
    nm = nm.rstrip(".,;: ")
    normalized_name = nm.title()

    # --- comparison_key (all 7 steps) ---
    key = cleaned.lower()                            # step 1
    key = _collapse_abbrev_dots(key)                 # step 2
    key = _apply_suffix_strip(key, country_code)     # step 3
    key = key.replace("&", "")                       # step 4
    # step 5: remove punctuation, but protect inner hyphens
    protected = _INNER_HYPHEN_RE.sub(lambda m: m.group(1) + "\x00" + m.group(2), key)
    protected = _PUNCT_RE.sub("", protected)
    key = protected.replace("\x00", "-")
    key = _collapse_spaces(key)                      # step 6
    key = key.strip()                                # step 7

    return normalized_name, key
