"""QC Regression Suite Fixtures.

(Prompt Pack v8, prompt K-130), context C10 Quality (Test & QC).

Provides deterministic regression fixture builders for both ``Shorts`` (9:16
vertical, 1080x1920, SRT captions, ShortsMetadata) and ``LongForm`` (16:9
horizontal, 1920x1080, WebVTT captions, LongFormMetadata with chapters) so
``QCReportBuilder`` (#129) can be regression-tested end-to-end across ``PASS``,
``WARN``, and ``FAIL`` scenarios for all 12 checks (#117–#128).
"""

import hashlib
import io
import math
import struct
import wave
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_usage import AssetUsage
from ai_youtube_agent.content.channel import Channel, YouTubeIdentifiers
from ai_youtube_agent.content.disclosure import DisclosureRecord
from ai_youtube_agent.content.disclosure_rule import (
    DISCLOSURE_RULES,
    DisclosureFacts,
    evaluate_disclosure,
)
from ai_youtube_agent.content.longform_metadata import (
    ChapterMetadata,
    LongFormMetadata,
)
from ai_youtube_agent.content.policy_check import PolicyStatus
from ai_youtube_agent.content.qc_report import QCReportBuilder
from ai_youtube_agent.content.rights import RightsRecord, RiskLevel
from ai_youtube_agent.content.rights_assessment import (
    Outcome,
    RightsAssessment,
)
from ai_youtube_agent.content.shorts_metadata import ShortsMetadata
from ai_youtube_agent.content.strategy import (
    Audience,
    Brand,
    Budget,
    Cadence,
    FormatSettings,
    LanguageSettings,
    LongFormFormat,
    Market,
    Monetization,
    Niche,
    Pillar,
    RevenueGoal,
    RevenueSource,
    ShortsFormat,
    StrategyProfile,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import (
    AssetRepository,
    AssetUsageRepository,
)
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.disclosure import DisclosureRepository
from ai_youtube_agent.core.db.repositories.review import RightsRecordRepository
from ai_youtube_agent.core.db.repositories.rights_assessment import (
    RightsAssessmentRepository,
)
from ai_youtube_agent.providers.mock_video_render import stub_mp4

DEFAULT_REGRESSION_TIME = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
REGRESSION_ACTOR = Actor(ActorKind.USER, "qc-regression-owner")


def make_regression_wav(
    duration_ms: int,
    *,
    sample_rate: int = 24000,
    amplitude: int = 8000,
    freq_hz: float = 440.0,
) -> bytes:
    """Build deterministic mono 16-bit PCM WAV bytes."""
    total_frames = max(1, int(sample_rate * duration_ms / 1000))
    cycle_frames = min(total_frames, sample_rate)
    cycle_samples = [
        int(amplitude * math.sin(2.0 * math.pi * freq_hz * i / sample_rate))
        for i in range(cycle_frames)
    ]
    cycle_bytes = struct.pack(f"<{cycle_frames}h", *cycle_samples)
    full_repeats, remainder = divmod(total_frames, cycle_frames)
    pcm = cycle_bytes * full_repeats + cycle_bytes[: remainder * 2]

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def make_regression_mp4(
    duration_ms: int,
    *,
    seed_label: str = "regression-frame",
) -> bytes:
    """Build a valid ISO-BMFF MP4 with non-zero, non-frozen 64-byte mdat blocks."""
    blocks = [
        hashlib.sha256(f"{seed_label}-{idx}".encode()).digest() * 2 for idx in range(16)
    ]
    return stub_mp4(duration_ms, b"".join(blocks))


def make_shorts_srt(duration_ms: int = 15_000) -> bytes:
    """Build valid UTF-8 NFC SRT captions for a Shorts fixture."""
    mid_ms = duration_ms // 2
    end_ms = duration_ms - 200
    mid_s, mid_rem = divmod(mid_ms, 1000)
    end_s, end_rem = divmod(end_ms, 1000)
    text = (
        "1\n"
        f"00:00:00,000 --> 00:00:{mid_s:02d},{mid_rem:03d}\n"
        "Welcome to this quick AI engineering tip.\n\n"
        "2\n"
        f"00:00:{mid_s:02d},{mid_rem:03d} --> 00:00:{end_s:02d},{end_rem:03d}\n"
        "Always validate rendered artifacts before publishing.\n"
    )
    return text.encode("utf-8")


def make_longform_vtt(duration_ms: int = 185_000) -> bytes:
    """Build valid UTF-8 NFC WebVTT captions for a LongForm fixture."""
    text = (
        "WEBVTT\n\n"
        "1\n"
        "00:00:00.000 --> 00:00:05.000\n"
        "Welcome to the complete system architecture deep dive.\n\n"
        "2\n"
        "00:01:00.000 --> 00:01:06.000\n"
        "In chapter two we inspect the quality control pipeline.\n\n"
        "3\n"
        "00:02:20.000 --> 00:02:26.000\n"
        "Finally we review compliance and disclosure verification.\n"
    )
    return text.encode("utf-8")


@dataclass(frozen=True)
class QCRegressionFixture:
    """Complete seeded fixture for either Shorts or LongForm QC regression testing."""

    content_type: ContentType
    database: Database
    storage: ArtifactStorage
    audit: AuditLog
    audit_sink: InMemoryAuditSink
    settings: Settings
    channel: Channel
    strategy: StrategyProfile
    item: ContentItem
    asset: Asset
    rights_record: RightsRecord
    rights_assessment: RightsAssessment
    disclosure_record: DisclosureRecord
    video_artifact: Artifact
    audio_artifact: Artifact
    subtitles_artifact: Artifact
    thumbnail_artifact: Artifact
    metadata_artifact: Artifact
    report_builder: QCReportBuilder


def _store_artifact(
    conn,
    storage: ArtifactStorage,
    content_item_id: str,
    kind: ArtifactKind,
    version: int,
    payload: bytes,
    media_type: str,
    clock: Callable[[], datetime],
) -> Artifact:
    stored = storage.write(
        content_item_id, kind, version, payload, media_type=media_type
    )
    artifact = Artifact.create(
        content_item_id,
        kind,
        uri=stored.uri,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        media_type=media_type,
        clock=clock,
    )
    ArtifactRepository(conn).add(artifact)
    return artifact


def build_shorts_qc_fixture(
    database: Database,
    storage: ArtifactStorage,
    *,
    duration_ms: int = 15_000,
    realistic_visual: bool = False,
    banned_phrases: tuple[str, ...] = ("guaranteed_profit",),
    clock: Callable[[], datetime] = lambda: DEFAULT_REGRESSION_TIME,
) -> QCRegressionFixture:
    """Create a complete, clean Shorts (9:16, 1080x1920, 30fps) fixture.

    Created in ``database`` and ``storage``.
    """
    sink = InMemoryAuditSink()
    audit = AuditLog(sink, clock=clock)
    settings = Settings(_env_file=None)

    channel = Channel.create(
        "Shorts QC Channel",
        YouTubeIdentifiers("UC" + "1" * 22, "@shorts_qc"),
        clock=clock,
    )
    strategy = StrategyProfile.create(
        channel.id,
        market=Market("US"),
        languages=LanguageSettings("en-US", ()),
        audience=Audience("Developers building autonomous media pipelines"),
        niche=Niche("AI Engineering", (Pillar("testing"), Pillar("production"))),
        brand=Brand(
            "AI QC Lab",
            "concise and educational",
            banned_phrases=banned_phrases,
        ),
        format=FormatSettings(ShortsFormat(10, 60), LongFormFormat(181, 900)),
        cadence=Cadence(shorts_per_day=2, longform_per_day=1),
        budget=Budget("USD", Decimal("10.00"), Decimal("200.00")),
        monetization=Monetization((RevenueGoal(RevenueSource.ADS),)),
        actor=REGRESSION_ACTOR,
        clock=clock,
    )
    item = ContentItem.create(
        channel.id,
        strategy.id,
        strategy.version,
        ContentType.SHORTS,
        "Fast AI Quality Control Tip",
        clock=clock,
    )
    asset = Asset.create(
        channel.id,
        AssetKind.IMAGE,
        AssetCategory.GENERATED,
        title="shorts-bg.png",
        source="internal-gen",
        clock=clock,
    )
    rights_record = RightsRecord.create(
        item.id,
        asset.id,
        source="internal-gen",
        license="owned",
        clock=clock,
    ).with_risk_level(RiskLevel.LOW, clock=clock)
    rights_assessment = RightsAssessment.create(
        rights_record.id,
        item.id,
        Outcome(
            level=RiskLevel.LOW,
            rule_codes=("generated.declared",),
            asset_id=asset.id,
            provenance_id=None,
        ),
        assessed_by=REGRESSION_ACTOR,
        clock=clock,
    )
    disc_eval = evaluate_disclosure(
        item.id,
        channel.id,
        DisclosureFacts(
            realistic_person=False,
            realistic_event=False,
            synthetic_voice_of_real_person=False,
            realistic_visual=realistic_visual,
        ),
        (asset,),
        rule_set=DISCLOSURE_RULES,
    )
    disclosure_record = DisclosureRecord.create(
        item.id,
        channel.id,
        disc_eval,
        decided_by=REGRESSION_ACTOR,
        clock=clock,
    )

    shorts_meta = ShortsMetadata(
        project_id="shorts-proj-1",
        project_version=1,
        content_item_id=item.id,
        channel_id=channel.id,
        script_id="shorts-scr-1",
        script_version=1,
        strategy_version=strategy.version,
        title="Fast AI Quality Control Tip #Shorts",
        description="Validate every video artifact before publishing. #Shorts",
        tags=("shorts", "ai", "qc"),
        language="en-US",
        made_for_kids=False,
        contains_synthetic_media=realistic_visual,
        disclosure_decision_id=disclosure_record.id,
        policy_rule_set_id="policy",
        policy_rule_set_version=1,
        policy_status=PolicyStatus.PASS,
    )

    video_bytes = make_regression_mp4(duration_ms, seed_label="shorts-qc")
    audio_bytes = make_regression_wav(duration_ms)
    subtitles_bytes = make_shorts_srt(duration_ms)
    thumbnail_bytes = b"\x89PNG\r\n\x1a\nshorts-thumb"
    metadata_bytes = shorts_meta.to_json_bytes()

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(strategy)
        ContentItemRepository(conn).add(item)
        assets_repo = AssetRepository(conn)
        assets_repo.add(asset)
        AssetUsageRepository(conn).add(
            AssetUsage.create(
                asset.id, item.id, attached_by=REGRESSION_ACTOR, clock=clock
            )
        )
        RightsRecordRepository(conn).add(rights_record)
        RightsAssessmentRepository(conn).add(rights_assessment)
        DisclosureRepository(conn).add(disclosure_record)

        video_artifact = _store_artifact(
            conn,
            storage,
            item.id,
            ArtifactKind.VIDEO,
            1,
            video_bytes,
            "video/mp4",
            clock,
        )
        audio_artifact = _store_artifact(
            conn,
            storage,
            item.id,
            ArtifactKind.AUDIO,
            1,
            audio_bytes,
            "audio/wav",
            clock,
        )
        subtitles_artifact = _store_artifact(
            conn,
            storage,
            item.id,
            ArtifactKind.SUBTITLES,
            1,
            subtitles_bytes,
            "application/x-subrip",
            clock,
        )
        thumbnail_artifact = _store_artifact(
            conn,
            storage,
            item.id,
            ArtifactKind.THUMBNAIL,
            1,
            thumbnail_bytes,
            "image/png",
            clock,
        )
        metadata_artifact = _store_artifact(
            conn,
            storage,
            item.id,
            ArtifactKind.METADATA,
            1,
            metadata_bytes,
            "application/json",
            clock,
        )

    builder = QCReportBuilder(
        database,
        storage,
        audit,
        settings,
        clock=clock,
    )
    return QCRegressionFixture(
        content_type=ContentType.SHORTS,
        database=database,
        storage=storage,
        audit=audit,
        audit_sink=sink,
        settings=settings,
        channel=channel,
        strategy=strategy,
        item=item,
        asset=asset,
        rights_record=rights_record,
        rights_assessment=rights_assessment,
        disclosure_record=disclosure_record,
        video_artifact=video_artifact,
        audio_artifact=audio_artifact,
        subtitles_artifact=subtitles_artifact,
        thumbnail_artifact=thumbnail_artifact,
        metadata_artifact=metadata_artifact,
        report_builder=builder,
    )


def build_longform_qc_fixture(
    database: Database,
    storage: ArtifactStorage,
    *,
    duration_ms: int = 185_000,
    realistic_visual: bool = False,
    banned_phrases: tuple[str, ...] = ("guaranteed_profit",),
    clock: Callable[[], datetime] = lambda: DEFAULT_REGRESSION_TIME,
) -> QCRegressionFixture:
    """Create a complete, clean LongForm (16:9, 1920x1080, 30fps, chapters) fixture."""
    sink = InMemoryAuditSink()
    audit = AuditLog(sink, clock=clock)
    settings = Settings(_env_file=None)

    channel = Channel.create(
        "LongForm QC Channel",
        YouTubeIdentifiers("UC" + "2" * 22, "@longform_qc"),
        clock=clock,
    )
    strategy = StrategyProfile.create(
        channel.id,
        market=Market("US"),
        languages=LanguageSettings("en-US", ()),
        audience=Audience("Software architects and reliability engineers"),
        niche=Niche("System Architecture", (Pillar("quality"), Pillar("compliance"))),
        brand=Brand(
            "Deep Architecture",
            "technical and rigorous",
            banned_phrases=banned_phrases,
        ),
        format=FormatSettings(ShortsFormat(10, 60), LongFormFormat(181, 900)),
        cadence=Cadence(shorts_per_day=1, longform_per_day=1),
        budget=Budget("USD", Decimal("15.00"), Decimal("300.00")),
        monetization=Monetization((RevenueGoal(RevenueSource.ADS),)),
        actor=REGRESSION_ACTOR,
        clock=clock,
    )
    item = ContentItem.create(
        channel.id,
        strategy.id,
        strategy.version,
        ContentType.LONGFORM,
        "Autonomous Video Quality Control Architecture",
        clock=clock,
    )
    asset = Asset.create(
        channel.id,
        AssetKind.VIDEO_CLIP,
        AssetCategory.GENERATED,
        title="longform-broll.mp4",
        source="internal-gen",
        clock=clock,
    )
    rights_record = RightsRecord.create(
        item.id,
        asset.id,
        source="internal-gen",
        license="owned",
        clock=clock,
    ).with_risk_level(RiskLevel.LOW, clock=clock)
    rights_assessment = RightsAssessment.create(
        rights_record.id,
        item.id,
        Outcome(
            level=RiskLevel.LOW,
            rule_codes=("generated.declared",),
            asset_id=asset.id,
            provenance_id=None,
        ),
        assessed_by=REGRESSION_ACTOR,
        clock=clock,
    )
    disc_eval = evaluate_disclosure(
        item.id,
        channel.id,
        DisclosureFacts(
            realistic_person=False,
            realistic_event=False,
            synthetic_voice_of_real_person=False,
            realistic_visual=realistic_visual,
        ),
        (asset,),
        rule_set=DISCLOSURE_RULES,
    )
    disclosure_record = DisclosureRecord.create(
        item.id,
        channel.id,
        disc_eval,
        decided_by=REGRESSION_ACTOR,
        clock=clock,
    )

    chapters = (
        ChapterMetadata("Introduction", 0, 60_000),
        ChapterMetadata("Technical QC Pipeline", 60_000, 140_000),
        ChapterMetadata("Compliance Verification", 140_000, duration_ms),
    )
    description = (
        "Complete walkthrough of the autonomous QC pipeline.\n\n"
        "Chapters:\n"
        "00:00 Introduction\n"
        "01:00 Technical QC Pipeline\n"
        "02:20 Compliance Verification"
    )
    lf_meta = LongFormMetadata(
        project_id="lf-proj-1",
        project_version=1,
        content_item_id=item.id,
        channel_id=channel.id,
        script_id="lf-scr-1",
        script_version=1,
        strategy_version=strategy.version,
        title="Autonomous Video Quality Control Architecture",
        description=description,
        tags=("architecture", "youtube", "qc"),
        chapters=chapters,
        language="en-US",
        made_for_kids=False,
        contains_synthetic_media=realistic_visual,
        disclosure_decision_id=disclosure_record.id,
        policy_rule_set_id="policy",
        policy_rule_set_version=1,
        policy_status=PolicyStatus.PASS,
    )

    video_bytes = make_regression_mp4(duration_ms, seed_label="longform-qc")
    audio_bytes = make_regression_wav(duration_ms)
    subtitles_bytes = make_longform_vtt(duration_ms)
    thumbnail_bytes = b"\x89PNG\r\n\x1a\nlongform-thumb"
    metadata_bytes = lf_meta.to_bytes()

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(strategy)
        ContentItemRepository(conn).add(item)
        assets_repo = AssetRepository(conn)
        assets_repo.add(asset)
        AssetUsageRepository(conn).add(
            AssetUsage.create(
                asset.id, item.id, attached_by=REGRESSION_ACTOR, clock=clock
            )
        )
        RightsRecordRepository(conn).add(rights_record)
        RightsAssessmentRepository(conn).add(rights_assessment)
        DisclosureRepository(conn).add(disclosure_record)

        video_artifact = _store_artifact(
            conn,
            storage,
            item.id,
            ArtifactKind.VIDEO,
            1,
            video_bytes,
            "video/mp4",
            clock,
        )
        audio_artifact = _store_artifact(
            conn,
            storage,
            item.id,
            ArtifactKind.AUDIO,
            1,
            audio_bytes,
            "audio/wav",
            clock,
        )
        subtitles_artifact = _store_artifact(
            conn,
            storage,
            item.id,
            ArtifactKind.SUBTITLES,
            1,
            subtitles_bytes,
            "text/vtt",
            clock,
        )
        thumbnail_artifact = _store_artifact(
            conn,
            storage,
            item.id,
            ArtifactKind.THUMBNAIL,
            1,
            thumbnail_bytes,
            "image/png",
            clock,
        )
        metadata_artifact = _store_artifact(
            conn,
            storage,
            item.id,
            ArtifactKind.METADATA,
            1,
            metadata_bytes,
            "application/json",
            clock,
        )

    builder = QCReportBuilder(
        database,
        storage,
        audit,
        settings,
        clock=clock,
    )
    return QCRegressionFixture(
        content_type=ContentType.LONGFORM,
        database=database,
        storage=storage,
        audit=audit,
        audit_sink=sink,
        settings=settings,
        channel=channel,
        strategy=strategy,
        item=item,
        asset=asset,
        rights_record=rights_record,
        rights_assessment=rights_assessment,
        disclosure_record=disclosure_record,
        video_artifact=video_artifact,
        audio_artifact=audio_artifact,
        subtitles_artifact=subtitles_artifact,
        thumbnail_artifact=thumbnail_artifact,
        metadata_artifact=metadata_artifact,
        report_builder=builder,
    )
