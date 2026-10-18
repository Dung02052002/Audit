from pathlib import Path

import pytest

from ai_youtube_agent.content.longform_chapters import (
    format_youtube_chapters,
    generate_chapters,
)
from ai_youtube_agent.content.longform_service import (
    CHAPTERS_GENERATED,
    LongFormProjectService,
)
from ai_youtube_agent.content.longform_timeline import ChapterMarker
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import migrate
from ai_youtube_agent.core.flags import FeatureFlags
from tests.test_longform_project import USER, Clock, _seed_longform_item


def test_generate_chapters_and_format_youtube_chapters(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "agent.db"
    migrate(db_path)
    database = Database(db_path)
    clock = Clock()
    sink = InMemoryAuditSink()
    _, item, script, _ = _seed_longform_item(database, clock)
    service = LongFormProjectService(
        database, AuditLog(sink), FeatureFlags(longform_enabled=True), clock=clock
    )

    project = service.create(item.id, actor=USER)
    planned = service.plan(project.id, expected_version=1, actor=USER)
    markers = generate_chapters(planned, script, planned.timeline)

    assert len(markers) == 4
    assert markers[0].start_ms == 0
    assert markers[0].title == "Introduction"
    assert markers[1].title == "Magnetic Confinement"
    assert markers[2].title == "Laser Compression"
    assert markers[3].title == "The Lawson Criterion"

    text = format_youtube_chapters(markers)
    lines = text.splitlines()
    assert lines[0] == "00:00 Introduction"
    assert lines[1].endswith("Magnetic Confinement")
    assert lines[2].endswith("Laser Compression")
    assert lines[3].endswith("The Lawson Criterion")


def test_service_generate_chapters_saves_and_audits(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    migrate(db_path)
    database = Database(db_path)
    clock = Clock()
    sink = InMemoryAuditSink()
    _, item, _, _ = _seed_longform_item(database, clock)
    service = LongFormProjectService(
        database, AuditLog(sink), FeatureFlags(longform_enabled=True), clock=clock
    )

    project = service.create(item.id, actor=USER)
    planned = service.plan(project.id, expected_version=1, actor=USER)
    updated = service.generate_chapters(
        project.id, expected_version=planned.version, actor=USER
    )

    assert len(updated.timeline.chapters) == 4
    events = [e for e in sink.events if e.action == CHAPTERS_GENERATED]
    assert len(events) == 1
    assert events[0].metadata["chapter_count"] == 4


def test_format_youtube_chapters_refuses_fewer_than_three_markers() -> None:
    with pytest.raises(ValueError, match="at least 3"):
        format_youtube_chapters(
            (
                ChapterMarker(index=0, section_index=0, start_ms=0, title="Intro"),
                ChapterMarker(index=1, section_index=1, start_ms=20_000, title="End"),
            )
        )

