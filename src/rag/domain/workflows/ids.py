"""Deterministic workflow identity and task-queue choice (LLD §9.2, RUN-2, WP-10)."""

from __future__ import annotations

from uuid import UUID

REGEN_PREFIX = "regen:"


def original_workflow_id(artefact_id: UUID) -> str:
    """An async original runs under its artefact's id (RUN-2)."""
    return str(artefact_id)


def regeneration_workflow_id(base_version_id: UUID) -> str:
    """A document regeneration runs under ``regen:{base_version_id}`` (RUN-2, LD-55).

    One artefact has many runs, so the artefact id would collide with the run that produced it;
    the base version belongs to exactly one artefact, so this id is unique and a repeat from the
    same base version computes the same id and is absorbed by the engine.
    """
    return f"{REGEN_PREFIX}{base_version_id}"


def task_queue_for(
    plan: str | None,
    residency_mode: str,
    *,
    default: str,
    enterprise: str,
    strict: str,
    enterprise_plans: frozenset[str] = frozenset({"enterprise"}),
) -> str:
    """The generation queue for the tenant (WP-10): strict residency, then enterprise, else shared.

    Strict residency wins over plan: a strict tenant needs a queue of its own whatever it pays.
    """
    if residency_mode != "standard":
        return strict
    if plan is not None and plan.lower() in enterprise_plans:
        return enterprise
    return default
