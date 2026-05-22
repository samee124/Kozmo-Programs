import sys
import os
import shutil
import logging
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from dotenv import load_dotenv
load_dotenv()

# ── Configuration — edit these before running ─────────────────────────────────
PROGRAMME_ID   = "nova-2026-adobe"
USER_ID        = os.getenv("COBALT_USER_ID", "user001")
OWN_COMPANY    = "Kozmo Programs"

VENDOR_LIST    = r"C:\Users\Samee.a\Downloads\Vendor_list_30 (2).xlsx"
DOCUMENTS_PATH = r"C:\Users\Samee.a\Downloads\adobe-doc"

UPLOADED_FILES = [
    {
        "path": r"C:\Users\Samee.a\Downloads\adobe-doc\FY25 Adobe Enterprise Renewal.pdf",
        "filename": "FY25 Adobe Enterprise Renewal.pdf",
    }
]
# ─────────────────────────────────────────────────────────────────────────────

from cobalt.core.blob_storage import is_configured as _azure_configured
if not _azure_configured():
    print("ERROR: AZURE_STORAGE_CONNECTION_STRING is not set in .env")
    print("       All workspace files would be lost — aborting.")
    sys.exit(1)

# Use a temp directory as the local workspace — it is wiped after the run.
# All files are mirrored to Azure Blob Storage during the run.
_tmp = tempfile.mkdtemp(prefix="cobalt_run_")
os.environ["WORKSPACE_ROOT"] = _tmp
os.environ["COBALT_USER_ID"]  = USER_ID

import cobalt.core.file_system as _fs
_fs.WORKSPACE_ROOT = Path(_tmp)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)-50s  %(message)s",
    datefmt="%H:%M:%S",
)

from cobalt.orchestrator.intake_orchestrator import run_intake
from cobalt.orchestrator.enrichment_orchestrator import enrich_all_confirmed
from cobalt.orchestrator.rs_orchestrator import run_rs_all_confirmed
from cobalt.db.queries import insert_user, insert_programme

try:
    insert_user(
        user_id=USER_ID,
        user_name="Kozmo Admin",
        email="admin@kozmoprograms.com",
        subscription_tier="PROFESSIONAL",
    )
    insert_programme(
        programme_id=PROGRAMME_ID,
        user_id=USER_ID,
        programme_name="Nova 2026 Vendor Programme",
    )

    # ── Step 1: ID — Vendor Identification ───────────────────────────────────
    print("\n" + "=" * 60)
    print("STEP 1 — ID (Vendor Identification)")
    print("=" * 60)
    intake = run_intake(
        programme_id=PROGRAMME_ID,
        vendor_list_path=VENDOR_LIST,
        documents_path=DOCUMENTS_PATH,
        own_company=OWN_COMPANY,
    )
    print(f"confirmed={len(intake.confirmed)}  triage={len(intake.triage)}  discarded={len(intake.discarded)}")
    for c in intake.confirmed:
        name = getattr(c, "canonical_name", None) or getattr(c, "raw_input", c.vendor_id)
        print(f"  CONFIRMED  {c.vendor_id:30s}  {name}")
    for t in intake.triage:
        name = getattr(t, "canonical_name", None) or getattr(t, "raw_input", t.vendor_id)
        print(f"  TRIAGE     {t.vendor_id:30s}  {name}")

    # ── Step 2: DE — Data Enrichment ─────────────────────────────────────────
    print("\n" + "=" * 60)
    print("STEP 2 — DE (Data Enrichment)")
    print("=" * 60)
    enrich_results = enrich_all_confirmed(programme_id=PROGRAMME_ID, max_vendors=50)
    for r in enrich_results:
        pb = f"{r.pcs_before:.2f}" if r.pcs_before is not None else "None"
        pa = f"{r.pcs_after:.2f}"  if r.pcs_after  is not None else "None"
        print(f"  {r.vendor_id:30s}  {r.status:12s}  PCS {pb} -> {pa}")
        if r.error:
            print(f"    ERROR: {r.error}")

    # ── Step 3: RS — Relationship & Spend ────────────────────────────────────
    print("\n" + "=" * 60)
    print("STEP 3 — RS (Relationship & Spend)")
    print("=" * 60)
    rs_results = run_rs_all_confirmed(
        programme_id=PROGRAMME_ID,
        uploaded_files=UPLOADED_FILES or None,
    )
    for r in rs_results:
        skip_or_err = r.skip_reason or r.error or ""
        pb = f"{r.pcs_before:.2f}" if r.pcs_before is not None else "None"
        pa = f"{r.pcs_after:.2f}"  if r.pcs_after  is not None else "None"
        print(f"  {r.vendor_id:30s}  {r.status:12s}  PCS {pb} -> {pa}  {skip_or_err}")
        if r.flags_raised:
            print(f"    flags: {r.flags_raised}")

    print("\n" + "=" * 60)
    print("RUN COMPLETE — files uploaded to Azure, local temp cleared")
    print("=" * 60)

finally:
    shutil.rmtree(_tmp, ignore_errors=True)
