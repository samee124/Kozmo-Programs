"""Workspace filesystem primitives for Cobalt.

Provides path helpers and directory initialisation.  These are low-level
functions — they do NOT go through atomic_write() because they operate on
directories and simple metadata files, not on agent-managed workspace files.
"""

import json
import os
from pathlib import Path
from typing import Optional

import yaml

WORKSPACE_ROOT: Path = Path(os.getenv("WORKSPACE_ROOT", "./workspace"))


def vendor_path(programme_id: str, vendor_id: str) -> Path:
    """Return the root workspace path for a single vendor."""
    return Path(WORKSPACE_ROOT) / programme_id / vendor_id


def programme_path(programme_id: str) -> Path:
    """Return the root workspace path for a programme."""
    return Path(WORKSPACE_ROOT) / programme_id


def init_vendor_workspace(programme_id: str, vendor_id: str) -> None:
    """Create all required subdirectories under the vendor workspace root."""
    root = vendor_path(programme_id, vendor_id)
    subdirs = [
        "identity",
        "cost_file",
        "evidence",
        "inference",
        "evaluation",
        "execution",
        "execution/campaigns",
        "intake",
        "communication",
        "communication/checkins",
        "connectors",
        "connectors/logs",
    ]
    for subdir in subdirs:
        (root / subdir).mkdir(parents=True, exist_ok=True)


def init_programme_workspace(programme_id: str) -> None:
    """Create all required subdirectories under the programme workspace root."""
    root = programme_path(programme_id)
    subdirs = [
        "programme_run",
        "programme_run/intake_plans",
        "checkpoint",
    ]
    for subdir in subdirs:
        (root / subdir).mkdir(parents=True, exist_ok=True)


def _parse_frontmatter(content: str) -> dict | None:
    """Return parsed YAML from a front-matter block (---…---), or None."""
    parts = content.split("---\n", 2)
    if len(parts) >= 3:
        return yaml.safe_load(parts[1]) or None
    return yaml.safe_load(content) or None


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
