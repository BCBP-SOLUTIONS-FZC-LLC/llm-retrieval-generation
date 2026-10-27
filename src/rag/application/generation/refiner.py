"""The Instruction Refiner (LLD §10.2, §10.4, §10.5): flag-gated, 3 s deadline, degradable.

Task class ``refine-instruction``, temperature 0. Off by flag it does nothing and records
nothing (REF-2, kill switch). On, it always yields a result: a usable refinement, or the raw
instruction with ``needs_retrieval`` true and a marker (IR-1, §14.4). The caller writes the
``instruction_refinements`` row for every call that ran (REF-1).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from rag.application.retrieval.ports import NullObserver, PipelineObserver
from rag.application.shared.gateway import (
    TASK_REFINE_INSTRUCTION,
    GatewayContext,
    GatewayError,
    ModelGateway,
)
from rag.domain.generation import instruction as decide
from rag.domain.generation.facts import DocumentFact
from rag.domain.generation.instruction import RefinedInstruction
from rag.domain.packs.profile import PackBundle
from rag.domain.retrieval.plan import DegradationMarker

STAGE = "refine_instruction"
FALLBACK = "raw_instruction"


def principal_hash(tenant_id: UUID, principal_id: UUID | None) -> str:
    """The per-tenant salted one-way hash of the user (§4.6.2), as the query refiner's."""
    return hashlib.sha256(f"{tenant_id}:{principal_id}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RefineRun:
    """One refiner call that ran: its result, its latency and the markers it appends."""

    result: RefinedInstruction
    latency_ms: int
    pack_version: str
    markers: tuple[DegradationMarker, ...] = ()


class InstructionRefiner:
    """``refine`` through the Gateway; degrades to the raw instruction on any failure."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        enabled: bool,
        deadline_seconds: float,
        observer: PipelineObserver | None = None,
    ) -> None:
        self._gw, self._enabled, self._deadline = gateway, enabled, deadline_seconds
        self._obs = observer or NullObserver()

    async def refine(
        self,
        *,
        instruction: str,
        section_text: str,
        section_type: str,
        mandatory_clauses: Iterable[str],
        facts: Sequence[DocumentFact],
        bundle: PackBundle,
        ctx: GatewayContext,
    ) -> RefineRun | None:
        """``None`` where the flag is off (REF-2), else a ``RefineRun`` (degraded on failure)."""
        if not self._enabled:
            return None
        pack_version = bundle.domain_profile.version
        version = decide.refiner_version(pack_version)
        started = time.monotonic()
        user = json.dumps(
            {
                "instruction": instruction,
                "section_text": section_text,
                "section_type": section_type,
                "mandatory_clauses": sorted(mandatory_clauses),
                "established_facts": {
                    f.fact_key: f.value for f in facts if f.superseded_at is None
                },
            }
        )
        try:
            completion = await asyncio.wait_for(
                self._gw.complete(
                    TASK_REFINE_INSTRUCTION,
                    decide.PROMPT_TEMPLATE,
                    user,
                    ctx=ctx,
                    deadline_seconds=self._deadline,
                ),
                self._deadline,
            )
            result = decide.parse(instruction, completion.text, version, facts)
        except (GatewayError, TimeoutError, ValueError, KeyError, TypeError, AttributeError) as exc:
            reason = (
                exc.reason
                if isinstance(exc, GatewayError)
                else "deadline_exceeded"
                if isinstance(exc, TimeoutError)
                else "invalid_output"
            )
            self._obs.marker(STAGE, reason)
            elapsed = time.monotonic() - started
            self._obs.instruction_refiner(elapsed)
            return RefineRun(
                decide.degraded(instruction, version, reason),
                int(elapsed * 1000),
                pack_version,
                (DegradationMarker(STAGE, reason, FALLBACK),),
            )
        elapsed = time.monotonic() - started
        self._obs.instruction_refiner(elapsed)
        return RefineRun(result, int(elapsed * 1000), pack_version)
