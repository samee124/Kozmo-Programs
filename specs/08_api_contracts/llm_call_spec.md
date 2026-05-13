# llm_call Specification

## Location
src/Cobalt/core/llm_call.py

## Model
claude-sonnet-4-6 for ALL calls. Never change without updating spec.

## Signatures
def llm_call(prompt, system, expect_json=True, model="claude-sonnet-4-6") -> dict | str
def llm_call_with_metadata(prompt, system, expect_json=True) -> tuple[dict|str, int, int]

## Rules
- temperature=0 always. Never a parameter.
- JSON: strip markdown fences before parse
- Retry: tenacity 3 attempts, exp backoff 1s-8s
- Raises LLMCallFailure after max retries
- Log tokens + duration at DEBUG level
- Module-level singleton client

## Implementation
from anthropic import Anthropic
client.messages.create(model=model, max_tokens=2000, temperature=0, ...)
