# atomic_write Specification

## Location
src/Cobalt/core/atomic_write.py

## Signatures
def atomic_write(path, content, schema_validator=None, vendor_id=None, programme_id=None) -> None
def append_md(path, entry, vendor_id=None, programme_id=None) -> None

## Rules
- Write to .tmp first, then tmp.replace(path) (Windows-safe)
- IMMUTABLE_FILENAMES = {"entity.md"}
  input_name change on entity.md → FileOwnershipViolation
- schema_validator runs on .tmp before replace
  Failure: delete .tmp, raise SchemaValidationError
- sync_to_db called after replace (warning only if fails)
- dict content: yaml.dump(). str content: as-is.
- Parent dirs created automatically
- append_md: OSError → raise LedgerWriteError
