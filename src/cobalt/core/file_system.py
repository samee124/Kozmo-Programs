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
    """Return the path to entity.md for a vendor.

    Multi-file arch (identity/entity.md) takes priority; single-file arch
    fallback searches the vendor root for any .md file.
    """
    standard = vendor_path(programme_id, vendor_id) / "identity" / "entity.md"
    if standard.exists():
        return standard
    found = _find_vendor_file(programme_id, vendor_id)
    if found is not None:
        return found
    return standard


def coverage_path(programme_id: str, vendor_id: str) -> Path:
    """Return the path to coverage.md for a vendor."""
    return vendor_path(programme_id, vendor_id) / "cost_file" / "coverage.md"


def vendor_profile_path(programme_id: str, vendor_id: str) -> Path:
    """Return the consolidated profile path (P1/P2/P3 merged). P2 data is under enrichment: key."""
    found = _find_vendor_file(programme_id, vendor_id)
    return found if found is not None else vendor_path(programme_id, vendor_id) / f"{vendor_id}_profile.md"


def rs_profile_path(programme_id: str, vendor_id: str) -> Path:
    """Return the consolidated profile path (P1/P2/P3 merged). P3 data is under relationship: key."""
    found = _find_vendor_file(programme_id, vendor_id)
    return found if found is not None else vendor_path(programme_id, vendor_id) / f"{vendor_id}_profile.md"


def investigation_plan_path(programme_id: str, vendor_id: str) -> Path:
    """Return the path to plans/investigation_plan.md for a vendor (P1)."""
    return vendor_path(programme_id, vendor_id) / "plans" / "investigation_plan.md"


def enrichment_plan_path(programme_id: str, vendor_id: str) -> Path:
    """Return the path to plans/enrichment_plan.md for a vendor (P2)."""
    return vendor_path(programme_id, vendor_id) / "plans" / "enrichment_plan.md"


def rs_plan_path(programme_id: str, vendor_id: str) -> Path:
    """Return the path to plans/rs_plan.md for a vendor (P3)."""
    return vendor_path(programme_id, vendor_id) / "plans" / "rs_plan.md"


def campaign_plan_path(programme_id: str, vendor_id: str) -> Path:
    """Return the path to plans/campaign_plan.md for a vendor (VW Agent Level 3)."""
    return vendor_path(programme_id, vendor_id) / "plans" / "campaign_plan.md"


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
    """Find the consolidated *_profile.md vendor file in the vendor workspace root directory.

    Search order:
      1. {vendor_id}_profile.md  (new consolidated format)
      2. {vendor_id}.md          (old single-file format, backward compat)
      3. Any *_profile.md        (new format with slug-derived name)
      4. Any *.md skipping known non-intake files (old format, slug-derived)
    Returns None if the directory does not exist or no matching file found.
    """
    root = (Path(workspace_root) if workspace_root else WORKSPACE_ROOT) / programme_id / vendor_id
    if not root.is_dir():
        return None
    _new = root / f"{vendor_id}_profile.md"
    if _new.is_file():
        return _new
    _direct = root / f"{vendor_id}.md"
    if _direct.is_file():
        return _direct
    profile_files = [
        f for f in root.iterdir()
        if f.suffix == ".md" and f.is_file() and f.name.endswith("_profile.md")
    ]
    if profile_files:
        return profile_files[0]
    _skip = {"action_plan.md", "analysis_result.md", "execution_state.md", "task_list.md"}
    md_files = [f for f in root.iterdir() if f.suffix == ".md" and f.is_file() and f.name not in _skip]
    return md_files[0] if md_files else None
