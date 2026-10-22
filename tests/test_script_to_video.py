"""End-to-end tests for the Script-to-Film & Script-to-Video Engine."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container, prepare_database
from ai_youtube_agent.content.qc import QCStatus
from ai_youtube_agent.content.scene_planner import (
    CameraMotion,
    SceneBreakdownPlanner,
    SceneStatus,
    ShotType,
)
from ai_youtube_agent.content.story_bible import (
    ProductionMode,
    ScriptAnalyzer,
    SpeakerKind,
)
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.main import create_app
from ai_youtube_agent.pipeline.script_to_video import (
    ScriptToVideoError,
    ScriptToVideoErrorCode,
    ScriptToVideoRequest,
    ScriptToVideoService,
    ScriptToVideoStage,
)
from ai_youtube_agent.providers.mock_video_generation import MockVideoClipGenerator
from ai_youtube_agent.providers.video_generation import VideoClipGenerator
from ai_youtube_agent.providers.video_render import probe_mp4

SAMPLE_SHORTS_SCRIPT = """
SCENE 1: PHÒNG TRÀ CỔ - BAN ĐÊM
Minh (trầm ngâm): Mỗi tách trà nóng đều giữ lại hương vị của một mùa cũ đã đi qua.
Narrator: Ánh đèn vàng phản chiếu trên mặt bàn gỗ tĩnh lặng giữa đêm mưa.

SCENE 2: HIÊN NHÀ MƯA - BAN ĐÊM
Lan (dịu dàng): Khi cơn mưa đầu mùa ghé qua phố nhỏ, câu chuyện năm ấy lại trở về rõ \
ràng.
Minh (ấm áp): Những ký ức chân thành nhất luôn ở lại cùng thời gian.

SCENE 3: PHÒNG TRÀ CỔ - BÌNH MINH
Narrator: Bình minh dần hé mở qua khung cửa sổ nhỏ, mang theo hơi ấm mới.
Lan (mỉm cười): Hãy trân trọng từng khoảnh khắc bình yên ngay trong hiện tại.
"""

SAMPLE_LONGFORM_SCRIPT = """
SCENE 1: THƯ VIỆN CỔ - BUỔI SÁNG
Narrator: Giữa lòng thành phố cổ kính, thư viện trăm năm tuổi lưu giữ hàng ngàn bản \
thảo viết tay.
An (tò mò): Cuốn sổ bìa da này ghi lại hành trình của những người thợ đồng hồ thế kỷ \
trước.

SCENE 2: XƯỞNG ĐỒNG HỒ CŨ - BAN NGÀY
Ông Bảo (điềm tĩnh): Mỗi bánh răng nhỏ đều có nhịp đập riêng, đòi hỏi sự kiên nhẫn \
tuyệt đối.
An (chăm chú): Khi lắng nghe tiếng tích tắc đều đặn, cháu hiểu vì sao thời gian lại \
quý giá đến vậy.

SCENE 3: THÁP CHUÔNG THÀNH PHỐ - HOÀNG HÔN
Narrator: Ánh hoàng hôn nhuộm vàng những mái ngói cổ khi hai thầy trò bước lên tháp \
chuông cao nhất.
Ông Bảo (xúc động): Chiếc đồng hồ lớn này đã điểm nhịp cho biết bao thế hệ cư dân nơi \
đây.

SCENE 4: XƯỞNG ĐỒNG HỒ CŨ - BAN ĐÊM
An (quyết tâm): Cháu sẽ khôi phục lại cơ cấu dây cót chính để tiếng chuông vang lên \
đúng vào sáng mai.
Narrator: Dưới ánh đèn dầu ấm áp, từng chi tiết đồng thau được lau chùi và lắp ghép \
chuẩn xác.

SCENE 5: QUẢNG TRƯỜNG TRUNG TÂM - BÌNH MINH
Narrator: Khi tia nắng đầu tiên chạm vào mặt đồng hồ, tiếng chuông ngân vang trong \
trẻo khắp quảng trường.
Ông Bảo (mỉm cười): Truyền thống không nằm ở quá khứ, mà sống tiếp trong đôi tay của \
người trẻ hôm nay.
"""


def _make_settings(tmp_path: Path, *, longform: bool = False) -> Settings:
    return Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "script_to_video.sqlite3",
        artifact_dir=tmp_path / "artifacts",
        flags=FeatureFlags(longform_enabled=longform),
    )


def test_script_analysis_and_story_bible_extraction() -> None:
    """1. Script parsing + Story Bible extraction (characters, locations, arc,
    continuity).
    """
    analyzer = ScriptAnalyzer()
    output = analyzer.analyze(
        script_text=SAMPLE_SHORTS_SCRIPT,
        title="Hương Trà Đêm Mưa",
        content_type=ContentType.SHORTS,
        production_mode=ProductionMode.STORY_VIDEO,
        visual_style="cinematic_watercolor",
        language="vi",
    )

    bible = output.story_bible
    assert bible.title == "Hương Trà Đêm Mưa"
    assert bible.content_type is ContentType.SHORTS
    assert bible.production_mode is ProductionMode.STORY_VIDEO
    assert bible.aspect_ratio == "9:16"
    assert len(bible.characters) >= 3  # Narrator, Minh, Lan
    char_names = {c.name for c in bible.characters}
    assert "Minh" in char_names
    assert "Lan" in char_names
    assert len(bible.locations) >= 2
    assert len(bible.continuity_constraints) >= 4
    assert len(output.sections) == 3
    assert len(output.beats) == 3
    assert any(
        seg.speaker_kind is SpeakerKind.CHARACTER
        for beat in output.beats
        for seg in beat.segments
    )


def test_scene_breakdown_and_shot_planning() -> None:
    """2. Scene breakdown + shot planning with camera motion and continuity prompts."""
    analyzer = ScriptAnalyzer()
    planner = SceneBreakdownPlanner()
    analysis = analyzer.analyze(
        script_text=SAMPLE_SHORTS_SCRIPT,
        content_type=ContentType.SHORTS,
        production_mode=ProductionMode.STORY_VIDEO,
    )
    breakdown = planner.plan(
        analysis, scene_windows_ms=[(0, 6000), (6000, 13500), (13500, 20000)]
    )

    assert breakdown.total_duration_ms == 20_000
    assert breakdown.canvas_size == (1080, 1920)
    assert len(breakdown.scenes) == 3
    for scene in breakdown.scenes:
        assert scene.status is SceneStatus.PLANNED
        assert len(scene.shots) >= 1
        assert sum(sh.duration_ms for sh in scene.shots) == scene.duration_ms
        assert " Continuity:" in scene.visual_prompt or "Style:" in scene.visual_prompt
        for shot in scene.shots:
            assert isinstance(shot.shot_type, ShotType)
            assert isinstance(shot.camera_motion, CameraMotion)


def test_shorts_story_video_end_to_end(tmp_path: Path) -> None:
    """3. End-to-end Shorts STORY_VIDEO pipeline producing a real playable MP4 + QC +
    Preview.
    """
    settings = _make_settings(tmp_path)
    prepare_database(settings)
    container = build_container(settings)
    service = container.resolve(ScriptToVideoService)

    manifest = service.run(
        ScriptToVideoRequest(
            script_text=SAMPLE_SHORTS_SCRIPT,
            title="Hương Trà Đêm Mưa",
            content_type=ContentType.SHORTS,
            production_mode=ProductionMode.STORY_VIDEO,
            visual_style="cinematic_story",
            language="vi",
            subtitles_enabled=True,
            request_approval=True,
        )
    )

    assert manifest.content_type is ContentType.SHORTS
    assert manifest.production_mode is ProductionMode.STORY_VIDEO
    assert manifest.status is ContentStatus.AWAITING_APPROVAL
    assert manifest.approval_id is not None
    assert manifest.qc_report.status in (QCStatus.PASS, QCStatus.WARN)

    video_path = Path(manifest.video_file_path)
    assert video_path.exists()
    video_bytes = video_path.read_bytes()
    assert len(video_bytes) == manifest.video_size_bytes
    mp4_info = probe_mp4(video_bytes)
    assert mp4_info.duration_ms > 0
    assert abs(mp4_info.duration_ms - manifest.audio_duration_ms) <= 500

    assert manifest.subtitle_artifact_id is not None
    assert manifest.thumbnail_artifact_id is not None
    assert manifest.metadata_artifact_id is not None
    assert len(manifest.scene_assets) == len(manifest.scene_breakdown.scenes)
    assert ScriptToVideoStage.VIDEO_RENDERED in manifest.completed_stages
    assert ScriptToVideoStage.QC_COMPLETED in manifest.completed_stages
    assert ScriptToVideoStage.PREVIEW_READY in manifest.completed_stages
    assert ScriptToVideoStage.APPROVAL_REQUESTED in manifest.completed_stages


def test_longform_story_video_end_to_end(tmp_path: Path) -> None:
    """4. End-to-end LongForm STORY_VIDEO pipeline producing a 16:9 MP4 (>= 181s) +
    chapters + QC.
    """
    settings = _make_settings(tmp_path, longform=True)
    prepare_database(settings)
    container = build_container(settings)
    service = container.resolve(ScriptToVideoService)

    manifest = service.run(
        ScriptToVideoRequest(
            script_text=SAMPLE_LONGFORM_SCRIPT,
            title="Người Giữ Nhịp Thời Gian",
            content_type=ContentType.LONGFORM,
            production_mode=ProductionMode.STORY_VIDEO,
            visual_style="historical_cinematic",
            language="vi",
            subtitles_enabled=True,
            request_approval=False,
        )
    )

    assert manifest.content_type is ContentType.LONGFORM
    assert manifest.status is ContentStatus.PREVIEW_READY
    assert manifest.approval_id is None
    assert manifest.audio_duration_ms >= 181_000
    assert manifest.qc_report.status in (QCStatus.PASS, QCStatus.WARN)

    video_path = Path(manifest.video_file_path)
    assert video_path.exists()
    mp4_info = probe_mp4(video_path.read_bytes())
    assert mp4_info.duration_ms >= 181_000
    assert manifest.scene_breakdown.canvas_size == (1920, 1080)
    assert len(manifest.scene_assets) >= 5


def test_empty_script_and_missing_file_rejected(tmp_path: Path) -> None:
    """5. Empty script or missing script file fails with EMPTY_SCRIPT error code."""
    settings = _make_settings(tmp_path)
    prepare_database(settings)
    container = build_container(settings)
    service = container.resolve(ScriptToVideoService)

    with pytest.raises(ScriptToVideoError) as exc_info:
        service.run(ScriptToVideoRequest(script_text="   \n  "))
    assert exc_info.value.code == ScriptToVideoErrorCode.EMPTY_SCRIPT.value

    with pytest.raises(ScriptToVideoError) as file_exc:
        service.run(ScriptToVideoRequest(script_path=tmp_path / "nonexistent.txt"))
    assert file_exc.value.code == ScriptToVideoErrorCode.EMPTY_SCRIPT.value


def test_ai_film_mode_capability_guard_and_mock_clip_provider(tmp_path: Path) -> None:
    """6. AI_FILM mode is honestly refused when unconfigured, and works when a
    VideoClipGenerator is registered.
    """
    settings = _make_settings(tmp_path)
    prepare_database(settings)
    container = build_container(settings)
    service = container.resolve(ScriptToVideoService)

    caps = service.check_capabilities()
    assert ProductionMode.STORY_VIDEO in caps.supported_modes
    assert ProductionMode.AI_FILM not in caps.supported_modes
    assert "ai_film" in caps.unavailable_reasons

    with pytest.raises(ScriptToVideoError) as exc_info:
        service.run(
            ScriptToVideoRequest(
                script_text=SAMPLE_SHORTS_SCRIPT,
                content_type=ContentType.SHORTS,
                production_mode=ProductionMode.AI_FILM,
            )
        )
    assert exc_info.value.code == ScriptToVideoErrorCode.MODE_UNAVAILABLE.value

    # Now register MockVideoClipGenerator and verify AI_FILM mode succeeds end-to-end
    film_container = build_container(settings)
    with film_container.override(
        VideoClipGenerator, lambda _c: MockVideoClipGenerator()
    ):
        film_service = film_container.resolve(ScriptToVideoService)
        film_caps = film_service.check_capabilities()
        assert ProductionMode.AI_FILM in film_caps.supported_modes

        manifest = film_service.run(
            ScriptToVideoRequest(
                script_text=SAMPLE_SHORTS_SCRIPT,
                title="AI Film Mode Short",
                content_type=ContentType.SHORTS,
                production_mode=ProductionMode.AI_FILM,
            )
        )
    assert manifest.production_mode is ProductionMode.AI_FILM
    assert all(sa.media_type == "video/mp4" for sa in manifest.scene_assets)
    assert Path(manifest.video_file_path).exists()


def test_resume_reuses_scene_assets_and_api_routes(tmp_path: Path) -> None:
    """7. Re-running for the same content_item_id reuses generated scene assets, and
    HTTP API works.
    """
    settings = _make_settings(tmp_path)
    prepare_database(settings)
    container = build_container(settings)
    service = container.resolve(ScriptToVideoService)

    first = service.run(
        ScriptToVideoRequest(
            script_text=SAMPLE_SHORTS_SCRIPT,
            title="Idempotent Story Run",
            content_item_id="item_resume_test_01",
        )
    )
    assert all(not sa.reused for sa in first.scene_assets)

    second = service.run(
        ScriptToVideoRequest(
            script_text=SAMPLE_SHORTS_SCRIPT,
            title="Idempotent Story Run",
            content_item_id="item_resume_test_01",
        )
    )
    assert second.content_item_id == first.content_item_id
    assert all(sa.reused for sa in second.scene_assets)
    assert second.video_sha256 == first.video_sha256

    # Verify FastAPI HTTP endpoints
    app = create_app(container)
    client = TestClient(app)

    cap_resp = client.get("/script-to-video/capabilities")
    assert cap_resp.status_code == 200
    assert "story_video" in cap_resp.json()["supported_modes"]

    analyze_resp = client.post(
        "/script-to-video/analyze",
        json={
            "script_text": SAMPLE_SHORTS_SCRIPT,
            "title": "API Analyze Test",
            "content_type": "shorts",
            "production_mode": "story_video",
        },
    )
    assert analyze_resp.status_code == 200
    assert analyze_resp.json()["story_bible"]["title"] == "API Analyze Test"


def test_rerun_after_restart_reuses_recorded_audio(tmp_path: Path) -> None:
    """8. A rerun in a new process (empty TTS chunk store) reuses the recorded
    audio instead of failing with domain.tts_result_unavailable."""
    settings = _make_settings(tmp_path)
    prepare_database(settings)
    request = ScriptToVideoRequest(
        script_text=SAMPLE_SHORTS_SCRIPT,
        title="Restart Story Run",
        content_item_id="item_restart_test_01",
    )
    first = build_container(settings).resolve(ScriptToVideoService).run(request)

    # A fresh container stands for a restarted server: nothing is in memory.
    second = build_container(settings).resolve(ScriptToVideoService).run(request)

    assert second.content_item_id == first.content_item_id
    assert second.audio_artifact_id == first.audio_artifact_id
    assert second.video_sha256 == first.video_sha256
    assert Path(second.video_file_path).exists()
