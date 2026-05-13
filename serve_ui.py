"""
Start the Cobalt UI server.

Usage:  python serve_ui.py
Then open:  http://localhost:8000         → Programme dashboard
            http://localhost:8000/vendor?prog=csd&id=salesforce  → Vendor workspace
            http://localhost:8000/docs    → API docs (auto-generated)
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from dotenv import load_dotenv
load_dotenv()

import uvicorn

if __name__ == "__main__":
    print("All Programmes  ->  http://localhost:8000")
    print("Programme view  ->  http://localhost:8000/programme?prog=csd")
    print("Vendor view     ->  http://localhost:8000/vendor?prog=csd&id=carestream-dental")
    print("API docs        ->  http://localhost:8000/docs")
    uvicorn.run(
        "cobalt.api.server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=["src", "ui"],
    )
