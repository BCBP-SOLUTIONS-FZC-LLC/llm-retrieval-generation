"""The gate and signal rules of W-3, W-4, W-5, W-7 and W-8 (LLD §5.7.14, §9.4, §13.11).

Each function checks that the run is in a state the action is valid for and that the caller's
guard (``outline_version``) matches, raising ``409 WORKFLOW_STATE_CONFLICT`` otherwise, then
applies the run's own machine. RUN-7: the only writer of ``approved`` / ``revising`` and of the
``outline_version`` increment is this service, and only here.
"""

from __future__ import annotations

from rag.domain.generation.run import GenerationRun
from rag.domain.shared.enums import OutlineState, RunFailureReason, RunStatus
from rag.domain.shared.errors import DomainError, ErrorCode

RESTARTABLE = frozenset({RunStatus.SUSPENDED, RunStatus.AWAITING_OUTLINE_APPROVAL})
"""W-8: only a run waiting on a person can have lost its workflow in a way an operator restarts."""


def conflict(detail: str) -> DomainError:
    """``409 WORKFLOW_STATE_CONFLICT``."""
    return DomainError(ErrorCode.WORKFLOW_STATE_CONFLICT, detail)


def _awaiting(run: GenerationRun, outline_version: int) -> None:
    if (
        run.status is not RunStatus.AWAITING_OUTLINE_APPROVAL
        or run.outline_state is not OutlineState.READY
    ):
        raise conflict("The run is not awaiting outline approval.")
    if run.outline_version != outline_version:
        raise conflict("outline_version is not the outline the run holds.")


def already_approved(run: GenerationRun, outline_version: int) -> bool:
    """A repeat of W-3 whose first call committed but whose signal was lost (re-signal, 202)."""
    return (
        run.outline_state is OutlineState.APPROVED
        and run.outline_version == outline_version
        and run.status in {RunStatus.GENERATING, RunStatus.SUSPENDED}
    )


def already_revised(run: GenerationRun, outline_version: int) -> bool:
    """A repeat of W-4 whose first call committed but whose signal was lost (re-signal, 202).

    The row holds ``revising`` at the incremented version and the outline activity has not yet
    picked it up (it moves ``revising -> building``).
    """
    return (
        run.status is RunStatus.OUTLINE_BUILDING
        and run.outline_state is OutlineState.REVISING
        and run.outline_version == outline_version + 1
    )


def approve(run: GenerationRun, outline_version: int) -> None:
    """W-3: ``ready -> approved``, run to ``generating`` at ``stage1``."""
    _awaiting(run, outline_version)
    run.approve_outline()


def revise(run: GenerationRun, outline_version: int) -> None:
    """W-4: ``ready -> revising``, ``outline_version`` + 1, run back to ``outline_building``."""
    _awaiting(run, outline_version)
    run.revise_outline()


def cancel(run: GenerationRun) -> None:
    """W-5: a terminal run cannot be cancelled again (the caller handles ``already cancelled``)."""
    if run.is_terminal:
        raise conflict(f"The run is {run.status.value}.")
    run.cancel()


def resume(run: GenerationRun) -> None:
    """W-7: only a suspended run resumes.

    The LLD also names ``409`` where quota is still exhausted. This service holds no quota
    state and the Gateway exposes no reservation API (LD-13), so that case cannot be answered
    here: a resumed run the Gateway still refuses suspends again (§13.11).
    """
    if run.status is not RunStatus.SUSPENDED:
        raise conflict("The run is not suspended.")
    run.resume()


def require_restartable(run: GenerationRun) -> None:
    """W-8: the run is suspended or awaiting outline approval."""
    if run.status not in RESTARTABLE:
        raise conflict("The run is not suspended or awaiting outline approval.")


def resumable_on_quota(run: GenerationRun) -> bool:
    """Whether the quota-restored consumer may signal the run (LD-70)."""
    return (
        run.status is RunStatus.SUSPENDED and run.failure_reason is RunFailureReason.QUOTA_EXHAUSTED
    )
