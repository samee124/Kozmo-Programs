import sys
import os
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

PROGRAMME      = "nova-2026"
USER_ID        = os.getenv("COBALT_USER_ID", "user001")
DOCUMENTS_PATH = "C:\\Users\\Admin\\Downloads\\sales-force-program"

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
    programme_name="Nova 2026 Vendor Programme",
)

# Step 1 — Intake
intake = run_intake(
    programme_id="Sales-02",
    vendor_list_path=r"C:\Users\Admin\Downloads\Vendor_list_30.xlsx",
    documents_path=DOCUMENTS_PATH,
    own_company="Kozmo Programs",
)
print(f"Intake done — confirmed={len(intake.confirmed)}  triage={len(intake.triage)}")

# Step 2 — Enrichment (only runs on CONFIRMED vendors)
results = enrich_all_confirmed(
    programme_id="Sales-02",
    max_vendors=30,
)
for r in results:
    print(f"{r.vendor_id:40s} -> {r.profile_status}")

# Step 3 — RS Pipeline (Process 3 — Relationship & Spend)
rs_results = run_rs_all_confirmed(
    programme_id="Sales-02",
    checkin_data={"spend_ytd": "50000", "currency": "USD"},
)
for r in rs_results:
    print(f"{r.vendor_id:40s} -> RS {r.status}  {r.skip_reason or r.error or ''}")
