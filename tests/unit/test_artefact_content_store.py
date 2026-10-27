"""S3 content store: tenant prefix enforcement, KMS context, batched prefix delete (ART-9)."""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from rag.domain.shared.errors import DomainError, ErrorCode
from rag.infrastructure.storage import artefact_content_store as acs

TENANT, ARTEFACT, SECTION = uuid4(), uuid4(), uuid4()


def _store(client: Any, kms: str | None = None) -> acs.S3ArtefactContentStore:
    return acs.S3ArtefactContentStore("bkt", kms_key_id=kms, client=client)


def test_keys_live_under_the_tenant_and_artefact_prefix() -> None:
    """Section and diagnostic keys sit under ``tenants/<t>/artefacts/<a>/``."""
    s = _store(MagicMock())
    key = s.section_key(TENANT, ARTEFACT, SECTION, 3)
    assert key == f"tenants/{TENANT}/artefacts/{ARTEFACT}/sections/{SECTION}/v3.md"
    assert s.diagnostic_key(TENANT, ARTEFACT, "d.json").endswith("/diagnostics/d.json")
    assert key.startswith(acs.artefact_prefix(TENANT, ARTEFACT))


@pytest.mark.asyncio
async def test_get_returns_the_object_bytes() -> None:
    """A key inside the prefix is read."""
    client = MagicMock()
    client.get_object.return_value = {"Body": MagicMock(read=lambda: b"text")}
    key = acs.tenant_prefix(TENANT) + "x"
    assert await _store(client).get(TENANT, key) == b"text"
    client.get_object.assert_called_once_with(Bucket="bkt", Key=key)


@pytest.mark.asyncio
async def test_get_refuses_a_foreign_key_and_maps_s3_errors() -> None:
    """Outside the prefix, and any client error, are ``ARTEFACT_CONTENT_UNAVAILABLE``."""
    client = MagicMock()
    with pytest.raises(DomainError) as foreign:
        await _store(client).get(TENANT, f"tenants/{uuid4()}/x")
    assert foreign.value.code == ErrorCode.ARTEFACT_CONTENT_UNAVAILABLE
    client.get_object.assert_not_called()
    client.get_object.side_effect = ClientError({"Error": {"Code": "500"}}, "GetObject")
    with pytest.raises(DomainError) as failed:
        await _store(client).get(TENANT, acs.tenant_prefix(TENANT) + "x")
    assert failed.value.code == ErrorCode.ARTEFACT_CONTENT_UNAVAILABLE


@pytest.mark.asyncio
async def test_put_without_kms_and_with_kms_context() -> None:
    """KMS puts carry the tenant in the encryption context; a foreign key is refused."""
    plain = MagicMock()
    key = acs.tenant_prefix(TENANT) + "k"
    await _store(plain).put(TENANT, key, b"d")
    plain.put_object.assert_called_once_with(Bucket="bkt", Key=key, Body=b"d")
    kms = MagicMock()
    await _store(kms, "alias/k").put(TENANT, key, b"d")
    extra = kms.put_object.call_args.kwargs
    assert extra["ServerSideEncryption"] == "aws:kms"
    assert extra["SSEKMSKeyId"] == "alias/k"
    assert json.loads(base64.b64decode(extra["SSEKMSEncryptionContext"])) == {
        "tenant_id": str(TENANT)
    }
    with pytest.raises(ValueError, match="tenant prefix"):
        await _store(plain).put(TENANT, "tenants/other/k", b"d")


@pytest.mark.asyncio
async def test_delete_prefix_batches_and_counts() -> None:
    """Pages are deleted in batches of 1000; the count is the objects removed."""
    client = MagicMock()
    pages = [{"Contents": [{"Key": f"k{i}"} for i in range(1500)]}, {}]
    client.get_paginator.return_value.paginate.return_value = pages
    assert await _store(client).delete_prefix(TENANT, ARTEFACT) == 1500
    assert client.delete_objects.call_count == 2
    client.get_paginator.return_value.paginate.assert_called_with(
        Bucket="bkt", Prefix=acs.artefact_prefix(TENANT, ARTEFACT)
    )
    await _store(client).delete_prefix(TENANT)
    assert client.get_paginator.return_value.paginate.call_args.kwargs["Prefix"] == (
        acs.tenant_prefix(TENANT)
    )
