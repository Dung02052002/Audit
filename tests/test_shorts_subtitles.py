"""I-098 Shorts subtitles (Prompt Pack v8, prompt #098).

Rules decided on 2026-10-17:

- ``render_srt`` writes one numbered cue per caption, ``HH:MM:SS,mmm`` times,
  the text wrapped like on screen, NFC, ``\\n`` line endings, trailing
  newline, deterministic bytes; ``render_vtt`` is the WebVTT twin;
- ``ShortsSubtitleRecorder.record`` stores the SRT as the next SUBTITLES
  artifact version through the ``ArtifactStorage``, invalidating stale
  approvals in the same transaction and deleting the file on any failure;
- the same bytes again are a replay (nothing written or audited);
- refusals: project missing, stale project version, captions off, no captions
  (or not covering every scene), invalid captions, stale script or audio and
  storage failure; the audit event carries ids and counts, never text.
"""

import dataclasses
import unicodedata
from datetime import timedelta
from pathlib import Path

import pytest
from factories import T0, USER, at, make_approval_request
from test_shorts_captions import CONFIG, RULES, cap, timeline_of
from test_shorts_scene import Stage

from ai_youtube_agent.content import shorts_subtitles
from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.shorts_captions import (
    CaptionCheck,
    CaptionIssue,
    IssueKind,
    wrap,
)
from ai_youtube_agent.content.shorts_subtitles import (
    HTTP_STATUS,
    RECORDED,
    SRT_MEDIA_TYPE,
    USER_MESSAGES,
    ShortsSubtitleError,
    ShortsSubtitleErrorCode,
    ShortsSubtitleRecorder,
    SubtitleResult,
    render_srt,
    render_vtt,
)
from ai_youtube_agent.content.shorts_timeline import (
    Scene,
    Timeline,
    TimingSource,
)
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, AuditResult, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ArtifactRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository

Code = ShortsSubtitleErrorCode
T1 = T0 + timedelta(hours=1)
SECRET = "Zebra-quartz-7731"


# Rendering


def test_srt_numbers_the_cues_and_formats_the_times() -> None:
    timeline = timeline_of(
        cap(1, "s1", "Hello there", 0, 1500),
        cap(2, "s1", "General Kenobi", 1500, 4321),
        cap(3, "s2", "You are a bold one", 6000, 8999),
    )

    assert render_srt(timeline, RULES) == (
        "1\n00:00:00,000 --> 00:00:01,500\nHello there\n\n"
        "2\n00:00:01,500 --> 00:00:04,321\nGeneral Kenobi\n\n"
        "3\n00:00:06,000 --> 00:00:08,999\nYou are a bold one\n"
    )


def test_srt_times_past_one_hour() -> None:
    scenes = (Scene("s1", 0, 0, 4_000_000),)
    timeline = Timeline(
        1,
        4_000_000,
        TimingSource.ESTIMATED,
        scenes,
        (cap(1, "s1", "Late", 3_723_004, 3_725_000),),
    )

    assert "01:02:03,004 --> 01:02:05,000" in render_srt(timeline, RULES)


def test_srt_wraps_like_the_screen() -> None:
    text = "one two three four five six seven eight nine ten"
    timeline = timeline_of(cap(1, "s1", text, 0, 3000))

    srt = render_srt(timeline, RULES)

    lines = srt.splitlines()
    assert lines[2:4] == list(wrap(text, RULES))
    assert len(lines[2]) <= RULES.max_chars_per_line


def test_srt_keeps_vietnamese_text_in_nfc() -> None:
    decomposed = unicodedata.normalize("NFD", "Xin chào các bạn")
    timeline = timeline_of(cap(1, "s1", decomposed, 0, 2000))

    srt = render_srt(timeline, RULES)

    assert "Xin chào các bạn" in srt
    assert unicodedata.is_normalized("NFC", srt)
    assert srt.encode("utf-8").decode("utf-8") == srt


def test_srt_has_unix_line_endings_no_bom_and_one_trailing_newline() -> None:
    timeline = timeline_of(cap(1, "s1", "Hello there", 0, 1500))

    data = render_srt(timeline, RULES).encode("utf-8")

    assert b"\r" not in data
    assert not data.startswith(b"\xef\xbb\xbf")
    assert data.endswith(b"there\n") and not data.endswith(b"\n\n")


def test_srt_is_deterministic() -> None:
    timeline = timeline_of(cap(1, "s1", "Hello there", 0, 1500))

    assert render_srt(timeline, RULES) == render_srt(timeline, RULES)


def test_srt_of_a_timeline_without_captions_is_empty() -> None:
    assert render_srt(timeline_of(), RULES) == ""


def test_srt_refuses_a_caption_that_does_not_wrap() -> None:
    timeline = timeline_of(cap(1, "s1", "x" * 40, 0, 2000))

    with pytest.raises(ValueError):
        render_srt(timeline, RULES)


def test_vtt_has_a_header_and_dots() -> None:
    timeline = timeline_of(
        cap(1, "s1", "Hello there", 0, 1500), cap(2, "s1", "Again", 1500, 3000)
    )

    assert render_vtt(timeline, RULES) == (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:01.500\nHello there\n\n"
        "00:00:01.500 --> 00:00:03.000\nAgain\n"
    )


# Recording


class Shop:
    """A captioned Shorts project and a recorder over a local storage."""

    def __init__(
        self, database: Database, tmp_path: Path, *, captions: bool = True
    ) -> None:
        self.database = database
        self.stage = Stage(database, captions=captions)
        self.service = self.stage.world.service
        self.item = self.stage.world.item
        self.project = self.stage.project
        if captions:
            self.project = self.service.caption(
                self.project.id, expected_version=self.project.version, actor=USER
            )
        self.root = tmp_path / "artifacts"
        self.storage = LocalFileStorage(self.root)
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=at(T1))

    def recorder(self, storage=None, sink=None) -> ShortsSubtitleRecorder:
        audit = AuditLog(sink, clock=at(T1)) if sink is not None else self.audit
        return ShortsSubtitleRecorder(
            self.database, storage or self.storage, audit, clock=at(T1)
        )

    def record(self, recorder=None, **kwargs) -> SubtitleResult:
        return (recorder or self.recorder()).record(
            self.project.id,
            expected_version=kwargs.pop("expected_version", self.project.version),
            actor=USER,
        )

    def retext(self, text: str) -> None:
        """Save the project again with the first caption showing ``text``."""
        timeline = self.project.timeline
        first = dataclasses.replace(timeline.captions[0], text=text)
        changed = dataclasses.replace(
            timeline, captions=(first, *timeline.captions[1:])
        )
        self.project = self.service.save_timeline(
            self.project.id,
            changed,
            expected_version=self.project.version,
            actor=USER,
        )

    def artifacts(self):
        with self.database.transaction() as connection:
            return [
                a
                for a in ArtifactRepository(connection).list_by_content_item(
                    self.item.id
                )
                if a.kind is ArtifactKind.SUBTITLES
            ]

    def files(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(p for p in self.root.rglob("*") if p.is_file())

    def nothing_stored(self) -> None:
        assert self.artifacts() == []
        assert self.files() == []
        assert self.sink.events() == ()


@pytest.fixture
def shop(database: Database, tmp_path: Path) -> Shop:
    return Shop(database, tmp_path)


def refused(call, code: Code) -> ShortsSubtitleError:
    with pytest.raises(ShortsSubtitleError) as info:
        call()
    assert info.value.code == code.value
    return info.value


def test_the_first_subtitles_are_version_one_and_readable(shop: Shop) -> None:
    result = shop.record()

    artifact = result.artifact
    assert result.created is True
    assert artifact.kind is ArtifactKind.SUBTITLES
    assert artifact.version == 1
    assert artifact.media_type == SRT_MEDIA_TYPE
    assert artifact.content_item_id == shop.item.id
    expected = render_srt(shop.project.timeline, CONFIG.caption_rules).encode("utf-8")
    assert shop.storage.read(artifact.uri) == expected
    assert artifact.size_bytes == len(expected)
    assert shop.files() == [shop.root.resolve() / shop.item.id / "subtitles" / "v1.srt"]
    assert shop.artifacts() == [artifact]


def test_the_same_subtitles_again_are_a_replay(shop: Shop) -> None:
    first = shop.record()
    events = shop.sink.events()

    again = shop.record()

    assert again.created is False
    assert again.artifact == first.artifact
    assert len(shop.artifacts()) == 1
    assert len(shop.files()) == 1
    assert shop.sink.events() == events


def test_changed_captions_give_a_new_version(shop: Shop) -> None:
    first = shop.record()
    shop.retext("A different opening")

    second = shop.record()

    assert second.created is True
    assert second.artifact.version == 2
    assert second.artifact.sha256 != first.artifact.sha256
    assert b"A different opening" in shop.storage.read(second.artifact.uri)
    assert len(shop.files()) == 2


def test_a_planned_project_without_backgrounds_may_have_subtitles(
    shop: Shop,
) -> None:
    assert shop.project.status.value == "planned"

    assert shop.record().created is True


def test_one_event_with_scalar_metadata_and_no_text(shop: Shop) -> None:
    shop.retext(SECRET)

    result = shop.record()

    (event,) = shop.sink.events()
    assert event.action == RECORDED == "shorts.subtitles_recorded"
    assert event.result is AuditResult.SUCCESS
    assert (event.entity.type, event.entity.id) == ("artifact", result.artifact.id)
    assert event.actor == USER
    assert dict(event.metadata) == {
        "project_id": shop.project.id,
        "content_item_id": shop.item.id,
        "version": 1,
        "sha256": result.artifact.sha256,
        "size_bytes": result.artifact.size_bytes,
        "cue_count": len(shop.project.timeline.captions),
        "timeline_sha256": shop.project.timeline.sha256,
        "project_version": shop.project.version,
    }
    assert SECRET not in repr(event)


def test_a_new_version_invalidates_the_approved_request_after_commit(
    shop: Shop,
) -> None:
    first = shop.record()
    approval = make_approval_request(
        shop.item, [first.artifact], status=ApprovalStatus.APPROVED
    )
    with shop.database.transaction() as connection:
        ApprovalRequestRepository(connection).add(approval)
    shop.retext("Another opening line")
    seen: list[tuple[str, list[ApprovalStatus]]] = []

    def statuses() -> list[ApprovalStatus]:
        with shop.database.transaction() as connection:
            requests = ApprovalRequestRepository(connection).list_by_content_item(
                shop.item.id
            )
        return [request.status for request in requests]

    class CheckingSink(InMemoryAuditSink):
        def append(self, event) -> None:
            # A fresh connection only sees committed data.
            seen.append((event.action, statuses()))
            super().append(event)

    sink = CheckingSink()

    second = shop.record(shop.recorder(sink=sink))

    assert second.artifact.version == 2
    assert statuses() == [ApprovalStatus.INVALIDATED]
    assert seen == [
        ("approval.invalidated", [ApprovalStatus.INVALIDATED]),
        (RECORDED, [ApprovalStatus.INVALIDATED]),
    ]


# Refusals


def test_an_unknown_project_is_refused(shop: Shop) -> None:
    refused(
        lambda: shop.recorder().record("nope", expected_version=1, actor=USER),
        Code.PROJECT_NOT_FOUND,
    )
    shop.nothing_stored()


def test_a_stale_project_version_is_a_conflict(shop: Shop) -> None:
    refused(
        lambda: shop.record(expected_version=shop.project.version - 1), Code.CONFLICT
    )
    shop.nothing_stored()


def test_a_format_with_captions_off_is_refused(
    database: Database, tmp_path: Path
) -> None:
    shop = Shop(database, tmp_path, captions=False)

    refused(shop.record, Code.CAPTIONS_OFF)
    shop.nothing_stored()


def test_a_project_without_captions_is_refused(
    database: Database, tmp_path: Path
) -> None:
    shop = Shop(database, tmp_path)
    shop.project = shop.stage.project

    refused(shop.record, Code.NO_CAPTIONS)
    shop.nothing_stored()


def test_captions_that_miss_a_scene_are_refused(shop: Shop) -> None:
    timeline = shop.project.timeline
    shop.project = shop.service.save_timeline(
        shop.project.id,
        dataclasses.replace(timeline, captions=timeline.captions[:1]),
        expected_version=shop.project.version,
        actor=USER,
    )

    refused(shop.record, Code.NO_CAPTIONS)
    shop.nothing_stored()


def test_caption_errors_are_refused(
    shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = CaptionCheck(
        (CaptionIssue(IssueKind.UNSAFE, "c1", "c1 is outside the safe area"),)
    )
    monkeypatch.setattr(shorts_subtitles, "check_captions", lambda *_: broken)

    error = refused(shop.record, Code.CAPTIONS_INVALID)

    assert "c1 is outside the safe area" in str(error)
    shop.nothing_stored()


def test_a_caption_that_cannot_wrap_is_refused(
    shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_):
        raise ValueError("does not wrap")

    monkeypatch.setattr(shorts_subtitles, "render_srt", broken)

    refused(shop.record, Code.CAPTIONS_INVALID)
    shop.nothing_stored()


def test_a_newer_script_makes_the_project_stale(shop: Shop) -> None:
    shop.stage.world.add_script()

    refused(shop.record, Code.STALE_INPUT)
    shop.nothing_stored()


def test_newer_audio_makes_the_project_stale(shop: Shop) -> None:
    shop.stage.world.add_audio(shop.stage.world.latest_script())

    refused(shop.record, Code.STALE_INPUT)
    shop.nothing_stored()


def test_a_failing_storage_is_refused_and_stores_nothing(shop: Shop) -> None:
    class Broken(LocalFileStorage):
        def write(self, *args, **kwargs):
            raise OSError("disk full")

    refused(
        lambda: shop.record(shop.recorder(storage=Broken(shop.root))),
        Code.STORAGE_FAILED,
    )
    shop.nothing_stored()


# Failures after the file was written


def test_a_failing_artifact_insert_rolls_back_and_deletes_the_file(
    shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self, artifact):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(ArtifactRepository, "add", broken)

    with pytest.raises(RuntimeError, match="insert failed"):
        shop.record()

    shop.nothing_stored()


def test_a_failing_invalidation_rolls_back_and_deletes_the_file(
    shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(connection, item_id, *, clock=None):
        raise RuntimeError("invalidation failed")

    monkeypatch.setattr(shorts_subtitles, "invalidate_stale_approvals", broken)

    with pytest.raises(RuntimeError, match="invalidation failed"):
        shop.record()

    shop.nothing_stored()


def test_a_failing_cleanup_still_raises_the_first_error(
    shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_add(self, artifact):
        raise RuntimeError("insert failed")

    def broken_delete(self, uri):
        raise OSError("cannot delete")

    monkeypatch.setattr(ArtifactRepository, "add", broken_add)
    monkeypatch.setattr(LocalFileStorage, "delete", broken_delete)

    with pytest.raises(RuntimeError, match="insert failed"):
        shop.record()

    assert shop.artifacts() == []


# Errors


def test_every_error_code_has_a_user_message_and_the_right_status() -> None:
    assert set(USER_MESSAGES) == set(Code)
    for code in Code:
        assert code.value.startswith("domain.shorts_subtitles")
        assert ShortsSubtitleError("x", code=code).code == code.value
    assert HTTP_STATUS[Code.PROJECT_NOT_FOUND] == 404
    assert HTTP_STATUS[Code.CONFLICT] == 409
    assert HTTP_STATUS[Code.STALE_INPUT] == 409
