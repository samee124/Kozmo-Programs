# Source Processor Specification

## Purpose
First tool in intake. Ingests ALL input sources.
Produces unified candidate bundle with document links.

## Location
src/Cobalt/intake/source_processor.py

## Function Signatures

def process_sources(
    vendor_list_path: str | None,
    google_drive_path: str | None,
    research_agent: ResearchAgent,
    analysis_agent: AnalysisAgent,
) -> SourceProcessorResult:

## What It Processes

### Vendor List (Excel/CSV)
  - Read all rows
  - Extract vendor name from name column
  - Extract optional columns: spend, category, department, country
  - Each row = one RawCandidate

### Google Drive Documents (if configured)
  - Call Research Agent: fetch_google_drive_docs(path)
  - Research Agent returns [{file_path, filename, raw_text}]
  - For each document:
    Call Analysis Agent: extract_vendor_name_from_doc(raw_text, filename)
    Returns {vendor_name, legal_name, doc_type_hint, confidence}
  - Each document creates a DocRecord

### Document-to-Candidate Linking
  For each document with extracted vendor name:
    normalize(extracted_vendor_name) → comparison_key
    Find matching RawCandidate by comparison_key
    If match found: attach doc_id to that candidate
    If no match: create new RawCandidate from document
      (vendor discovered via contract but not in vendor list)

## SourceProcessorResult
@dataclass
class SourceProcessorResult:
    candidates:     list[RawCandidate]
    doc_registry:   dict[str, DocRecord]  # doc_id → DocRecord
    source_summary: SourceSummary

@dataclass
class RawCandidate:
    raw_name:        str
    source:          str   # VENDOR_LIST / DOCUMENT / BOTH
    source_refs:     list[str]
    linked_doc_ids:  list[str]
    spend_hint:      Decimal | None   # from Excel column
    category_hint:   str | None       # from Excel column
    department_hint: str | None

@dataclass
class DocRecord:
    doc_id:     str
    filename:   str
    file_path:  str
    raw_text:   str
    doc_type:   str   # MSA/SOW/NDA/BAA/AMENDMENT/INVOICE/UNKNOWN
    vendor_key: str | None   # comparison_key of linked candidate

## Hard Rules
RULE 1: Never writes files. Returns result only.
RULE 2: Research Agent fetches PDFs. Analysis Agent extracts names.
RULE 3: If vendor list missing: process documents only.
RULE 4: If Google Drive missing: process vendor list only.
RULE 5: Never fails entirely — missing source returns empty list for that source.
