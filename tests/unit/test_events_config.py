"""Every event configuration comes from one module, with eventcommon's own tuning parsing.

Outbox and SQS tuning is read and validated by ``eventcommon.config`` (the library's variable names,
durations with a unit); topics, queues, region, the redrive threshold and two service defaults are
this service's decisions and win over the environment.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from eventcommon.config import ConfigValidationError
from tests.support_logs import RecordingLog

from rag.infrastructure.config import ConfigError, Settings
from rag.infrastructure.events import config as events_config
from rag.infrastructure.events.config import runner_config, sns_config, sqs_settings

QUEUE = "http://sqs.test/000000000000/tender-rag-q"
TOPIC = "arn:aws:sns:eu-west-1:000000000000:llm-index-events"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> RecordingLog:
    """No ambient library variables, a fresh once-per-process warning memory."""
    prefixes = ("OUTBOX_", "SQS_", "SNS_", "AWS_", "PG_")
    for key in [k for k in os.environ if k.startswith(prefixes) or k.endswith("DATABASE_URL")]:
        monkeypatch.delenv(key)
    monkeypatch.setattr(events_config, "_warned", set())
    rec = RecordingLog()
    monkeypatch.setattr(events_config, "log", rec)
    return rec


def test_the_relay_defaults_are_the_librarys(_clean_env: RecordingLog) -> None:
    cfg = runner_config(None, object())  # type: ignore[arg-type]
    assert (cfg.poll_interval, cfg.batch_size, cfg.max_attempts) == (5.0, 50, 5)
    assert cfg.publish_timeout == 10.0 and cfg.publish_concurrency == 1
    assert _clean_env.records == []  # the database warnings are pool_config's, not repeated here


def test_the_librarys_outbox_variables_are_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUTBOX_POLL_INTERVAL", "1s")
    monkeypatch.setenv("OUTBOX_BATCH_SIZE", "7")
    monkeypatch.setenv("OUTBOX_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("OUTBOX_PUBLISH_TIMEOUT", "2s")
    monkeypatch.setenv("OUTBOX_DRAIN_TIMEOUT", "45s")
    cfg = runner_config(None, object())  # type: ignore[arg-type]
    assert (cfg.poll_interval, cfg.batch_size, cfg.max_attempts) == (1.0, 7, 3)
    assert (cfg.publish_timeout, cfg.drain_timeout) == (2.0, 45.0)


def test_a_bad_duration_becomes_the_default_with_one_logged_warning(
    monkeypatch: pytest.MonkeyPatch, _clean_env: RecordingLog
) -> None:
    """A duration needs a unit: ``1`` is not valid, ``1s`` is."""
    monkeypatch.setenv("OUTBOX_POLL_INTERVAL", "1")
    for _ in range(3):  # the relay for each database: warned once
        cfg = runner_config(None, object())  # type: ignore[arg-type]
    assert cfg.poll_interval == 5.0
    [warning] = _clean_env.find("eventcommon_config_warning", "warning")
    assert "OUTBOX_POLL_INTERVAL" in warning["warning"]


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("OUTBOX_POLL_INTERVAL", "-3s"),
        ("OUTBOX_BATCH_SIZE", "0"),
        ("OUTBOX_MAX_ATTEMPTS", "-1"),
        ("OUTBOX_PUBLISH_TIMEOUT", "0s"),
    ],
)
def test_a_non_positive_value_the_library_would_accept_stops_startup(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(ConfigError, match=variable):
        runner_config(None, object())  # type: ignore[arg-type]


def test_a_topic_config_takes_arn_region_and_endpoint_from_settings() -> None:
    cfg = sns_config(
        Settings(aws_region="eu-central-1", aws_endpoint_url="http://floci:4566"), TOPIC
    )
    assert (cfg.topic_arn, cfg.region, cfg.endpoint_url) == (
        TOPIC,
        "eu-central-1",
        "http://floci:4566",
    )
    assert sns_config(Settings(), TOPIC).endpoint_url is None


def test_a_missing_topic_is_refused() -> None:
    with pytest.raises(ConfigValidationError):
        sns_config(Settings(), "")


def test_a_queue_config_and_its_consumer_options() -> None:
    config, options = sqs_settings(Settings(), QUEUE)
    assert (config.queue_url, config.region) == (QUEUE, "eu-west-1")
    assert (config.max_messages, config.wait_seconds) == (10, 20)  # the library's defaults
    assert options["concurrency"] == 1
    assert options["max_receive_count"] == Settings().sqs_max_receive_count == 3  # LD-86: ours
    assert options["visibility_timeout"] == 60.0  # the service default, not the library's 30 s
    assert options["queue_depth_interval"] == 30.0  # on here, off in the library


def test_the_librarys_sqs_variables_are_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQS_CONCURRENCY", "4")
    monkeypatch.setenv("SQS_MAX_MESSAGES", "5")
    monkeypatch.setenv("SQS_WAIT_SECONDS", "3")
    monkeypatch.setenv("SQS_HANDLER_TIMEOUT", "45s")
    monkeypatch.setenv("SQS_VISIBILITY_TIMEOUT", "120s")
    config, options = sqs_settings(Settings(), QUEUE)
    assert (config.max_messages, config.wait_seconds) == (5, 3)
    assert options["concurrency"] == 4 and options["handler_timeout"] == 45.0
    assert options["visibility_timeout"] == 120.0  # the explicit variable beats the service default


def test_the_queue_depth_gauge_can_be_switched_off_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SQS_QUEUE_DEPTH_INTERVAL", "0s")
    _, options = sqs_settings(Settings(), QUEUE)
    assert "queue_depth_interval" not in options


def test_the_redrive_threshold_is_ours_and_validated_at_startup() -> None:
    """A typo cannot turn dead-letter forwarding off (LD-86): Settings refuses it first."""
    _, options = sqs_settings(Settings(sqs_max_receive_count=5), QUEUE)
    assert options["max_receive_count"] == 5
    with pytest.raises(Exception, match="sqs_max_receive_count"):
        Settings(sqs_max_receive_count=0)


def test_a_missing_queue_is_refused_and_a_zero_concurrency_stops_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ConfigValidationError):
        sqs_settings(Settings(), "")
    monkeypatch.setenv("SQS_CONCURRENCY", "0")
    with pytest.raises(ConfigError, match="SQS_CONCURRENCY"):
        sqs_settings(Settings(), QUEUE)


def test_no_event_setting_is_left_in_settings_that_the_library_owns() -> None:
    """The old duplicates (``outbox_poll_interval_seconds`` ...) are gone: one name per knob."""
    owned_elsewhere: Any = (
        "outbox_poll_interval_seconds",
        "outbox_batch_size",
        "outbox_max_attempts",
        "outbox_publish_timeout_seconds",
        "sqs_concurrency",
        "sqs_visibility_timeout_seconds",
        "sqs_queue_depth_interval_seconds",
    )
    assert not [f for f in owned_elsewhere if f in Settings.model_fields]
