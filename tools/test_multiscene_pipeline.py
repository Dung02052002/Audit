"""Multi-scene AI Video Motion pipeline test runner.

Executes a 3-scene sequence through ComfyUIVideoAdapter sequentially:
- Scene 1: Character looking around and turning head (2s, 16 frames @ 8fps).
- Scene 2: Character performing a hand action / waving lotus flower (2s, 16 frames @ 8fps).
- Scene 3: Character walking gently in an ancient Hanoi environment (2s, 16 frames @ 8fps).

Validates each clip with ffprobe and full decode, extracts key frames,
and produces a 3x3 contact sheet comparing consistency across scenes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from PIL import Image, ImageDraw

from ai_youtube_agent.providers.registry.comfyui_video_adapter import ComfyUIVideoAdapter
from ai_youtube_agent.providers.video_generation import VideoClipGenerationRequest

PROJECT_ROOT = Path("D:/Project_Audit")
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "multiscene_test"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"
COMFYUI_URL = "http://127.0.0.1:8188"


def get_vram_free_mb() -> float:
    try:
        with urllib.request.urlopen(f"{COMFYUI_URL}/system_stats", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["devices"][0]["vram_free"] / (1024 * 1024)
    except Exception:
        return 0.0


def free_comfyui_cache() -> None:
    try:
        req = urllib.request.Request(
            f"{COMFYUI_URL}/free",
            data=json.dumps({"free_memory": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def run_multiscene_test() -> None:
    print("=" * 60)
    print("STARTING 3-SCENE MULTI-SCENE AI VIDEO MOTION TEST")
    print("=" * 60)

    adapter = ComfyUIVideoAdapter(base_url=COMFYUI_URL, job_timeout_seconds=600.0)
    caps = adapter.capabilities_video_clip()
    print(f"Provider: {adapter.name}")
    print(f"Available: {caps.available}")
    print(f"Model: {caps.model_name}")
    if not caps.available:
        print(f"ERROR: Adapter not available: {caps.reason}")
        sys.exit(1)

    scenes_config = [
        {
            "id": "scene_01_turn_head",
            "title": "Scene 1: Character looking around and turning head",
            "prompt": (
                "1girl, young Vietnamese woman, 20yo, long black hair, traditional white silk ao dai, "
                "red floral embroidery, delicate conical hat with flower decor, looking around curiously "
                "then slowly turning head towards camera with a gentle warm smile, Hanoi Old Quarter street background, "
                "cinematic lighting, sharp focus, 8k, photorealistic"
            ),
        },
        {
            "id": "scene_02_hand_gesture",
            "title": "Scene 2: Character lifting hand and holding lotus flower",
            "prompt": (
                "1girl, young Vietnamese woman, 20yo, long black hair, traditional white silk ao dai, "
                "red floral embroidery, delicate conical hat with flower decor, raising right hand gently, "
                "holding a pink lotus flower, smiling warmly at camera, Hanoi Old Quarter street background, "
                "cinematic lighting, sharp focus, 8k, photorealistic"
            ),
        },
        {
            "id": "scene_03_walk_environment",
            "title": "Scene 3: Character walking gracefully in environment",
            "prompt": (
                "1girl, young Vietnamese woman, 20yo, long black hair, traditional white silk ao dai, "
                "red floral embroidery, delicate conical hat with flower decor, walking gracefully forward "
                "along ancient Hanoi street, gentle breeze moving her silk ao dai, warm sunlight, "
                "cinematic lighting, sharp focus, 8k, photorealistic"
            ),
        },
    ]

    results = []

    for idx, sc in enumerate(scenes_config, 1):
        scene_id = sc["id"]
        print(f"\n[{idx}/3] RENDERING {sc['title']}")
        print(f"  Prompt: {sc['prompt'][:80]}...")

        free_comfyui_cache()
        vram_start = get_vram_free_mb()
        print(f"  VRAM Free before start: {vram_start:.1f} MB")

        req = VideoClipGenerationRequest(
            scene_id=scene_id,
            shot_id=f"shot_{idx:02d}",
            prompt=sc["prompt"],
            duration_ms=2000,
            width=512,
            height=512,
            fps=8,
        )

        t0 = time.time()
        clip = adapter.generate_clip(req)
        render_time = time.time() - t0
        vram_end = get_vram_free_mb()

        mp4_path = OUTPUT_DIR / f"{scene_id}.mp4"
        mp4_path.write_bytes(clip.data)
        file_size = len(clip.data)

        print(f"  SUCCESS! Render time: {render_time:.1f}s ({render_time/60:.1f} mins)")
        print(f"  Saved MP4: {mp4_path.name} ({file_size} bytes)")
        print(f"  VRAM Free after run: {vram_end:.1f} MB")

        # ffprobe verification
        probe_cmd = [
            FFPROBE_BIN, "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(mp4_path),
        ]
        probe_res = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        probe_data = json.loads(probe_res.stdout)
        duration = float(probe_data["format"]["duration"])
        codec = probe_data["streams"][0]["codec_name"]
        width = probe_data["streams"][0]["width"]
        height = probe_data["streams"][0]["height"]

        # decode test
        decode_cmd = [FFMPEG_BIN, "-v", "error", "-i", str(mp4_path), "-f", "null", "-"]
        decode_res = subprocess.run(decode_cmd, capture_output=True, text=True)
        is_valid_decode = (decode_res.returncode == 0)

        # Extract 3 key frames: first (0), mid (7), last (15)
        key_frames = []
        for f_idx in [0, 7, 15]:
            frame_png = OUTPUT_DIR / f"{scene_id}_frame_{f_idx:02d}.png"
            extract_cmd = [
                FFMPEG_BIN, "-y",
                "-i", str(mp4_path),
                "-vf", f"select=eq(n\\,{f_idx})",
                "-vframes", "1",
                str(frame_png),
            ]
            subprocess.run(extract_cmd, capture_output=True, check=True)
            key_frames.append(frame_png)

        results.append({
            "scene_id": scene_id,
            "title": sc["title"],
            "path": mp4_path,
            "render_time": render_time,
            "file_size": file_size,
            "duration": duration,
            "resolution": f"{width}x{height}",
            "codec": codec,
            "decode_valid": is_valid_decode,
            "key_frames": key_frames,
        })

    # Build 3x3 contact sheet comparing all 3 scenes
    print("\nAssembling 3x3 multi-scene contact sheet...")
    w, h = 512, 512
    pad = 12
    header_h = 36
    label_h = 24
    sheet_w = 3 * w + 4 * pad
    sheet_h = 3 * (h + label_h) + header_h + 4 * pad

    sheet = Image.new("RGB", (sheet_w, sheet_h), (20, 22, 28))
    draw = ImageDraw.Draw(sheet)

    draw.text((pad, 10), "MULTI-SCENE AI VIDEO MOTION EVALUATION (AnimateDiff SD 1.5 - Local 0 VND)", fill=(240, 240, 240))

    for row_idx, res in enumerate(results):
        y_base = header_h + pad + row_idx * (h + label_h + pad)
        row_title = f"{res['title']} | Render: {res['render_time']:.1f}s | {res['duration']:.2f}s"
        draw.text((pad, y_base), row_title, fill=(200, 220, 255))

        for col_idx, kf_path in enumerate(res["key_frames"]):
            img = Image.open(kf_path)
            x = pad + col_idx * (w + pad)
            y = y_base + label_h
            sheet.paste(img, (x, y))
            t_label = ["Start (t=0.0s)", "Mid (t=0.88s)", "End (t=1.88s)"][col_idx]
            draw.text((x + 8, y + 8), t_label, fill=(255, 255, 0))

    contact_sheet_path = OUTPUT_DIR / "contact_sheet_multiscene.png"
    sheet.save(contact_sheet_path)
    print(f"Contact sheet saved to: {contact_sheet_path.resolve()}")

    print("\n" + "=" * 60)
    print("MULTI-SCENE PIPELINE TEST SUMMARY")
    print("=" * 60)
    total_time = sum(r["render_time"] for r in results)
    print(f"Total pipeline render time: {total_time:.1f}s ({total_time/60:.1f} mins)")
    for r in results:
        print(f"- {r['title']}: {r['duration']:.2f}s, {r['resolution']}, {r['file_size']} bytes, Decode Valid: {r['decode_valid']}, Time: {r['render_time']:.1f}s")


if __name__ == "__main__":
    run_multiscene_test()
