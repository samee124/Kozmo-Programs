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

PROGRAMME = "salesforce-2026"

# Step 1 — Intake
intake = run_intake(
    programme_id=PROGRAMME,
    vendor_list_path=None,
    documents_path=r"D:\vendor\salesforce",
    own_company=None,
)
print(f"Intake done — confirmed={len(intake.confirmed)}  triage={len(intake.triage)}")

# Step 2 — Enrichment (only runs on CONFIRMED vendors)
results = enrich_all_confirmed(
    programme_id=PROGRAMME,
    max_vendors=30,
)
for r in results:
    print(f"{r.vendor_id:40s} -> {r.profile_status}")
