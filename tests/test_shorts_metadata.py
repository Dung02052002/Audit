"""I-102 Shorts metadata (Prompt Pack v8, prompt #102).

Tests for:
- ``ShortsMetadata`` validation (title <= 100 chars, description <= 5000 chars,
  no ``<`` or ``>`` angle brackets, tags <= 30 items and <= 500 total chars,
  case-insensitive tag deduplication, ``#Shorts`` marker requirement, BCP-47
  language tag, ``made_for_kids``, ``contains_synthetic_media``, canonical JSON
  round-trip);
- ``ShortsMetadataRecorder.record`` storing ``ArtifactKind.METADATA``
  (``application/json``), appending timeline asset attributions and ``#Shorts``,
  enforcing AI disclosure decision presence, enforcing policy checks (blocking
  banned phrases), replaying identical JSON without writing or auditing,
  invalidating pending approvals on version bump, cleaning up on failure, and
  auditing scalar-only metadata.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from factories import T0, USER, at, make_approval_request
from test_shorts_scene import Stage

from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.asset import AssetCategory, AssetKind
from ai_youtube_agent.content.disclosure import DisclosureDecision
from ai_youtube_agent.content.disclosure_decider import DisclosureDecider
from ai_youtube_agent.content.disclosure_rule import (
     DISCLOSURE_RULES,
    DisclosureFacts,
)
from ai_youtube_agent.content.policy_check import PolicyStatus
from ai_youtube_agent.content.shorts_metadata import (
    METADATA_MEDIA_TYPE,
    METADATA_RECORDED,
    ShortsMetadata,
    ShortsMetadataConflictError,
    ShortsMetadataError,
    ShortsMetadataErrorCode,
    ShortsMetadataRecorder,
    has_shorts_marker,
)
from ai_youtube_agent.content.shorts_timeline import AssetRole, SceneAsset
from ai_youtube_agent.content.strategy import Brand
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, AuditResult, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import ArtifactRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository

Code = ShortsMetadataErrorCode
T1 = T0 + timedelta(hours=1)
SECRET_DESC = "Secret-Panther-4421"


def test_has_shorts_marker_recognises_hashtag_or_tag() -> None:
    assert has_shorts_marker("My Clip #Shorts", "desc", ()) is True
    assert has_shorts_marker("My Clip", "Watch now\n#shorts!", ()) is True
    assert has_shorts_marker("My Clip", "Watch now", ("tech", "Shorts")) is True
    assert has_shorts_marker("My Clip #shortstory", "Watch now", ("video",)) is False


def test_shorts_metadata_json_round_trip_and_validation() -> None:
    meta = ShortsMetadata(
        project_id="p1",
        project_version=2,
        content_item_id="ci1",
        channel_id="ch1",
        script_id="sc1",
        script_version=1,
        strategy_version=1,
        title="  Fast   Python Tip  ",
        description="Learn this in 30 seconds.\n#Shorts",
        tags=("Python", "python", "Coding"),
        language="en-US",
        made_for_kids=False,
        contains_synthetic_media=True,
        disclosure_decision_id="disc1",
        policy_rule_set_id="policy",
        policy_rule_set_version=1,
        policy_status=PolicyStatus.PASS,
    )
    assert meta.title == "Fast Python Tip"
    assert meta.tags == ("Python", "Coding")
    raw = meta.to_json_bytes()
    assert raw.endswith(b"\n")
    restored = ShortsMetadata.from_json_bytes(raw)
    assert restored == meta

    # Refuses angle brackets, missing #Shorts marker, or blocked policy status
    with pytest.raises(ValueError, match="angle|<|>"):
        ShortsMetadata.from_dict({**meta.to_dict(), "title": "Bad <Title> #Shorts"})

    with pytest.raises(ValueError, match="#Shorts"):
        ShortsMetadata.from_dict(
            {
                **meta.to_dict(),
                "title": "No Marker",
                "description": "Plain description",
                "tags": ["python"],
            }
        )

    with pytest.raises(ValueError, match="blocked"):
        ShortsMetadata.from_dict({**meta.to_dict(), "policy_status": "block"})


class MetadataRig:
    def __init__(self, database: Database, tmp_path: Path) -> None:
        self.database = database
        self.stage = Stage(database)
        self.service = self.stage.world.service
        self.item = self.stage.world.item
        self.bg_asset = self.stage.add_asset(
            AssetKind.IMAGE,
            category=AssetCategory.LICENSED,
            license_ref="CC-BY-4.0",
            attribution="Photo by Alice (CC BY 4.0)",
            provenance=True,
        )
        self.project = self.stage.save(
            self.stage.timeline(SceneAsset(self.bg_asset.id, AssetRole.BACKGROUND))
        )
        self.root = tmp_path / "artifacts"
        self.storage = LocalFileStorage(self.root)
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=at(T1))
        self.decider = DisclosureDecider(self.database, self.audit, clock=at(T0))

    def decide_disclosure(self, *, realistic_visual: bool = False):
        return self.decider.decide(
            self.item.id,
            DisclosureFacts(realistic_visual=realistic_visual),
            rule_set=DISCLOSURE_RULES,
            actor=USER,
        )

    def recorder(self) -> ShortsMetadataRecorder:
        return ShortsMetadataRecorder(
            self.database,
            self.storage,
            self.audit,
            clock=at(T1),
        )


@pytest.fixture
def rig(database: Database, tmp_path: Path) -> MetadataRig:
    return MetadataRig(database, tmp_path)


def test_record_stores_metadata_appends_attributions_and_audits_scalars_only(
    rig: MetadataRig,
) -> None:
    disclosure = rig.decide_disclosure(realistic_visual=True)
    assert disclosure.decision is DisclosureDecision.REQUIRED
    rig.sink.events.clear()

    record = rig.recorder().record(
        rig.project.id,
        title="Quick AI Trick",
        description=f"Watch this {SECRET_DESC}",
        tags=("AI", "Tech", "ai"),
        actor=USER,
        expected_version=rig.project.version,
    )

    assert record.created is True
    assert record.artifact.kind is ArtifactKind.METADATA
    assert record.artifact.version == 1
    assert record.artifact.media_type == METADATA_MEDIA_TYPE
    assert record.metadata.contains_synthetic_media is True
    assert record.metadata.disclosure_decision_id == disclosure.id
    assert "Attribution: Photo by Alice (CC BY 4.0)" in record.metadata.description
    assert record.metadata.description.endswith("#Shorts")
    assert record.metadata.tags == ("AI", "Tech")

    stored = ShortsMetadata.from_json_bytes(rig.storage.read(record.artifact.uri))
    assert stored == record.metadata

    assert len(rig.sink.events) == 1
    event = rig.sink.events[0]
    assert event.action == METADATA_RECORDED
    assert event.result is AuditResult.SUCCESS
    assert event.metadata["contains_synthetic_media"] is True
    assert SECRET_DESC not in str(event.metadata)
    assert "Quick AI Trick" not in str(event.metadata)


def test_record_replays_when_unchanged_and_invalidates_approvals_on_new_version(
    rig: MetadataRig,
) -> None:
    rig.decide_disclosure(realistic_visual=False)
    recorder = rig.recorder()
    first = recorder.record(
        rig.project.id,
        description="First description",
        tags=("shorts",),
        actor=USER,
    )
    replay = recorder.record(
        rig.project.id,
        description="First description",
        tags=("shorts",),
        actor=USER,
    )
    assert replay.created is False
    assert replay.artifact == first.artifact

    approval = make_approval_request(
        content_item_id=rig.item.id,
        artifacts=(first.artifact,),
    )
    with rig.database.transaction() as connection:
        ApprovalRequestRepository(connection).add(approval)

    second = recorder.record(
        rig.project.id,
        description="Updated description",
        tags=("shorts",),
        actor=USER,
    )
    assert second.created is True
    assert second.artifact.version == 2
    assert second.invalidation is not None
    assert second.invalidation.invalidated_ids == (approval.id,)
    with rig.database.transaction() as connection:
        updated = ApprovalRequestRepository(connection).get(approval.id)
        assert updated is not None
        assert updated.status is ApprovalStatus.INVALIDATED


def test_record_refuses_missing_disclosure_or_policy_block(
    rig: MetadataRig,
) -> None:
    recorder = rig.recorder()

    # Disclosure missing
    with pytest.raises(ShortsMetadataError) as exc_info:
        recorder.record(rig.project.id, description="Desc", actor=USER)
    assert exc_info.value.code == Code.DISCLOSURE_MISSING.value

    rig.decide_disclosure(realistic_visual=False)

    # Configure banned phrase on channel strategy
    with rig.database.transaction() as connection:
        strategies = StrategyProfileRepository(connection)
        current = strategies.get_by_channel(rig.item.channel_id)
        assert current is not None
        updated = current.update(
            brand=Brand(
                tone="Clear",
                banned_phrases=("guaranteed riches",),
            ),
            actor=USER,
            clock=at(T1),
        )
        strategies.update(updated, expected_version=current.version)

    with pytest.raises(ShortsMetadataError) as exc_info:
        recorder.record(
            rig.project.id,
            description="Get guaranteed riches today!",
            actor=USER,
        )
    assert exc_info.value.code == Code.POLICY_BLOCKED.value


def test_record_refuses_version_conflict_and_cleans_up_on_db_error(
    rig: MetadataRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.decide_disclosure(realistic_visual=False)
    recorder = rig.recorder()

    with pytest.raises(ShortsMetadataConflictError):
        recorder.record(
            rig.project.id,
            description="Desc",
            actor=USER,
            expected_version=rig.project.version + 5,
        )

    def boom(self, artifact):
        raise RuntimeError("db failure")

    monkeypatch.setattr(ArtifactRepository, "add", boom)
    with pytest.raises(RuntimeError, match="db failure"):
        recorder.record(rig.project.id, description="Desc", actor=USER)

    assert not rig.root.exists() or list(rig.root.rglob("*.json")) == []

