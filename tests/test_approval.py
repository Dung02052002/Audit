import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.content.approval import (
    ApprovalRequest,
    ApprovalStatus,
    ArtifactBinding,
)
from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import Actor, ActorKind

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
PIPELINE = Actor(ActorKind.SYSTEM, "pipeline")


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
QC = QCResult.create(
    "item-1", [VIDEO, SUBTITLES], [QCCheck("video.fps", QCStatus.PASS)]
)


def new_request(artifacts=(VIDEO, SUBTITLES), **kwargs) -> ApprovalRequest:
    values = {"requested_by": PIPELINE, "clock": at(T0), **kwargs}
    return ApprovalRequest.create("item-1", artifacts, **values)


def rebuild(entity, **changes):
    values = {f.name: getattr(entity, f.name) for f in dataclasses.fields(entity)}
    return type(entity)(**{**values, **changes})


# Statuses


def test_statuses() -> None:
    assert [s.value for s in ApprovalStatus] == [
        "pending",
        "approved",
        "rejected",
        "changes_requested",
        "invalidated",
        "expired",
    ]


# Artifact bindings


def test_binding_snapshots_the_exact_artifact_version() -> None:
    binding = ArtifactBinding.of(VIDEO)
    assert binding == ArtifactBinding(VIDEO.id, ArtifactKind.VIDEO, 1, "a" * 64)


def test_a_new_artifact_version_gives_a_different_binding() -> None:
    newer = VIDEO.next_version(
        uri="store://v2", sha256="c" * 64, size_bytes=100, media_type="video/mp4"
    )
    binding = ArtifactBinding.of(newer)
    assert binding != ArtifactBinding.of(VIDEO)
    assert (binding.version, binding.sha256) == (2, "c" * 64)


@pytest.mark.parametrize(
    "changes",
    [
        {"artifact_id": " "},
        {"version": 0},
        {"version": True},
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
    ],
)
def test_binding_rejects_invalid_values(changes) -> None:
    with pytest.raises(ValueError):
        rebuild(ArtifactBinding.of(VIDEO), **changes)


def test_binding_kind_must_be_an_artifact_kind() -> None:
    with pytest.raises(TypeError):
        rebuild(ArtifactBinding.of(VIDEO), kind="video")


def test_binding_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        ArtifactBinding.of(VIDEO).sha256 = "b" * 64  # type: ignore[misc]


# Creating a request


def test_create_builds_a_pending_request() -> None:
    request = new_request(qc_result=QC)

    assert len(request.id) == 32
    assert request.content_item_id == "item-1"
    assert request.artifacts == (
        ArtifactBinding.of(VIDEO),
        ArtifactBinding.of(SUBTITLES),
    )
    assert request.artifacts[1] == ArtifactBinding(
        SUBTITLES.id, ArtifactKind.SUBTITLES, 1, "b" * 64
    )
    assert request.status is ApprovalStatus.PENDING
    assert request.requested_by == PIPELINE
    assert request.qc_result_id == QC.id
    assert request.created_at == T0


def test_qc_result_is_optional() -> None:
    assert new_request().qc_result_id is None


@pytest.mark.parametrize("kind", list(ActorKind))
def test_any_actor_may_request_an_approval(kind: ActorKind) -> None:
    actor = Actor(kind, "someone")
    assert new_request(requested_by=actor).requested_by == actor


def test_each_request_gets_its_own_id() -> None:
    assert new_request().id != new_request().id


def test_create_defaults_to_the_current_utc_time() -> None:
    before = datetime.now(UTC)
    request = ApprovalRequest.create("item-1", [VIDEO], requested_by=PIPELINE)
    assert before <= request.created_at <= datetime.now(UTC)


def test_create_accepts_any_iterable() -> None:
    assert isinstance(new_request(iter([VIDEO])).artifacts, tuple)


def test_artifacts_must_belong_to_the_content_item() -> None:
    other = artifact(ArtifactKind.THUMBNAIL, item="item-2")
    with pytest.raises(ValueError, match="another content item"):
        new_request((VIDEO, other))


def test_qc_result_must_belong_to_the_content_item() -> None:
    other_video = artifact(ArtifactKind.VIDEO, item="item-2")
    other_qc = QCResult.create(
        "item-2", [other_video], [QCCheck("video.fps", QCStatus.PASS)]
    )
    with pytest.raises(ValueError, match="QC result"):
        new_request(qc_result=other_qc)


def test_request_needs_an_artifact() -> None:
    with pytest.raises(ValueError):
        new_request(())


def test_the_same_artifact_cannot_be_listed_twice() -> None:
    with pytest.raises(ValueError, match="must not repeat"):
        new_request((VIDEO, VIDEO))


def test_two_versions_of_one_kind_cannot_be_approved_together() -> None:
    newer = VIDEO.next_version(
        uri="store://v2", sha256="c" * 64, size_bytes=100, media_type="video/mp4"
    )
    with pytest.raises(ValueError, match="only once"):
        new_request((VIDEO, newer))


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"content_item_id": " "},
        {"artifacts": [ArtifactBinding.of(VIDEO)]},
        {"artifacts": ("not a binding",)},
        {"status": "pending"},
        {"requested_by": "pipeline"},
        {"qc_result_id": " "},
        {"created_at": datetime(2026, 9, 29, 10, 0)},
        {"created_at": T0.astimezone(timezone(timedelta(hours=7)))},
    ],
)
def test_request_rejects_invalid_state(changes) -> None:
    with pytest.raises((ValueError, TypeError)):
        rebuild(new_request(), **changes)


@pytest.mark.parametrize("status", list(ApprovalStatus))
def test_a_stored_request_can_hold_any_status(status: ApprovalStatus) -> None:
    assert rebuild(new_request(), status=status).status is status


def test_request_is_frozen() -> None:
    request = new_request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.status = ApprovalStatus.APPROVED  # type: ignore[misc]


def test_request_has_no_decision_methods_yet() -> None:
    # invalidate came with #035 (C-035); the rest come with #139-#142.
    for name in ("approve", "reject", "request_changes", "expire"):
        assert not hasattr(ApprovalRequest, name)


# Serialisation


def test_as_dict_is_json_friendly() -> None:
    request = new_request((VIDEO,), qc_result=QC)

    assert request.as_dict() == {
        "id": request.id,
        "content_item_id": "item-1",
        "artifacts": [
            {
                "artifact_id": VIDEO.id,
                "kind": "video",
                "version": 1,
                "sha256": "a" * 64,
            }
        ],
        "status": "pending",
        "requested_by": {"kind": "system", "id": "pipeline"},
        "qc_result_id": QC.id,
        "created_at": "2026-09-29T10:00:00+00:00",
    }
