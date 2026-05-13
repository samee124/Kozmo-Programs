# Campaign Manager Specification

## Role
Campaign lifecycle execution.
Manages stages, dispatches communications, records outcomes.
Not a separate running process — runs as function call inside VW Agent.

## State Model
Stateful via CAMP-{id}.md files.
VW Agent creates campaign file and delegates execution tracking.

## APE Pattern Per Campaign Action
ANALYZE: Read CAMP-{id}.md current step + completion conditions
PLAN:    Check completion condition (deterministic — no LLM in V1)
EXECUTE: Advance campaign state, update file, return result

## Campaign Status Transitions
CREATED → ACTIVE:      first action picked up
ACTIVE → CLOSED_WON:   completion condition met
ACTIVE → CLOSED_PARTIAL: some progress but timed out
ACTIVE → TIMED_OUT:    no progress, max attempts exhausted

## Completion Conditions
ERP_INTEGRATION:   spend.status == OBSERVED
CONTRACT_DISCOVERY: contract.status == OBSERVED
OWNER_CHECKIN:     all blocking questions answered OR timed out
MVC_CHECKIN:       at least 1 response OR timed out

## Hard Rules
RULE 1: Never calls LLM in V1.
RULE 2: Never writes evidence files.
RULE 3: Never writes cost_file files.
RULE 4: Does not communicate with vendors directly.
