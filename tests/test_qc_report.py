"""Tests for K-129 QC Report (`QCReport`, `QCReportBuilder`, `TestGate`, `QCGate`)."""

import dataclasses
from datetime import UTC, datetime

from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.content.qc_report import (
    ALL_QC_CHECK_NAMES,
    QCGate,
    TestGate,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.gates import GateContext, GateName, GateOutcome

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
SYSTEM = Actor(ActorKind.SYSTEM, "qc")


class _StubQCResults:
    def __init__(self, results: list[QCResult]) -> None:
        self._results = results

    def list_by_content_item(self, content_item_id: str) -> list[QCResult]:
        return [r for r in self._results if r.content_item_id == content_item_id]


class _StubArtifacts:
    def __init__(self, artifacts: list[Artifact]) -> None:
        self._artifacts = artifacts

    def list_by_content_item(self, content_item_id: str) -> list[Artifact]:
        return [a for a in self._artifacts if a.content_item_id == content_item_id]


def _make_item(status: ContentStatus = ContentStatus.APPROVED) -> ContentItem:
    item = ContentItem.create(
        "ch-1", "sp-1", 1, ContentType.SHORTS, "Title", clock=lambda: NOW
    )
    return dataclasses.replace(item, id="item-1", status=status)


def test_all_qc_check_names_order() -> None:
    assert len(ALL_QC_CHECK_NAMES) == 12
    assert ALL_QC_CHECK_NAMES[:9] == (
        "video.container",
        "video.resolution",
        "video.fps",
        "video.duration",
        "video.audio",
        "video.subtitles",
        "video.black_frames",
        "video.integrity",
        "video.metadata",
    )
    assert ALL_QC_CHECK_NAMES[9:] == (
        "qc.rights",
        "qc.policy",
        "qc.ai_disclosure",
    )


def test_test_gate_and_qc_gate_pass_when_all_checks_pass() -> None:
    video = Artifact.create(
        "item-1",
        ArtifactKind.VIDEO,
        uri="video/v1.mp4",
        sha256="a" * 64,
        size_bytes=1024,
        media_type="video/mp4",
        clock=lambda: NOW,
    )
    checks = tuple(QCCheck(name, QCStatus.PASS, "ok") for name in ALL_QC_CHECK_NAMES)
    qc_res = QCResult.create("item-1", (video,), checks, clock=lambda: NOW)

    test_gate = TestGate(_StubQCResults([qc_res]), _StubArtifacts([video]))
    qc_gate = QCGate(_StubQCResults([qc_res]), _StubArtifacts([video]))

    ctx = GateContext(
        _make_item(ContentStatus.APPROVED), ContentStatus.PUBLISHING, SYSTEM, NOW
    )
    assert test_gate.evaluate(ctx).outcome is GateOutcome.PASS
    assert qc_gate.evaluate(ctx).outcome is GateOutcome.PASS


def test_test_gate_and_qc_gate_block_when_missing_or_failed() -> None:
    video = Artifact.create(
        "item-1",
        ArtifactKind.VIDEO,
        uri="video/v1.mp4",
        sha256="a" * 64,
        size_bytes=1024,
        media_type="video/mp4",
        clock=lambda: NOW,
    )
    ctx = GateContext(
        _make_item(ContentStatus.APPROVED), ContentStatus.PUBLISHING, SYSTEM, NOW
    )

    empty_test = TestGate(_StubQCResults([]), _StubArtifacts([video])).evaluate(ctx)
    assert empty_test.gate is GateName.TEST
    assert empty_test.outcome is GateOutcome.BLOCK
    assert empty_test.reasons[0].code == "test.missing"

    failed_checks = (
        QCCheck("video.fps", QCStatus.FAIL, "bad fps"),
        QCCheck("qc.rights", QCStatus.FAIL, "blocked"),
    )
    failed_qc = QCResult.create("item-1", (video,), failed_checks, clock=lambda: NOW)
    test_res = TestGate(_StubQCResults([failed_qc]), _StubArtifacts([video])).evaluate(
        ctx
    )
    qc_res = QCGate(_StubQCResults([failed_qc]), _StubArtifacts([video])).evaluate(ctx)

    assert test_res.outcome is GateOutcome.BLOCK
    assert test_res.reasons[0].code == "test.failed"
    assert qc_res.outcome is GateOutcome.BLOCK
    assert len(qc_res.reasons) == 2
