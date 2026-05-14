"""Integration tests — full intake pipeline, two end-to-end scenarios."""

import csv
import hashlib
from pathlib import Path

import pytest

from cobalt.orchestrator.intake_orchestrator import IntakeRunResult, run_intake


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ibm_vendor_id(programme_id: str) -> str:
    return "ibm"


def _salesforce_vendor_id(programme_id: str) -> str:
    return "salesforce"


def _blackboard_vendor_id(programme_id: str) -> str:
    return "blackboard"


def _dell_vendor_id(programme_id: str) -> str:
    return "dell"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_csv(tmp_path):
    """20-row vendor CSV — 4 rows dedup down to 16 unique candidates.

    Dedup pairs (same comparison_key after clean + normalize):
      IBM  +  I.B.M.                           ->  ibm
      Aramark  +  ARAMARK CORP  +  Amendment 3 – Aramark  ->  aramark
      Infosys Ltd  +  Infosys Limited           ->  infosys
    Structural cleaner:
      Service Agreement – Dell  ->  key  dell  (known_vendor)
      Amendment 3 – Aramark    ->  key  aramark (merges with Aramark)
    Alias map:
      Azure  ->  microsoft  (single-word; entity_type COMPANY)
    """
    rows = [
        ("IBM",                       "480000", "IT"),
        ("I.B.M.",                    "",       ""),
        ("Salesforce",                "840000", "IT"),
        ("Aramark",                   "",       ""),
        ("ARAMARK CORP",              "",       ""),
        ("Microsoft",                 "",       ""),
        ("Blackboard Inc",            "",       ""),
        ("AWS",                       "",       ""),
        ("Azure",                     "",       ""),   # alias_map: azure -> microsoft
        ("Service Agreement – Dell", "",   ""),   # en-dash; cleaner strips to Dell
        ("John Smith",                "",       ""),
        ("HR Department",             "",       ""),
        ("Amendment 3 – Aramark","",       ""),   # en-dash; cleaner strips to Aramark
        ("Infosys Ltd",               "",       ""),
        ("Infosys Limited",           "",       ""),
        ("Redpath Corp",              "",       ""),
        ("Vertex Inc",                "",       ""),
        ("Praxis Ltd",                "80000",  "IT"),
        ("Solaro Group Inc",          "",       ""),
        ("Brentfield Corp",           "",       ""),
    ]
    p = tmp_path / "vendors.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["vendor_name", "spend", "category"])
        writer.writerows(rows)
    return str(p)


@pytest.fixture
def mock_web_research(monkeypatch):
    """Patch ResearchAgent.web_research to return a non-empty stub (no network calls)."""
    from cobalt.agents.research_agent import ResearchAgent

    def _stub(self, vendor_name, tier="STANDARD", country_hint=None):
        if tier == "SKIP":
            return ""
        return f"{vendor_name} is an established commercial vendor."

    monkeypatch.setattr(ResearchAgent, "web_research", _stub)


@pytest.fixture
def mock_drive_docs(monkeypatch):
    """Patch fetch_google_drive_docs to return two controlled doc dicts (no PDF I/O)."""
    from cobalt.agents.research_agent import ResearchAgent

    docs = [
        {
            "filename":      "ibm_msa.pdf",
            "file_path":     "/fake/drive/ibm_msa.pdf",
            "relative_path": "ibm_msa.pdf",
            "raw_text":      "This Master Service Agreement is entered into by IBM.",
        },
        {
            "filename":      "salesforce_msa.pdf",
            "file_path":     "/fake/drive/salesforce_msa.pdf",
            "relative_path": "salesforce_msa.pdf",
            "raw_text":      "This agreement is between Acme Corp and Salesforce Inc.",
        },
    ]
    monkeypatch.setattr(
        ResearchAgent, "fetch_google_drive_docs", lambda self, path: docs
    )
    return "/fake/drive"


@pytest.fixture
def mock_analysis_llm(monkeypatch):
    """Patch AnalysisAgent extraction methods directly for Scenario 2."""
    from cobalt.agents.analysis_agent import AnalysisAgent

    def _fake_extract_vendor_name(self, raw_text, filename="", own_company=None):
        fn = filename.lower()
        if "ibm" in fn or "IBM" in raw_text:
            return {"vendor_name": "IBM", "legal_name": "IBM", "doc_type_hint": "MSA", "confidence": 0.90}
        if "salesforce" in fn or "Salesforce" in raw_text:
            return {"vendor_name": "Salesforce", "legal_name": "Salesforce Inc", "doc_type_hint": "MSA", "confidence": 0.90}
        return {"vendor_name": None, "legal_name": None, "doc_type_hint": "UNKNOWN", "confidence": 0.0}

    def _fake_extract_contract_terms(self, raw_text, vendor_name=""):
        return {
            "doc_type": "MSA",
            "counterparty_name": vendor_name or "Vendor",
            "renewal_date": "2026-12-31",
            "opt_out_deadline": "2026-09-30",
            "auto_renewal": True,
            "contract_value": 500000,
            "contract_value_type": "CEILING",
            "price_escalation": False,
            "escalation_rate_max": None,
            "payment_terms": "Net 30",
            "early_termination": "PERMITTED",
            "liability_cap": 1000000,
            "baa_present": False,
            "phi_scope": None,
            "nda_active": True,
            "nda_expiry": None,
            "sla_uptime_pct": 99.9,
            "confidence": 0.85,
        }

    monkeypatch.setattr(AnalysisAgent, "extract_vendor_name_from_doc", _fake_extract_vendor_name)
    monkeypatch.setattr(AnalysisAgent, "extract_contract_terms", _fake_extract_contract_terms)


# ---------------------------------------------------------------------------
# Scenario 1 — CSV-only intake (no documents)
# ---------------------------------------------------------------------------

class TestScenario1CsvOnly:
    """Full pipeline with vendor list only.

    Input:  20 CSV rows
    After dedup: 16 unique candidates
    Expected: 1 discarded (HR Department INTERNAL). "John Smith" without a title is
    now treated as COMPANY and investigated — only explicit titles (Mr/Mrs/Dr) trigger
    PERSON classification.
    """

    @pytest.fixture(autouse=True)
    def _run(self, sample_csv, mock_web_research, mock_llm, tmp_workspace):
        self.result = run_intake("prog-s1", vendor_list_path=sample_csv)
        self.workspace_root = tmp_workspace

    def test_run_completes_without_error(self):
        assert isinstance(self.result, IntakeRunResult)
        assert self.result.programme_id == "prog-s1"

    def test_known_vendors_confirmed(self):
        # IBM, Salesforce, Aramark, Microsoft, Blackboard Inc (rebrand), AWS (alias),
        # Dell Technologies (alias), Service Agreement – Dell → "dell" (known_vendor) = 8 brain-known
        brain_known = [d for d in self.result.confirmed if d.confidence >= 0.90]
        assert len(brain_known) >= 8

    def test_internal_discarded(self):
        # HR Department contains "department" → INTERNAL → DISCARDED
        # "John Smith" (no title) is now COMPANY, not PERSON → investigated, not discarded
        assert len(self.result.discarded) == 1

    def test_dedup_reduces_input_count(self):
        # 20 CSV rows deduplicate to 16 unique candidates
        assert self.result.summary["total_input"] == 16

    def test_workspace_files_created_for_confirmed_vendor(self):
        ibm_vid = _ibm_vendor_id("prog-s1")
        ibm = next((d for d in self.result.confirmed if d.vendor_id == ibm_vid), None)
        assert ibm is not None
        assert ibm.workspace_path is not None
        ws = Path(ibm.workspace_path)
        # Single-file architecture: one *.md in vendor root
        md_files = list(ws.glob("*.md"))
        assert md_files, f"No vendor *.md file found in {ws}"

    def test_contract_not_found_without_docs(self):
        # No documents supplied → legal.renewal_date has INSUF confidence
        ibm_vid = _ibm_vendor_id("prog-s1")
        ibm = next((d for d in self.result.confirmed if d.vendor_id == ibm_vid), None)
        assert ibm is not None and ibm.workspace_path is not None
        ws = Path(ibm.workspace_path)
        md_files = list(ws.glob("*.md"))
        assert md_files
        vendor_text = md_files[0].read_text(encoding="utf-8")
        assert "INSUF" in vendor_text

    def test_pcs_zero_without_erp_or_docs(self):
        # No ERP connector, no extracted contract terms → PCS = 0 for all confirmed
        for decision in self.result.confirmed:
            assert decision.initial_pcs == 0

    def test_programme_files_written(self):
        from cobalt.core.file_system import programme_path
        run_dir = programme_path("prog-s1") / "programme_run"
        assert (run_dir / "vendor_register.md").exists()
        assert (run_dir / "deduplication_report.md").exists()
        assert (run_dir / "triage_queue.md").exists()
        assert (run_dir / "run_log.md").exists()

    def test_summary_counts_add_up(self):
        s = self.result.summary
        total = s["confirmed"] + s["triage"] + s["discarded"] + s["blocked"]
        assert total == s["total_input"]
        assert s["confirmed"] == len(self.result.confirmed)
        assert s["discarded"] == len(self.result.discarded)
        assert s["total_input"] == 16


# ---------------------------------------------------------------------------
# Scenario 2 — CSV + synthetic Google Drive documents
# ---------------------------------------------------------------------------

class TestScenario2CsvPlusDocs:
    """Full pipeline with vendor list AND two synthetic MSA documents.

    IBM and Salesforce get linked documents → extracted_terms populated.
    """

    @pytest.fixture(autouse=True)
    def _run(self, sample_csv, mock_drive_docs, mock_web_research,
             mock_analysis_llm, mock_llm, tmp_workspace):
        self.result = run_intake(
            "prog-s2",
            vendor_list_path=sample_csv,
            documents_path=mock_drive_docs,
        )
        self.workspace_root = tmp_workspace

    def test_run_completes_with_docs(self):
        assert isinstance(self.result, IntakeRunResult)
        assert len(self.result.confirmed) > 0

    def test_document_backed_vendor_has_observed_contract(self):
        # IBM is linked to ibm_msa.pdf → extract_contract_terms called → legal section has data
        ibm_vid = _ibm_vendor_id("prog-s2")
        ibm = next((d for d in self.result.confirmed if d.vendor_id == ibm_vid), None)
        assert ibm is not None and ibm.workspace_path is not None
        ws = Path(ibm.workspace_path)
        md_files = list(ws.glob("*.md"))
        assert md_files
        vendor_text = md_files[0].read_text(encoding="utf-8")
        # The legal section should have at least one contract field populated
        assert "CONTRACT" in vendor_text

    def test_evidence_file_created_for_linked_doc(self):
        # Contract documents stored inline in commercial.documents
        ibm_doc_id = hashlib.md5("/fake/drive/ibm_msa.pdf".encode()).hexdigest()[:12]
        ibm_vid = _ibm_vendor_id("prog-s2")
        ibm = next((d for d in self.result.confirmed if d.vendor_id == ibm_vid), None)
        assert ibm is not None and ibm.workspace_path is not None
        ws = Path(ibm.workspace_path)
        md_files = list(ws.glob("*.md"))
        assert md_files
        vendor_text = md_files[0].read_text(encoding="utf-8")
        assert ibm_doc_id in vendor_text

    def test_ip_files_written_for_unknown_vendors(self):
        # Infosys, Redpath, Vertex, Praxis, Solaro, Brentfield all follow INVESTIGATE path
        # → write_investigation_plan creates IP-*.md files in intake_plans/
        from cobalt.core.file_system import programme_path
        intake_plans = programme_path("prog-s2") / "programme_run" / "intake_plans"
        ip_files = list(intake_plans.glob("IP-*.md"))
        assert len(ip_files) >= 6

    def test_pcs_elevated_for_vendor_with_contract_terms(self):
        # IBM has extracted_terms with renewal_date (+15), auto_renewal (+10),
        # contract_value (+8), price_escalation False (+5) → PCS = 38
        ibm_vid = _ibm_vendor_id("prog-s2")
        ibm = next((d for d in self.result.confirmed if d.vendor_id == ibm_vid), None)
        assert ibm is not None
        assert ibm.initial_pcs > 0


# ---------------------------------------------------------------------------
# Smoke tests — specific pipeline behaviours
# ---------------------------------------------------------------------------

def test_rebrand_resolved(sample_csv, mock_web_research, mock_llm, tmp_workspace):
    """Blackboard Inc → rebrand_map → AUTO_CONFIRM → CONFIRMED with confidence 0.95."""
    result = run_intake("prog-rb", vendor_list_path=sample_csv)
    blackboard_vid = _blackboard_vendor_id("prog-rb")
    blackboard = next((d for d in result.confirmed if d.vendor_id == blackboard_vid), None)
    assert blackboard is not None
    assert blackboard.confidence == pytest.approx(0.95, abs=1e-9)


def test_document_title_cleaned(sample_csv, mock_web_research, mock_llm, tmp_workspace):
    """'Service Agreement – Dell' → structural_clean → 'Dell' → known_vendor → CONFIRMED."""
    result = run_intake("prog-dc", vendor_list_path=sample_csv)
    # comparison_key for "Service Agreement – Dell" after cleaning is "dell"
    dell_vid = _dell_vendor_id("prog-dc")
    dell = next((d for d in result.confirmed if d.vendor_id == dell_vid), None)
    assert dell is not None
    assert dell.confidence == pytest.approx(0.99, abs=1e-9)
