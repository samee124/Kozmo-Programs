"""Tests for the agent tool registries (TOOLS, run, _dispatch) and llm_tool_call."""

import json

import pytest

import cobalt.agents.analysis_agent as aa_module
import cobalt.agents.planning_agent as pa_module
import cobalt.agents.research_agent as ra_module
import cobalt.core.llm_call as llm_call_module
from cobalt.agents.analysis_agent import AnalysisAgent
from cobalt.agents.planning_agent import PlanningAgent
from cobalt.agents.research_agent import ResearchAgent
from cobalt.core.exceptions import LLMCallFailure
from cobalt.core.llm_call import ToolCallResponse, llm_tool_call


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _text_response(content: str = "done") -> ToolCallResponse:
    return ToolCallResponse(type="text", content=content)


def _tool_response(name: str, args: dict, call_id: str = "call_1") -> ToolCallResponse:
    return ToolCallResponse(type="tool_call", tool_name=name, tool_args=args, tool_call_id=call_id)


@pytest.fixture
def mock_tool_call(monkeypatch):
    """Patch _call_with_tools so tests never hit the real API."""
    responses: list[ToolCallResponse] = []

    def fake_call_with_tools(messages, tools, system, model):
        r = responses.pop(0) if responses else _text_response("fallback")
        return r, 10, 5

    monkeypatch.setattr(llm_call_module, "_call_with_tools", fake_call_with_tools)
    return responses  # caller appends ToolCallResponse objects


# ---------------------------------------------------------------------------
# ToolCallResponse dataclass
# ---------------------------------------------------------------------------

def test_tool_call_response_text_fields():
    r = ToolCallResponse(type="text", content="hello")
    assert r.type == "text"
    assert r.content == "hello"
    assert r.tool_name is None
    assert r.tool_args is None
    assert r.tool_call_id is None


def test_tool_call_response_tool_call_fields():
    r = ToolCallResponse(type="tool_call", tool_name="web_research", tool_args={"vendor_name": "IBM"}, tool_call_id="c1")
    assert r.type == "tool_call"
    assert r.tool_name == "web_research"
    assert r.tool_args == {"vendor_name": "IBM"}
    assert r.tool_call_id == "c1"


# ---------------------------------------------------------------------------
# llm_tool_call
# ---------------------------------------------------------------------------

def test_llm_tool_call_returns_text_response(mock_tool_call):
    mock_tool_call.append(_text_response("research complete"))
    result = llm_tool_call([{"role": "user", "content": "research IBM"}], [])
    assert result.type == "text"
    assert result.content == "research complete"


def test_llm_tool_call_returns_tool_call_response(mock_tool_call):
    mock_tool_call.append(_tool_response("web_research", {"vendor_name": "IBM"}))
    result = llm_tool_call([{"role": "user", "content": "research IBM"}], [])
    assert result.type == "tool_call"
    assert result.tool_name == "web_research"
    assert result.tool_args == {"vendor_name": "IBM"}


def test_llm_tool_call_raises_llm_failure_on_api_error(monkeypatch):
    import openai

    def bad_call(messages, tools, system, model):
        raise openai.APIError("boom", request=None, body=None)

    monkeypatch.setattr(llm_call_module, "_call_with_tools", bad_call)
    with pytest.raises(LLMCallFailure):
        llm_tool_call([{"role": "user", "content": "x"}], [])


def test_llm_tool_call_raises_llm_failure_on_unexpected_error(monkeypatch):
    def bad_call(messages, tools, system, model):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(llm_call_module, "_call_with_tools", bad_call)
    with pytest.raises(LLMCallFailure):
        llm_tool_call([{"role": "user", "content": "x"}], [])


# ---------------------------------------------------------------------------
# ResearchAgent — TOOLS list
# ---------------------------------------------------------------------------

def test_research_agent_tools_list_is_non_empty():
    assert len(ra_module._TOOLS) > 0


def test_research_agent_tools_has_web_research():
    names = {t["function"]["name"] for t in ra_module._TOOLS}
    assert "web_research" in names


def test_research_agent_tools_has_fetch_pdf_text():
    names = {t["function"]["name"] for t in ra_module._TOOLS}
    assert "fetch_pdf_text" in names


def test_research_agent_tools_has_fetch_google_drive_docs():
    names = {t["function"]["name"] for t in ra_module._TOOLS}
    assert "fetch_google_drive_docs" in names


# ---------------------------------------------------------------------------
# ResearchAgent — run()
# ---------------------------------------------------------------------------

def test_research_agent_run_returns_text_immediately(monkeypatch):
    monkeypatch.setattr(ra_module, "llm_tool_call", lambda *a, **kw: _text_response("raw data here"))
    agent = ResearchAgent()
    result = agent.run("research IBM")
    assert result == "raw data here"


def test_research_agent_run_dispatches_tool_then_returns_text(monkeypatch):
    calls: list[str] = []
    responses = iter([
        _tool_response("web_research", {"vendor_name": "IBM"}),
        _text_response("done"),
    ])

    def fake_tool_call(messages, tools, **kw):
        return next(responses)

    def fake_web_research(self, vendor_name, tier="STANDARD", country_hint=None):
        calls.append(vendor_name)
        return "IBM Corp founded 1911"

    monkeypatch.setattr(ra_module, "llm_tool_call", fake_tool_call)
    monkeypatch.setattr(ResearchAgent, "web_research", fake_web_research)

    agent = ResearchAgent()
    result = agent.run("research IBM")
    assert result == "done"
    assert "IBM" in calls


def test_research_agent_run_never_raises_on_llm_failure(monkeypatch):
    monkeypatch.setattr(ra_module, "llm_tool_call", lambda *a, **kw: (_ for _ in ()).throw(LLMCallFailure("fail")))
    agent = ResearchAgent()
    result = agent.run("research IBM")
    assert result == ""


def test_research_agent_run_returns_empty_after_max_iter(monkeypatch):
    monkeypatch.setattr(
        ra_module, "llm_tool_call",
        lambda *a, **kw: _tool_response("web_research", {"vendor_name": "IBM"}),
    )
    monkeypatch.setattr(ResearchAgent, "web_research", lambda self, **kw: "data")
    agent = ResearchAgent()
    result = agent.run("research IBM")
    assert result == ""


# ---------------------------------------------------------------------------
# ResearchAgent — _dispatch()
# ---------------------------------------------------------------------------

def test_research_dispatch_web_research(monkeypatch):
    monkeypatch.setattr(ResearchAgent, "web_research", lambda self, vendor_name, tier, country_hint: f"results for {vendor_name}")
    agent = ResearchAgent()
    result = agent._dispatch("web_research", {"vendor_name": "Acme"})
    assert "Acme" in result


def test_research_dispatch_fetch_pdf_text(monkeypatch):
    monkeypatch.setattr(ResearchAgent, "fetch_pdf_text", lambda self, file_path: "pdf text content")
    agent = ResearchAgent()
    result = agent._dispatch("fetch_pdf_text", {"file_path": "/tmp/doc.pdf"})
    assert result == "pdf text content"


def test_research_dispatch_fetch_google_drive_docs(monkeypatch):
    monkeypatch.setattr(ResearchAgent, "fetch_google_drive_docs", lambda self, folder_path: [{"filename": "a.pdf", "raw_text": "text"}])
    agent = ResearchAgent()
    result = agent._dispatch("fetch_google_drive_docs", {"folder_path": "/docs"})
    parsed = json.loads(result)
    assert parsed[0]["filename"] == "a.pdf"


def test_research_dispatch_unknown_tool_returns_error_string():
    agent = ResearchAgent()
    result = agent._dispatch("nonexistent_tool", {})
    assert "unknown tool" in result


def test_research_dispatch_exception_returns_error_string(monkeypatch):
    monkeypatch.setattr(ResearchAgent, "fetch_pdf_text", lambda self, file_path: (_ for _ in ()).throw(OSError("not found")))
    agent = ResearchAgent()
    result = agent._dispatch("fetch_pdf_text", {"file_path": "/missing.pdf"})
    assert "error" in result


# ---------------------------------------------------------------------------
# AnalysisAgent — TOOLS list
# ---------------------------------------------------------------------------

def test_analysis_agent_tools_list_is_non_empty():
    assert len(aa_module._TOOLS) > 0


def test_analysis_agent_tools_has_extract_vendor_name():
    names = {t["function"]["name"] for t in aa_module._TOOLS}
    assert "extract_vendor_name_from_doc" in names


def test_analysis_agent_tools_has_extract_contract_terms():
    names = {t["function"]["name"] for t in aa_module._TOOLS}
    assert "extract_contract_terms" in names


def test_analysis_agent_tools_has_structure_entity():
    names = {t["function"]["name"] for t in aa_module._TOOLS}
    assert "structure_entity" in names


# ---------------------------------------------------------------------------
# AnalysisAgent — run()
# ---------------------------------------------------------------------------

def test_analysis_agent_run_returns_text_immediately(monkeypatch):
    monkeypatch.setattr(aa_module, "llm_tool_call", lambda *a, **kw: _text_response("analysis complete"))
    agent = AnalysisAgent()
    result = agent.run("analyse this document")
    assert result == "analysis complete"


def test_analysis_agent_run_never_raises_on_failure(monkeypatch):
    monkeypatch.setattr(aa_module, "llm_tool_call", lambda *a, **kw: (_ for _ in ()).throw(LLMCallFailure("fail")))
    agent = AnalysisAgent()
    assert agent.run("analyse") == ""


# ---------------------------------------------------------------------------
# AnalysisAgent — _dispatch()
# ---------------------------------------------------------------------------

def test_analysis_dispatch_extract_vendor_name(mock_llm):
    agent = AnalysisAgent()
    result = agent._dispatch("extract_vendor_name_from_doc", {"raw_text": "IBM contract text"})
    parsed = json.loads(result)
    assert "vendor_name" in parsed


def test_analysis_dispatch_extract_contract_terms(mock_llm):
    agent = AnalysisAgent()
    result = agent._dispatch("extract_contract_terms", {"raw_text": "renewal date 2027", "vendor_name": "IBM"})
    parsed = json.loads(result)
    assert "doc_type" in parsed or "confidence" in parsed


def test_analysis_dispatch_structure_entity(mock_llm):
    agent = AnalysisAgent()
    result = agent._dispatch("structure_entity", {"vendor_name": "IBM", "raw_research_text": "IBM is a US tech company"})
    parsed = json.loads(result)
    assert "legal_entity" in parsed or "confidence" in parsed


def test_analysis_dispatch_score_confidence():
    agent = AnalysisAgent()
    result = agent._dispatch("score_confidence", {"evidence_sources": ["web", "erp"], "resolution_method": "WEB_RESEARCH"})
    score = float(result)
    assert 0.0 <= score <= 1.0


def test_analysis_dispatch_unknown_tool_returns_error_string():
    agent = AnalysisAgent()
    result = agent._dispatch("nonexistent", {})
    assert "unknown tool" in result


# ---------------------------------------------------------------------------
# PlanningAgent — TOOLS list
# ---------------------------------------------------------------------------

def test_planning_agent_tools_list_is_non_empty():
    assert len(pa_module._TOOLS) > 0


def test_planning_agent_tools_has_compute_investigation_plan():
    names = {t["function"]["name"] for t in pa_module._TOOLS}
    assert "compute_investigation_plan" in names


def test_planning_agent_tools_has_write_programme_plan():
    names = {t["function"]["name"] for t in pa_module._TOOLS}
    assert "write_programme_plan" in names


# ---------------------------------------------------------------------------
# PlanningAgent — run()
# ---------------------------------------------------------------------------

def test_planning_agent_run_returns_text_immediately(monkeypatch):
    monkeypatch.setattr(pa_module, "llm_tool_call", lambda *a, **kw: _text_response("plan written"))
    agent = PlanningAgent()
    result = agent.run("write a plan")
    assert result == "plan written"


def test_planning_agent_run_never_raises_on_failure(monkeypatch):
    monkeypatch.setattr(pa_module, "llm_tool_call", lambda *a, **kw: (_ for _ in ()).throw(LLMCallFailure("fail")))
    agent = PlanningAgent()
    assert agent.run("write a plan") == ""


# ---------------------------------------------------------------------------
# PlanningAgent — _dispatch()
# ---------------------------------------------------------------------------

def test_planning_dispatch_compute_investigation_plan():
    agent = PlanningAgent()
    result = agent._dispatch("compute_investigation_plan", {
        "vendor_name": "Acme Corp",
        "entity_type": "COMPANY",
    })
    parsed = json.loads(result)
    assert "depth" in parsed
    assert "steps" in parsed
    assert "reason" in parsed


def test_planning_dispatch_compute_plan_person_entity():
    agent = PlanningAgent()
    result = agent._dispatch("compute_investigation_plan", {
        "vendor_name": "John Smith",
        "entity_type": "PERSON",
    })
    parsed = json.loads(result)
    assert parsed["depth"] == "SKIP"


def test_planning_dispatch_write_programme_plan(mock_llm, tmp_workspace):
    agent = PlanningAgent()
    result = agent._dispatch("write_programme_plan", {
        "programme_id": "prog-dispatch-001",
        "candidate_count": 5,
        "doc_count": 2,
    })
    assert "programme_plan.md" in result


def test_planning_dispatch_unknown_tool_returns_error_string():
    agent = PlanningAgent()
    result = agent._dispatch("nonexistent", {})
    assert "unknown tool" in result


def test_planning_dispatch_exception_returns_error_string(monkeypatch):
    monkeypatch.setattr(PlanningAgent, "compute_investigation_plan", lambda self, p: (_ for _ in ()).throw(RuntimeError("boom")))
    agent = PlanningAgent()
    result = agent._dispatch("compute_investigation_plan", {"vendor_name": "X"})
    assert "error" in result
