# Plan Writer Specification

## Purpose
Writes plan files to the workspace file system.
Called by Orchestrator after Planning Agent returns a plan.
Planning Agent returns the plan. Plan Writer writes it.

## Location
src/Cobalt/workspace/plan_writer.py

## Functions

def write_programme_plan(
    programme_id: str,
    plan: ProgrammePlan,
) -> Path:
  Writes: workspace/{programme_id}/programme_run/programme_plan.md
  Called once per programme at intake start.

def write_investigation_plan(
    programme_id: str,
    candidate_key: str,
    profile: SignalProfile,
    plan: InvestigationPlan,
) -> Path:
  Writes: workspace/{programme_id}/programme_run/
          intake_plans/IP-{candidate_key}-{n}.md
  n = sequential number (001, 002, ...)
  Called by Orchestrator after Planning Agent returns InvestigationPlan.

def update_plan_step(
    plan_path: Path,
    step: str,
    result: StepResult,
) -> None:
  Updates the step_results section of an existing IP file.
  Called after each execution step completes.

def finalise_plan(
    plan_path: Path,
    outcome: IntakeResult,
) -> None:
  Writes the outcome section of the IP file.
  Called after all steps complete.

## Programme Plan File
workspace/{programme_id}/programme_run/programme_plan.md
Contains:
  programme_id, created_at, sources, batch_plan,
  expected_distribution, processing_status, outcomes

## Investigation Plan File
workspace/{programme_id}/programme_run/intake_plans/IP-{key}-{n}.md
Contains:
  intake_plan_id, candidate_raw, candidate_key,
  signals (SignalProfile summary),
  plan (InvestigationPlan),
  execution (step_results updated as steps run),
  outcome (filled at end)

## Resume Capability
On Orchestrator restart: read all IP-*.md files with outcome.status=PENDING.
Re-execute those plans from where they stopped.
This is the crash recovery mechanism.

## Hard Rules
RULE 1: Plan files are written by plan_writer via atomic_write().
RULE 2: Planning Agent returns plan dataclass. Never writes files directly.
RULE 3: IP files are created BEFORE execution starts, not after.
RULE 4: IP files updated after each step, not only at end.
