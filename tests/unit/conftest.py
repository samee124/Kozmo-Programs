"""Unit-test fixtures — no network access in unit tests."""

import pytest


@pytest.fixture(autouse=True)
def _no_ddg(monkeypatch):
    """Disable DuckDuckGo so unit tests never make real network calls."""
    import cobalt.agents.research_agent as ra_module
    monkeypatch.setattr(ra_module, "DDGS", None)
