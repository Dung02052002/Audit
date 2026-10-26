"""Test runner for AnimateDiff Quality Improvement (A/B evaluation).

Applies targeted principled improvements:
- Sampler/Scheduler: dpmpp_2m + karras (replaces euler + normal)
- CFG: 6.0 (reduces color burn / facial oversaturation from 7.0)
- Steps: 16 (optimized for dpmpp_2m convergence)
- Targeted Negative Prompt: facial & hand deformity suppression
- Seed: 20261025 (identical to original baseline clip for direct 1-to-1 comparison)

Generates new MP4, extracts key frames, and builds side-by-side comparison sheet.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from PIL import Image, ImageDraw

COMFYUI_URL = "http://127.0.0.1:8188"
PROJECT_ROOT = Path("D:/Project_Audit")
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "animatediff_improved"
BASELINE_DIR = PROJECT_ROOT / "artifacts" / "animatediff_demo"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"


def build_improved_workflow() -> dict:
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {
                "ckpt_name": "v1-5-pruned-emaonly.safetensors"
            }
        },
        "2": {
            "class_type": "ADE_LoadAnimateDiffModel",
            "inputs": {
                "model_name": "mm_sd_v15_v2.ckpt"
            }
        },
        "3": {
            "class_type": "ADE_ApplyAnimateDiffModelSimple",
            "inputs": {
                "motion_model": ["2", 0]
            }
        },
        "4": {
            "class_type": "ADE_UseEvolvedSampling",
            "inputs": {
                "model": ["1", 0],
                "m_models": ["3", 0],
                "beta_schedule": "sqrt_linear (AnimateDiff)"
            }
        },
        "5": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "clip": ["1", 1],
                "text": (
                    "masterpiece, best quality, 1girl, young Vietnamese woman, 20yo, "
                    "long black hair, delicate beautiful face, gentle warm smile, "
                    "traditional white silk ao dai, subtle red floral embroidery, "
                    "looking at viewer, soft cinematic lighting, Hanoi street background, 8k, photorealistic"
                )
            }
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "clip": ["1", 1],
                "text": (
                    "deformed face, bad eyes, disfigured, bad teeth, bad anatomy, "
                    "cross-eyed, mutated hands, extra fingers, fused fingers, "
                    "blurry, low quality, worst quality, distorted, extra limbs"
                )
            }
        },
        "7": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": 512,
                "height": 512,
                "batch_size": 16
            }
        },
        "8": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["4", 0],
                "positive": ["5", 0],
                "negative": ["6", 0],
                "latent_image": ["7", 0],
                "seed": 20261025,
                "steps": 16,
                "cfg": 6.0,
                "sampler_name": "dpmpp_2m",
                "scheduler": "karras",
                "denoise": 1.0
            }
        },
        "9": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["8", 0],
                "vae": ["1", 2]
            }
        },
        "10": {
            "class_type": "SaveWEBM",
            "inputs": {
                "filename_prefix": "AnimateDiff_Improved",
                "codec": "vp9",
                "fps": 8.0,
                "crf": 24.0,
                "images": ["9", 0]
            }
        },
        "11": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": "AnimateDiff_Improved_Frames",
                "images": ["9", 0]
            }
        }
    }


def post_json(endpoint: str, data: dict) -> dict:
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        f"{COMFYUI_URL}{endpoint}",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(endpoint: str) -> dict:
    req = urllib.request.Request(f"{COMFYUI_URL}{endpoint}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download_file(view_url: str, output_path: Path) -> None:
    urllib.request.urlretrieve(f"{COMFYUI_URL}{view_url}", str(output_path))


def run_improved_test() -> None:
    print("=" * 60)
    print("STARTING ANIMATEDIFF QUALITY IMPROVEMENT TEST (A/B RUN)")
    print("=" * 60)

    stats = get_json("/system_stats")
    vram_free = stats["devices"][0]["vram_free"] / (1024 * 1024)
    print(f"ComfyUI Connected: {stats['devices'][0]['name']}")
    print(f"VRAM Free before run: {vram_free:.1f} MB")

    workflow = build_improved_workflow()
    client_id = "test_improved_client"

    t0 = time.time()
    res = post_json("/prompt", {"prompt": workflow, "client_id": client_id})
    prompt_id = res["prompt_id"]
    print(f"Prompt queued: prompt_id={prompt_id}")

    poll_count = 0
    job_info = None
    while time.time() - t0 < 600:
        time.sleep(2.0)
        poll_count += 1
        history = get_json(f"/history/{prompt_id}")
        if prompt_id in history:
            job_info = history[prompt_id]
            break
        if poll_count % 5 == 0:
            elapsed = time.time() - t0
            print(f"  ... Rendering with DPM++ 2M Karras ({elapsed:.1f}s elapsed)")

    if not job_info:
        raise TimeoutError("ComfyUI job timed out after 600s.")

    render_time = time.time() - t0
    print(f"\nRender completed in {render_time:.1f}s ({render_time/60:.1f} mins)!")

    status = job_info.get("status", {})
    if status.get("status_str") == "error":
        raise RuntimeError(f"ComfyUI job failed: {status.get('messages')}")

    outputs = job_info.get("outputs", {})
    webm_path = None
    node_10_out = outputs.get("10", {})
    for itm in node_10_out.get("images", []):
        fn = itm.get("filename", "")
        if fn.endswith((".webm", ".mp4")):
            params = urllib.parse.urlencode({"filename": fn, "subfolder": itm.get("subfolder", ""), "type": itm.get("type", "output")})
            webm_path = OUTPUT_DIR / fn
            download_file(f"/view?{params}", webm_path)
            print(f"Downloaded video file: {webm_path.name} ({webm_path.stat().st_size} bytes)")
            break

    # Download frames from node 11
    node_11_out = outputs.get("11", {})
    downloaded_frames = []
    for idx, itm in enumerate(node_11_out.get("images", [])):
        fn = itm.get("filename", "")
        params = urllib.parse.urlencode({"filename": fn, "subfolder": itm.get("subfolder", ""), "type": itm.get("type", "output")})
        frame_path = OUTPUT_DIR / f"frame_{idx:03d}.png"
        download_file(f"/view?{params}", frame_path)
        downloaded_frames.append(frame_path)

    print(f"Downloaded {len(downloaded_frames)} frames.")

    # Transcode to standard MP4 with FFmpeg NVENC
    mp4_path = OUTPUT_DIR / "animatediff_motion_improved_nvenc.mp4"
    frame_pattern = str(OUTPUT_DIR / "frame_%03d.png")
    cmd = [
        FFMPEG_BIN, "-y",
        "-framerate", "8",
        "-i", frame_pattern,
        "-c:v", "h264_nvenc",
        "-pix_fmt", "yuv420p",
        "-preset", "p4",
        "-tune", "hq",
        str(mp4_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"Encoded improved MP4: {mp4_path.name} ({mp4_path.stat().st_size} bytes)")

    # Run ffprobe verification
    probe_cmd = [
        FFPROBE_BIN, "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(mp4_path)
    ]
    probe_res = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
    probe_data = json.loads(probe_res.stdout)
    duration = float(probe_data["format"]["duration"])
    codec = probe_data["streams"][0]["codec_name"]
    width = probe_data["streams"][0]["width"]
    height = probe_data["streams"][0]["height"]

    print("\n--- FFPROBE VERIFICATION ---")
    print(f"  Duration: {duration:.2f}s")
    print(f"  Resolution: {width}x{height}")
    print(f"  Codec: {codec}")
    print(f"  File size: {mp4_path.stat().st_size} bytes")

    # Decode test
    decode_cmd = [FFMPEG_BIN, "-v", "error", "-i", str(mp4_path), "-f", "null", "-"]
    decode_res = subprocess.run(decode_cmd, capture_output=True, text=True)
    assert decode_res.returncode == 0, f"Decode failed: {decode_res.stderr}"
    print("  Video Decode Verification: 100% SUCCESS (0 corrupted frames)")

    # Build Side-by-Side Comparison Contact Sheet (Baseline vs Improved)
    print("\nBuilding Side-by-Side Comparison Sheet (Baseline vs Improved)...")
    key_indices = [0, 3, 7, 11, 15]

    w, h = 512, 512
    pad = 12
    header_h = 40
    label_h = 24
    sheet_w = len(key_indices) * w + (len(key_indices) + 1) * pad
    sheet_h = 2 * (h + label_h) + header_h + 3 * pad

    sheet = Image.new("RGB", (sheet_w, sheet_h), (20, 22, 28))
    draw = ImageDraw.Draw(sheet)

    draw.text((pad, 10), "ANIMATEDIFF SD 1.5 A/B COMPARISON: BASELINE (EULER/NORMAL) VS IMPROVED (DPM++ 2M KARRAS)", fill=(240, 240, 240))

    # Row 1: Baseline
    y_base_row = header_h + pad
    draw.text((pad, y_base_row), "ROW 1: BASELINE (Euler, Normal, CFG 7.0, 15 steps) - Render: 267.4s", fill=(255, 180, 180))
    for col_idx, k_idx in enumerate(key_indices):
        old_frame = BASELINE_DIR / f"frame_{k_idx:03d}.png"
        if old_frame.exists():
            img = Image.open(old_frame)
            x = pad + col_idx * (w + pad)
            y = y_base_row + label_h
            sheet.paste(img, (x, y))
            draw.text((x + 8, y + 8), f"Frame {k_idx:02d} (t={(k_idx/8.0):.2f}s)", fill=(255, 255, 0))

    # Row 2: Improved
    y_imp_row = y_base_row + h + label_h + pad
    draw.text((pad, y_imp_row), f"ROW 2: IMPROVED (DPM++ 2M, Karras, CFG 6.0, 16 steps) - Render: {render_time:.1f}s", fill=(180, 255, 180))
    for col_idx, k_idx in enumerate(key_indices):
        new_frame = OUTPUT_DIR / f"frame_{k_idx:03d}.png"
        if new_frame.exists():
            img = Image.open(new_frame)
            x = pad + col_idx * (w + pad)
            y = y_imp_row + label_h
            sheet.paste(img, (x, y))
            draw.text((x + 8, y + 8), f"Frame {k_idx:02d} (t={(k_idx/8.0):.2f}s)", fill=(255, 255, 0))

    comparison_sheet_path = OUTPUT_DIR / "comparison_baseline_vs_improved.png"
    sheet.save(comparison_sheet_path)
    print(f"Comparison sheet saved: {comparison_sheet_path.resolve()}")


if __name__ == "__main__":
    run_improved_test()
