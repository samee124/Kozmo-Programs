# Research Agent Specification

## Role
Collects raw evidence. Returns raw data.
Stops at collection. Never interprets.

## The Absolute Rule
Research Agent returns RAW data only.
Analysis Agent extracts meaning from that raw data.

Document extraction: Research Agent fetches PDF text.
                     Analysis Agent extracts commercial terms from text.
Web research: Research Agent returns raw search text.
              Analysis Agent structures entity from that text.

## State Model
Stateless per call.
Receives task. Returns raw evidence dict.
No files written except connector logs.

## Public Methods

### web_research(vendor_name, tier, country_hint) → str
  Calls Brave Search API (via cobalt.core.search). Returns raw search result text.
  Tiers: FAST (1 query) / STANDARD (1 richer query) / DEEP (2 queries)
  Results cached (shelve, TTL=30 days).
  Empty string if the search provider is not configured or returns no results.

### erp_batch_scan(all_keys) → dict[str, ErpSignal]
  ONE batch call to ERP for all vendor keys.
  Returns {comparison_key: ErpSignal} dict.
  Empty dict if ERP not configured.

### fetch_pdf_text(file_path) → str
  Read PDF from local path or Google Drive path.
  Extract raw text using pypdf.
  Return raw text string.
  Empty string if PDF unreadable.
  DOES NOT extract structured fields — returns raw text only.

### fetch_google_drive_docs(folder_path) → list[dict]
  List all PDFs in Google Drive folder.
  For each: fetch_pdf_text() and return.
  Returns list of {file_path, filename, raw_text, size_kb}

### ap_batch_scan(all_keys) → dict[str, ApSignal]
  ONE batch call to AP system for invoice counts and patterns.
  Returns {comparison_key: ApSignal} dict.
  Empty dict if AP not configured.

## Files Research Agent Writes
connectors/logs/{tool}.log ONLY.
No workspace files. No evidence files. No plan files.

## Hard Rules
RULE 1: Returns raw data only. Never structured fields.
RULE 2: fetch_pdf_text returns text. Analysis Agent extracts terms.
RULE 3: On any connector failure: log error, return empty, continue.
RULE 4: Search results cached before returning
RULE 5: ERP and AP scans are always batch — never per-vendor.
RULE 6: Never calls LLM. Never interprets results.

## Failure Handling
Any connector failure: caught, logged to connector log,
empty result returned. Never raises to Orchestrator.
