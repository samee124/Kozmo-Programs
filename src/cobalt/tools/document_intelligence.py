"""Tool 2 (Process 3) — document_intelligence.

Read and extract structured facts from unstructured documents — contract PDFs,
uploaded invoices, SOWs, amendment letters, QBR notes, compliance certs.

Uses LLM (one call per document). No workspace writes. Returns
DocumentIntelligenceResult in memory. Never raises.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from cobalt.core.llm_call import llm_call
from cobalt.models.schemas.rs_schema import (
    ContractTerms,
    DocumentIntelligenceResult,
    DocumentType,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Document type classification patterns (precedence order matters)
# ---------------------------------------------------------------------------
_TYPE_PATTERNS: list[tuple[str, re.Pattern]] = [
    (DocumentType.CONTRACT.value,    re.compile(r"master service|msa|master agreement|framework agreement|contract|agreement|terms and conditions|service agreement", re.I)),
    (DocumentType.SOW.value,         re.compile(r"statement of work|sow|scope of work", re.I)),
    (DocumentType.AMENDMENT.value,   re.compile(r"amendment|addendum|change order|variation order", re.I)),
    (DocumentType.INVOICE.value,     re.compile(r"invoice|inv-|bill of", re.I)),
    (DocumentType.QBR.value,         re.compile(r"quarterly business review|qbr|business review", re.I)),
    (DocumentType.COMPLIANCE.value,  re.compile(r"certificate|certification|iso \d{4,5}|compliance cert", re.I)),
]

_LLM_PROMPT_TEMPLATE = """\
You are extracting contract terms from a {document_type} document for procurement records.
Extract the following fields and return as JSON only.
Return null for any field not present in the document — do not infer or guess values.

Fields to extract:
- effective_date: ISO date string (YYYY-MM-DD) or null
- expiry_date: ISO date string (YYYY-MM-DD) or null
- auto_renews: boolean (true/false) or null
- notice_period_days: integer (number of days) or null
- total_value: float (numeric value only, no currency symbol) or null
- currency: 3-letter ISO currency code or null
- payment_terms_days: integer (e.g. 30 for "Net 30") or null
- governing_law: string (jurisdiction name) or null
- termination_clauses: list of strings (one sentence each) or []
- key_obligations: list of strings (one sentence each) or []
- sla_summary: string (one sentence) or null

Document type: {document_type}
Document text:
{text}

Return only the JSON object. No explanation.\
"""

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _classify_document_type(filename: str, first_500_chars: str) -> str:
    """Classify document type from filename + first 500 chars via regex heuristics."""
    combined = f"{filename} {first_500_chars}"
    for doc_type, pattern in _TYPE_PATTERNS:
        if pattern.search(combined):
            return doc_type
    return DocumentType.OTHER.value


def _fetch_document_text(path: str) -> tuple[str | None, bool]:
    """Extract text from a document file.

    Returns (text | None, was_truncated).
    Returns None if document is unreadable.
    """
    p = Path(path)
    suffix = p.suffix.lower()

    if suffix not in (".pdf", ".txt", ".md"):
        return None, False

    try:
        if suffix == ".pdf":
            import pypdf
            reader = pypdf.PdfReader(str(p))
            text = "\n".join(
                page.extract_text() or "" for page in reader.pages
            )
        else:
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = p.read_text(encoding="latin-1")
    except Exception:
        return None, False

    if not text:
        return None, False

    if len(text) < 100:
        return None, False  # treated as unreadable

    truncated = False
    if len(text) > 50_000:
        text = text[:50_000]
        truncated = True

    return text, truncated


def _extract_contract_terms(
    doc_text: str,
    document_id: str,
    document_type: str,
) -> tuple[ContractTerms, list[str]]:
    """Call LLM to extract contract terms. Returns (ContractTerms, warnings)."""
    warnings: list[str] = []

    prompt = _LLM_PROMPT_TEMPLATE.format(
        document_type=document_type,
        text=doc_text[:6000],
    )

    try:
        response = llm_call(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o",
            temperature=0,
            max_tokens=1000,
        )
        raw_text = response.strip()
        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            raw_text = re.sub(r"^```[^\n]*\n?", "", raw_text)
            raw_text = re.sub(r"\n?```$", "", raw_text.strip())
        parsed = json.loads(raw_text)
    except Exception as exc:
        logger.debug("LLM extraction failed for %s: %s", document_id, exc)
        warnings.append(f"LLM_EXTRACTION_FAILED_{document_id}")
        return ContractTerms(
            document_id=document_id,
            document_type=document_type,
            effective_date=None,
            expiry_date=None,
            auto_renews=None,
            notice_period_days=None,
            total_value=None,
            currency=None,
            payment_terms_days=None,
            governing_law=None,
            termination_clauses=[],
            key_obligations=[],
            sla_summary=None,
            extraction_confidence="LOW",
        ), warnings

    def _date(val) -> str | None:
        if val is None:
            return None
        s = str(val).strip()
        return s if _ISO_DATE_RE.match(s) else None

    def _float(val) -> float | None:
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _int(val) -> int | None:
        if val is None:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    def _bool(val) -> bool | None:
        if val is None:
            return None
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "yes", "1")
        return None

    def _str_list(val) -> list[str]:
        if not isinstance(val, list):
            return []
        return [str(item) for item in val if item]

    terms = ContractTerms(
        document_id=document_id,
        document_type=document_type,
        effective_date=_date(parsed.get("effective_date")),
        expiry_date=_date(parsed.get("expiry_date")),
        auto_renews=_bool(parsed.get("auto_renews")),
        notice_period_days=_int(parsed.get("notice_period_days")),
        total_value=_float(parsed.get("total_value")),
        currency=parsed.get("currency"),
        payment_terms_days=_int(parsed.get("payment_terms_days")),
        governing_law=parsed.get("governing_law"),
        termination_clauses=_str_list(parsed.get("termination_clauses")),
        key_obligations=_str_list(parsed.get("key_obligations")),
        sla_summary=parsed.get("sla_summary"),
        extraction_confidence="LOW",  # set by _score_confidence
    )
    return terms, warnings


def _score_confidence(terms: ContractTerms, was_truncated: bool) -> str:
    """Assign extraction confidence to a ContractTerms record."""
    non_null_fields = sum(
        1 for v in [
            terms.effective_date, terms.expiry_date, terms.auto_renews,
            terms.notice_period_days, terms.total_value, terms.currency,
            terms.payment_terms_days, terms.governing_law, terms.sla_summary,
        ]
        if v is not None
    ) + len(terms.termination_clauses) + len(terms.key_obligations)

    if was_truncated:
        return "LOW"
    if non_null_fields >= 5 and (terms.effective_date or terms.total_value):
        return "HIGH"
    if non_null_fields >= 2:
        return "MEDIUM"
    return "LOW"


def _is_duplicate(new_terms: ContractTerms, existing: list[ContractTerms]) -> bool:
    """Check fingerprint (effective_date, total_value, currency) for duplicates."""
    if (
        new_terms.effective_date is None
        or new_terms.total_value is None
        or new_terms.currency is None
    ):
        return False
    fingerprint = (new_terms.effective_date, new_terms.total_value, new_terms.currency)
    for ct in existing:
        if ct.effective_date is None or ct.total_value is None or ct.currency is None:
            continue
        if (ct.effective_date, ct.total_value, ct.currency) == fingerprint:
            return True
    return False


def process_documents(
    vendor_id: str,
    programme_id: str,
    document_paths: list[str],
) -> DocumentIntelligenceResult:
    """Extract structured contract terms from a list of document files.

    One LLM call per document. Never raises — all failures become warnings.
    """
    extracted_contracts: list[ContractTerms] = []
    extraction_warnings: list[str] = []
    documents_processed = 0
    documents_skipped = 0

    for path_str in document_paths:
        p = Path(path_str)
        suffix = p.suffix.lower()
        doc_id = p.stem

        if suffix not in (".pdf", ".txt", ".md"):
            extraction_warnings.append(f"UNSUPPORTED_FORMAT_{doc_id}")
            documents_skipped += 1
            continue

        doc_text, was_truncated = _fetch_document_text(path_str)

        if doc_text is None:
            extraction_warnings.append(f"DOCUMENT_UNREADABLE_{doc_id}")
            documents_skipped += 1
            continue

        if was_truncated:
            extraction_warnings.append(f"DOCUMENT_TRUNCATED_{doc_id}")

        first_500 = doc_text[:500]
        doc_type = _classify_document_type(p.name, first_500)

        terms, llm_warnings = _extract_contract_terms(doc_text, doc_id, doc_type)
        extraction_warnings.extend(llm_warnings)

        if f"LLM_EXTRACTION_FAILED_{doc_id}" not in llm_warnings:
            # Only score confidence if extraction succeeded
            terms.extraction_confidence = _score_confidence(terms, was_truncated)

        documents_processed += 1

        if _is_duplicate(terms, extracted_contracts):
            extraction_warnings.append(f"DUPLICATE_DOCUMENT_{doc_id}")
            continue

        extracted_contracts.append(terms)

    return DocumentIntelligenceResult(
        vendor_id=vendor_id,
        documents_processed=documents_processed,
        documents_skipped=documents_skipped,
        extracted_contracts=extracted_contracts,
        extraction_warnings=extraction_warnings,
    )
