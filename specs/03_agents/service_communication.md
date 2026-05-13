# Communication Service Specification
# Read this before touching src/Cobalt/agents/communication_service.py

## Role
NOT an agent. A stateless service.
Sends emails, check-ins, survey forms.
Logs every outbound message.
Tracks 48hr gestation clock.
NEVER makes intelligence decisions.

## State Model
Truly stateless. Receives payload, sends message, writes log.
Gestation tracking is via DB (reply_deadline field) and CI-{id}.md status field.

## APE Pattern (per dispatch)
ANALYZE: receive campaign payload from VWAgent
PLAN:    none — told exactly what to send
EXECUTE: send via configured channel, write log, set DB deadline

## Public Methods

### dispatch_checkin()
def dispatch_checkin(ci_id: str, vendor_id: str, programme_id: str,
                     questions: list[str], stakeholder: str) -> DispatchResult:
  Compose email/Slack/Teams message with questions.
  Send via configured channel (email_config.yaml).
  Write CI-{id}.md with status=DISPATCHED, dispatched_at=now().
  Update DB: status=CHECKIN_SENT, reply_deadline=now()+48hr.
  Log in communication/checkins/CI-{id}.md.
  Returns: DispatchResult(sent=True/False, channel, dispatched_at)

### send_reminder()
def send_reminder(ci_id: str, vendor_id: str) -> DispatchResult:
  Called by SLA watcher at T+24.
  Re-send to same contact with reminder framing.
  Append T+24 REMINDER_SENT to CI-{id}.md gestation_log.

### send_escalation()
def send_escalation(ci_id: str, vendor_id: str, escalate_to: str) -> DispatchResult:
  Called by SLA watcher at T+72.
  Send to next stakeholder level.
  Append T+72 ESCALATED to CI-{id}.md gestation_log.

## CI-{id}.md Schema
checkin_id:         str         # CI-{id}
vendor_id:          str
campaign_id:        str
questions:          list[str]   # exact questions sent
stakeholder:        str
channel:            str         # EMAIL / SLACK / TEAMS
status:             str         # DRAFT / APPROVED / DISPATCHED / RESPONSE_RECEIVED / TIMED_OUT
dispatched_at:      str | None
response_received_at: str | None
response_raw:       str | None
pcs_before:         int | None
pcs_after:          int | None
pcs_delta:          int | None
evidence_created:   list[str]   # ev-{id} refs created from response
gestation_log:      list[dict]  # [{timestamp, event}]

## 48hr Gestation Schedule
T+0:   DISPATCHED              sent to vendor
T+24:  REMINDER_SENT           SLA watcher fires reminder
T+48:  ATTENTION_FLAGGED       added to triage_queue for programme director
T+72:  ESCALATED               sent to manager of original contact
T+96:  TIMED_OUT               CI marked TIMED_OUT, campaign proceeds with inference

## What Communication Service Writes
communication/checkins/CI-{id}.md   (creates on dispatch, updates on each event)

## What Communication Service NEVER Does
NEVER makes decisions about what to ask (VWAgent decides questions).
NEVER processes responses (VWAgent processes on next tick).
NEVER writes evidence files.
NEVER calls LLM.

## Failure Handling
Channel unavailable: try secondary channel. If all fail: triage item created,
                     human must deliver manually.
CI file write fail: raise RecoverableError (VWAgent retries).
DB update fail: log warning, continue (file is source of truth).
