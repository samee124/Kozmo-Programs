"""Workspace filesystem primitives for Cobalt.

Provides path helpers, directory initialisation, and file read/write utilities.
These are low-level functions — they do NOT go through atomic_write() because
they operate on directories and simple metadata files, not agent-managed files.

WORKSPACE_ROOT is the local filesystem root (V1).
In V2 (Azure), the same relative paths are used as blob paths under
{UserId}/{ProgrammeId}/ in the Azure workspace container.
"""

import json
import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

load_dotenv()

WORKSPACE_ROOT: Path = Path(os.getenv("WORKSPACE_ROOT", "./workspace"))


# ─── Path helpers ─────────────────────────────────────────────────────────────

def vendor_path(programme_id: str, vendor_id: str) -> Path:
    """Return the root workspace path for a single vendor."""
    return Path(WORKSPACE_ROOT) / programme_id / vendor_id


def programme_path(programme_id: str) -> Path:
    """Return the root workspace path for a programme."""
    return Path(WORKSPACE_ROOT) / programme_id


def entity_path(programme_id: str, vendor_id: str) -> Path:
    """Return the vendor workspace .md file path (single-file arch), or identity/entity.md fallback."""
    found = _find_vendor_file(programme_id, vendor_id)
    if found is not None:
        return found
    return vendor_path(programme_id, vendor_id) / "identity" / "entity.md"


def coverage_path(programme_id: str, vendor_id: str) -> Path:
    """Return the path to coverage.md for a vendor."""
    return vendor_path(programme_id, vendor_id) / "cost_file" / "coverage.md"


def vendor_profile_path(programme_id: str, vendor_id: str) -> Path:
    """Return the path to vendor_profile.md for a vendor (P2)."""
    return vendor_path(programme_id, vendor_id) / "profile" / "vendor_profile.md"


def rs_profile_path(programme_id: str, vendor_id: str) -> Path:
    """Return the path to relationship_spend_profile.md for a vendor (P3)."""
    return vendor_path(programme_id, vendor_id) / "profile" / "relationship_spend_profile.md"


def ledger_path(programme_id: str, vendor_id: str) -> Path:
    """Return the path to the append-only action ledger for a vendor."""
    return vendor_path(programme_id, vendor_id) / "execution" / "ledger.md"


def connectors_path(programme_id: str, vendor_id: str) -> Path:
    """Return the directory for P3 ERP connector stub JSON files."""
    return vendor_path(programme_id, vendor_id) / "connectors"


def programme_run_path(programme_id: str) -> Path:
    """Return the path to the programme_run directory."""
    return programme_path(programme_id) / "programme_run"


def workflow_path(programme_id: str, workflow_id: str) -> Path:
    """Return the directory for a specific workflow's JSON files (V2)."""
    return programme_path(programme_id) / "workflows" / workflow_id


# ─── Directory initialisation ─────────────────────────────────────────────────

def init_vendor_workspace(programme_id: str, vendor_id: str) -> None:
    """Create the vendor workspace root directory (single-file architecture)."""
    vendor_path(programme_id, vendor_id).mkdir(parents=True, exist_ok=True)


def init_programme_workspace(programme_id: str) -> None:
    """Create all required subdirectories under the programme workspace root."""
    root = programme_path(programme_id)
    for subdir in ("programme_run", "programme_run/intake_plans", "workflows", "checkpoint"):
        (root / subdir).mkdir(parents=True, exist_ok=True)


# ─── File read helpers ────────────────────────────────────────────────────────

def _parse_frontmatter(content: str) -> dict | None:
    """Return parsed YAML from a front-matter block (---…---), or None."""
    parts = content.split("---\n", 2)
    if len(parts) >= 3:
        return yaml.safe_load(parts[1]) or None
    return yaml.safe_load(content) or None


def read_md(path: str | Path) -> Optional[dict]:
    """Read and parse the YAML front-matter from a .md file.

    Returns:
        Parsed dict, or None if the file is missing or unparseable.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        return _parse_frontmatter(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def md_exists(path: str | Path) -> bool:
    """Return True if the given file exists."""
    return Path(path).exists()


# ─── File write helper (low-level, bypasses atomic_write) ────────────────────

def write_md(path: str | Path, data: dict) -> None:
    """Write a dict as YAML front-matter + markdown table to path.

    This is a low-level primitive and does NOT go through atomic_write().
    Use atomic_write() for all agent-managed workspace files.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
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
    content = f"---\n{fm}---\n\n# {title}\n\n| Field | Value |\n| --- | --- |\n{rows}"
    path.write_text(content, encoding="utf-8")


def _find_vendor_file(
    programme_id: str,
    vendor_id: str,
    workspace_root: "Path | None" = None,
) -> "Path | None":
    """Find the single *.md vendor file in the vendor workspace root directory.

    Returns the first .md file found directly in the vendor root, or None.
    """
    root = (Path(workspace_root) if workspace_root else WORKSPACE_ROOT) / programme_id / vendor_id
    if not root.is_dir():
        return None
    md_files = [f for f in root.iterdir() if f.suffix == ".md" and f.is_file()]
    return md_files[0] if md_files else None
