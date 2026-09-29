"""Fixture factories: one function per entity that builds a valid object.

Every factory uses a fixed UTC clock and accepts keyword overrides. Factories
only build objects; tests save them explicitly, so each write stays visible.
Parents are passed in, so ids line up when rows are stored.
"""

import dataclasses
import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.content.approval import ApprovalRequest, ApprovalStatus
from ai_youtube_agent.content.channel import Channel, YouTubeIdentifiers
from ai_youtube_agent.content.comment import (
    Comment,
    CommentClassification,
    CommentLabel,
    ReplyDraft,
)
from ai_youtube_agent.content.cost import CostCategory, CostRecord
from ai_youtube_agent.content.experiment import (
    Experiment,
    ExperimentType,
    ExperimentVariant,
)
from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.content.revenue import RevenueRecord, RevenueStage
from ai_youtube_agent.content.rights import RightsRecord
from ai_youtube_agent.content.script import Claim, Evidence, Script
from ai_youtube_agent.content.strategy import (
    Audience,
    Brand,
    Budget,
    Cadence,
    LanguageSettings,
    Market,
    Monetization,
    Niche,
    StrategyProfile,
)
from ai_youtube_agent.content.voice import AudioMetadata, VoiceProfile
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.pipeline.job import AIJob, Session
from ai_youtube_agent.pipeline.publish import PublishJob

T0 = datetime(2026, 9, 30, 8, 0, 0, 123456, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner-1")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
AI = Actor(ActorKind.AI, "assistant")
_counter = itertools.count(1)


def at(moment: datetime = T0):
    return lambda: moment


def _unique() -> int:
    return next(_counter)


def make_channel(**overrides) -> Channel:
    n = _unique()
    channel = Channel.create(
        f"Channel {n}",
        YouTubeIdentifiers("UC" + f"{n:022d}", f"@channel{n}"),
        clock=at(),
    )
    return dataclasses.replace(channel, **overrides)


def make_strategy_profile(channel: Channel, **overrides) -> StrategyProfile:
    profile = StrategyProfile.create(
        channel.id,
        market=Market("VN"),
        languages=LanguageSettings("vi", ("en-US",)),
        audience=Audience("Adults interested in personal finance"),
        niche=Niche("Personal finance", ("budgeting", "investing")),
        brand=Brand("Money Minute", "calm and clear"),
        cadence=Cadence(shorts_per_day=2, longform_per_day=0),
        budget=Budget("USD", Decimal("5.00"), Decimal("100.00")),
        monetization=Monetization(("ads", "affiliate")),
        actor=USER,
        clock=at(),
    )
    return dataclasses.replace(profile, **overrides)


def make_voice_profile(channel: Channel, **overrides) -> VoiceProfile:
    voice = VoiceProfile.create(
        channel.id,
        provider="mock_tts",
        voice_id="vi-female-01",
        language="vi",
        speaking_style="warm",
        actor=USER,
        clock=at(),
    )
    return dataclasses.replace(voice, **overrides)


def make_content_item(
    channel: Channel, strategy: StrategyProfile, **overrides
) -> ContentItem:
    item = ContentItem.create(
        channel.id,
        strategy.id,
        strategy.version,
        ContentType.SHORTS,
        "Five bank fees you never noticed",
        clock=at(),
    )
    return dataclasses.replace(item, **overrides)


def make_artifact(
    item: ContentItem, kind: ArtifactKind = ArtifactKind.VIDEO, **overrides
) -> Artifact:
    n = _unique()
    artifact = Artifact.create(
        item.id,
        kind,
        uri=f"store://{item.id}/{kind.value}/{n}",
        sha256=f"{n:064x}",
        size_bytes=1000 + n,
        media_type="application/octet-stream",
        clock=at(),
    )
    return dataclasses.replace(artifact, **overrides)


def make_script(item: ContentItem, **overrides) -> Script:
    script = Script.create(item.id, "Most people never check bank fees.", clock=at())
    return dataclasses.replace(script, **overrides)


def make_claim(script: Script, **overrides) -> Claim:
    claim = Claim.create(script.id, "Most people never check bank fees.", clock=at())
    return dataclasses.replace(claim, **overrides)


def make_evidence(claim: Claim, **overrides) -> Evidence:
    evidence = Evidence.create(
        claim.id, "source-1", "61% never compare fees.", clock=at()
    )
    return dataclasses.replace(evidence, **overrides)


def make_audio_metadata(
    artifact: Artifact, script: Script, voice: VoiceProfile, **overrides
) -> AudioMetadata:
    audio = AudioMetadata.create(artifact, script, voice, duration_ms=30250, clock=at())
    return dataclasses.replace(audio, **overrides)


def make_rights_record(item: ContentItem, **overrides) -> RightsRecord:
    record = RightsRecord.create(
        item.id,
        f"asset-{_unique()}",
        source="stock-music.example",
        license="CC-BY-4.0",
        clock=at(),
    )
    return dataclasses.replace(record, **overrides)


def make_qc_result(
    item: ContentItem, artifacts: list[Artifact], **overrides
) -> QCResult:
    result = QCResult.create(
        item.id,
        artifacts,
        [
            QCCheck("video.fps", QCStatus.PASS),
            QCCheck("audio.silence", QCStatus.WARN, "2 s of silence"),
        ],
        clock=at(),
    )
    return dataclasses.replace(result, **overrides)


def make_approval_request(
    item: ContentItem,
    artifacts: list[Artifact],
    qc_result: QCResult | None = None,
    **overrides,
) -> ApprovalRequest:
    request = ApprovalRequest.create(
        item.id, artifacts, requested_by=SYSTEM, qc_result=qc_result, clock=at()
    )
    return dataclasses.replace(request, **overrides)


def make_publish_job(approval: ApprovalRequest, **overrides) -> PublishJob:
    approved = dataclasses.replace(approval, status=ApprovalStatus.APPROVED)
    job = PublishJob.create(
        approved, ContentType.SHORTS, f"publish:{approval.id}", clock=at()
    )
    return dataclasses.replace(job, **overrides)


def make_metric_snapshot(**overrides) -> MetricSnapshot:
    snapshot = MetricSnapshot.create(
        MetricScope.VIDEO,
        "dQw4w9WgXcQ",
        source="youtube_analytics",
        period_start=T0 - timedelta(days=1),
        period_end=T0,
        metrics={
            "views": Decimal("1520"),
            "subscribers_net": Decimal("-3"),
            "ctr": Decimal("0.0412"),
        },
        clock=at(T0 + timedelta(hours=2)),
    )
    return dataclasses.replace(snapshot, **overrides)


def make_revenue_record(**overrides) -> RevenueRecord:
    record = RevenueRecord.create(
        RevenueStage.ESTIMATED,
        MetricScope.CHANNEL,
        "channel-1",
        revenue_type="ads",
        source="youtube_analytics",
        amount=Decimal("12.30"),
        currency="USD",
        period_start=T0 - timedelta(days=30),
        period_end=T0,
        clock=at(T0 - timedelta(days=1)),
    )
    return dataclasses.replace(record, **overrides)


def make_cost_record(channel: Channel, **overrides) -> CostRecord:
    record = CostRecord.create(
        channel.id,
        CostCategory.TTS,
        provider="mock_tts",
        amount=Decimal("0.0421"),
        currency="USD",
        ref="tts-job-1",
        clock=at(),
    )
    return dataclasses.replace(record, **overrides)


def make_comment(channel: Channel, **overrides) -> Comment:
    comment = Comment.create(
        channel.id,
        f"Ugx-comment-{_unique()}",
        "dQw4w9WgXcQ",
        author_display_name="Viewer One",
        text="Great video, what about index funds?",
        published_at=T0,
    )
    return dataclasses.replace(comment, **overrides)


def make_classification(comment: Comment, **overrides) -> CommentClassification:
    classification = CommentClassification.create(
        comment.id,
        CommentLabel.REPLY_CANDIDATE,
        classified_by=AI,
        rationale="asks a question",
        clock=at(),
    )
    return dataclasses.replace(classification, **overrides)


def make_reply_draft(comment: Comment, **overrides) -> ReplyDraft:
    draft = ReplyDraft.create(
        comment.id, "Thanks! Index funds are next week.", created_by=AI, clock=at()
    )
    return dataclasses.replace(draft, **overrides)


def make_session(**overrides) -> Session:
    session = Session.start(actor=USER, clock=at())
    return dataclasses.replace(session, **overrides)


def make_ai_job(
    item: ContentItem | None = None, session: Session | None = None, **overrides
) -> AIJob:
    job = AIJob.create(
        "script.generate",
        f"script.generate:{_unique()}",
        content_item_id=item.id if item else None,
        session_id=session.id if session else None,
        clock=at(),
    )
    return dataclasses.replace(job, **overrides)


def make_experiment(
    channel: Channel, item: ContentItem | None = None, **overrides
) -> Experiment:
    experiment = Experiment.propose(
        channel.id,
        ExperimentType.TITLE,
        hypothesis="A direct 'you' title gets a higher CTR.",
        variants=[
            ExperimentVariant("a", "5 bank fees you never noticed"),
            ExperimentVariant("b", "Your bank is charging you for this"),
        ],
        proposed_by=AI,
        content_item_id=item.id if item else None,
        clock=at(),
    )
    return dataclasses.replace(experiment, **overrides)
