"""Single entry-point for all LLM calls in Cobalt.

Every agent and pipeline MUST call llm_call(), llm_call_with_metadata(), or
llm_tool_call(). Direct use of the OpenAI client anywhere else is forbidden.
"""

import json
import logging
import os
import time
from dataclasses import dataclass

import openai
from openai import OpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from cobalt.core.exceptions import LLMCallFailure

logger = logging.getLogger(__name__)

# Model is fixed. Never change without updating this comment.
_MODEL = "gpt-4o"

# Module-level singleton — created once, reused for every call.
# Falls back to a placeholder so the module imports cleanly in test environments
# where OPENAI_API_KEY is not set; real API calls require a valid key.
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY") or "placeholder-set-OPENAI_API_KEY")


@dataclass
class ToolCallResponse:
    """Result of one LLM turn: a tool call or a text completion."""
    type: str  # "tool_call" | "text"
    content: str | None = None
    tool_name: str | None = None
    tool_args: dict | None = None
    tool_call_id: str | None = None


def _strip_fences(text: str) -> str:
    """Remove leading/trailing ```json or ``` fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # drop opening fence line and closing fence line
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        return "\n".join(inner).strip()
    return text


@retry(
    retry=retry_if_exception_type(openai.APIError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def _call(prompt: str, system: str, model: str) -> tuple[str, int, int]:
    """Execute a single API call and return (content, prompt_tokens, completion_tokens)."""
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=2000,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content
    prompt_tokens = response.usage.prompt_tokens
    completion_tokens = response.usage.completion_tokens
    return content, prompt_tokens, completion_tokens


def llm_call(
    prompt: str,
    system: str,
    expect_json: bool = True,
) -> dict | str:
    """Call gpt-4o and return a parsed dict or raw string.

    Raises:
        LLMCallFailure: After 3 failed retries, or if JSON parsing fails.
    """
    result, _, _ = llm_call_with_metadata(prompt, system, expect_json=expect_json)
    return result


def llm_call_with_metadata(
    prompt: str,
    system: str,
    expect_json: bool = True,
) -> tuple[dict | str, int, int]:
    """Call gpt-4o and return (result, prompt_tokens, completion_tokens).

    Raises:
        LLMCallFailure: After 3 failed retries, or if JSON parsing fails.
    """
    t0 = time.monotonic()
    try:
        raw, prompt_tokens, completion_tokens = _call(prompt, system, _MODEL)
    except openai.APIError as exc:
        raise LLMCallFailure(f"LLM API call failed after retries: {exc}") from exc
    except Exception as exc:
        raise LLMCallFailure(f"Unexpected error during LLM call: {exc}") from exc

    duration_ms = int((time.monotonic() - t0) * 1000)
    logger.debug(
        "llm_call completed model=%s prompt_tokens=%d completion_tokens=%d duration_ms=%d",
        _MODEL,
        prompt_tokens,
        completion_tokens,
        duration_ms,
    )

    if not expect_json:
        return raw, prompt_tokens, completion_tokens

    cleaned = _strip_fences(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMCallFailure(
            f"LLM response could not be decoded as JSON: {exc}\nRaw: {raw!r}"
        ) from exc

    return parsed, prompt_tokens, completion_tokens


@retry(
    retry=retry_if_exception_type(openai.APIError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def _call_with_tools(
    messages: list[dict],
    tools: list[dict],
    system: str | None,
    model: str,
) -> tuple[ToolCallResponse, int, int]:
    """Execute a single API call with tool definitions. Returns (ToolCallResponse, prompt_tokens, completion_tokens)."""
    full_messages: list[dict] = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    response = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=2000,
        messages=full_messages,
        tools=tools,
        tool_choice="auto",
    )

    msg = response.choices[0].message
    prompt_tokens = response.usage.prompt_tokens
    completion_tokens = response.usage.completion_tokens

    if response.choices[0].finish_reason == "tool_calls" and msg.tool_calls:
        tc = msg.tool_calls[0]
        result = ToolCallResponse(
            type="tool_call",
            tool_name=tc.function.name,
            tool_args=json.loads(tc.function.arguments),
            tool_call_id=tc.id,
        )
    else:
        result = ToolCallResponse(type="text", content=msg.content or "")

    return result, prompt_tokens, completion_tokens


def llm_tool_call(
    messages: list[dict],
    tools: list[dict],
    system: str | None = None,
    model: str | None = None,
) -> ToolCallResponse:
    """Call gpt-4o with tool definitions; return a ToolCallResponse.

    Raises:
        LLMCallFailure: After 3 failed retries.
    """
    _model = model or _MODEL
    try:
        result, _, _ = _call_with_tools(messages, tools, system, _model)
        return result
    except openai.APIError as exc:
        raise LLMCallFailure(f"LLM tool call failed after retries: {exc}") from exc
    except Exception as exc:
        raise LLMCallFailure(f"Unexpected error during LLM tool call: {exc}") from exc
