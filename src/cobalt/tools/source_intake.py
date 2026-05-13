"""Tool 1 — source_intake: ingest all input sources into a unified candidate bundle."""

import csv
import hashlib
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cobalt.agents.analysis_agent import AnalysisAgent

from cobalt.agents.analysis_agent import AnalysisAgent
from cobalt.agents.research_agent import ResearchAgent
from cobalt.intake._cleaner import structural_clean
from cobalt.intake._normalizer import normalize

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RawCandidate:
    raw_name:        str
    source:          str           # VENDOR_LIST / DOCUMENT / BOTH
    source_refs:     list[str]
    linked_doc_ids:  list[str]
    spend_hint:      Decimal | None
    category_hint:   str | None
    department_hint: str | None
    extra_fields:    dict = field(default_factory=dict)  # all other columns verbatim


@dataclass
class DocRecord:
    doc_id:     str
    filename:   str
    file_path:  str
    raw_text:   str
    doc_type:   str        # MSA/SOW/NDA/BAA/AMENDMENT/INVOICE/UNKNOWN
    vendor_key: str | None


@dataclass
class SourceIntakeResult:
    candidates:     list[RawCandidate]
    doc_registry:   dict[str, DocRecord]
    source_summary: dict


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_key(name: str) -> str:
    try:
        _, key = normalize(structural_clean(name))
        return key
    except Exception:
        return name.lower().strip()


_PATH_DOC_TYPE_RULES: list[tuple[list[str], str]] = [
    (["NON DISCLOSURE", "NON-DISCLOSURE", "NONDISCLOSURE", "NDA"],   "NDA"),
    (["STATEMENT OF WORK", "STATEMENTS OF WORK"],                     "SOW"),
    (["BUSINESS ASSOCIATE", " BAA"],                                  "BAA"),
    (["MASTER SERVICE", "MASTER AGREEMENT"],                          "MSA"),
    (["SOFTWARE LICENS", "LICENSING AGREEMENT"],                      "MSA"),
    (["PARTNER PROGRAM", "PARTNER AGREEMENT"],                        "MSA"),
    (["PROFESSIONAL SERVICES", "PROF SERVICES"],                      "SOW"),
    (["PRODUCT SERVICES"],                                            "MSA"),
    (["AMENDMENT"],                                                   "AMENDMENT"),
    (["INVOICE"],                                                     "INVOICE"),
]


def _infer_doc_type_from_path(relative_path: str) -> str:
    """Infer doc type from folder/filename when LLM returns UNKNOWN."""
    upper = relative_path.upper()
    for keywords, doc_type in _PATH_DOC_TYPE_RULES:
        if any(kw in upper for kw in keywords):
            return doc_type
    return "UNKNOWN"


def _parse_decimal(val: str) -> Decimal | None:
    if not val:
        return None
    try:
        cleaned = val.replace("$", "").replace("£", "").replace("€", "").replace(",", "").strip()
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def _detect_name_col(fieldnames: list[str]) -> str | None:
    priority = [
        "vendor_name", "Vendor Name", "name", "Name", "VENDOR",
        "supplier", "Supplier", "company", "Company", "payee", "Payee",
        "contractor", "Contractor", "counterparty", "Counterparty",
        "vendor", "Vendor",
    ]
    for p in priority:
        if p in fieldnames:
            return p
    lower_map = {n.lower(): n for n in fieldnames}
    for p in priority:
        if p.lower() in lower_map:
            return lower_map[p.lower()]
    return None


def _detect_name_col_by_cardinality(
    headers: list[str],
    col_values: dict[str, list[str]],
) -> str | None:
    """Last-resort fallback: the column with the most unique text values is most
    likely the vendor name column. Skips columns that look like IDs or numbers."""
    best_col: str | None = None
    best_unique = 0

    for h in headers:
        vals = col_values.get(h, [])
        if not vals:
            continue
        # Skip columns where most values are purely numeric (IDs, amounts)
        numeric = sum(1 for v in vals if v.replace(".", "").replace(",", "").replace("-", "").isdigit())
        if len(vals) > 0 and numeric / len(vals) > 0.7:
            continue
        unique_count = len(set(vals))
        if unique_count > best_unique and unique_count > 5:
            best_unique = unique_count
            best_col = h

    if best_col:
        logger.warning(
            "Using cardinality fallback — picked %r (%d unique values). "
            "Fix your OpenAI API key for accurate LLM-based detection.",
            best_col, best_unique,
        )
    return best_col


def _find_col(fieldnames: list[str], candidates: list[str]) -> str | None:
    lower_map = {n.lower(): n for n in fieldnames}
    for c in candidates:
        if c.lower() in lower_map:
            return lower_map[c.lower()]
    return None


def _read_csv(path: str, analysis_agent: "AnalysisAgent | None" = None) -> list[RawCandidate]:
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            rows_raw = list(reader)

        name_col = _detect_name_col(fieldnames)

        # Cardinality guard: reject if matched column looks like a category field
        if name_col is not None:
            col_vals = [
                (row.get(name_col, "") or "").strip()
                for row in rows_raw[:50]
            ]
            col_vals = [v for v in col_vals if v]
            if len(col_vals) >= 5 and len(set(col_vals)) <= 5:
                logger.warning(
                    "CSV: heuristic matched %r but only %d unique values in %d rows — likely a category column; trying LLM",
                    name_col, len(set(col_vals)), len(col_vals),
                )
                name_col = None

        # Build column → values map for LLM and cardinality fallback
        col_values: dict[str, list[str]] = {h: [] for h in fieldnames if h}
        for row in rows_raw[:50]:
            for h in fieldnames:
                if not h:
                    continue
                v = (row.get(h, "") or "").strip()
                if v:
                    col_values[h].append(v)

        if name_col is None and analysis_agent is not None and fieldnames:
            sample = [[str(row.get(h, "") or "") for h in fieldnames] for row in rows_raw[:5]]
            name_col = analysis_agent.detect_vendor_column(fieldnames, sample)
            if name_col:
                logger.info("CSV: LLM detected vendor column %r in %r", name_col, path)

        # Last resort: highest-cardinality text column
        if name_col is None:
            name_col = _detect_name_col_by_cardinality(fieldnames, col_values)

        if name_col is None:
            logger.warning("CSV: could not identify vendor column in %r — skipping", path)
            return []

        spend_col  = _find_col(fieldnames, ["spend", "annual_spend", "total_spend"])
        cat_col    = _find_col(fieldnames, ["category", "cat"])
        dept_col   = _find_col(fieldnames, ["department", "dept"])
        known_cols = {name_col, spend_col, cat_col, dept_col} - {None}
        extra_cols = [h for h in fieldnames if h and h not in known_cols]

        results: list[RawCandidate] = []
        for row in rows_raw:
            raw_name = (row.get(name_col, "") or "").strip()
            if not raw_name:
                continue
            extra: dict = {}
            for h in extra_cols:
                v = (row.get(h, "") or "").strip()
                if v:
                    extra[h] = v
            results.append(RawCandidate(
                raw_name=raw_name,
                source="VENDOR_LIST",
                source_refs=[path],
                linked_doc_ids=[],
                spend_hint=_parse_decimal(row.get(spend_col, "") or "") if spend_col else None,
                category_hint=(row.get(cat_col, "") or "").strip() or None if cat_col else None,
                department_hint=(row.get(dept_col, "") or "").strip() or None if dept_col else None,
                extra_fields=extra,
            ))
        return results
    except Exception as exc:
        logger.warning("Failed to read CSV %r: %s", path, exc)
        return []


def _read_excel(path: str, analysis_agent: "AnalysisAgent | None" = None) -> list[RawCandidate]:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        all_rows = list(ws.rows)
        wb.close()
        if not all_rows:
            return []

        headers = [str(cell.value) if cell.value is not None else "" for cell in all_rows[0]]
        name_col = _detect_name_col(headers)

        # Cardinality guard: heuristic may match a type/category column (e.g. "Vendor" with
        # values Vendor/Other/Affiliate). If the matched column has ≤5 unique values across
        # the data rows, it is almost certainly a category column — reject and use the LLM.
        if name_col is not None:
            n_idx = headers.index(name_col)
            data_vals = [
                str(row[n_idx]).strip()
                for row in [list(r) for r in all_rows[1:51]]
                if n_idx < len(row) and row[n_idx] is not None
                and str(row[n_idx]).strip() not in ("", "None")
            ]
            # Re-read properly using cell values
            data_vals = []
            for row in all_rows[1:51]:
                vals = [cell.value for cell in row]
                if n_idx < len(vals) and vals[n_idx] is not None:
                    v = str(vals[n_idx]).strip()
                    if v and v != "None":
                        data_vals.append(v)
            if len(data_vals) >= 5 and len(set(data_vals)) <= 5:
                logger.warning(
                    "Excel: heuristic matched %r but only %d unique values in %d rows — likely a category column; trying LLM",
                    name_col, len(set(data_vals)), len(data_vals),
                )
                name_col = None

        # Build column → sample values map (used by both LLM and cardinality fallback)
        col_values: dict[str, list[str]] = {h: [] for h in headers if h}
        for row in all_rows[1:51]:
            vals = [cell.value for cell in row]
            for i, h in enumerate(headers):
                if not h:
                    continue
                if i < len(vals) and vals[i] is not None:
                    v = str(vals[i]).strip()
                    if v and v != "None":
                        col_values[h].append(v)

        if name_col is None and analysis_agent is not None:
            sample = [
                [str(cell.value).strip() if cell.value is not None else "" for cell in row]
                for row in all_rows[1:6]
            ]
            name_col = analysis_agent.detect_vendor_column(headers, sample)
            if name_col:
                logger.info("Excel: LLM detected vendor column %r in %r", name_col, path)

        # Last resort: highest-cardinality text column
        if name_col is None:
            name_col = _detect_name_col_by_cardinality(headers, col_values)

        if name_col is None:
            logger.warning("Excel: could not identify vendor column in %r — skipping", path)
            return []

        name_idx  = headers.index(name_col)
        spend_col = _find_col(headers, ["spend", "annual_spend", "total_spend"])
        cat_col   = _find_col(headers, ["category", "cat"])
        dept_col  = _find_col(headers, ["department", "dept"])
        spend_idx = headers.index(spend_col) if spend_col else None
        cat_idx   = headers.index(cat_col) if cat_col else None
        dept_idx  = headers.index(dept_col) if dept_col else None

        known_idx = {i for i in [name_idx, spend_idx, cat_idx, dept_idx] if i is not None}
        extra_cols = [(i, h) for i, h in enumerate(headers) if i not in known_idx and h]

        results: list[RawCandidate] = []
        for row in all_rows[1:]:
            vals = [cell.value for cell in row]

            def _cell(idx: int | None) -> str:
                if idx is None or idx >= len(vals) or vals[idx] is None:
                    return ""
                return str(vals[idx]).strip()

            raw_name = _cell(name_idx)
            if not raw_name or raw_name == "None":
                continue

            extra: dict = {}
            for idx, hdr in extra_cols:
                v = _cell(idx)
                if v and v != "None":
                    extra[hdr] = v

            results.append(RawCandidate(
                raw_name=raw_name,
                source="VENDOR_LIST",
                source_refs=[path],
                linked_doc_ids=[],
                spend_hint=_parse_decimal(_cell(spend_idx)),
                category_hint=_cell(cat_idx) or None,
                department_hint=_cell(dept_idx) or None,
                extra_fields=extra,
            ))
        return results
    except Exception as exc:
        logger.warning("Failed to read Excel %r: %s", path, exc)
        return []


def _read_vendor_list(path: str, analysis_agent: "AnalysisAgent | None" = None) -> list[RawCandidate]:
    low = path.lower()
    if low.endswith(".csv"):
        return _read_csv(path, analysis_agent)
    if low.endswith((".xlsx", ".xls")):
        return _read_excel(path, analysis_agent)
    logger.warning("Unsupported vendor list format: %r", path)
    return []


def _deduplicate(candidates: list[RawCandidate]) -> list[RawCandidate]:
    seen: dict[str, RawCandidate] = {}
    for cand in candidates:
        key = _to_key(cand.raw_name)
        if key in seen:
            existing = seen[key]
            existing.linked_doc_ids = list(set(existing.linked_doc_ids + cand.linked_doc_ids))
            existing.source_refs = list(dict.fromkeys(existing.source_refs + cand.source_refs))
            if existing.source != cand.source:
                existing.source = "BOTH"
            if existing.spend_hint is None:
                existing.spend_hint = cand.spend_hint
            if existing.category_hint is None:
                existing.category_hint = cand.category_hint
            if existing.department_hint is None:
                existing.department_hint = cand.department_hint
            # merge extra_fields — first-seen value wins on key collision
            for k, v in cand.extra_fields.items():
                existing.extra_fields.setdefault(k, v)
        else:
            seen[key] = cand
    return list(seen.values())


def _build_summary(candidates: list[RawCandidate], doc_registry: dict) -> dict:
    doc_types: dict[str, int] = {}
    for doc in doc_registry.values():
        doc_types[doc.doc_type] = doc_types.get(doc.doc_type, 0) + 1
    return {
        "vendor_list_count": sum(1 for c in candidates if c.source in ("VENDOR_LIST", "BOTH")),
        "doc_count": len(doc_registry),
        "doc_types": doc_types,
        "candidates_total": len(candidates),
        "from_list_only": sum(1 for c in candidates if c.source == "VENDOR_LIST"),
        "from_docs_only": sum(1 for c in candidates if c.source == "DOCUMENT"),
        "from_both": sum(1 for c in candidates if c.source == "BOTH"),
    }


# ---------------------------------------------------------------------------
# run — public entry point
# ---------------------------------------------------------------------------

def run(
    vendor_list_path: str | None,
    documents_path: str | None,
    research_agent: ResearchAgent,
    analysis_agent: AnalysisAgent,
    own_company: str | None = None,
) -> SourceIntakeResult:
    """Ingest vendor list and/or Google Drive documents. Never raises."""
    candidates: list[RawCandidate] = []
    doc_registry: dict[str, DocRecord] = {}
    extracted_vendors: dict[str, str | None] = {}

    try:
        # Step A — read vendor list
        if vendor_list_path:
            logger.info("Reading vendor list: %r", vendor_list_path)
            candidates = _read_vendor_list(vendor_list_path, analysis_agent)
            logger.info("Vendor list loaded — %d raw candidates", len(candidates))

        # Step B — process Google Drive documents
        if documents_path:
            try:
                logger.info("Fetching documents from: %r", documents_path)
                docs = research_agent.fetch_google_drive_docs(documents_path)
                logger.info("Found %d documents to process", len(docs))
                for doc_info in docs:
                    filename      = doc_info.get("filename", "") or ""
                    raw_text      = doc_info.get("raw_text", "") or ""
                    file_path     = doc_info.get("file_path", "") or ""
                    relative_path = doc_info.get("relative_path", "") or filename

                    # Use file_path for doc_id — prevents collision when multiple
                    # vendors each have a file named "contract.pdf"
                    doc_id = hashlib.md5(file_path.encode()).hexdigest()[:12]

                    # Always use LLM — folder structure can be anything (by doc type,
                    # by year, by department).  Pass relative_path so the LLM sees
                    # folder context + filename together (e.g. "MSA/IBM MSA 2024.pdf").
                    # Pass own_company so the LLM knows NOT to return us as the vendor.
                    try:
                        extraction = analysis_agent.extract_vendor_name_from_doc(
                            raw_text, relative_path, own_company=own_company
                        )
                        doc_type    = extraction.get("doc_type_hint", "UNKNOWN") or "UNKNOWN"
                        vendor_name = extraction.get("vendor_name")
                    except Exception as exc:
                        logger.warning("extract_vendor_name_from_doc failed for %r: %s", relative_path, exc)
                        doc_type    = "UNKNOWN"
                        vendor_name = None

                    # Fallback: infer doc_type from folder/filename when LLM returns UNKNOWN
                    if doc_type == "UNKNOWN":
                        inferred = _infer_doc_type_from_path(relative_path)
                        if inferred != "UNKNOWN":
                            doc_type = inferred

                    logger.info(
                        "    [Doc] LLM extraction → vendor=%r  doc_type=%s  path=%s",
                        vendor_name, doc_type, relative_path,
                    )

                    doc_registry[doc_id] = DocRecord(
                        doc_id=doc_id,
                        filename=filename,
                        file_path=file_path,
                        raw_text=raw_text,
                        doc_type=doc_type,
                        vendor_key=None,
                    )
                    extracted_vendors[doc_id] = vendor_name
            except Exception as exc:
                logger.warning("Step B (Google Drive) failed: %s", exc)

        # Step C — link documents to candidates
        candidate_key_map: dict[str, int] = {}
        for i, cand in enumerate(candidates):
            key = _to_key(cand.raw_name)
            candidate_key_map[key] = i

        new_from_docs: list[RawCandidate] = []

        for doc_id, vendor_name in extracted_vendors.items():
            if not vendor_name:
                continue
            extracted_key = _to_key(vendor_name)
            doc = doc_registry[doc_id]

            if extracted_key in candidate_key_map:
                idx = candidate_key_map[extracted_key]
                target = candidates[idx] if idx < len(candidates) else new_from_docs[idx - len(candidates)]
                if doc_id not in target.linked_doc_ids:
                    target.linked_doc_ids.append(doc_id)
                if target.source == "VENDOR_LIST":
                    target.source = "BOTH"
                doc.vendor_key = extracted_key
            else:
                new_cand = RawCandidate(
                    raw_name=vendor_name,
                    source="DOCUMENT",
                    source_refs=[doc.filename],
                    linked_doc_ids=[doc_id],
                    spend_hint=None,
                    category_hint=None,
                    department_hint=None,
                )
                new_idx = len(candidates) + len(new_from_docs)
                new_from_docs.append(new_cand)
                candidate_key_map[extracted_key] = new_idx
                doc.vendor_key = extracted_key

        candidates.extend(new_from_docs)

        # Step D — deduplicate
        before_dedup = len(candidates)
        candidates = _deduplicate(candidates)
        logger.info("Dedup: %d → %d candidates (%d merged)", before_dedup, len(candidates), before_dedup - len(candidates))

    except Exception as exc:
        logger.error("source_intake.run failed unexpectedly: %s", exc)

    # Step E — build summary (always runs)
    source_summary = _build_summary(candidates, doc_registry)
    logger.info(
        "Source intake complete — %d candidates  %d docs  (list=%d | docs=%d | both=%d)",
        source_summary.get("candidates_total", 0),
        source_summary.get("doc_count", 0),
        source_summary.get("from_list_only", 0),
        source_summary.get("from_docs_only", 0),
        source_summary.get("from_both", 0),
    )

    return SourceIntakeResult(
        candidates=candidates,
        doc_registry=doc_registry,
        source_summary=source_summary,
    )
