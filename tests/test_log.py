import asyncio
import io
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_youtube_agent.core.config import ENV_PREFIX, get_settings, load_settings
from ai_youtube_agent.core.log import (
    CONTEXT_FIELDS,
    Severity,
    configure_logging,
    current_context,
    get_logger,
    log_context,
    new_correlation_id,
)


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def stream() -> io.StringIO:
    buffer = io.StringIO()
    configure_logging(Severity.DEBUG, buffer)
    return buffer


def records(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_record_is_json_with_required_fields(stream: io.StringIO) -> None:
    get_logger("test.fields").info("hello %s", "world")

    (record,) = records(stream)
    assert record["message"] == "hello world"
    assert record["severity"] == "INFO"
    assert record["logger"] == "test.fields"
    assert record["timestamp"].endswith("+00:00")
    for name in CONTEXT_FIELDS:
        assert record[name] is None


@pytest.mark.parametrize("severity", list(Severity))
def test_every_severity_is_logged(stream: io.StringIO, severity: Severity) -> None:
    get_logger("test.levels").log(logging.getLevelName(severity.value), "msg")

    assert records(stream)[0]["severity"] == severity.value


def test_level_filters_lower_severities() -> None:
    buffer = io.StringIO()
    configure_logging(Severity.WARNING, buffer)
    logger = get_logger("test.filter")

    logger.info("dropped")
    logger.warning("kept")

    assert [r["message"] for r in records(buffer)] == ["kept"]


def test_context_ids_are_attached(stream: io.StringIO) -> None:
    with log_context(correlation_id="corr-1", session_id="sess-1", job_id="job-1"):
        get_logger("test.ctx").info("inside")

    (record,) = records(stream)
    assert record["correlation_id"] == "corr-1"
    assert record["session_id"] == "sess-1"
    assert record["job_id"] == "job-1"


def test_nested_context_keeps_outer_ids_and_restores(stream: io.StringIO) -> None:
    logger = get_logger("test.nested")
    with log_context(correlation_id="corr-1", session_id="sess-1"):
        with log_context(job_id="job-1"):
            logger.info("inner")
        logger.info("outer")
    logger.info("after")

    inner, outer, after = records(stream)
    assert (inner["session_id"], inner["job_id"]) == ("sess-1", "job-1")
    assert (outer["session_id"], outer["job_id"]) == ("sess-1", None)
    assert after["correlation_id"] is None


def test_context_is_restored_after_exception() -> None:
    with pytest.raises(RuntimeError), log_context(job_id="job-1"):
        raise RuntimeError("boom")

    assert current_context()["job_id"] is None


def test_ids_do_not_leak_between_concurrent_tasks(stream: io.StringIO) -> None:
    logger = get_logger("test.async")

    async def job(job_id: str) -> None:
        with log_context(job_id=job_id):
            await asyncio.sleep(0)
            logger.info("step")

    async def main() -> None:
        await asyncio.gather(job("job-a"), job("job-b"))

    asyncio.run(main())

    job_ids = [r["job_id"] for r in records(stream) if r["logger"] == "test.async"]
    assert sorted(job_ids) == ["job-a", "job-b"]


def test_structured_fields_are_included(stream: io.StringIO) -> None:
    get_logger("test.extra").info("rendered", extra={"fields": {"frames": 120}})

    assert records(stream)[0]["fields"] == {"frames": 120}


def test_exception_is_included(stream: io.StringIO) -> None:
    try:
        raise ValueError("bad input")
    except ValueError:
        get_logger("test.exc").exception("failed")

    record = records(stream)[0]
    assert record["severity"] == "ERROR"
    assert "ValueError: bad input" in record["exception"]


def test_configure_logging_does_not_duplicate_handlers() -> None:
    first, second = io.StringIO(), io.StringIO()
    configure_logging(Severity.INFO, first)
    configure_logging(Severity.INFO, second)

    get_logger("test.once").info("only once")

    assert first.getvalue() == ""
    assert len(records(second)) == 1


def test_new_correlation_ids_are_unique() -> None:
    assert new_correlation_id() != new_correlation_id()


def test_settings_log_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)
    get_settings.cache_clear()

    assert load_settings(tmp_path).log_level is Severity.INFO

    monkeypatch.setenv(f"{ENV_PREFIX}LOG_LEVEL", "DEBUG")
    assert load_settings(tmp_path).log_level is Severity.DEBUG

    monkeypatch.setenv(f"{ENV_PREFIX}LOG_LEVEL", "VERBOSE")
    with pytest.raises(ValidationError):
        load_settings(tmp_path)
