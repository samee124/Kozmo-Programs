import sys
import os
import logging

# Make 'cobalt' importable without pip install
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# Load .env before anything else
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)-50s  %(message)s",
    datefmt="%H:%M:%S",
)

from cobalt.orchestrator.enrichment_orchestrator import enrich_all_confirmed

results = enrich_all_confirmed(
    programme_id="nova-2026",
    max_vendors=5,        # start small
    declared_depth="STANDARD",
)
for r in results:
    print(f"{r.vendor_id:40s} → {r.status:12s} profile={r.profile_status or '—'}")


