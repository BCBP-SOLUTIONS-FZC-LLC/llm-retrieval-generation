"""The judge model's answers as verification reads them: a bad answer is "unavailable".

Tier 2 (entailment) and tier 3 (judge) call the Gateway and parse JSON. Anything that is not the
promised shape raises ``VerificationUnavailable`` so the ladder falls back, and a Gateway failure
is the same (VER-4).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rag.application.shared.gateway import Completion, GatewayContext, GatewayUnavailable
from rag.application.verification.gateway import GatewayEntailer, GatewayJudge
from rag.domain.verification.ports import VerificationUnavailable
from rag.domain.verification.tiers import EvidenceChunk

CTX = GatewayContext()
PASSAGE = EvidenceChunk("c-1", "Crane erection needs a lifting plan.")


class Judge:
    """A Gateway whose `complete` returns one scripted text, or raises."""

    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text, self.error, self.calls = text, error, 0

    async def complete(self, *_: Any, **__: Any) -> Completion:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return Completion(self.text, "judge-1")


def entailer(text: str = "", error: Exception | None = None) -> GatewayEntailer:
    return GatewayEntailer(Judge(text, error), CTX)  # type: ignore[arg-type]


def judge(text: str = "", error: Exception | None = None) -> GatewayJudge:
    return GatewayJudge(Judge(text, error), CTX)  # type: ignore[arg-type]


async def test_entailment_reads_a_score_between_zero_and_one() -> None:
    got = await entailer(json.dumps({"score": 0.8})).score("claim", PASSAGE)
    assert (got.score, got.model) == (0.8, "judge-1")


@pytest.mark.parametrize(
    "text",
    [
        "not json at all",  # the model did not answer in JSON
        json.dumps(["score", 1]),  # JSON, but not an object
        json.dumps({"score": 1.5}),  # outside [0, 1]
        json.dumps({"score": -0.1}),
        json.dumps({"score": True}),  # a bool is not a number here
        json.dumps({"score": "high"}),
        json.dumps({}),
    ],
)
async def test_an_entailment_answer_of_the_wrong_shape_is_unavailable(text: str) -> None:
    with pytest.raises(VerificationUnavailable):
        await entailer(text).score("claim", PASSAGE)


async def test_a_gateway_failure_is_unavailable_for_both_tiers() -> None:
    with pytest.raises(VerificationUnavailable):
        await entailer(error=GatewayUnavailable("down")).score("claim", PASSAGE)
    with pytest.raises(VerificationUnavailable):
        await judge(error=GatewayUnavailable("down")).judge("claim", [PASSAGE], [])


async def test_the_judge_reads_a_verdict_and_the_corrected_chunks() -> None:
    other = EvidenceChunk("c-2", "Other text.")
    answer = json.dumps({"supported": False, "corrected_chunk_ids": ["c-2", 3]})
    got = await judge(answer).judge("claim", [PASSAGE], [other])
    assert got.supported is False and got.corrected_chunk_ids == ("c-2", "3")
    # a corrected list that is not a list is ignored, not trusted
    odd = await judge(json.dumps({"supported": True, "corrected_chunk_ids": "c-2"})).judge(
        "claim", [PASSAGE], []
    )
    assert odd.supported is True and odd.corrected_chunk_ids == ()


@pytest.mark.parametrize("text", ["nope", json.dumps({"supported": "yes"}), json.dumps({})])
async def test_a_judge_verdict_that_is_not_a_boolean_is_unavailable(text: str) -> None:
    with pytest.raises(VerificationUnavailable):
        await judge(text).judge("claim", [PASSAGE], [])
