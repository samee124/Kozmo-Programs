"""Tool 1 (Process 3) — structured_data_collector.

Collect all raw structured spend, contract metadata, and ownership data from
every connected system and structured file upload. Three arrival modes:
CONNECTOR, FILE_UPLOAD, CHECK_IN.

No LLM calls. No external HTTP. Returns StructuredDataBundle in memory.
All failures become collection_warnings — this tool never raises.

Arrival mode order when arrival_modes=None: CONNECTOR → FILE_UPLOAD → CHECK_IN.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

from cobalt.core.file_system import connectors_path
from cobalt.core.name_matching import best_match, normalise_for_match
from cobalt.models.schemas.rs_schema import (
    ArrivalMode,
    RawSpendRecord,
    StructuredDataBundle,
    TrustLevel,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# V1 static exchange rates to USD
# ---------------------------------------------------------------------------
_EXCHANGE_RATES: dict[str, float] = {
    "USD": 1.00,
    "GBP": 1.26,
    "EUR": 1.08,
    "AUD": 0.65,
    "CAD": 0.74,
    "JPY": 0.0067,
    "CHF": 1.11,
}

# Currency symbol → ISO code mapping
_SYMBOL_TO_CODE: dict[str, str] = {
    "£": "GBP",
    "€": "EUR",
    "$": "USD",
    "¥": "JPY",
    "A$": "AUD",
    "C$": "CAD",
    "Fr": "CHF",
}

# ---------------------------------------------------------------------------
# File upload header aliases (canonical → accepted aliases)
# ---------------------------------------------------------------------------
_HEADER_ALIASES: dict[str, list[str]] = {
    "vendor":          ["vendor", "supplier", "payee", "company", "vendor name", "supplier name"],
    "amount":          ["amount", "value", "total", "cost", "spend", "invoice amount"],
    "currency":        ["currency", "ccy", "currency code"],
    "period_start":    ["period start", "from date", "start date", "date from"],
    "period_end":      ["period end", "to date", "end date", "invoice date", "date"],
    "po_number":       ["po", "po number", "purchase order", "po ref"],
    "invoice_ref":     ["invoice", "invoice ref", "invoice number", "inv ref"],
    "cost_centre":     ["cost centre", "cost center", "cc", "department", "dept"],
    "category":        ["category", "spend category", "type", "service type"],
    "payment_terms":   ["payment terms", "payment terms days", "net terms", "net days", "terms"],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_header(header: str) -> str:
    """Lowercase + strip punctuation for header comparison."""
    return re.sub(r"[^\w\s]", "", header.lower()).strip()


def _map_headers(raw_headers: list[str]) -> dict[str, int]:
    """Map raw CSV headers to canonical field names. Returns {canonical: col_index}."""
    mapping: dict[str, int] = {}
    for idx, raw_h in enumerate(raw_headers):
        normalised = _normalise_header(raw_h)
        for canonical, aliases in _HEADER_ALIASES.items():
            norm_aliases = [_normalise_header(a) for a in aliases]
            if normalised in norm_aliases and canonical not in mapping:
                mapping[canonical] = idx
    return mapping


def _normalise_currency(amount_raw: str, currency_raw: str | None) -> tuple[float | None, str | None, list[str]]:
    """Parse amount_raw to USD. Returns (amount_usd, detected_currency_code, warnings)."""
    warnings: list[str] = []
    if not amount_raw:
        return None, currency_raw, warnings

    text = amount_raw.strip()
    detected_code: str | None = currency_raw

    # Detect symbol prefix
    for symbol, code in sorted(_SYMBOL_TO_CODE.items(), key=lambda x: -len(x[0])):
        if text.startswith(symbol):
            text = text[len(symbol):].strip()
            if detected_code is None:
                detected_code = code
            break

    # Handle EUR decimal format (period as thousands sep, comma as decimal)
    # e.g. "2.500,00" → "2500.00"
    if detected_code == "EUR" and "," in text and "." in text:
        # thousands period, decimal comma
        text = text.replace(".", "").replace(",", ".")
    else:
        # Standard: remove commas as thousand separators
        text = text.replace(",", "")

    try:
        amount_float = float(text)
    except ValueError:
        warnings.append(f"UNPARSEABLE_AMOUNT_{amount_raw}")
        return None, detected_code, warnings

    if detected_code is None:
        return amount_float, None, warnings  # no conversion possible

    code_upper = detected_code.upper()
    rate = _EXCHANGE_RATES.get(code_upper)
    if rate is None:
        warnings.append(f"UNKNOWN_CURRENCY_{code_upper}")
        return None, code_upper, warnings

    return round(amount_float * rate, 4), code_upper, warnings


def _match_to_vendor(
    row_vendor_name: str | None,
    vendor_id: str,
    canonical_name: str,
    aliases: list[str],
) -> tuple[str | None, str]:
    """Match a row's vendor name to the target vendor.

    Returns (matched_vendor_id | None, match_confidence).
    """
    if row_vendor_name is None:
        # No vendor column — presumed vendor-specific upload, MEDIUM confidence
        return vendor_id, "MEDIUM"

    candidates = [canonical_name] + aliases
    result = best_match(row_vendor_name, candidates, threshold=0.60)
    if result is None:
        return None, "UNMATCHED"

    _, score = result
    if score >= 0.90:
        return vendor_id, "HIGH"
    if score >= 0.75:
        return vendor_id, "MEDIUM"
    if score >= 0.60:
        return vendor_id, "LOW"
    return None, "UNMATCHED"


def _deduplicate_records(records: list[RawSpendRecord]) -> tuple[list[RawSpendRecord], int]:
    """Collapse exact duplicates within same arrival mode.

    Duplicate definition: same non-null invoice_ref AND same period_start AND same amount_raw.
    Returns (deduplicated_records, collapsed_count).
    """
    seen: set[tuple] = set()
    result: list[RawSpendRecord] = []
    collapsed = 0

    for rec in records:
        if rec.invoice_ref is not None:
            key = (rec.invoice_ref, rec.period_start, rec.amount_raw, rec.arrival_mode)
            if key in seen:
                collapsed += 1
                continue
            seen.add(key)
        result.append(rec)

    return result, collapsed


def _collect_from_connectors(
    vendor_id: str,
    programme_id: str,
    config: dict,
) -> tuple[list[RawSpendRecord], dict, list[str]]:
    """Read records from connector stub JSON files.

    Returns (records, connector_metadata, warnings).
    """
    records: list[RawSpendRecord] = []
    metadata: dict = {}
    warnings: list[str] = []

    stub_dir = connectors_path(programme_id, vendor_id)
    if not stub_dir.is_dir():
        warnings.append("CONNECTOR_DIR_MISSING")
        metadata["stub"] = {"status": "CONNECTOR_DIR_MISSING"}
        return records, metadata, warnings

    is_authoritative = config.get("authoritative", False)
    trust_level = TrustLevel.OFFICIAL.value if is_authoritative else TrustLevel.SYSTEM_EXPORT.value

    json_files = list(stub_dir.glob("*.json"))
    if not json_files:
        metadata["stub"] = {"status": "NO_FILES", "records_pulled": 0, "errors": []}
        return records, metadata, warnings

    for jf in json_files:
        source_id = jf.stem
        try:
            raw_list = json.loads(jf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            warnings.append(f"CONNECTOR_PARSE_ERROR_{source_id}")
            metadata[source_id] = {"status": "PARSE_ERROR", "error": str(exc)}
            continue

        if not isinstance(raw_list, list):
            raw_list = [raw_list]

        file_records: list[RawSpendRecord] = []
        for row in raw_list:
            if not isinstance(row, dict):
                continue
            amount_raw = str(row.get("amount_raw", ""))
            currency_raw = row.get("currency_raw")
            amount_usd, currency_code, curr_warnings = _normalise_currency(amount_raw, currency_raw)
            warnings.extend(curr_warnings)

            rec = RawSpendRecord(
                source_id=source_id,
                arrival_mode=ArrivalMode.CONNECTOR.value,
                trust_level=trust_level,
                period_start=row.get("period_start"),
                period_end=row.get("period_end"),
                amount_raw=amount_raw,
                currency_raw=currency_code or currency_raw,
                amount_usd=amount_usd,
                category_raw=row.get("category_raw"),
                cost_centre=row.get("cost_centre"),
                po_number=row.get("po_number"),
                invoice_ref=row.get("invoice_ref"),
                matched_vendor_id=vendor_id,
                match_confidence="HIGH",
                payment_terms_days=row.get("payment_terms_days"),
            )
            file_records.append(rec)

        deduped, collapsed = _deduplicate_records(file_records)
        records.extend(deduped)
        errors: list[str] = []
        if collapsed:
            errors.append(f"COLLAPSED_{collapsed}_DUPLICATES")
        metadata[source_id] = {
            "status": "OK",
            "records_pulled": len(deduped),
            "errors": errors,
        }

    return records, metadata, warnings


def _collect_from_file_upload(
    vendor_id: str,
    files: list[dict],
    canonical_name: str = "",
    aliases: list[str] | None = None,
) -> tuple[list[RawSpendRecord], dict, list[str]]:
    """Read structured spend data from CSV or Excel files.

    Returns (records, upload_metadata, warnings).
    """
    records: list[RawSpendRecord] = []
    metadata: dict = {}
    warnings: list[str] = []
    aliases = aliases or []

    for file_info in files:
        file_id = file_info.get("file_id", "unknown")
        path_str = file_info.get("path", "")
        path = Path(path_str) if path_str else None

        if path is None or not path.exists():
            warnings.append(f"FILE_PARSE_ERROR_{file_id}")
            metadata[file_id] = {"filename": path_str, "rows": 0, "errors": [f"FILE_NOT_FOUND"]}
            continue

        suffix = path.suffix.lower()
        if suffix not in (".csv", ".xlsx", ".xls"):
            warnings.append(f"FILE_PARSE_ERROR_{file_id}")
            metadata[file_id] = {"filename": path.name, "rows": 0, "errors": ["UNSUPPORTED_FORMAT"]}
            continue

        try:
            rows_data = _read_file_rows(path, suffix)
        except Exception as exc:
            warnings.append(f"FILE_PARSE_ERROR_{file_id}")
            metadata[file_id] = {"filename": path.name, "rows": 0, "errors": [str(exc)]}
            continue

        if not rows_data:
            warnings.append(f"EMPTY_FILE_{file_id}")
            metadata[file_id] = {"filename": path.name, "rows": 0, "errors": []}
            continue

        headers = rows_data[0]
        col_map = _map_headers(headers)
        data_rows = rows_data[1:]

        if not data_rows:
            warnings.append(f"EMPTY_FILE_{file_id}")
            metadata[file_id] = {"filename": path.name, "rows": 0, "errors": []}
            continue

        file_records: list[RawSpendRecord] = []
        for row_idx, row in enumerate(data_rows):
            def _get(canonical: str) -> str | None:
                idx = col_map.get(canonical)
                if idx is None or idx >= len(row):
                    return None
                val = str(row[idx]).strip()
                return val if val else None

            amount_raw = _get("amount") or ""
            currency_raw = _get("currency")
            amount_usd, currency_code, curr_warnings = _normalise_currency(amount_raw, currency_raw)
            warnings.extend(curr_warnings)

            vendor_name_in_row = _get("vendor")
            matched_id, confidence = _match_to_vendor(
                vendor_name_in_row, vendor_id, canonical_name, aliases
            )

            # payment_terms_days from column
            pt_raw = _get("payment_terms")
            payment_terms_days: int | None = None
            if pt_raw is not None:
                try:
                    payment_terms_days = int(pt_raw)
                except (ValueError, TypeError):
                    payment_terms_days = None

            rec = RawSpendRecord(
                source_id=file_id,
                arrival_mode=ArrivalMode.FILE_UPLOAD.value,
                trust_level=TrustLevel.USER_SUBMITTED.value,
                period_start=_get("period_start"),
                period_end=_get("period_end"),
                amount_raw=amount_raw,
                currency_raw=currency_code or currency_raw,
                amount_usd=amount_usd,
                category_raw=_get("category"),
                cost_centre=_get("cost_centre"),
                po_number=_get("po_number"),
                invoice_ref=_get("invoice_ref"),
                matched_vendor_id=matched_id,
                match_confidence=confidence,
                payment_terms_days=payment_terms_days,
            )
            file_records.append(rec)

        deduped, collapsed = _deduplicate_records(file_records)
        records.extend(deduped)
        errors: list[str] = []
        if collapsed:
            errors.append(f"COLLAPSED_{collapsed}_DUPLICATES")

        # Check match confidence ratios
        total = len(deduped)
        if total > 0:
            low_count = sum(1 for r in deduped if r.match_confidence == "LOW")
            unmatched_count = sum(1 for r in deduped if r.match_confidence == "UNMATCHED")
            if low_count / total >= 0.20:
                warnings.append("LOW_MATCH_CONFIDENCE_RECORDS")
            if unmatched_count > 0:
                warnings.append("UNMATCHED_RECORDS_PRESENT")

        metadata[file_id] = {"filename": path.name, "rows": total, "errors": errors}

    return records, metadata, warnings


def _read_file_rows(path: Path, suffix: str) -> list[list[str]]:
    """Read CSV or Excel file and return list of rows (including header)."""
    if suffix == ".csv":
        text = path.read_text(encoding="utf-8", errors="replace")
        reader = csv.reader(StringIO(text))
        return [list(row) for row in reader]
    else:
        # Excel — requires openpyxl
        try:
            import openpyxl
        except ImportError:
            raise ImportError("openpyxl is required for Excel file reading")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows: list[list[str]] = []
        for row in ws.iter_rows(values_only=True):
            rows.append([str(cell) if cell is not None else "" for cell in row])
        wb.close()
        return rows


def _collect_from_checkin(
    vendor_id: str,
    checkin_data: dict,
) -> tuple[list[RawSpendRecord], dict, list[str]]:
    """Parse a structured check-in response dict.

    Returns (records, checkin_metadata, warnings).
    """
    records: list[RawSpendRecord] = []
    warnings: list[str] = []

    today = date.today()
    year_start = today.replace(month=1, day=1).isoformat()
    ttm_start = today.replace(year=today.year - 1).isoformat()
    today_iso = today.isoformat()

    payment_terms_days: int | None = None
    raw_pt = checkin_data.get("payment_terms_days")
    if raw_pt is not None:
        try:
            payment_terms_days = int(raw_pt)
        except (ValueError, TypeError):
            payment_terms_days = None

    currency_raw = checkin_data.get("currency")

    _KNOWN_KEYS = {
        "spend_ytd", "spend_ttm", "currency", "contract_ref",
        "contract_expiry", "payment_terms_days", "po_coverage", "notes",
    }

    source_id = f"checkin_{vendor_id}"

    def _make_record(amount_raw: str, period_start: str, po_ref: str | None) -> RawSpendRecord:
        amount_usd, currency_code, curr_warnings = _normalise_currency(amount_raw, currency_raw)
        warnings.extend(curr_warnings)
        return RawSpendRecord(
            source_id=source_id,
            arrival_mode=ArrivalMode.CHECK_IN.value,
            trust_level=TrustLevel.USER_SUBMITTED.value,
            period_start=period_start,
            period_end=today_iso,
            amount_raw=amount_raw,
            currency_raw=currency_code or currency_raw,
            amount_usd=amount_usd,
            category_raw=None,
            cost_centre=None,
            po_number=po_ref,
            invoice_ref=None,
            matched_vendor_id=vendor_id,
            match_confidence="HIGH",
            payment_terms_days=payment_terms_days,
        )

    if "spend_ytd" in checkin_data:
        amount_raw = str(checkin_data["spend_ytd"])
        records.append(_make_record(amount_raw, year_start, checkin_data.get("contract_ref")))

    if "spend_ttm" in checkin_data:
        amount_raw = str(checkin_data["spend_ttm"])
        records.append(_make_record(amount_raw, ttm_start, checkin_data.get("contract_ref")))

    # Warn on unknown keys
    for key in checkin_data:
        if key not in _KNOWN_KEYS:
            warnings.append(f"UNKNOWN_CHECKIN_KEY_{key}")

    metadata: dict = {
        source_id: {
            "po_coverage":    checkin_data.get("po_coverage"),
            "notes":          checkin_data.get("notes"),
            "contract_expiry": checkin_data.get("contract_expiry"),
            "fields_provided": list(checkin_data.keys()),
        }
    }

    return records, metadata, warnings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_structured_data(
    vendor_id: str,
    programme_id: str,
    arrival_modes: list[str] | None = None,
    connector_config: dict | None = None,
    uploaded_files: list[dict] | None = None,
    checkin_data: dict | None = None,
) -> StructuredDataBundle:
    """Collect raw spend records from all available sources.

    When arrival_modes is None, attempts modes in order:
    CONNECTOR → FILE_UPLOAD → CHECK_IN.

    Never raises — all failures become collection_warnings.
    """
    if arrival_modes is None:
        modes_to_try = [
            ArrivalMode.CONNECTOR.value,
            ArrivalMode.FILE_UPLOAD.value,
            ArrivalMode.CHECK_IN.value,
        ]
    else:
        modes_to_try = arrival_modes

    all_records: list[RawSpendRecord] = []
    connector_metadata: dict = {}
    upload_metadata: dict = {}
    checkin_metadata: dict = {}
    all_warnings: list[str] = []
    modes_used: list[str] = []

    for mode in modes_to_try:
        if mode == ArrivalMode.CONNECTOR.value:
            if not connector_config:
                all_warnings.append("NO_CONNECTOR_CONFIG")
                connector_metadata["status"] = "NO_CONFIG"
                continue
            recs, meta, warns = _collect_from_connectors(vendor_id, programme_id, connector_config)
            all_records.extend(recs)
            connector_metadata.update(meta)
            all_warnings.extend(warns)
            if recs:
                modes_used.append(mode)

        elif mode == ArrivalMode.FILE_UPLOAD.value:
            if not uploaded_files:
                continue
            recs, meta, warns = _collect_from_file_upload(vendor_id, uploaded_files)
            all_records.extend(recs)
            upload_metadata.update(meta)
            all_warnings.extend(warns)
            if recs:
                modes_used.append(mode)

        elif mode == ArrivalMode.CHECK_IN.value:
            if not checkin_data:
                continue
            recs, meta, warns = _collect_from_checkin(vendor_id, checkin_data)
            all_records.extend(recs)
            checkin_metadata.update(meta)
            all_warnings.extend(warns)
            if recs:
                modes_used.append(mode)

    if not all_records:
        all_warnings.append("NO_DATA_ANY_MODE")

    return StructuredDataBundle(
        vendor_id=vendor_id,
        programme_id=programme_id,
        collected_at=_now_iso(),
        arrival_modes_used=modes_used,
        raw_spend_records=all_records,
        connector_metadata=connector_metadata,
        upload_metadata=upload_metadata,
        checkin_metadata=checkin_metadata,
        collection_warnings=list(dict.fromkeys(all_warnings)),  # deduplicate preserving order
    )
