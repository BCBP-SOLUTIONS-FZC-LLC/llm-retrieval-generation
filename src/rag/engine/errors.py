"""What the engine raises. It knows nothing of Temporal (WB-7); the activity wrappers translate.

Every message is fixed and content-free: an exception text can reach Temporal history, which
purge and erasure do not reach (ASYNC-12, workers LLD P0-4).
"""

from __future__ import annotations


class EngineError(Exception):
    """Base of every error the engine raises."""


class RunGone(EngineError):  # noqa: N818
    """The run's row no longer exists: a purge or an offboarding erased it (ART-9, EVT-15).

    Not a failure. The workflow ends quietly with a metric and pages nobody; there is nothing left
    to record a failure on (workers LLD P0-6).
    """

    def __init__(self) -> None:
        super().__init__("run_gone")


class ScopeRevoked(EngineError):  # noqa: N818
    """Re-resolved scope is empty: the principal can no longer read what the run needs."""

    def __init__(self) -> None:
        super().__init__("scope_revoked")


class QuotaRefused(EngineError):  # noqa: N818
    """The Gateway refused for quota. A result, not a failure: the run suspends (WP-8)."""

    def __init__(self) -> None:
        super().__init__("quota_refused")


class ResidencyRefused(EngineError):  # noqa: N818
    """No capacity in the residency the tenant requires. Terminal (WR-1)."""

    def __init__(self) -> None:
        super().__init__("residency_refused")


class SchemaViolation(EngineError):  # noqa: N818
    """A model answer broke the schema it was asked to follow. Terminal (WP-8)."""

    def __init__(self) -> None:
        super().__init__("schema_violation")


class InferenceUnavailable(EngineError):  # noqa: N818
    """The Gateway or a backend could not answer. Retryable up to the activity's attempts."""

    def __init__(self) -> None:
        super().__init__("inference_unavailable")


class RunTerminal(EngineError):  # noqa: N818
    """The run already ended (cancelled, failed or complete) before this write: nothing was written.

    The API service or a purge got there first and its outcome stands (WB-6). The activity returns
    ``skipped_terminal`` and the workflow stops.
    """

    def __init__(self) -> None:
        super().__init__("run_terminal")


class ContentUnavailable(EngineError):  # noqa: N818
    """A section's stored text could not be read, or no longer matches its digest (ASV-4).

    Retryable: the object store may recover. The message never names the section or its text.
    """

    def __init__(self) -> None:
        super().__init__("content_unavailable")


class ProfileUnavailable(EngineError):  # noqa: N818
    """The run's Domain Profile cannot be served now (Tender outage). Retryable; never a default.

    Generation and extraction never run on the platform default profile, so an outage is a wait
    and a retry, not a degraded run (Tender, 27 Nov 2026).
    """

    def __init__(self) -> None:
        super().__init__("profile_unavailable")


class NothingToMint(EngineError):  # noqa: N818
    """No section has a version, so there is no document to roll up. The workflow fails the run."""

    def __init__(self) -> None:
        super().__init__("nothing_to_mint")
