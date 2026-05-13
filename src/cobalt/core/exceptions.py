"""Custom exceptions for the Cobalt platform."""


class FileOwnershipViolation(Exception):
    """Raised when code attempts to overwrite an immutable field.

    Specifically: any write to entity.md that changes the ``input_name``
    field after the file has been created.  Caller must HALT immediately.
    """


class LedgerWriteError(Exception):
    """Raised when an append to a ledger file (e.g. ledger.md) fails.

    This is an unrecoverable condition — the caller must HALT immediately.
    No catch-and-continue is permitted.
    """


class LLMCallFailure(Exception):
    """Raised when an LLM call fails after all retry attempts are exhausted,
    or when the response cannot be decoded as valid JSON (when expect_json=True).
    """


class SchemaValidationError(Exception):
    """Raised when file content fails schema validation before the atomic
    commit step.  The .tmp file is deleted and the original is left untouched.
    """


class ConnectorFailure(Exception):
    """Raised when an external connector (ERP, registry, sanctions) returns
    an error or times out.  This is a recoverable condition — the queue may
    retry the operation.
    """


class IntakeError(Exception):
    """Raised when the intake pipeline fails to process a single candidate
    entry.  Recoverable — other entries continue to be processed.
    """


class WorkspaceCreationError(Exception):
    """Raised when workspace directory or file creation fails during the
    workspace initialisation step.
    """


class HaltError(Exception):
    """Raised to signal an unrecoverable condition that requires the agent
    to stop immediately.  Nothing should catch this except the top-level
    process boundary.
    """


class RecoverableError(Exception):
    """Raised for transient failures that the queue infrastructure may safely
    retry.  The operation that raised this exception has not partially
    committed any state.
    """


class WorkflowParseError(Exception):
    """Raised when workflow.json or state.json is malformed or fails schema validation."""


class WorkflowSaveError(Exception):
    """Raised when atomic_write fails during WorkflowDefinition or ExecutionState save."""


class InvalidConditionExpression(Exception):
    """Raised when a workflow step condition string is syntactically invalid or
    uses disallowed constructs (e.g. function calls, eval-able expressions).
    """


class StepIdCollision(Exception):
    """Raised when apply_revision attempts to add a step_id that already exists
    in the set of completed (DONE/FAILED/SKIPPED) steps.
    """


class ReplanLimitExceeded(Exception):
    """Reserved for explicit policy callers (V3+). The RuntimeEngine never raises this —
    it downgrades excess REPLAN decisions to CONTINUE and logs at WARNING.
    """


class StepRegistryMiss(Exception):
    """Raised by RuntimeEngine._execute_step when the step_type is not found in
    the step registry. This is a configuration bug — the step is recorded as
    failed and the engine continues to the next runnable step.
    """


class WorkflowCreationError(Exception):
    """Raised by PlanningAgent.create_workflow when the workflow_type is unsupported
    or required context fields are missing (e.g. vendor_id for ENRICHMENT workflows).
    """


class ReplanLLMError(Exception):
    """Raised internally by PlanningAgent.replan when the LLM response is invalid
    or missing required fields. The caller catches this and returns the fallback
    ROUTE_TO_HUMAN step. External callers should not see this exception.
    """


class CheckpointParseError(Exception):
    """Raised when checkpoint.json is present but cannot be parsed (malformed JSON
    or missing required fields). The intake run cannot safely resume.
    """


class EnrichmentSchemaError(Exception):
    """Raised when an enrichment schema dataclass receives an invalid enum value
    in __post_init__.  Callers should treat this as a programming error (bad data
    passed at construction time) rather than a runtime recoverable condition.
    """


class EnrichmentReadinessReadError(Exception):
    """Raised when a workspace file required by enrichment_readiness_check exists
    but contains malformed YAML that cannot be parsed.  The enrichment run cannot
    safely proceed.
    """


class EnrichedProfileWriteError(Exception):
    """Raised when the atomic write of vendor_profile.md fails.

    The caller (create_enriched_profile) catches this, preserves the prior
    profile, records a FAILED_ENRICHMENT ledger entry, and returns a
    FAILED_ENRICHMENT EnrichedProfileResult.
    """


class EnrichmentOrchestrationError(Exception):
    """Raised when the enrichment orchestrator encounters an unrecoverable
    configuration or infrastructure error (e.g. workflow creation fails
    after all retries).  run_enrichment catches this and returns a FAILED
    EnrichmentRunResult rather than propagating.
    """


class BraveSearchError(Exception):
    """Raised when the Brave Search API call fails — missing API key, HTTP error,
    or transport failure.  Callers (collectors) catch this and record a collection
    flag; the workflow continues with reduced evidence.
    """


class CompaniesHouseError(Exception):
    """Raised when the Companies House API call fails — missing API key, HTTP error,
    rate limit (429), or transport failure.  Callers catch this and record a
    REGISTRY_FETCH_ERROR flag; the workflow continues with reduced evidence.
    """


class SecEdgarError(Exception):
    """Raised when the SEC EDGAR API call fails — HTTP error, transport failure,
    or malformed response.  Callers catch this and record a FINANCIAL_FETCH_ERROR flag;
    the workflow continues with reduced evidence.
    """


class GleifError(Exception):
    """Raised when the GLEIF LEI registry API call fails — rate limit (429),
    transport failure, or malformed response.  404 is NOT an error — it means
    'no parent relationship exists' and is returned as None.
    """


class WikidataError(Exception):
    """Raised when the Wikidata API call fails — rate limit (429), transport
    failure, or malformed response.  404 is returned as None (no data).
    Callers catch this and record a WIKIDATA_FETCH_ERROR flag; the workflow
    continues with reduced evidence.
    """


class OpenCorporatesError(Exception):
    """Raised when the OpenCorporates API call fails — missing token, auth
    failure (401/403), rate limit (429), or transport failure.  404 is
    returned as None (no company found).  Callers catch this and record a
    REGISTRY_FETCH_ERROR flag; the workflow continues with reduced evidence.
    """
