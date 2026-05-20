"""Atomic file-write primitives for Cobalt.

All workspace file writes MUST go through atomic_write() or append_md().
Direct open().write() calls anywhere in the codebase are forbidden.

Storage modes (controlled by environment):
  Local (V1):  AZURE_STORAGE_CONNECTION_STRING not set → tmp.replace(path)
  Azure (V2):  AZURE_STORAGE_CONNECTION_STRING set → blob upload + local write

In both modes, sync_to_db() is called explicitly after the write completes.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

import yaml
from dotenv import load_dotenv

from cobalt.core.exceptions import (
    FileOwnershipViolation,
    LedgerWriteError,
    SchemaValidationError,
)

load_dotenv()

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


def _call_sync_to_db(path: Path, vendor_id: str | None, programme_id: str | None) -> None:
    """Call sync_to_db() if vendor_id and programme_id are provided.

    Failures are logged as warnings — never re-raised.
    DB is projection only; a sync failure must not block the write.
    """
    if not vendor_id or not programme_id:
        return
    try:
        from cobalt.db.sync_to_db import sync_to_db
        sync_to_db(path, vendor_id, programme_id)
    except Exception as exc:
        logger.warning("sync_to_db failed for %s: %s", path, exc)


def _upload_to_blob(
    path: Path,
    serialised: str,
    vendor_id: str | None,
    programme_id: str | None,
    user_id: str | None,
) -> None:
    """Upload the written file to Azure Blob Storage if configured.

    Called after the local write succeeds. Failures are warnings only.

    The blob_path is derived from the workspace path by stripping the
    WORKSPACE_ROOT prefix and prepending user_id.
    """
    try:
        from cobalt.core.blob_storage import (
            APPEND_ONLY_BLOBS,
            _get_workspace_container,
            append_to_blob,
            is_configured,
            upload_blob,
            workspace_blob_path,
        )

        if not is_configured():
            return

        if not user_id or not programme_id:
            logger.debug("_upload_to_blob: user_id or programme_id not provided — skipping blob upload for %s", path)
            return

        import os
        workspace_root = os.getenv("WORKSPACE_ROOT", "./workspace")
        abs_workspace = Path(workspace_root).resolve()
        try:
            relative = path.resolve().relative_to(abs_workspace)
        except ValueError:
            logger.debug("_upload_to_blob: %s is outside WORKSPACE_ROOT — skipping blob upload", path)
            return

        # relative is like: PROG-001/v-V-ABCD-001/identity/entity.md
        # blob path should be: User001/PROG-001/v-V-ABCD-001/identity/entity.md
        blob_path = f"{user_id}/{relative.as_posix()}"
        container = _get_workspace_container()

        if path.name in APPEND_ONLY_BLOBS:
            append_to_blob(container, blob_path, serialised)
        else:
            upload_blob(container, blob_path, serialised, overwrite=True)

    except Exception as exc:
        logger.warning("_upload_to_blob: failed for %s: %s", path, exc)


def atomic_write(
    path: str | Path,
    content: dict | str,
    schema_validator: Callable[[dict | str], None] | None = None,
    vendor_id: str | None = None,
    programme_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Write content to path atomically.

    Steps:
      1. Serialise content (dict → YAML string).
      2. Check immutability for entity.md.
      3. Write to <path>.tmp.
      4. Run schema_validator (raises SchemaValidationError on failure).
      5. tmp.replace(path)  — atomic swap, Windows-safe.
      6. Upload to Azure Blob if AZURE_STORAGE_CONNECTION_STRING is set.
      7. Call sync_to_db() (warning only on failure).

    Args:
        path:             Destination file path.
        content:          dict (serialised as YAML) or str (written as-is).
        schema_validator: Optional callable that validates content.
        vendor_id:        Passed to sync_to_db and blob upload.
        programme_id:     Passed to sync_to_db and blob upload.
        user_id:          Passed to blob upload for path construction.
                          Falls back to COBALT_USER_ID env var if not provided.

    Raises:
        FileOwnershipViolation: input_name change attempted on entity.md.
        SchemaValidationError:  schema_validator rejected content.
    """
    if user_id is None:
        user_id = os.getenv("COBALT_USER_ID")
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

    # Retry replace on Windows PermissionError (antivirus briefly locks .tmp files)
    for _attempt in range(4):
        try:
            tmp_path.replace(path)
            break
        except PermissionError:
            if _attempt == 3:
                tmp_path.unlink(missing_ok=True)
                raise
            time.sleep(0.05 * (2 ** _attempt))  # 50ms, 100ms, 200ms

    _upload_to_blob(path, serialised, vendor_id, programme_id, user_id)
    _call_sync_to_db(path, vendor_id, programme_id)


def append_md(
    path: str | Path,
    entry: str,
    vendor_id: str | None = None,
    programme_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Append a Markdown entry to a file, separated by a --- divider.

    Used exclusively for append-only ledger files. Any OSError raises
    LedgerWriteError and the caller must HALT (Rule 4).

    In Azure mode, uses append_block() on an Append Blob. Locally uses
    standard file append.

    Args:
        path:         Path to the ledger file.
        entry:        Markdown string to append.
        vendor_id:    Passed to sync_to_db.
        programme_id: Passed to sync_to_db.
        user_id:      Passed to blob append. Falls back to COBALT_USER_ID env var.

    Raises:
        LedgerWriteError: Any OS-level write failure.
    """
    if user_id is None:
        user_id = os.getenv("COBALT_USER_ID")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(path, "a", encoding="utf-8") as fh:
            if path.exists() and path.stat().st_size > 0:
                fh.write("\n---\n\n")
            fh.write(entry)
    except OSError as exc:
        raise LedgerWriteError(
            f"Failed to append to ledger {path}: {exc}"
        ) from exc

    _upload_to_blob(path, entry, vendor_id, programme_id, user_id)
    _call_sync_to_db(path, vendor_id, programme_id)
