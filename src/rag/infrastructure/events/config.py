"""Event configuration: ``eventcommon``'s own loaders for tuning, ``Settings`` for topology.

Every ``RunnerConfig``, ``SNSConfig`` and ``SQSConfig`` this service uses is built here and
nowhere else. Tuning (``OUTBOX_POLL_INTERVAL``, ``OUTBOX_BATCH_SIZE``, ``OUTBOX_MAX_ATTEMPTS``,
``OUTBOX_PUBLISH_TIMEOUT``, ``OUTBOX_CLAIM_LEASE_DURATION``, ``OUTBOX_DRAIN_TIMEOUT``,
``SQS_CONCURRENCY``, ``SQS_MAX_MESSAGES``, ``SQS_WAIT_SECONDS``, ``SQS_HANDLER_TIMEOUT``, ... the
library's variable names, durations written with a unit such as ``5s``) is parsed and validated by
``eventcommon.config`` (``load_outbox`` / ``load_sqs``), which replaces a bad value with a safe
default and a warning, logged once here.

What stays ours, because it is topology or a decision of this service and not tuning:

* the topic ARNs and queue URLs (two topics and six queues, each with its own setting, where the
  library's single ``SNS_TOPIC_ARN`` / ``SQS_QUEUE_URL`` cannot say which),
* the region and endpoint (also used by S3),
* the redrive threshold ``SQS_MAX_RECEIVE_COUNT`` (LD-86: the fourth receive dead-letters; a typo
  must fail at startup instead of silently turning forwarding off),
* two defaults that differ from the library's: a 60 s visibility timeout and a 30 s queue-depth
  gauge, applied only when the library's own variable is unset.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

from eventcommon.config import (
    load_outbox,
    load_sns,
    load_sqs,
    runner_config_from_env,
    sns_config_from_env,
    sqs_config_from_env,
    sqs_consumer_options,
)
from eventcommon.consumer import SQSConfig
from eventcommon.outbox import RunnerConfig
from eventcommon.publisher import SNSConfig
from fastapicommon.logging import get_logger
from pgcommon import Pool

from rag.infrastructure.config import ConfigError, Settings

log = get_logger(__name__)

SERVICE_VISIBILITY_TIMEOUT_SECONDS = 60.0
"""Longer than the library's 30 s: a handler runs a use case across two databases."""
SERVICE_QUEUE_DEPTH_INTERVAL_SECONDS = 30.0
"""The queue-depth gauge is on by default here (the library leaves it off)."""

# The outbox loader also runs pgcommon.config_from_env and merges its warnings; pool_config owns
# those (and logs them once), so they are not repeated here.
_DB_WARNING_PREFIX = "platform-pgcommon:"
_warned: set[str] = set()


def _log_warnings_once(warnings: list[str]) -> None:
    """The library asks for every configuration warning to be logged at startup."""
    for warning in warnings:
        if warning.startswith(_DB_WARNING_PREFIX) or warning in _warned:
            continue
        _warned.add(warning)
        log.warning("eventcommon_config_warning", warning=warning)


def _require_positive(**values: float) -> None:
    """The library accepts a non-positive duration or count; a busy loop is not a setting."""
    bad = {name: value for name, value in values.items() if value <= 0}
    if bad:
        raise ConfigError(f"event configuration must be positive: {bad}")


def runner_config(pool: Pool, publisher: Any) -> RunnerConfig:
    """The outbox relay's ``RunnerConfig``: all tuning from the library's ``OUTBOX_*`` variables."""
    env = load_outbox()
    _log_warnings_once(env.warnings)
    _require_positive(
        OUTBOX_POLL_INTERVAL=env.poll_interval,
        OUTBOX_BATCH_SIZE=env.batch_size,
        OUTBOX_MAX_ATTEMPTS=env.max_attempts,
        OUTBOX_PUBLISH_TIMEOUT=env.publish_timeout,
        OUTBOX_PUBLISH_CONCURRENCY=env.publish_concurrency,
    )
    return runner_config_from_env(env, pool, publisher, logger=log)


def sns_config(settings: Settings, topic_arn: str) -> SNSConfig:
    """One topic's ``SNSConfig``: the ARN, region and endpoint are this service's."""
    env = replace(
        load_sns(),
        topic_arn=topic_arn,
        region=settings.aws_region,
        endpoint_url=settings.aws_endpoint_url or None,
    )
    env.validate()
    return sns_config_from_env(env, logger=log)


def sqs_settings(settings: Settings, queue_url: str) -> tuple[SQSConfig, dict[str, Any]]:
    """One queue's ``SQSConfig`` and the keyword options of its ``SQSConsumer``.

    The caller adds what cannot come from the environment: the dead-letter publisher, the codec
    and (in tests) the client.
    """
    env = load_sqs()
    _log_warnings_once(env.warnings)
    env = replace(
        env,
        queue_url=queue_url,
        region=settings.aws_region,
        endpoint_url=settings.aws_endpoint_url or None,
        max_receive_count=settings.sqs_max_receive_count,
    )
    if "SQS_VISIBILITY_TIMEOUT" not in os.environ:
        env = replace(env, visibility_timeout=SERVICE_VISIBILITY_TIMEOUT_SECONDS)
    if "SQS_QUEUE_DEPTH_INTERVAL" not in os.environ:
        env = replace(env, queue_depth_interval=SERVICE_QUEUE_DEPTH_INTERVAL_SECONDS)
    env.validate()
    _require_positive(
        SQS_CONCURRENCY=env.concurrency, SQS_VISIBILITY_TIMEOUT=env.visibility_timeout
    )
    return sqs_config_from_env(env, logger=log), sqs_consumer_options(env)
