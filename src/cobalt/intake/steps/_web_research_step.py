"""Web research step — calls Research Agent and returns raw text with confidence."""

from cobalt.agents.research_agent import ResearchAgent
from cobalt.intake.steps import StepResult


def run(
    vendor_name: str,
    comparison_key: str,
    tier: str,
    research_agent: ResearchAgent,
    country_hint: str | None = None,
) -> StepResult:
    step_name = f"WEB_RESEARCH_{tier}"
    try:
        research_text = research_agent.web_research(vendor_name, tier, country_hint)
        confidence = 0.60 if research_text else 0.15
        return StepResult(
            step=step_name,
            success=True,
            early_exit=False,
            exit_status=None,
            data={"research_text": research_text, "confidence": confidence},
            note="",
        )
    except Exception as exc:
        return StepResult(
            step=step_name,
            success=False,
            early_exit=False,
            exit_status=None,
            data={},
            note=f"web_research failed: {exc}",
        )
