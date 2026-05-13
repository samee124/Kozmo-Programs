"""Atomic file-write primitives for Cobalt.

All workspace file writes MUST go through atomic_write() or append_md().
Direct open().write() calls anywhere in the codebase are forbidden.
"""

import json
import logging
from pathlib import Path
from typing import Callable

import yaml

from cobalt.core.exceptions import (
    FileOwnershipViolation,
    LedgerWriteError,
    SchemaValidationError,
)

logger = logging.getLogger(__name__)

# Files whose fields must never be mutated after creation.
IMMUTABLE_FILENAMES: frozenset[str] = frozenset({"entity.md"})


def _parse_frontmatter(content: str) -> dict | None:
    """Return parsed YAML from a front-matter block (---…---), or None."""
    parts = content.split("---\n", 2)
    if len(parts) >= 3:
        return yaml.safe_load(parts[1]) or None
    return yaml.safe_load(content) or None


def _dict_to_md(path: Path, data: dict) -> str:
    """Serialise a dict to YAML front-matter + a human-readable markdown table."""
    fm = yaml.dump(data, default_flow_style=False, allow_unicode=True)
    title = path.stem.replace("_", " ").replace("-", " ").title()
    rows = ""
    for k, v in data.items():
        if v is None:
            cell = ""
        elif isinstance(v, (dict, list)):
            cell = json.dumps(v, ensure_ascii=False)
        else:
            cell = str(v)
        rows += f"| `{k}` | {cell} |\n"
    body = f"# {title}\n\n| Field | Value |\n| --- | --- |\n{rows}"
    return f"---\n{fm}---\n\n{body}"


def _read_existing(path: Path) -> dict | None:
    """Return parsed front-matter dict from an existing .md file, or None."""
    if not path.exists():
        return None
    try:
        return _parse_frontmatter(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _check_immutability(path: Path, new_content: dict) -> None:
    """Raise FileOwnershipViolation if input_name would change on entity.md."""
    if path.name not in IMMUTABLE_FILENAMES:
        return
    existing = _read_existing(path)
    if existing is None:
        return
    existing_name = existing.get("input_name")
    new_name = new_content.get("input_name")
    if existing_name is not None and existing_name != new_name:
        raise FileOwnershipViolation(
            f"Attempt to change immutable field 'input_name' in {path}: "
            f"{existing_name!r} → {new_name!r}"
        )


def _sync_to_db(path: Path, vendor_id: str | None, programme_id: str | None) -> None:
    """Placeholder — real implementation wired in when DB layer exists."""
    try:
        # DB sync is a no-op in V1 until the DB layer is built.
        pass
    except Exception as exc:
        logger.warning("sync_to_db failed for %s: %s", path, exc)


def atomic_write(
    path: str | Path,
    content: dict | str,
    schema_validator: Callable[[dict | str], None] | None = None,
    vendor_id: str | None = None,
    programme_id: str | None = None,
) -> None:
    """Write content to path atomically.

    Steps:
    1. If content is dict, serialise to YAML string.
    2. Check immutability for IMMUTABLE_FILENAMES.
    3. Write to <path>.tmp.
    4. Run schema_validator on content (raise SchemaValidationError on failure).
    5. tmp.replace(path)  — Windows-safe atomic swap.
    6. Call sync_to_db (warning only on failure).

    Args:
        path: Destination file path.
        content: dict (serialised as YAML) or str (written as-is).
        schema_validator: Optional callable that validates content; raises on failure.
        vendor_id: Passed through to sync_to_db.
        programme_id: Passed through to sync_to_db.

    Raises:
        FileOwnershipViolation: input_name change attempted on entity.md.
        SchemaValidationError: schema_validator rejected content.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(content, dict):
        _check_immutability(path, content)
        serialised = _dict_to_md(path, content)
    else:
        serialised = content

    tmp_path = path.with_suffix(path.suffix + ".tmp")

    tmp_path.write_text(serialised, encoding="utf-8")

    if schema_validator is not None:
        try:
            schema_validator(content)
        except SchemaValidationError:
            tmp_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise SchemaValidationError(str(exc)) from exc

    tmp_path.replace(path)

    _sync_to_db(path, vendor_id, programme_id)


def append_md(
    path: str | Path,
    entry: str,
    vendor_id: str | None = None,
    programme_id: str | None = None,
) -> None:
    """Append a Markdown entry to a file, separated by a --- divider.

    Used for append-only ledger files. Any OSError raises LedgerWriteError
    and the caller must HALT.

    Args:
        path: Path to the ledger file.
        entry: Markdown string to append.
        vendor_id: Passed through to sync_to_db.
        programme_id: Passed through to sync_to_db.

    Raises:
        LedgerWriteError: Any OS-level write failure.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(path, "a", encoding="utf-8") as fh:
            if path.stat().st_size > 0 if path.exists() else False:
                fh.write("\n---\n\n")
            fh.write(entry)
    except OSError as exc:
        raise LedgerWriteError(
            f"Failed to append to ledger {path}: {exc}"
        ) from exc

    _sync_to_db(path, vendor_id, programme_id)
