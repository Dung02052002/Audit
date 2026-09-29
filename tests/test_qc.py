import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
PASS = QCStatus.PASS
WARN = QCStatus.WARN
FAIL = QCStatus.FAIL


def at(moment: datetime):
    return lambda: moment


def artifact(kind: ArtifactKind, item: str = "item-1", sha: str = "a") -> Artifact:
    return Artifact.create(
        item,
        kind,
        uri=f"store://{item}/{kind.value}",
        sha256=sha * 64,
        size_bytes=100,
        media_type="application/octet-stream",
        clock=at(T0),
    )


VIDEO = artifact(ArtifactKind.VIDEO)
SUBTITLES = artifact(ArtifactKind.SUBTITLES, sha="b")


def checks(*statuses: QCStatus) -> list[QCCheck]:
    return [QCCheck(f"check.n{i}", status) for i, status in enumerate(statuses)]


def new_result(*statuses: QCStatus, artifacts=(VIDEO, SUBTITLES)) -> QCResult:
    return QCResult.create(
        "item-1", artifacts, checks(*(statuses or (PASS,))), clock=at(T0)
    )


def rebuild(entity, **changes):
    values = {f.name: getattr(entity, f.name) for f in dataclasses.fields(entity)}
    return type(entity)(**{**values, **changes})


# Checks


def test_statuses_are_pass_warn_fail() -> None:
    assert [s.value for s in QCStatus] == ["pass", "warn", "fail"]


def test_check_holds_name_status_and_detail() -> None:
    check = QCCheck("video.fps", FAIL, "expected 30 fps, got 24")
    assert (check.name, check.status, check.detail) == (
        "video.fps",
        FAIL,
        "expected 30 fps, got 24",
    )


def test_check_detail_is_optional() -> None:
    assert QCCheck("audio", PASS).detail is None


@pytest.mark.parametrize(
    "name", ["", "Video.fps", "video.", ".fps", "video fps", "video..fps", "1video"]
)
def test_check_name_must_be_a_lowercase_dotted_name(name: str) -> None:
    with pytest.raises(ValueError):
        QCCheck(name, PASS)


@pytest.mark.parametrize("status", ["pass", "ok", None])
def test_check_status_must_be_a_qc_status(status) -> None:
    with pytest.raises(TypeError):
        QCCheck("video.fps", status)


def test_check_detail_must_not_be_empty() -> None:
    with pytest.raises(ValueError):
        QCCheck("video.fps", WARN, "  ")


def test_check_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        QCCheck("video.fps", FAIL).status = PASS  # type: ignore[misc]


# Creating a result


def test_create_binds_the_exact_artifact_versions() -> None:
    result = new_result(PASS, WARN)

    assert len(result.id) == 32
    assert result.content_item_id == "item-1"
    assert result.artifact_ids == (VIDEO.id, SUBTITLES.id)
    assert [c.status for c in result.checks] == [PASS, WARN]
    assert result.created_at == T0


def test_a_new_artifact_version_is_a_different_target() -> None:
    newer = VIDEO.next_version(
        uri="store://v2",
        sha256="c" * 64,
        size_bytes=100,
        media_type="video/mp4",
        clock=at(T0),
    )
    assert (
        new_result().artifact_ids
        != new_result(artifacts=(newer, SUBTITLES)).artifact_ids
    )


def test_each_run_gets_its_own_id() -> None:
    assert new_result().id != new_result().id


def test_create_defaults_to_the_current_utc_time() -> None:
    before = datetime.now(UTC)
    result = QCResult.create("item-1", [VIDEO], checks(PASS))
    assert before <= result.created_at <= datetime.now(UTC)


def test_create_accepts_any_iterable() -> None:
    result = QCResult.create("item-1", iter([VIDEO]), iter(checks(PASS)))
    assert isinstance(result.artifact_ids, tuple)
    assert isinstance(result.checks, tuple)


def test_artifacts_must_belong_to_the_content_item() -> None:
    other = artifact(ArtifactKind.THUMBNAIL, item="item-2")
    with pytest.raises(ValueError, match="another content item"):
        QCResult.create("item-1", [VIDEO, other], checks(PASS))


def test_result_needs_an_artifact() -> None:
    with pytest.raises(ValueError):
        QCResult.create("item-1", [], checks(PASS))


def test_result_needs_a_check() -> None:
    with pytest.raises(ValueError):
        QCResult.create("item-1", [VIDEO], [])


def test_the_same_artifact_cannot_be_listed_twice() -> None:
    with pytest.raises(ValueError, match="must not repeat"):
        QCResult.create("item-1", [VIDEO, VIDEO], checks(PASS))


def test_check_names_must_be_unique() -> None:
    with pytest.raises(ValueError, match="check names"):
        QCResult.create(
            "item-1", [VIDEO], [QCCheck("video.fps", PASS), QCCheck("video.fps", FAIL)]
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"content_item_id": " "},
        {"artifact_ids": ["a"]},
        {"artifact_ids": ("a", " ")},
        {"checks": [QCCheck("x", PASS)]},
        {"checks": ("not a check",)},
        {"created_at": datetime(2026, 9, 29, 10, 0)},
        {"created_at": T0.astimezone(timezone(timedelta(hours=7)))},
    ],
)
def test_result_rejects_invalid_state(changes) -> None:
    with pytest.raises((ValueError, TypeError)):
        rebuild(new_result(), **changes)


def test_result_is_frozen() -> None:
    result = new_result()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.checks = ()  # type: ignore[misc]


# Overall status


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((PASS,), PASS),
        ((PASS, PASS), PASS),
        ((WARN,), WARN),
        ((PASS, WARN), WARN),
        ((FAIL,), FAIL),
        ((PASS, FAIL), FAIL),
        ((WARN, FAIL), FAIL),
        ((FAIL, WARN, PASS), FAIL),
    ],
)
def test_status_is_the_worst_check(statuses, expected) -> None:
    assert new_result(*statuses).status is expected


# Serialisation


def test_as_dict_is_json_friendly() -> None:
    result = QCResult.create(
        "item-1",
        [VIDEO],
        [QCCheck("video.fps", PASS), QCCheck("audio.silence", WARN, "2 s of silence")],
        clock=at(T0),
    )

    assert result.as_dict() == {
        "id": result.id,
        "content_item_id": "item-1",
        "artifact_ids": [VIDEO.id],
        "status": "warn",
        "checks": [
            {"name": "video.fps", "status": "pass", "detail": None},
            {"name": "audio.silence", "status": "warn", "detail": "2 s of silence"},
        ],
        "created_at": "2026-09-29T10:00:00+00:00",
    }
