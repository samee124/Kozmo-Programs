"""Shared pytest fixtures for all Cobalt tests."""

import os
import pytest

import cobalt.core.llm_call as llm_call_module


@pytest.fixture
def mock_llm(monkeypatch):
    """Patch llm_call._call so tests never hit the real Anthropic API.

    Returns the monkeypatch fixture so individual tests can further customise
    the fake response by calling monkeypatch.setattr again.
    """
    def fake_call(prompt: str, system: str, model: str):
        return '{"result": "ok"}', 10, 5

    monkeypatch.setattr(llm_call_module, "_call", fake_call)
    return monkeypatch


@pytest.fixture
def tmp_workspace(tmp_path, monkeypatch):
    """Set WORKSPACE_ROOT to a temporary directory for full isolation."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    import cobalt.core.file_system as fs_module
    monkeypatch.setattr(fs_module, "WORKSPACE_ROOT", tmp_path)

    return tmp_path
