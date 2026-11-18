"""Why a trial failed: one failure category per trial, shared by the driver, the store and reports."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skill_arena.models import GraderVerdict, TrialResult

FAILURE_KINDS: tuple[str, ...] = (
    "passed",
    "wrong",
    "interrupted",
    "anti_cheat",
    "craft",
    "infra",
    "grader_error",
)

# Same signals agent-relay's QUOTA_RE looks for, plus provider wording for auth and billing errors.
QUOTA_OR_RATE_LIMIT_RE = re.compile(
    r"\b429\b|rate[ _-]?limit|usage[ _-]?limit|quota|too many requests|overloaded|insufficient[ _]credit|"
    r"credit balance is too low|exceeded your current",
    re.IGNORECASE,
)
_INTERRUPT_MARKERS = ("timeout", "deadline", "interrupted")


def is_interrupted(relay_status: str | None, verdict_summary: str | None) -> bool:
    """True when the agent was stopped by the time limit instead of finishing on its own."""
    status = (relay_status or "").lower()
    if any(marker in status for marker in _INTERRUPT_MARKERS):
        return True
    return (verdict_summary or "").startswith("AGENT TIMEOUT")


def looks_like_quota_or_rate_limit(*texts: str | None) -> bool:
    return any(t and QUOTA_OR_RATE_LIMIT_RE.search(t) for t in texts)


def grading_failure_kind(verdict: GraderVerdict, *, interrupted: bool = False) -> str:
    """Category from the grader's own evidence (no knowledge of the agent run beyond the time limit)."""
    if verdict.passed:
        return "passed"
    if not verdict.anti_cheat_clean or verdict.leaked_processes:
        # Leaked background processes break the harness rules just like test tampering does.
        return "anti_cheat"
    if interrupted or verdict.exit_code == 124 and verdict.summary.startswith("AGENT TIMEOUT"):
        return "interrupted"
    if verdict.summary.startswith("GRADER ERROR"):
        return "grader_error"
    tests_ok = verdict.exit_code == 0
    if tests_ok and (verdict.sloplint_fails or verdict.sloplint_warns):
        return "craft"
    return "wrong"


def classify_trial_failure(
    verdict: GraderVerdict,
    *,
    interrupted: bool,
    is_simulated: bool = False,
    relay_status: str | None = None,
    agent_texts: tuple[str | None, ...] = (),
    agent_produced_output: bool = True,
    relay_wrote_log: bool = True,
) -> str:
    """Full category including infrastructure failures of the agent CLI or relay.

    ``infra`` means the agent could not really try: relay never wrote its log (setup or usage error),
    the relay step failed with a quota / rate-limit message, or the agent produced neither a reply nor a
    file change. Quota wording is only trusted when the relay step itself failed, because a successful
    agent may well talk about rate limits (one built-in task is a rate limiter).
    """
    kind = grading_failure_kind(verdict, interrupted=interrupted)
    if kind in {"passed", "anti_cheat", "interrupted", "grader_error"} or is_simulated:
        return kind
    if not relay_wrote_log:
        return "infra"
    status = (relay_status or "").lower()
    relay_failed = bool(status) and not status.startswith(("ok", "partial"))
    if relay_failed and looks_like_quota_or_rate_limit(relay_status, *agent_texts):
        return "infra"
    if not agent_produced_output:
        return "infra"
    return kind


def effective_failure_kind(trial: TrialResult) -> str:
    """Stored category, or one derived from the grader verdict for rows written before categories existed."""
    if trial.verdict.failure_kind:
        return trial.verdict.failure_kind
    if trial.passed:
        return "passed"
    return grading_failure_kind(
        trial.verdict, interrupted=is_interrupted(trial.usage.relay_status, trial.verdict.summary)
    )
