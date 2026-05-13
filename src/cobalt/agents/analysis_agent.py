"""Analysis Agent — extracts meaning from raw evidence. Returns structured dicts."""

import json
import logging
from typing import Any

from cobalt.core.llm_call import ToolCallResponse, llm_call, llm_tool_call

logger = logging.getLogger(__name__)

_CONTRACT_FIELDS = [
    "doc_type", "counterparty_name", "renewal_date", "opt_out_deadline",
    "auto_renewal", "contract_value", "contract_value_type", "price_escalation",
    "escalation_rate_max", "payment_terms", "early_termination", "liability_cap",
    "baa_present", "phi_scope", "nda_active", "nda_expiry", "sla_uptime_pct",
]

_ENTITY_FIELDS = [
    "legal_entity", "ticker", "parent_company", "acquired_by", "rebranded_to",
    "category", "subcategory", "vendor_type", "company_status",
    "hq_city", "hq_country", "acquisition_status",
]

# AMENDMENT > SOW > everything else
_DOC_PRIORITY: dict[str, int] = {
    "AMENDMENT": 3, "SOW": 2, "MSA": 1, "ORDER_FORM": 1,
    "BAA": 1, "NDA": 1, "INVOICE": 1, "UNKNOWN": 0,
}

_BASE_SCORES: dict[str, float] = {
    "BRAIN_LOOKUP": 0.97, "REBRAND": 0.95, "ALIAS": 0.97,
    "DEDUP_MERGE": 0.88, "WEB_RESEARCH": 0.60, "DOCUMENT": 0.88,
    "ERP_ONLY": 0.50, "UNKNOWN": 0.25,
}

_NULL_VENDOR: dict[str, Any] = {
    "vendor_name": None, "legal_name": None,
    "doc_type_hint": "UNKNOWN", "confidence": 0.0,
}
_NULL_CONTRACT: dict[str, Any] = {f: None for f in _CONTRACT_FIELDS}
_NULL_CONTRACT["confidence"] = 0.10

_NULL_ENTITY: dict[str, Any] = {f: None for f in _ENTITY_FIELDS}
_NULL_ENTITY["confidence"] = 0.20

_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "extract_vendor_name_from_doc",
            "description": "Extract the vendor/counterparty name and doc type from raw document text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "raw_text": {"type": "string"},
                    "filename": {"type": "string"},
                },
                "required": ["raw_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_contract_terms",
            "description": "Extract commercial contract terms from raw document text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "raw_text": {"type": "string"},
                    "vendor_name": {"type": "string"},
                },
                "required": ["raw_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "structure_entity",
            "description": "Extract structured entity fields from raw research text about a vendor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor_name": {"type": "string"},
                    "raw_research_text": {"type": "string"},
                },
                "required": ["vendor_name", "raw_research_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "score_confidence",
            "description": "Return a confidence score for an entity resolution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "evidence_sources": {"type": "array", "items": {"type": "string"}},
                    "resolution_method": {"type": "string"},
                },
                "required": ["evidence_sources", "resolution_method"],
            },
        },
    },
]

_SYSTEM = (
    "You are the Analysis Agent for the Cobalt vendor intelligence platform. "
    "Extract meaning and structure from raw evidence using the available tools. "
    "When analysis is complete, return a text summary or JSON result."
)

_MAX_ITER = 8


class AnalysisAgent:

    # ------------------------------------------------------------------
    # extract_vendor_name_from_doc
    # ------------------------------------------------------------------

    def extract_vendor_name_from_doc(
        self,
        raw_text: str,
        filename: str = "",
        own_company: str | None = None,
    ) -> dict:
        """Extract counterparty vendor name from raw PDF text. Never raises."""
        if not raw_text.strip():
            return {**_NULL_VENDOR, "confidence": 0.0}

        own_company_line = (
            f"The document is between '{own_company}' (our organization — the buyer/client) "
            f"and a third-party vendor. Extract the COUNTERPARTY name — return null if the "
            f"only identifiable party is '{own_company}' itself.\n"
            if own_company else ""
        )
        system = (
            "You are a contract analysis assistant.\n"
            "Return ONLY valid JSON, no markdown fences.\n"
            f"{own_company_line}"
            "Extract the vendor/counterparty name — NOT the buyer/client organization.\n"
            "Look in: parties clause, signature block, document header.\n"
            'Return: {"vendor_name": string|null, "legal_name": string|null, '
            '"doc_type_hint": "MSA|SOW|NDA|BAA|AMENDMENT|INVOICE|UNKNOWN", '
            '"confidence": float 0.0-1.0}'
        )
        prompt = f"Filename: {filename!r}\n\nDocument text:\n{raw_text[:8000]}"

        try:
            result = llm_call(prompt, system, expect_json=True)
            assert isinstance(result, dict)
            return {
                "vendor_name": result.get("vendor_name"),
                "legal_name": result.get("legal_name"),
                "doc_type_hint": result.get("doc_type_hint", "UNKNOWN"),
                "confidence": float(result.get("confidence", 0.5)),
            }
        except Exception as exc:
            logger.warning("extract_vendor_name_from_doc failed: %s", exc)
            return {**_NULL_VENDOR, "confidence": 0.1}

    # ------------------------------------------------------------------
    # extract_contract_terms
    # ------------------------------------------------------------------

    def extract_contract_terms(
        self,
        raw_text: str,
        vendor_name: str = "",
    ) -> dict:
        """Extract all commercial terms from raw PDF text. Never raises."""
        if not raw_text.strip():
            return dict(_NULL_CONTRACT)

        system = (
            "You are a contract analysis expert.\n"
            "Return ONLY valid JSON.\n"
            "Extract all fields or return null for missing.\n"
            "No preamble, no markdown fences."
        )
        schema_hint = (
            '{\n'
            '  "doc_type": "MSA|SOW|AMENDMENT|BAA|NDA|ORDER_FORM|INVOICE|UNKNOWN",\n'
            '  "counterparty_name": string or null,\n'
            '  "renewal_date": "YYYY-MM-DD" or null,\n'
            '  "opt_out_deadline": "YYYY-MM-DD" or null,\n'
            '  "auto_renewal": true|false|null,\n'
            '  "contract_value": number or null,\n'
            '  "contract_value_type": "FIXED|CEILING|MINIMUM|RANGE" or null,\n'
            '  "price_escalation": true|false|null,\n'
            '  "escalation_rate_max": number or null,\n'
            '  "payment_terms": string or null,\n'
            '  "early_termination": "PERMITTED|NOT_PERMITTED" or null,\n'
            '  "liability_cap": number or null,\n'
            '  "baa_present": true|false|null,\n'
            '  "phi_scope": string or null,\n'
            '  "nda_active": true|false|null,\n'
            '  "nda_expiry": "YYYY-MM-DD" or null,\n'
            '  "sla_uptime_pct": number or null\n'
            '}'
        )
        prompt = (
            f"Vendor context: {vendor_name!r}\n\n"
            f"Extract contract terms from this document:\n{raw_text[:12000]}\n\n"
            f"Return JSON matching this schema:\n{schema_hint}"
        )

        try:
            result = llm_call(prompt, system, expect_json=True)
            assert isinstance(result, dict)
            terms: dict[str, Any] = {f: result.get(f) for f in _CONTRACT_FIELDS}
            terms["confidence"] = self._contract_confidence(terms, vendor_name)
            return terms
        except Exception as exc:
            logger.warning("extract_contract_terms failed: %s", exc)
            return dict(_NULL_CONTRACT)

    def _contract_confidence(self, terms: dict, vendor_name: str) -> float:
        total = len(_CONTRACT_FIELDS)
        non_null = sum(1 for f in _CONTRACT_FIELDS if terms.get(f) is not None)
        base = non_null / total
        bonus = 0.0
        if terms.get("doc_type") and terms["doc_type"] != "UNKNOWN":
            bonus += 0.10
        if (
            vendor_name
            and terms.get("counterparty_name")
            and vendor_name.lower() in str(terms["counterparty_name"]).lower()
        ):
            bonus += 0.05
        return min(base + bonus, 0.97)

    # ------------------------------------------------------------------
    # consolidate_documents
    # ------------------------------------------------------------------

    def consolidate_documents(self, extracted_terms_list: list[dict]) -> dict:
        """Merge multiple document extractions into effective_terms. Deterministic."""
        if not extracted_terms_list:
            return {"effective_terms": {}, "conflicts_detected": [], "source_map": {}}

        effective: dict[str, Any] = {}
        source_map: dict[str, str] = {}
        conflicts: list[str] = []

        # baa_present: True wins if ANY document has baa_present=True
        if any(d.get("baa_present") is True for d in extracted_terms_list):
            effective["baa_present"] = True
            source_map["baa_present"] = next(
                d.get("doc_type", "UNKNOWN")
                for d in extracted_terms_list
                if d.get("baa_present") is True
            )

        # Process docs in ascending priority — highest priority last so it wins
        for doc in sorted(extracted_terms_list, key=lambda d: _DOC_PRIORITY.get(d.get("doc_type", "UNKNOWN"), 0)):
            doc_type = doc.get("doc_type", "UNKNOWN")
            for field, value in doc.items():
                if field in ("doc_type", "confidence", "baa_present"):
                    continue
                if value is None:
                    continue
                if field in effective and effective[field] != value:
                    if field not in conflicts:
                        conflicts.append(field)
                effective[field] = value
                source_map[field] = doc_type

        return {
            "effective_terms": effective,
            "conflicts_detected": conflicts,
            "source_map": source_map,
        }

    # ------------------------------------------------------------------
    # structure_entity
    # ------------------------------------------------------------------

    def structure_entity(
        self,
        vendor_name: str,
        raw_research_text: str,
    ) -> dict:
        """Extract entity fields from raw research text via ONE LLM call. Never raises."""
        if not raw_research_text.strip():
            return dict(_NULL_ENTITY)

        system = (
            "You are a business intelligence analyst.\n"
            "Return ONLY valid JSON, no markdown fences.\n"
            "Extract entity information about the vendor from the research text.\n"
            "Return null for any field not found in the text."
        )
        schema_hint = (
            '{\n'
            '  "legal_entity": string or null,\n'
            '  "ticker": string or null,\n'
            '  "parent_company": string or null,\n'
            '  "acquired_by": string or null,\n'
            '  "rebranded_to": string or null,\n'
            '  "category": string or null,\n'
            '  "subcategory": string or null,\n'
            '  "vendor_type": string or null,\n'
            '  "company_status": "PUBLIC|PRIVATE|SUBSIDIARY" or null,\n'
            '  "hq_city": string or null,\n'
            '  "hq_country": string or null,\n'
            '  "acquisition_status": "ACTIVE|ACQUIRED|REBRANDED|DISSOLVED" or null\n'
            '}'
        )
        prompt = (
            f"Vendor: {vendor_name!r}\n\n"
            f"Research text:\n{raw_research_text[:10000]}\n\n"
            f"Return JSON matching this schema:\n{schema_hint}"
        )

        try:
            result = llm_call(prompt, system, expect_json=True)
            assert isinstance(result, dict)
            entity: dict[str, Any] = {f: result.get(f) for f in _ENTITY_FIELDS}
            non_null = sum(1 for f in _ENTITY_FIELDS if entity.get(f) is not None)
            entity["confidence"] = min(non_null / len(_ENTITY_FIELDS) * 0.55 + 0.40, 0.92)
            return entity
        except Exception as exc:
            logger.warning("structure_entity failed for %r: %s", vendor_name, exc)
            return dict(_NULL_ENTITY)

    # ------------------------------------------------------------------
    # score_confidence
    # ------------------------------------------------------------------

    def score_confidence(
        self,
        evidence_sources: list[str],
        resolution_method: str,
    ) -> float:
        """Deterministic confidence score. Never raises."""
        base = _BASE_SCORES.get(resolution_method, 0.25)
        extra = max(0, len(evidence_sources) - 1)
        return min(base + extra * 0.05, 0.99)

    # ------------------------------------------------------------------
    # run — open-ended agentic loop
    # ------------------------------------------------------------------

    def run(self, task: str, raw_data: str = "") -> str:
        """Execute an open-ended analysis task via LLM tool loop. Never raises."""
        content = f"{task}\n\nRaw data:\n{raw_data}" if raw_data else task
        messages: list[dict] = [{"role": "user", "content": content}]

        for _ in range(_MAX_ITER):
            try:
                response = llm_tool_call(messages, _TOOLS, system=_SYSTEM)
            except Exception as exc:
                logger.warning("AnalysisAgent.run: llm_tool_call failed: %s", exc)
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
        """Route a tool call by name. Returns a JSON-serialised string result."""
        try:
            if name == "extract_vendor_name_from_doc":
                result = self.extract_vendor_name_from_doc(
                    raw_text=args["raw_text"],
                    filename=args.get("filename", ""),
                )
                return json.dumps(result)
            if name == "extract_contract_terms":
                result = self.extract_contract_terms(
                    raw_text=args["raw_text"],
                    vendor_name=args.get("vendor_name", ""),
                )
                return json.dumps(result)
            if name == "structure_entity":
                result = self.structure_entity(
                    vendor_name=args["vendor_name"],
                    raw_research_text=args["raw_research_text"],
                )
                return json.dumps(result)
            if name == "score_confidence":
                score = self.score_confidence(
                    evidence_sources=args["evidence_sources"],
                    resolution_method=args["resolution_method"],
                )
                return str(score)
            logger.warning("AnalysisAgent._dispatch: unknown tool %r", name)
            return f"unknown tool: {name}"
        except Exception as exc:
            logger.warning("AnalysisAgent._dispatch failed for %r: %s", name, exc)
            return f"error: {exc}"

    # ------------------------------------------------------------------
    # detect_vendor_column
    # ------------------------------------------------------------------

    def detect_vendor_column(
        self,
        headers: list[str],
        sample_rows: list[list[str]],
    ) -> str | None:
        """Use LLM to identify which column contains actual company/vendor names.

        Shows the LLM each column's header AND its sample values so it can distinguish
        a 'Vendor Type' category column (values: Vendor/Other/Affiliate) from the real
        company name column (values: Accenture, IBM Corp, Deloitte, ...).
        Never raises.
        """
        if not headers:
            return None

        # Build column → sample values mapping so the LLM sees actual cell content
        col_samples: dict[str, list[str]] = {}
        for i, h in enumerate(headers):
            col_samples[h] = [
                row[i] if i < len(row) else ""
                for row in sample_rows[:5]
            ]

        col_lines = "\n".join(
            f"  {h!r}: {col_samples[h]}" for h in headers
        )

        system = (
            "You are a data analyst. Identify which column contains actual vendor or company names.\n"
            "Real company names are proper business names: 'Accenture', 'IBM Corp', 'Deloitte LLP'.\n"
            "REJECT columns whose values are category labels ('Vendor', 'Other', 'Affiliate', 'Internal'), "
            "identifiers, numbers, dates, or department names.\n"
            "Return ONLY valid JSON, no markdown fences."
        )
        prompt = (
            f"Each column header is shown with its actual sample cell values:\n\n"
            f"{col_lines}\n\n"
            "Which header contains the actual company/vendor/supplier names?\n"
            'Return: {"column": "<exact header name>"} or {"column": null} if none qualify.'
        )

        try:
            result = llm_call(prompt, system, expect_json=True)
            assert isinstance(result, dict)
            col = result.get("column")
            if col and col in headers:
                logger.info("detect_vendor_column: LLM identified %r", col)
                return col
            return None
        except Exception as exc:
            logger.warning("detect_vendor_column failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # detect_contradictions
    # ------------------------------------------------------------------

    def detect_contradictions(self, evidence_list: list[dict]) -> list[dict]:
        """Find fields where two evidence items disagree. Deterministic. Never raises."""
        if len(evidence_list) < 2:
            return []

        result: list[dict] = []
        seen: set[tuple] = set()

        for i in range(len(evidence_list)):
            for j in range(i + 1, len(evidence_list)):
                a = evidence_list[i]
                b = evidence_list[j]
                src_a = a.get("source", f"source_{i}")
                src_b = b.get("source", f"source_{j}")
                for field in set(a.keys()) | set(b.keys()):
                    if field == "source":
                        continue
                    val_a = a.get(field)
                    val_b = b.get(field)
                    if val_a is None or val_b is None:
                        continue
                    if val_a != val_b:
                        key = (field, str(val_a), str(val_b), src_a, src_b)
                        if key not in seen:
                            seen.add(key)
                            result.append({
                                "field": field,
                                "value_a": val_a,
                                "value_b": val_b,
                                "source_a": src_a,
                                "source_b": src_b,
                            })

        return result
