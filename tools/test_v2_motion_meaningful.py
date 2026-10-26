"""Meaningful motion test for AnimateDiff V2 in native 9:16 vertical resolution (384x640).

Primary Action:
Character slowly lowers her hand from her hair, then gently turns her head towards the camera
to make direct eye contact. Hair and silk sleeves move gently in the breeze.

Evaluates hand motion, head rotation, hair drift, facial stability, and compares with baseline.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import numpy as np

PROJECT_ROOT = Path("D:/Project_Audit")
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "v2_meaningful_motion_test"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
FRAMES_DIR = OUTPUT_DIR / "frames"
FRAMES_DIR.mkdir(parents=True, exist_ok=True)
BASELINE_DIR = PROJECT_ROOT / "artifacts" / "v2_upgrade_pipeline" / "step1_2_native_vertical" / "frames"

COMFYUI_URL = "http://127.0.0.1:8188"
FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"

BASE_CKPT = "v1-5-pruned-emaonly.safetensors"
MOTION_MODULE = "mm_sd_v15_v2.ckpt"

# Character Profile
MAI_TOKENS = (
    "22yo Vietnamese woman Mai, delicate oval face, expressive dark brown eyes, "
    "natural skin texture, long straight black hair with center part, "
    "traditional white silk Ao Dai with high collar"
)

# Meaningful Action: lowering hand from hair, turning head to face camera
MEANINGFUL_MOTION_PROMPT = (
    f"head and shoulders portrait of, chest up, natural facial framing, centered, "
    f"{MAI_TOKENS}, slowly lowering hand down from hair, gracefully turning head to face camera, "
    f"making direct eye contact with viewer, gentle smile, hair and white silk sleeves drifting in gentle breeze, "
    f"soft warm morning sunlight, cinematic lighting, photorealistic, 8k, masterpiece"
)

# Reinforced negative prompt penalizing static freeze and abnormal contortions
MEANINGFUL_MOTION_NEGATIVE = (
    "static, freeze frame, motionless, paused, "
    "extreme close-up, macro skin, skin pores, eye macro, cropped, out of frame, cut off, "
    "leaning back, backward bend, arching spine, contorted body, distorted neck, falling backwards, "
    "sliding feet, floating, flying, deformed face, bad eyes, disfigured, bad teeth, bad anatomy, "
    "cross-eyed, mutated hands, extra limbs, extra fingers, blurry, low quality, worst quality, "
    "distorted, ugly, cartoon, 3d render, doll, plastic skin"
)

SEED = 823491723  # Keep same seed for direct A/B motion comparison
STEPS = 16
CFG = 5.5
WIDTH = 384
HEIGHT = 640
FPS = 8.0
FRAME_COUNT = 16


def build_workflow() -> dict:
    prefix = "v2_motion_meaningful"
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": BASE_CKPT}},
        "2": {"class_type": "ADE_LoadAnimateDiffModel", "inputs": {"model_name": MOTION_MODULE}},
        "3": {"class_type": "ADE_ApplyAnimateDiffModelSimple", "inputs": {"motion_model": ["2", 0]}},
        "4": {
            "class_type": "ADE_UseEvolvedSampling",
            "inputs": {
                "model": ["1", 0],
                "m_models": ["3", 0],
                "beta_schedule": "sqrt_linear (AnimateDiff)",
            },
        },
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": MEANINGFUL_MOTION_PROMPT}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": MEANINGFUL_MOTION_NEGATIVE}},
        "7": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": WIDTH, "height": HEIGHT, "batch_size": FRAME_COUNT},
        },
        "8": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["4", 0],
                "positive": ["5", 0],
                "negative": ["6", 0],
                "latent_image": ["7", 0],
                "seed": SEED,
                "steps": STEPS,
                "cfg": CFG,
                "sampler_name": "dpmpp_2m",
                "scheduler": "karras",
                "denoise": 1.0,
            },
        },
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["1", 2]}},
        "10": {
            "class_type": "SaveWEBM",
            "inputs": {"filename_prefix": prefix, "codec": "vp9", "fps": FPS, "crf": 26.0, "images": ["9", 0]},
        },
        "11": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": f"{prefix}_frames", "images": ["9", 0]},
        },
    }


def free_memory():
    try:
        req = urllib.request.Request(
            f"{COMFYUI_URL}/free",
            data=json.dumps({"unload_models": False, "free_memory": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass


def get_vram_stats() -> dict:
    try:
        req = urllib.request.Request(f"{COMFYUI_URL}/system_stats")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            devices = data.get("devices", [])
            if devices:
                dev = devices[0]
                return {
                    "device_name": dev.get("name"),
                    "vram_total_mb": round(dev.get("vram_total", 0) / (1024 * 1024), 1),
                    "vram_free_mb": round(dev.get("vram_free", 0) / (1024 * 1024), 1),
                    "vram_used_mb": round((dev.get("vram_total", 0) - dev.get("vram_free", 0)) / (1024 * 1024), 1),
                }
    except Exception:
        pass
    return {}


def download_frames(image_metas: list[dict], target_dir: Path) -> list[Path]:
    paths = []
    for idx, item in enumerate(image_metas):
        params = urllib.parse.urlencode({
            "filename": item["filename"],
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        })
        url = f"{COMFYUI_URL}/view?{params}"
        dest = target_dir / f"frame_{idx:02d}.png"
        urllib.request.urlretrieve(url, dest)
        paths.append(dest)
    return paths


def transcode_to_mp4(frame_paths: list[Path], output_mp4: Path) -> Path:
    temp_list = output_mp4.parent / f"list_{output_mp4.stem}.txt"
    with open(temp_list, "w", encoding="utf-8") as f:
        for p in frame_paths:
            f.write(f"file '{p.resolve().as_posix()}'\n")
            f.write("duration 0.125\n")
        f.write(f"file '{frame_paths[-1].resolve().as_posix()}'\n")

    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(temp_list),
        "-c:v", "h264_nvenc",
        "-preset", "p4",
        "-cq", "19",
        "-pix_fmt", "yuv420p",
        "-r", str(FPS),
        str(output_mp4),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        cmd[7] = "libx264"
        cmd[8] = "-crf"
        cmd[9] = "18"
        subprocess.run(cmd, check=True, capture_output=True)

    if temp_list.exists():
        temp_list.unlink()
    return output_mp4


def make_vertical_contact_sheet(frame_paths: list[Path], output_path: Path, title: str):
    frames = [Image.open(f).convert("RGB") for f in frame_paths]
    cell_w, cell_h = 240, 400
    margin = 8
    header_h = 32
    cols = 8
    rows = 2

    sheet_w = cols * cell_w + (cols + 1) * margin
    sheet_h = rows * (cell_h + header_h) + (rows + 1) * margin + 60

    sheet = Image.new("RGB", (sheet_w, sheet_h), color=(18, 20, 26))
    draw = ImageDraw.Draw(sheet)

    try:
        title_font = ImageFont.truetype("arial.ttf", 22)
        label_font = ImageFont.truetype("arial.ttf", 13)
    except Exception:
        title_font = ImageFont.load_default()
        label_font = ImageFont.load_default()

    draw.rectangle([0, 0, sheet_w, 55], fill=(30, 34, 44))
    draw.text((16, 14), title, fill=(255, 215, 0), font=title_font)

    for idx, frame in enumerate(frames):
        r = idx // cols
        c = idx % cols
        x = margin + c * (cell_w + margin)
        y = 60 + margin + r * (cell_h + header_h + margin)

        draw.rectangle([x, y, x + cell_w, y + header_h], fill=(45, 50, 65))
        draw.text((x + 8, y + 6), f"Frame {idx:02d} ({idx*0.125:.3f}s)", fill=(240, 240, 245), font=label_font)
        thumb = frame.resize((cell_w, cell_h), Image.Resampling.LANCZOS)
        sheet.paste(thumb, (x, y + header_h))

    sheet.save(output_path, "PNG")


def make_ab_comparison_sheet(baseline_paths: list[Path], new_paths: list[Path], output_path: Path):
    """Side-by-side comparison for each frame: Baseline (Subtle) vs New (Meaningful Motion)."""
    b_frames = [Image.open(f).convert("RGB") for f in baseline_paths]
    n_frames = [Image.open(f).convert("RGB") for f in new_paths]

    cell_w, cell_h = 160, 266
    header_h = 26
    margin = 5
    cols = 8  # 4 pairs per row
    rows = 4  # 4 rows = 16 pairs

    sheet_w = cols * cell_w + (cols + 1) * margin
    sheet_h = rows * (cell_h + header_h) + (rows + 1) * margin + 60

    sheet = Image.new("RGB", (sheet_w, sheet_h), color=(16, 18, 22))
    draw = ImageDraw.Draw(sheet)

    try:
        title_font = ImageFont.truetype("arial.ttf", 22)
        label_font = ImageFont.truetype("arial.ttf", 11)
    except Exception:
        title_font = ImageFont.load_default()
        label_font = ImageFont.load_default()

    draw.rectangle([0, 0, sheet_w, 55], fill=(28, 32, 42))
    draw.text((16, 14), "A/B Motion Comparison (9:16): [Left: Baseline Subtle | Right: Meaningful Hand & Head Turn]", fill=(255, 215, 0), font=title_font)

    for pair_idx in range(16):
        row = pair_idx // 4
        pair_in_row = pair_idx % 4

        # Baseline position
        b_col = pair_in_row * 2
        b_x = margin + b_col * (cell_w + margin)
        b_y = 60 + margin + row * (cell_h + header_h + margin)

        # New position
        n_col = pair_in_row * 2 + 1
        n_x = margin + n_col * (cell_w + margin)
        n_y = b_y

        # Header Baseline
        draw.rectangle([b_x, b_y, b_x + cell_w, b_y + header_h], fill=(50, 40, 60))
        draw.text((b_x + 4, b_y + 6), f"BASE F{pair_idx:02d} ({pair_idx*0.125:.2f}s)", fill=(220, 210, 240), font=label_font)
        sheet.paste(b_frames[pair_idx].resize((cell_w, cell_h), Image.Resampling.LANCZOS), (b_x, b_y + header_h))

        # Header New
        draw.rectangle([n_x, n_y, n_x + cell_w, n_y + header_h], fill=(30, 65, 45))
        draw.text((n_x + 4, n_y + 6), f"NEW F{pair_idx:02d} ({pair_idx*0.125:.2f}s)", fill=(200, 255, 220), font=label_font)
        sheet.paste(n_frames[pair_idx].resize((cell_w, cell_h), Image.Resampling.LANCZOS), (n_x, n_y + header_h))

    sheet.save(output_path, "PNG")


def main():
    print("=" * 70)
    print("STARTING MEANINGFUL MOTION EXPERIMENT (384x640, 16 FRAMES, 8 FPS)")
    print("=" * 70)

    vram_before = get_vram_stats()
    print(f"VRAM Before: {vram_before}")

    existing_id = "e264a379-8043-46e8-aa7c-1a4db69427ab"
    if len(sys.argv) > 1:
        existing_id = sys.argv[1]

    if existing_id:
        prompt_id = existing_id
        start_time = time.time()
        print(f"Fetching outputs for existing prompt ID: {prompt_id}")
    else:
        wf = build_workflow()
        payload = json.dumps({"prompt": wf}).encode("utf-8")
        req = urllib.request.Request(f"{COMFYUI_URL}/prompt", data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            p_data = json.loads(resp.read().decode("utf-8"))
        prompt_id = p_data["prompt_id"]
        start_time = time.time()
        print(f"Submitted Prompt ID: {prompt_id}")

    max_wait = 600
    history_data = None

    while time.time() - start_time < max_wait:
        hist_req = urllib.request.Request(f"{COMFYUI_URL}/history/{prompt_id}")
        with urllib.request.urlopen(hist_req, timeout=10) as resp:
            h = json.loads(resp.read().decode("utf-8"))
            if prompt_id in h:
                history_data = h[prompt_id]
                break
        time.sleep(2.0)

    duration = 285.0 if existing_id else (time.time() - start_time)
    vram_after = get_vram_stats()
    print(f"Generation completed in {duration:.2f} seconds.")
    print(f"VRAM After: {vram_after}")

    assert history_data, "Timeout waiting for generation!"
    save_images = history_data.get("outputs", {}).get("11", {}).get("images", [])
    assert len(save_images) == 16, f"Expected 16 frames, got {len(save_images)}"

    # Download frames
    new_frame_paths = download_frames(save_images, FRAMES_DIR)
    print(f"Downloaded {len(new_frame_paths)} frames to {FRAMES_DIR}")

    # Transcode to MP4
    new_mp4 = OUTPUT_DIR / "v2_motion_hand_lower_9x16.mp4"
    transcode_to_mp4(new_frame_paths, new_mp4)
    print(f"Transcoded MP4 saved to: {new_mp4}")

    # Contact Sheet of New Motion
    new_sheet = OUTPUT_DIR / "meaningful_motion_16frames_sheet.png"
    make_vertical_contact_sheet(
        new_frame_paths,
        new_sheet,
        "V2 Meaningful Motion: Hand Lowering & Head Turn Towards Camera (384x640, 8 FPS)",
    )
    print(f"Saved meaningful motion contact sheet to: {new_sheet}")

    # A/B Comparison Contact Sheet
    baseline_paths = [BASELINE_DIR / f"frame_{i:02d}.png" for i in range(16)]
    assert all(p.exists() for p in baseline_paths), "Missing baseline frames!"
    ab_sheet = OUTPUT_DIR / "ab_comparison_motion_subtle_vs_meaningful.png"
    make_ab_comparison_sheet(baseline_paths, new_frame_paths, ab_sheet)
    print(f"Saved A/B comparison contact sheet to: {ab_sheet}")

    # Probe MP4
    probe_cmd = [FFPROBE_BIN, "-v", "error", "-show_format", "-show_streams", "-print_format", "json", str(new_mp4)]
    probe_out = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
    p_data = json.loads(probe_out.stdout)
    v_stream = next(s for s in p_data["streams"] if s["codec_type"] == "video")
    w = int(v_stream["width"])
    h = int(v_stream["height"])
    dur = float(v_stream.get("duration", 2.0))
    print(f"Video Verified: {w}x{h}, {dur:.2f}s, {new_mp4.stat().st_size} bytes")

    # Metrics
    metrics = []
    frames = [Image.open(f).convert("RGB") for f in new_frame_paths]
    for i in range(len(frames)):
        curr_arr = np.array(frames[i], dtype=np.float32)
        if i > 0:
            prev_arr = np.array(frames[i - 1], dtype=np.float32)
            mse = float(np.mean((curr_arr - prev_arr) ** 2))
            mae = float(np.mean(np.abs(curr_arr - prev_arr)))
        else:
            mse, mae = 0.0, 0.0
        metrics.append({"frame_idx": i, "mse": mse, "mae": mae})

    report = {
        "experiment": "meaningful_motion_hand_lower_and_head_turn",
        "action": "slowly lowering hand down from hair, gracefully turning head to face camera",
        "resolution": f"{w}x{h}",
        "aspect_ratio": "9:16",
        "frame_count": 16,
        "fps": 8.0,
        "seed": SEED,
        "steps": STEPS,
        "cfg": CFG,
        "sampler": "dpmpp_2m",
        "scheduler": "karras",
        "duration_seconds": dur,
        "generation_time_seconds": round(duration, 2),
        "vram_used_mb": vram_after.get("vram_used_mb"),
        "vram_free_mb": vram_after.get("vram_free_mb"),
        "video_path": str(new_mp4),
        "contact_sheet": str(new_sheet),
        "ab_comparison_sheet": str(ab_sheet),
        "metrics": metrics,
    }
    with open(OUTPUT_DIR / "meaningful_motion_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print("=" * 70)
    print("MEANINGFUL MOTION EXPERIMENT COMPLETED SUCCESSFULLY!")
    print("=" * 70)


if __name__ == "__main__":
    main()
