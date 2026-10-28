"""S3 adapter for section text (LLD 4.1): tenant-prefixed keys, KMS context has ``tenant_id``."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any
from uuid import UUID

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import (  # type: ignore[import-untyped]
    BotoCoreError,
    ClientError,
)
from fastapicommon.logging import get_logger

from rag.domain.shared.errors import DomainError, ErrorCode

log = get_logger(__name__)

_DELETE_BATCH = 1000


def tenant_prefix(tenant_id: UUID) -> str:
    """Every object of a tenant lives under this prefix."""
    return f"tenants/{tenant_id}/"


def artefact_prefix(tenant_id: UUID, artefact_id: UUID) -> str:
    """Every object of one artefact (ART-9 deletes by this prefix)."""
    return f"{tenant_prefix(tenant_id)}artefacts/{artefact_id}/"


def section_version_key(
    tenant_id: UUID, artefact_id: UUID, section_id: UUID, version_no: int
) -> str:
    """The ``content_ref`` an activity writes for a section version."""
    return f"{artefact_prefix(tenant_id, artefact_id)}sections/{section_id}/v{version_no}.md"


def _unavailable() -> DomainError:
    return DomainError(
        ErrorCode.ARTEFACT_CONTENT_UNAVAILABLE, "The section content is unavailable."
    )


class S3ArtefactContentStore:
    """``ArtefactContentStore`` over boto3 (sync client, run in a worker thread)."""

    def __init__(
        self,
        bucket: str,
        *,
        region: str | None = None,
        endpoint_url: str | None = None,
        kms_key_id: str | None = None,
        client: Any = None,
    ) -> None:
        self._bucket = bucket
        self._kms_key_id = kms_key_id or None
        self._s3 = client or boto3.client(
            "s3", region_name=region or None, endpoint_url=endpoint_url or None
        )

    def section_key(
        self, tenant_id: UUID, artefact_id: UUID, section_id: UUID, version_no: int
    ) -> str:
        """The ``content_ref`` of a section version (``ArtefactContentStore`` port)."""
        return section_version_key(tenant_id, artefact_id, section_id, version_no)

    def diagnostic_key(self, tenant_id: UUID, artefact_id: UUID, name: str) -> str:
        """``<artefact prefix>diagnostics/<name>``: deleted with the artefact (ART-9)."""
        return f"{artefact_prefix(tenant_id, artefact_id)}diagnostics/{name}"

    async def get(self, tenant_id: UUID, content_ref: str) -> bytes:
        """Read ``content_ref``, refusing any key outside the tenant's prefix."""
        if not content_ref.startswith(tenant_prefix(tenant_id)):
            log.error("content_ref_outside_tenant_prefix")
            raise _unavailable()
        try:
            obj = await asyncio.to_thread(self._s3.get_object, Bucket=self._bucket, Key=content_ref)
            data: bytes = await asyncio.to_thread(obj["Body"].read)
        except (ClientError, BotoCoreError) as exc:
            log.error("artefact_content_read_failed", error=type(exc).__name__)
            raise _unavailable() from exc
        return data

    async def put(self, tenant_id: UUID, key: str, data: bytes) -> None:
        """Write under the tenant prefix, SSE-KMS with ``tenant_id`` in the encryption context."""
        if not key.startswith(tenant_prefix(tenant_id)):
            raise ValueError("key is outside the tenant prefix")
        extra: dict[str, str] = {}
        if self._kms_key_id:
            context = json.dumps({"tenant_id": str(tenant_id)}, separators=(",", ":"))
            extra = {
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": self._kms_key_id,
                "SSEKMSEncryptionContext": base64.b64encode(context.encode()).decode(),
            }
        await asyncio.to_thread(
            self._s3.put_object, Bucket=self._bucket, Key=key, Body=data, **extra
        )

    async def delete_prefix(self, tenant_id: UUID, artefact_id: UUID | None = None) -> int:
        """Delete every object under the artefact's (or the tenant's) prefix."""
        prefix = (
            tenant_prefix(tenant_id)
            if artefact_id is None
            else artefact_prefix(tenant_id, artefact_id)
        )
        return await asyncio.to_thread(self._delete_prefix_sync, prefix)

    def _delete_prefix_sync(self, prefix: str) -> int:
        deleted = 0
        pages = self._s3.get_paginator("list_objects_v2").paginate(
            Bucket=self._bucket, Prefix=prefix
        )
        for page in pages:
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            for start in range(0, len(keys), _DELETE_BATCH):
                batch = keys[start : start + _DELETE_BATCH]
                self._s3.delete_objects(
                    Bucket=self._bucket, Delete={"Objects": batch, "Quiet": True}
                )
                deleted += len(batch)
        return deleted
