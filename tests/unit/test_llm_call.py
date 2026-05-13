"""Tests for cobalt.core.llm_call."""

import pytest
from unittest.mock import MagicMock

import openai

import cobalt.core.llm_call as llm_call_module
from cobalt.core.llm_call import llm_call, llm_call_with_metadata
from cobalt.core.exceptions import LLMCallFailure


def _make_openai_response(content: str, prompt_tokens: int = 10, completion_tokens: int = 5) -> MagicMock:
    """Build a minimal mock that looks like an openai ChatCompletion response."""
    response = MagicMock()
    response.choices[0].message.content = content
    response.usage.prompt_tokens = prompt_tokens
    response.usage.completion_tokens = completion_tokens
    return response


@pytest.fixture
def mock_openai_client(monkeypatch):
    """Patch cobalt.core.llm_call.client so tests never hit the real OpenAI API."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_openai_response('{"result": "ok"}')
    monkeypatch.setattr(llm_call_module, "client", mock_client)
    return mock_client


def test_llm_call_returns_dict_when_expect_json(mock_openai_client):
    result = llm_call("prompt", "system", expect_json=True)
    assert isinstance(result, dict)
    assert result == {"result": "ok"}


def test_llm_call_returns_str_when_not_expect_json(mock_openai_client):
    mock_openai_client.chat.completions.create.return_value = _make_openai_response("plain text")
    result = llm_call("prompt", "system", expect_json=False)
    assert isinstance(result, str)
    assert result == "plain text"


def test_markdown_fences_stripped_before_json_parse(mock_openai_client):
    fenced = '```json\n{"key": "value"}\n```'
    mock_openai_client.chat.completions.create.return_value = _make_openai_response(fenced)
    result = llm_call("prompt", "system", expect_json=True)
    assert result == {"key": "value"}


def test_markdown_fences_no_language_stripped(mock_openai_client):
    fenced = '```\n{"key": "value"}\n```'
    mock_openai_client.chat.completions.create.return_value = _make_openai_response(fenced)
    result = llm_call("prompt", "system", expect_json=True)
    assert result == {"key": "value"}


def test_json_decode_error_raises_llm_call_failure(mock_openai_client):
    mock_openai_client.chat.completions.create.return_value = _make_openai_response("not json {{")
    with pytest.raises(LLMCallFailure):
        llm_call("prompt", "system", expect_json=True)


def test_api_exception_retries_then_raises_llm_call_failure(mock_openai_client):
    call_count = {"n": 0}

    def failing_create(**kwargs):
        call_count["n"] += 1
        raise openai.APIError("boom", request=MagicMock(), body=None)

    mock_openai_client.chat.completions.create.side_effect = failing_create
    with pytest.raises(LLMCallFailure):
        llm_call("prompt", "system")
    assert call_count["n"] == 3


def test_llm_call_with_metadata_returns_tuple(mock_openai_client):
    result, prompt_tokens, completion_tokens = llm_call_with_metadata(
        "prompt", "system", expect_json=True
    )
    assert isinstance(result, dict)
    assert isinstance(prompt_tokens, int)
    assert isinstance(completion_tokens, int)
    assert prompt_tokens == 10
    assert completion_tokens == 5
