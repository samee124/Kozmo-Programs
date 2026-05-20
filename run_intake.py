"""CLI runner for the Cobalt pipeline.

All values are read from environment variables or passed as CLI args.
For the interactive web UI use run_server.py instead.

Usage:
    python run_intake.py \
        --programme  Sales-Q3-2026 \
        --vendor-list C:/path/to/vendors.xlsx \
        --documents  C:/path/to/docs \
        --own-company "Kozmo Programs"

Env vars (override defaults):
    COBALT_PROGRAMME_ID   — programme id
    COBALT_VENDOR_LIST    — path to vendor list CSV/XLSX
    COBALT_DOCUMENTS_PATH — path to documents folder
    COBALT_OWN_COMPANY    — your company name
    COBALT_USER_ID        — user id (default: user001)
"""

import sys
import os
import argparse
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)-50s  %(message)s",
    datefmt="%H:%M:%S",
)

from cobalt.orchestrator.intake_orchestrator import run_intake
from cobalt.orchestrator.enrichment_orchestrator import enrich_all_confirmed
from cobalt.orchestrator.rs_orchestrator import run_rs_all_confirmed
from cobalt.db.queries import insert_user, insert_programme

# ── Parse args (CLI overrides env, env overrides nothing) ─────────────────────
parser = argparse.ArgumentParser(description="Cobalt pipeline CLI runner")
parser.add_argument("--programme",    default=os.getenv("COBALT_PROGRAMME_ID"),  help="Programme ID")
parser.add_argument("--vendor-list",  default=os.getenv("COBALT_VENDOR_LIST"),   help="Path to vendor list CSV/XLSX")
parser.add_argument("--documents",    default=os.getenv("COBALT_DOCUMENTS_PATH", ""), help="Path to documents folder")
parser.add_argument("--own-company",  default=os.getenv("COBALT_OWN_COMPANY", "Kozmo Programs"), help="Your company name")
args = parser.parse_args()

PROGRAMME      = args.programme
VENDOR_LIST    = args.vendor_list
DOCUMENTS_PATH = args.documents
OWN_COMPANY    = args.own_company
USER_ID        = os.getenv("COBALT_USER_ID", "user001")

if not PROGRAMME:
    parser.error("--programme is required (or set COBALT_PROGRAMME_ID env var)")
if not VENDOR_LIST:
    parser.error("--vendor-list is required (or set COBALT_VENDOR_LIST env var)")

# ── DB bootstrap (idempotent — safe to re-run) ────────────────────────────────
insert_user(
    user_id=USER_ID,
    user_name="Kozmo Admin",
    email="admin@kozmoprograms.com",
    subscription_tier="PROFESSIONAL",
)
insert_programme(
    programme_id=PROGRAMME,
    user_id=USER_ID,
    programme_name=PROGRAMME,
)

print(f"\nProgramme : {PROGRAMME}")
print(f"User      : {USER_ID}")
print(f"Vendors   : {VENDOR_LIST}")
print(f"Docs      : {DOCUMENTS_PATH or '(none)'}")
print(f"Company   : {OWN_COMPANY}\n")

# ── Step 1 — Intake ───────────────────────────────────────────────────────────
intake = run_intake(
    programme_id=PROGRAMME,
    vendor_list_path=VENDOR_LIST,
    documents_path=DOCUMENTS_PATH,
    own_company=OWN_COMPANY,
)
print(f"Intake done — confirmed={len(intake.confirmed)}  triage={len(intake.triage)}")

# ── Step 2 — Enrichment ───────────────────────────────────────────────────────
results = enrich_all_confirmed(
    programme_id=PROGRAMME,
    max_vendors=200,
)
for r in results:
    print(f"{r.vendor_id:40s} -> {r.profile_status}")

# ── Step 3 — RS Pipeline ──────────────────────────────────────────────────────
rs_results = run_rs_all_confirmed(
    programme_id=PROGRAMME,
    checkin_data={"currency": "USD"},
)
for r in rs_results:
    print(f"{r.vendor_id:40s} -> RS {r.status}  {r.skip_reason or r.error or ''}")
