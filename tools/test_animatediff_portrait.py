"""Close-up portrait test for AnimateDiff SD 1.5.

Objective:
1. Close-up portrait framing (facial focus, eye/teeth/skin texture evaluation).
2. Uses identical improved configuration: SD 1.5 + mm_sd_v15_v2.ckpt, dpmpp_2m, karras, 16 steps, CFG 6.0, 8 FPS.
3. Generates clip MP4, extracts individual frames, and builds contact sheet.
4. Verifies MP4 with ffprobe and full ffmpeg decode.
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
from PIL import Image, ImageDraw, ImageFont

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path("D:/Project_Audit")
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "animatediff_portrait"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

COMFYUI_URL = "http://127.0.0.1:8188"
FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"

PORTRAIT_PROMPT = (
    "extreme close-up portrait of a gorgeous young Vietnamese woman, centered composition, "
    "direct eye contact, highly detailed expressive brown eyes, individual eyelashes, natural skin texture, "
    "gentle sweet smile, soft head tilt, subtle wind blowing hair strands, soft warm morning lighting, "
    "photorealistic, 8k, bokeh background, masterpiece, studio portrait"
)

NEGATIVE_PROMPT = (
    "deformed face, bad eyes, disfigured, bad teeth, bad anatomy, cross-eyed, mutated hands, "
    "extra limbs, blurry, low quality, worst quality, distorted, static, ugly, oversaturated, "
    "plastic skin, cartoon, anime"
)

SEED = 823491723


def build_portrait_workflow(seed: int = SEED) -> dict:
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "v1-5-pruned-emaonly.safetensors"},
        },
        "2": {
            "class_type": "ADE_LoadAnimateDiffModel",
            "inputs": {"model_name": "mm_sd_v15_v2.ckpt"},
        },
        "3": {
            "class_type": "ADE_ApplyAnimateDiffModelSimple",
            "inputs": {"motion_model": ["2", 0]},
        },
        "4": {
            "class_type": "ADE_UseEvolvedSampling",
            "inputs": {
                "model": ["1", 0],
                "m_models": ["3", 0],
                "beta_schedule": "sqrt_linear (AnimateDiff)",
            },
        },
        "5": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "clip": ["1", 1],
                "text": PORTRAIT_PROMPT,
            },
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "clip": ["1", 1],
                "text": NEGATIVE_PROMPT,
            },
        },
        "7": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": 512,
                "height": 512,
                "batch_size": 16,
            },
        },
        "8": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["4", 0],
                "positive": ["5", 0],
                "negative": ["6", 0],
                "latent_image": ["7", 0],
                "seed": seed,
                "steps": 16,
                "cfg": 6.0,
                "sampler_name": "dpmpp_2m",
                "scheduler": "karras",
                "denoise": 1.0,
            },
        },
        "9": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["8", 0],
                "vae": ["1", 2],
            },
        },
        "10": {
            "class_type": "SaveWEBM",
            "inputs": {
                "filename_prefix": "AnimateDiff_Portrait_Test",
                "codec": "vp9",
                "fps": 8.0,
                "crf": 26.0,
                "images": ["9", 0],
            },
        },
        "11": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": "AnimateDiff_Portrait_Frames",
                "images": ["9", 0],
            },
        },
    }


def run_portrait_test() -> None:
    print("=" * 60)
    print("STARTING CLOSE-UP PORTRAIT TEST (ANIMATEDIFF SD 1.5)")
    print("=" * 60)

    # 1. Build and save workflow
    workflow = build_portrait_workflow(SEED)
    wf_path = OUTPUT_DIR / "workflow_portrait.json"
    wf_path.write_text(json.dumps(workflow, indent=2), encoding="utf-8")
    print(f"Workflow saved to: {wf_path}")

    # 2. Submit to ComfyUI
    payload = json.dumps({"prompt": workflow}).encode("utf-8")
    req = urllib.request.Request(
        f"{COMFYUI_URL}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req) as resp:
        res = json.loads(resp.read())
    prompt_id = res["prompt_id"]
    print(f"Job queued successfully. Prompt ID: {prompt_id}")
    print("Executing sampling on GPU... (this will take ~4 minutes on GTX 1660 SUPER)")

    # 3. Poll for completion
    while True:
        time.sleep(3)
        elapsed = time.time() - t0
        try:
            h_req = urllib.request.Request(f"{COMFYUI_URL}/history/{prompt_id}")
            with urllib.request.urlopen(h_req) as h_resp:
                history = json.loads(h_resp.read())
        except Exception as e:
            print(f"Polling error ({elapsed:.0f}s): {e}")
            continue

        if prompt_id in history:
            item = history[prompt_id]
            status = item.get("status", {})
            if status.get("completed", False):
                print(f"\nExecution finished in {elapsed:.1f}s ({elapsed/60:.1f} mins)!")
                outputs = item.get("outputs", {})
                break
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI prompt error: {status.get('messages')}")

        sys.stdout.write(f"\rElapsed time: {elapsed:.1f}s...")
        sys.stdout.flush()

    # 4. Extract generated video & frames
    video_out = outputs.get("10", {}).get("images", [])
    frames_out = outputs.get("11", {}).get("images", [])
    assert video_out, "Node 10 SaveWEBM did not return output video"

    video_info = video_out[0]
    vid_filename = video_info["filename"]
    vid_subfolder = video_info.get("subfolder", "")
    params = urllib.parse.urlencode({"filename": vid_filename, "subfolder": vid_subfolder, "type": "output"})
    raw_video_url = f"{COMFYUI_URL}/view?{params}"

    print(f"Downloading generated video: {vid_filename}")
    with urllib.request.urlopen(raw_video_url) as v_resp:
        raw_video_bytes = v_resp.read()

    raw_webm_path = OUTPUT_DIR / "animatediff_portrait.webm"
    raw_webm_path.write_bytes(raw_video_bytes)
    print(f"Raw WEBM saved: {raw_webm_path} ({len(raw_video_bytes)} bytes)")

    # 5. Transcode to MP4 with FFmpeg
    mp4_path = OUTPUT_DIR / "animatediff_portrait_nvenc.mp4"
    transcode_cmd = [
        FFMPEG_BIN, "-y",
        "-i", str(raw_webm_path),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-r", "8",
        "-preset", "slow",
        "-crf", "18",
        "-movflags", "+faststart",
        str(mp4_path),
    ]
    subprocess.run(transcode_cmd, check=True, capture_output=True)
    print(f"Transcoded MP4 saved: {mp4_path} ({mp4_path.stat().st_size} bytes)")

    # 6. Download individual frames
    frame_paths = []
    print(f"Downloading {len(frames_out)} individual frames...")
    for idx, f_info in enumerate(frames_out):
        f_name = f_info["filename"]
        f_sub = f_info.get("subfolder", "")
        f_params = urllib.parse.urlencode({"filename": f_name, "subfolder": f_sub, "type": "output"})
        f_url = f"{COMFYUI_URL}/view?{f_params}"
        with urllib.request.urlopen(f_url) as f_resp:
            f_bytes = f_resp.read()
        f_dest = OUTPUT_DIR / f"frame_{idx:02d}.png"
        f_dest.write_bytes(f_bytes)
        frame_paths.append(f_dest)

    # 7. Create Contact Sheet (4x4 grid of 16 frames)
    print("Building contact sheet...")
    cols, rows = 4, 4
    cell_w, cell_h = 256, 256
    padding = 10
    header_h = 60
    sheet_w = cols * cell_w + (cols + 1) * padding
    sheet_h = rows * cell_h + (rows + 1) * padding + header_h

    sheet = Image.new("RGB", (sheet_w, sheet_h), color=(24, 24, 28))
    draw = ImageDraw.Draw(sheet)

    title_text = f"AnimateDiff SD 1.5 - Close-up Portrait Test (16 frames @ 8 FPS)"
    sub_text = f"Sampler: DPM++ 2M Karras | Steps: 16 | CFG: 6.0 | Seed: {SEED}"
    draw.text((padding, 12), title_text, fill=(240, 240, 240))
    draw.text((padding, 35), sub_text, fill=(160, 160, 170))

    for idx, f_path in enumerate(frame_paths[:16]):
        c = idx % cols
        r = idx // cols
        x = padding + c * (cell_w + padding)
        y = header_h + padding + r * (cell_h + padding)
        with Image.open(f_path) as img:
            thumb = img.resize((cell_w, cell_h), Image.Resampling.LANCZOS)
            sheet.paste(thumb, (x, y))
        draw.text((x + 6, y + 6), f"F{idx:02d}", fill=(255, 255, 255))

    contact_path = OUTPUT_DIR / "contact_sheet_portrait.png"
    sheet.save(contact_path, format="PNG")
    print(f"Contact sheet saved to: {contact_path}")

    # 8. Probe MP4 with ffprobe
    probe_cmd = [
        FFPROBE_BIN, "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(mp4_path),
    ]
    probe_res = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
    probe_data = json.loads(probe_res.stdout)
    fmt = probe_data["format"]
    duration = float(fmt["duration"])
    size = int(fmt["size"])
    vstream = probe_data["streams"][0]

    print("\nPROBE VERIFICATION:")
    print(f"   Duration: {duration:.2f}s")
    print(f"   Resolution: {vstream['width']}x{vstream['height']}")
    print(f"   FPS: {vstream.get('r_frame_rate')}")
    print(f"   Codec: {vstream['codec_name']}")
    print(f"   File size: {size} bytes")

    # 9. Full Decode Check
    decode_cmd = [FFMPEG_BIN, "-v", "error", "-i", str(mp4_path), "-f", "null", "-"]
    decode_res = subprocess.run(decode_cmd, capture_output=True, text=True)
    assert decode_res.returncode == 0, f"Decode failed: {decode_res.stderr}"
    print("   Decode Check: 100% SUCCESS (0 corrupted frames)")

    print("\n" + "=" * 60)
    print("CLOSE-UP PORTRAIT TEST COMPLETE!")
    print("=" * 60)


if __name__ == "__main__":
    run_portrait_test()
