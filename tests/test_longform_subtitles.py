from pathlib import Path

import pytest

from ai_youtube_agent.content.longform_service import LongFormProjectService
from ai_youtube_agent.content.longform_subtitles import (
    RECORDED,
    LongFormSubtitleError,
    LongFormSubtitleErrorCode,
    LongFormSubtitleRecorder,
    SubtitleFormat,
    render_longform_srt,
    render_longform_vtt,
)
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import migrate
from ai_youtube_agent.core.flags import FeatureFlags
from tests.test_longform_project import USER, Clock, _seed_longform_item


def test_longform_subtitles_record_srt_replay_and_vtt(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "agent.db"
    migrate(db_path)
    database = Database(db_path)
    storage = LocalFileStorage(tmp_path / "artifacts")
    clock = Clock()
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    flags = FeatureFlags(longform_enabled=True)
    _, item, _, _ = _seed_longform_item(database, clock)

    service = LongFormProjectService(database, audit, flags, clock=clock)
    recorder = LongFormSubtitleRecorder(
        database, storage, audit, flags, clock=clock
    )

    project = service.create(item.id, actor=USER)
    planned = service.plan(project.id, expected_version=1, actor=USER)
    captioned = service.caption(
        project.id, expected_version=planned.version, actor=USER
    )

    first = recorder.record(
        project.id, expected_version=captioned.version, actor=USER
    )
    assert first.created is True
    assert first.artifact.version == 1
    assert first.artifact.uri.endswith("v1.srt")
    assert storage.read(first.artifact.uri).decode("utf-8") == render_longform_srt(
        captioned.timeline, captioned.config.caption_rules
    )

    # Idempotent replay returns created=False
    second = recorder.record(
        project.id, expected_version=captioned.version, actor=USER
    )
    assert second.created is False
    assert second.artifact.id == first.artifact.id

    # Recording as WebVTT writes a new version with .vtt extension
    vtt = recorder.record(
        project.id,
        expected_version=captioned.version,
        actor=USER,
        subtitle_format=SubtitleFormat.VTT,
    )
    assert vtt.created is True
    assert vtt.artifact.version == 2
    assert vtt.artifact.uri.endswith("v2.vtt")
    assert storage.read(vtt.artifact.uri).decode("utf-8") == render_longform_vtt(
        captioned.timeline, captioned.config.caption_rules
    )

    events = [e for e in sink.events if e.action == RECORDED]
    assert len(events) == 2
    assert events[0].metadata["subtitle_format"] == "srt"
    assert events[1].metadata["subtitle_format"] == "vtt"


def test_longform_subtitles_refuses_when_captions_off(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    migrate(db_path)
    database = Database(db_path)
    storage = LocalFileStorage(tmp_path / "artifacts")
    clock = Clock()
    audit = AuditLog(InMemoryAuditSink())
    flags = FeatureFlags(longform_enabled=True)
    _, item, _, _ = _seed_longform_item(database, clock, captions=False)

    service = LongFormProjectService(database, audit, flags, clock=clock)
    recorder = LongFormSubtitleRecorder(
        database, storage, audit, flags, clock=clock
    )

    project = service.create(item.id, actor=USER)
    planned = service.plan(project.id, expected_version=1, actor=USER)

    with pytest.raises(LongFormSubtitleError) as exc_info:
        recorder.record(
            project.id, expected_version=planned.version, actor=USER
        )
    assert exc_info.value.code == LongFormSubtitleErrorCode.CAPTIONS_OFF.value

