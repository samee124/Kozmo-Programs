"""Brain data loader — reads curated vendor knowledge from the Brain directory."""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_cache: "BrainData | None" = None


@dataclass
class KnownVendor:
    canonical_name: str
    confidence: float
    country_code: str | None
    category: str | None


@dataclass
class BrainData:
    known_vendors:   dict[str, KnownVendor]
    rebrand_map:     dict[str, str]
    alias_map:       dict[str, str]
    acquisition_map: dict[str, dict] = field(default_factory=dict)
    brand_map:       dict[str, str]  = field(default_factory=dict)


def load_brain() -> BrainData:
    """Load Brain data, returning the cached instance on subsequent calls.

    Raises:
        FileNotFoundError: If any of the three required JSON files are missing.
    """
    global _cache
    if _cache is not None:
        return _cache

    root = Path(os.getenv("BRAIN_ROOT", "./Brain"))

    known_path = root / "known_vendors.json"
    rebrand_path = root / "rebrand_map.json"
    alias_path = root / "alias_map.json"

    for p in (known_path, rebrand_path, alias_path):
        if not p.exists():
            raise FileNotFoundError(f"Brain file not found: {p}")

    with open(known_path, encoding="utf-8") as fh:
        raw_vendors: dict = json.load(fh)

    known_vendors: dict[str, KnownVendor] = {
        key: KnownVendor(
            canonical_name=v["canonical_name"],
            confidence=v["confidence"],
            country_code=v.get("country_code"),
            category=v.get("category"),
        )
        for key, v in raw_vendors.items()
    }

    with open(rebrand_path, encoding="utf-8") as fh:
        rebrand_map: dict[str, str] = json.load(fh)

    with open(alias_path, encoding="utf-8") as fh:
        alias_map: dict[str, str] = json.load(fh)

    acquisition_path = root / "acquisition_map.json"
    if acquisition_path.exists():
        with open(acquisition_path, encoding="utf-8") as fh:
            acquisition_map: dict[str, dict] = json.load(fh)
    else:
        logger.warning("Brain file not found (optional): %s — acquisition_map will be empty", acquisition_path)
        acquisition_map = {}

    brand_path = root / "brand_map.json"
    if brand_path.exists():
        with open(brand_path, encoding="utf-8") as fh:
            brand_map: dict[str, str] = json.load(fh)
    else:
        logger.warning("Brain file not found (optional): %s — brand_map will be empty", brand_path)
        brand_map = {}

    _cache = BrainData(
        known_vendors=known_vendors,
        rebrand_map=rebrand_map,
        alias_map=alias_map,
        acquisition_map=acquisition_map,
        brand_map=brand_map,
    )
    return _cache


def invalidate_cache() -> None:
    """Clear the module-level Brain cache. For use in tests only."""
    global _cache
    _cache = None
