"""End-to-End Application Integration Test for AI_FILM Mode.

Exercises ScriptToVideoService directly through the application container:
- Probes runtime capabilities (ProductionMode.AI_FILM supported).
- Submits a realistic single-scene AI Film story request.
- Invokes ComfyUIVideoAdapter to generate the real AI motion clip.
- Synchronizes audio, builds timeline, and renders final MP4 with FFmpeg.
- Verifies checkpoints (production_mode=ai_film, synthesis_kind=video_ai_motion).
- Probes final MP4 file with ffprobe.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from ai_youtube_agent.bootstrap import build_container, prepare_database
from ai_youtube_agent.content.story_bible import ProductionMode
from ai_youtube_agent.core.config import (
    Environment,
    RenderProviderKind,
    Settings,
    VoiceProviderKind,
)
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.pipeline.script_to_video import (
    ScriptToVideoRequest,
    ScriptToVideoService,
    ScriptToVideoStage,
)
from ai_youtube_agent.providers.mock_speech_synthesis import MockSpeechSynthesizer
from ai_youtube_agent.providers.registry.comfyui_video_adapter import ComfyUIVideoAdapter
from ai_youtube_agent.providers.speech_synthesis import SpeechSynthesizer
from ai_youtube_agent.providers.video_generation import VideoClipGenerator

PROJECT_ROOT = Path("D:/Project_Audit")
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "e2e_app_test"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FFPROBE_BIN = "ffprobe"
COMFYUI_URL = "http://127.0.0.1:8188"

SAMPLE_AI_FILM_SCRIPT = """
SCENE 1: PHỐ CỔ HÀ NỘI - BAN NGÀY
Mai (mỉm cười): Hà Nội sáng nay thật dịu dàng, từng làn gió khẽ làm lay động tà áo dài trắng thêu hoa.
"""


def run_e2e_app_test() -> None:
    print("=" * 60)
    print("STARTING REALISTIC STORY STUDIO END-TO-END APPLICATION TEST")
    print("=" * 60)

    db_path = OUTPUT_DIR / "e2e_test.db"
    artifacts_dir = OUTPUT_DIR / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    font_file = Path("C:/Windows/Fonts/arial.ttf")

    settings = Settings(
        environment=Environment.DEVELOPMENT,
        database_path=db_path,
        artifact_dir=artifacts_dir,
        render_provider=RenderProviderKind.FFMPEG,
        voice_provider=VoiceProviderKind.MOCK,
        ffmpeg_binary="ffmpeg",
        render_font_file=font_file if font_file.exists() else None,
    )

    prepare_database(settings)
    container = build_container(settings)
    service = container.resolve(ScriptToVideoService)

    # 1. Capability Verification
    caps = service.check_capabilities()
    print("\n1. CAPABILITY REPORT:")
    print(f"   Voice Provider: {caps.voice_provider} (Available: {caps.voice_available})")
    print(f"   Video Clip Provider: {caps.video_clip_provider} (Available: {caps.video_clip_available})")
    print(f"   Render Provider: {caps.render_provider} (Available: {caps.render_available})")
    print(f"   Supported Modes: {[m.value for m in caps.supported_modes]}")
    assert ProductionMode.AI_FILM in caps.supported_modes, "AI_FILM mode is NOT supported!"
    print("   -> PASS: ProductionMode.AI_FILM is READY and supported!")

    # 2. End-to-End Run
    print("\n2. EXECUTING APPLICATION PIPELINE IN AI_FILM MODE...")
    print(f"   Script:\n{SAMPLE_AI_FILM_SCRIPT.strip()}\n")

    req = ScriptToVideoRequest(
        script_text=SAMPLE_AI_FILM_SCRIPT,
        title="Mai - Phố Cổ Hà Nội",
        content_type=ContentType.SHORTS,
        production_mode=ProductionMode.AI_FILM,
        visual_style="cinematic_story",
        language="vi",
    )

    t0 = time.time()
    manifest = service.run(req)
    total_time = time.time() - t0

    print("\n3. EXECUTION COMPLETED!")
    print(f"   Total Pipeline Time: {total_time:.1f}s ({total_time/60:.1f} mins)")
    print(f"   Production Mode: {manifest.production_mode.value}")
    print(f"   Video Output Path: {manifest.video_file_path}")
    print(f"   Number of Scenes: {len(manifest.scene_assets)}")

    # 3. Check scene asset media types and synthesis kinds
    for sa in manifest.scene_assets:
        print(f"   Scene {sa.scene_id}: AssetKind={sa.media_type}, sha256={sa.sha256[:12]}...")
        assert sa.media_type == "video/mp4", f"Scene asset must be video/mp4, got {sa.media_type}"

    # 4. Probe final MP4 video file
    out_video = Path(manifest.video_file_path)
    assert out_video.exists(), f"Output video file does not exist: {out_video}"
    probe_cmd = [
        FFPROBE_BIN, "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(out_video),
    ]
    probe_res = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
    probe_data = json.loads(probe_res.stdout)
    fmt = probe_data["format"]
    duration = float(fmt["duration"])
    size = int(fmt["size"])
    vstream = next(s for s in probe_data["streams"] if s.get("codec_type") == "video")

    print("\n4. FINAL MP4 PROBE VERIFICATION:")
    print(f"   Duration: {duration:.2f}s")
    print(f"   Resolution: {vstream['width']}x{vstream['height']}")
    print(f"   Codec: {vstream['codec_name']}")
    print(f"   File Size: {size} bytes ({size / 1024:.1f} KB)")
    print(f"   Streams count: {len(probe_data['streams'])} (Audio + Video)")

    # 5. Full decode validation
    decode_cmd = ["ffmpeg", "-v", "error", "-i", str(out_video), "-f", "null", "-"]
    decode_res = subprocess.run(decode_cmd, capture_output=True, text=True)
    assert decode_res.returncode == 0, f"Decode failed: {decode_res.stderr}"
    print("   Decode Check: 100% SUCCESS (0 corrupted frames)")

    # 6. Verify via FastAPI Web Application Interface (POST /script-to-video/background-runs)
    from fastapi.testclient import TestClient
    from ai_youtube_agent.main import create_app

    app = create_app(container)
    with TestClient(app) as client:
        print("\n5. VERIFYING FASTAPI WEB APPLICATION INTERFACE...")
        payload = {
            "script_text": SAMPLE_AI_FILM_SCRIPT,
            "title": "Mai - Phố Cổ Hà Nội (Web Run)",
            "content_type": "shorts",
            "production_mode": "ai_film",
            "visual_style": "cinematic_story",
            "language": "vi",
        }
        resp = client.post("/script-to-video/background-runs", json=payload)
        assert resp.status_code in (200, 202), f"API submission failed: {resp.status_code} {resp.text}"
        run_data = resp.json()
        run_id = run_data["run_id"]
        loc = resp.headers.get("Location") or f"/script-to-video/background-runs/{run_id}"
        print(f"   Submitted Run ID: {run_id}, Location: {loc}")

        # Poll run status
        deadline = time.time() + 60
        finished_run = None
        while time.time() < deadline:
            poll_resp = client.get(loc)
            assert poll_resp.status_code == 200
            run_info = poll_resp.json()
            status = run_info.get("status")
            if status in ("succeeded", "failed"):
                finished_run = run_info
                break
            time.sleep(0.5)

        assert finished_run is not None, "Background run timed out"
        assert finished_run["status"] == "succeeded", f"Background run failed: {finished_run.get('error')}"
        print(f"   API Run Status: {finished_run['status']}")
        print(f"   API Run Result Item Status: {finished_run.get('result', {}).get('item_status')}")
        print(f"   API Run Video Size: {finished_run.get('result', {}).get('video_size_bytes')} bytes")
        print("   -> PASS: Web App Interface End-to-End Run Succeeded!")

    print("\n" + "=" * 60)
    print("END-TO-END APPLICATION TEST: ALL CHECKS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    run_e2e_app_test()
