"""Tender, 27 Nov 2026: generation and extraction never run on the default profile."""

from __future__ import annotations

from uuid import uuid4

import pytest

from rag.application.packs.resolve import resolve_or_default
from rag.domain.packs.profile import PLATFORM_DEFAULT_BUNDLE, PackUnavailable
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.engine import ProfileUnavailable
from rag.engine.blocks import translate


class _Down:
    async def resolve(self, *_a: object, **_k: object) -> None:
        raise PackUnavailable("down")


async def test_search_and_answers_may_degrade_to_the_default_with_a_marker() -> None:
    bundle, markers = await resolve_or_default(_Down(), uuid4())  # type: ignore[arg-type]
    assert bundle is PLATFORM_DEFAULT_BUNDLE
    assert markers


async def test_generation_gets_a_retryable_503_never_the_default() -> None:
    with pytest.raises(DomainError) as caught:
        await resolve_or_default(_Down(), uuid4(), allow_default=False)  # type: ignore[arg-type]
    assert caught.value.code is ErrorCode.PACK_UNAVAILABLE
    assert isinstance(translate(caught.value), ProfileUnavailable)
