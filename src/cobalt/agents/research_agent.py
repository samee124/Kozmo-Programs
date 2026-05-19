"""Research Agent — collects raw evidence only. Returns raw data. Never interprets."""

import hashlib
import json
import logging
import os
import shelve
from pathlib import Path
from typing import Any

from cobalt.core.exceptions import BraveSearchError
from cobalt.core.llm_call import ToolCallResponse, llm_tool_call
from cobalt.core.search import search_with_content
from cobalt.models.schemas.signal_profile_schema import ApSignal, ErpSignal

logger = logging.getLogger(__name__)

try:
    from firecrawl import FirecrawlApp
except ImportError:
    FirecrawlApp = None  # type: ignore[assignment, misc]

try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None  # type: ignore[assignment, misc]

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None  # type: ignore[assignment, misc]

_DEFAULT_CACHE_DIR = "runtime/cache/search_cache"

_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "web_research",
            "description": "Search the web for raw information about a vendor. Returns raw text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor_name": {"type": "string"},
                    "tier": {"type": "string", "enum": ["FAST", "STANDARD", "DEEP"]},
                    "country_hint": {"type": "string"},
                },
                "required": ["vendor_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_pdf_text",
            "description": "Read a local PDF file and return its raw text content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_google_drive_docs",
            "description": "List PDFs in a folder and return their raw text as a JSON list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder_path": {"type": "string"},
                },
                "required": ["folder_path"],
            },
        },
    },
]

_SYSTEM = (
    "You are the Research Agent for the Cobalt vendor intelligence platform. "
    "Collect raw evidence only — do not interpret or extract structure. "
    "Use the available tools to gather the information requested. "
    "When you have collected sufficient raw data, return it as a text response."
)

_MAX_ITER = 8


class ResearchAgent:

    # ------------------------------------------------------------------
    # web_research — cascades: Brave → Firecrawl → DuckDuckGo
    # ------------------------------------------------------------------

    def web_research(
        self,
        vendor_name: str,
        tier: str = "STANDARD",
        country_hint: str | None = None,
    ) -> str:
        """Search the web for vendor information. Tries Brave, Firecrawl, DuckDuckGo in order."""
        if tier == "SKIP":
            return ""

        if not os.getenv("BRAVE_API_KEY"):
            return ""

        cache_dir = os.getenv("SEARCH_CACHE_DIR", _DEFAULT_CACHE_DIR)
        Path(cache_dir).mkdir(parents=True, exist_ok=True)

        count_by_tier = {"FAST": 3, "STANDARD": 5, "DEEP": 10}
        count = count_by_tier.get(tier, 5)

        if tier == "FAST":
            queries = [f"{vendor_name} company"]
        elif tier == "STANDARD":
            queries = [f"{vendor_name} company {country_hint or ''}".strip()]
        elif tier == "DEEP":
            q1 = f"{vendor_name} company {country_hint or ''}".strip()
            q2 = f"{vendor_name} official website"
            queries = [q1, q2]
        else:
            return ""

        logger.info("    web_research  vendor=%r  tier=%s  queries=%d", vendor_name, tier, len(queries))

        parts: list[str] = []
        for query in queries:
            text = (
                self._brave_search(query, count)
                or self._firecrawl_search(query, cache_dir)
            )
            if text:
                parts.append(text)

        result = "\n\n".join(parts)
        logger.info("    web_research done — %d chars", len(result))
        return result

    # ------------------------------------------------------------------
    # Provider implementations
    # ------------------------------------------------------------------

    def _cache_get(self, prefix: str, query: str, cache_dir: str) -> str | None:
        key = hashlib.md5(f"{prefix}:{query}".encode()).hexdigest()
        cache_path = str(Path(cache_dir) / "cache")
        try:
            with shelve.open(cache_path) as db:
                return db.get(key)
        except Exception:
            return None

    def _cache_set(self, prefix: str, query: str, cache_dir: str, text: str) -> None:
        key = hashlib.md5(f"{prefix}:{query}".encode()).hexdigest()
        cache_path = str(Path(cache_dir) / "cache")
        try:
            with shelve.open(cache_path) as db:
                db[key] = text
        except Exception:
            pass

    def _brave_search(self, query: str, count: int = 5) -> str:
        """Search via Brave Search API + page content fetch. Returns concatenated text."""
        try:
            results = search_with_content(query, count=count, fetch_content=True)
        except BraveSearchError as exc:
            logger.warning("    [Brave] failed for %r: %s", query[:60], exc)
            return ""

        if not results:
            return ""

        parts = []
        for r in results:
            full = r.get("full_content") or r.get("content") or ""
            if full:
                parts.append(full)
        return "\n".join(parts)

    def _firecrawl_search(self, query: str, cache_dir: str) -> str:
        """Search via Firecrawl. Returns empty string if not configured or on failure."""
        api_key = os.getenv("FIRECRAWL_API_KEY")
        if not api_key or FirecrawlApp is None:
            return ""
        cached = self._cache_get("fc", query, cache_dir)
        if cached is not None:
            return cached
        try:
            app = FirecrawlApp(api_key=api_key)
            response = app.search(query, limit=5)
            items = response if isinstance(response, list) else getattr(response, "data", [])
            text = "\n".join(
                item.get("description", "") or item.get("content", "") or item.get("markdown", "")
                for item in items
                if isinstance(item, dict)
            )
            self._cache_set("fc", query, cache_dir, text)
            logger.info("    [Firecrawl] %d chars for %r", len(text), query[:60])
            return text
        except Exception as exc:
            logger.warning("    [Firecrawl] failed for %r: %s", query[:60], exc)
            return ""

    def _ddg_search(self, query: str, cache_dir: str) -> str:
        """Search via DuckDuckGo (free, no API key). Returns empty string on failure."""
        if DDGS is None:
            return ""
        cached = self._cache_get("ddg", query, cache_dir)
        if cached is not None:
            return cached
        try:
            results = list(DDGS().text(query, max_results=5))
            text = "\n".join(r.get("body", "") for r in results if r.get("body"))
            self._cache_set("ddg", query, cache_dir, text)
            logger.info("    [DuckDuckGo] %d chars for %r", len(text), query[:60])
            return text
        except Exception as exc:
            logger.warning("    [DuckDuckGo] failed for %r: %s", query[:60], exc)
            return ""

    # ------------------------------------------------------------------
    # erp_batch_scan
    # ------------------------------------------------------------------

    def erp_batch_scan(self, all_keys: list[str]) -> dict[str, ErpSignal]:
        """Batch ERP scan for all comparison keys. Empty if not configured."""
        endpoint = os.getenv("ERP_ENDPOINT")
        if not endpoint:
            logger.info("ERP connector not configured — returning empty")
            return {}
        try:
            # V1 stub — real connector is V2
            return {}
        except Exception as exc:
            logger.warning("erp_batch_scan failed: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # fetch_pdf_text
    # ------------------------------------------------------------------

    def fetch_pdf_text(self, file_path: str) -> str:
        """Read a local PDF and return all page text joined by newlines."""
        if PdfReader is None:
            return ""
        try:
            reader = PdfReader(file_path)
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:
            logger.warning("fetch_pdf_text failed for %r: %s", file_path, exc)
            return ""

    # ------------------------------------------------------------------
    # fetch_google_drive_docs
    # ------------------------------------------------------------------

    def fetch_google_drive_docs(self, folder_path: str) -> list[dict]:
        """Walk folder_path recursively and return raw text for every PDF found."""
        try:
            root = Path(folder_path)
            if not root.exists():
                logger.warning("fetch_google_drive_docs: path does not exist: %r", folder_path)
                return []

            docs: list[dict] = []
            for p in sorted(root.rglob("*.pdf")):
                rel = str(p.relative_to(root))
                docs.append({
                    "file_path":     str(p),
                    "filename":      p.name,
                    "relative_path": rel,
                    "raw_text":      self.fetch_pdf_text(str(p)),
                })
                logger.info("    [Drive] %s", rel)

            logger.info("fetch_google_drive_docs: %d docs from %r", len(docs), folder_path)
            return docs

        except Exception as exc:
            logger.warning("fetch_google_drive_docs failed for %r: %s", folder_path, exc)
            return []

    # ------------------------------------------------------------------
    # run — open-ended agentic loop
    # ------------------------------------------------------------------

    def run(self, task: str, context: str = "") -> str:
        """Execute an open-ended research task via LLM tool loop. Never raises."""
        content = f"{task}\n\nContext:\n{context}" if context else task
        messages: list[dict] = [{"role": "user", "content": content}]

        for _ in range(_MAX_ITER):
            try:
                response = llm_tool_call(messages, _TOOLS, system=_SYSTEM)
            except Exception as exc:
                logger.warning("ResearchAgent.run: llm_tool_call failed: %s", exc)
                return ""

            if response.type == "text":
                return response.content or ""

            result_text = self._dispatch(response.tool_name or "", response.tool_args or {})
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": response.tool_call_id or "call_0",
                    "type": "function",
                    "function": {
                        "name": response.tool_name or "",
                        "arguments": json.dumps(response.tool_args or {}),
                    },
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": response.tool_call_id or "call_0",
                "content": result_text,
            })

        return ""

    def _dispatch(self, name: str, args: dict) -> str:
        """Route a tool call by name. Returns a string result."""
        try:
            if name == "web_research":
                return self.web_research(
                    vendor_name=args["vendor_name"],
                    tier=args.get("tier", "STANDARD"),
                    country_hint=args.get("country_hint"),
                )
            if name == "fetch_pdf_text":
                return self.fetch_pdf_text(args["file_path"])
            if name == "fetch_google_drive_docs":
                docs = self.fetch_google_drive_docs(args["folder_path"])
                return json.dumps(docs)
            logger.warning("ResearchAgent._dispatch: unknown tool %r", name)
            return f"unknown tool: {name}"
        except Exception as exc:
            logger.warning("ResearchAgent._dispatch failed for %r: %s", name, exc)
            return f"error: {exc}"

    # ------------------------------------------------------------------
    # ap_batch_scan
    # ------------------------------------------------------------------

    def ap_batch_scan(self, all_keys: list[str]) -> dict[str, ApSignal]:
        """Batch AP scan for all comparison keys. Empty if not configured."""
        endpoint = os.getenv("AP_ENDPOINT")
        if not endpoint:
            logger.info("AP connector not configured — returning empty")
            return {}
        try:
            # V1 stub — real connector is V2
            return {}
        except Exception as exc:
            logger.warning("ap_batch_scan failed: %s", exc)
            return {}
