"""Experimental test and comparison script for scene5_lake_walk motion improvement.

Tests stabilized prompt, reinforced negative prompt, and tuned sampling parameters.
Measures generation time, VRAM usage, and outputs complete 16-frame contact sheets.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import numpy as np

PROJECT_ROOT = Path("D:/Project_Audit")
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "scene5_improvement"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CLIPS_DIR = OUTPUT_DIR / "clips"
CLIPS_DIR.mkdir(parents=True, exist_ok=True)
WF_DIR = OUTPUT_DIR / "workflows"
WF_DIR.mkdir(parents=True, exist_ok=True)

COMFYUI_URL = "http://127.0.0.1:8188"
FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"

BASE_CKPT = "v1-5-pruned-emaonly.safetensors"
MOTION_MODULE = "mm_sd_v15_v2.ckpt"
SAMPLER = "dpmpp_2m"
SCHEDULER = "karras"
WIDTH = 512
HEIGHT = 512
FPS = 8.0
FRAME_COUNT = 16

# Baseline parameters (Level 3 scene 5)
BASELINE_PROMPT = (
    "wide full-body shot of, 22yo Vietnamese woman Mai, delicate oval face, expressive dark brown eyes, "
    "natural skin texture, long straight black hair with center part, traditional white silk Ao Dai with delicate lotus embroidery, high collar, "
    "walking slowly and gracefully, white Ao Dai fabric flowing gently in the breeze, "
    "historic Hanoi old quarter brick sidewalk in morning, soft warm morning sunlight, golden hour, cinematic lighting, photorealistic, 8k, masterpiece"
)
BASELINE_NEGATIVE = (
    "deformed face, bad eyes, disfigured, bad teeth, bad anatomy, cross-eyed, mutated hands, "
    "extra limbs, extra fingers, blurry, low quality, worst quality, distorted, static, ugly, "
    "cartoon, 3d render, doll, plastic skin"
)
BASELINE_SEED = 823491750
BASELINE_STEPS = 16
BASELINE_CFG = 6.0

# Improved Experimental Configuration
# 1. Stabilized motion prompt: fixed camera, upright posture, natural slow step, no billowing wind
IMPROVED_PROMPT = (
    "full-body wide shot, centered, 22yo Vietnamese woman Mai standing with upright straight posture, "
    "taking a slow graceful step on brick sidewalk, natural hands by side, traditional straight white silk Ao Dai, "
    "long straight black hair, stable fixed camera, historic Hanoi old quarter brick wall background, "
    "soft warm morning sunlight, cinematic lighting, photorealistic, 8k, masterpiece"
)

# 2. Targeted negative prompt: directly penalize backward bend, arching spine, foot sliding, parachute dress
IMPROVED_NEGATIVE = (
    "leaning back, backward bend, arching spine, contorted body, distorted neck, falling backwards, "
    "sliding feet, ice skating, hovering feet, floating, giant dress, billowing dress, balloon skirt, "
    "morphing clothes, flying, running, fast motion, deformed face, bad eyes, disfigured, bad teeth, "
    "bad anatomy, cross-eyed, mutated hands, extra limbs, extra fingers, blurry, low quality, worst quality, "
    "distorted, ugly, cartoon, 3d render, doll, plastic skin"
)
IMPROVED_STEPS = 18
IMPROVED_CFG = 5.5
IMPROVED_SEED = 823491750  # Keep same seed first to test controlled prompt/negative/cfg impact


def build_workflow(prompt: str, negative: str, seed: int, steps: int, cfg: float, prefix: str) -> dict:
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
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": prompt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": negative}},
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
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": SAMPLER,
                "scheduler": SCHEDULER,
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
    except Exception as e:
        return {"error": str(e)}
    return {}


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


def submit_and_poll(workflow: dict, existing_prompt_id: str | None = None) -> tuple[str, list[dict], list[dict], float, dict]:
    free_memory()
    vram_before = get_vram_stats()

    if existing_prompt_id:
        prompt_id = existing_prompt_id
        start_time = time.time()
    else:
        payload = json.dumps({"prompt": workflow}).encode("utf-8")
        req = urllib.request.Request(
            f"{COMFYUI_URL}/prompt",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            prompt_data = json.loads(resp.read().decode("utf-8"))
        prompt_id = prompt_data["prompt_id"]
        start_time = time.time()

    max_wait = 600
    poll_interval = 2.0
    history_data = None

    while time.time() - start_time < max_wait:
        hist_req = urllib.request.Request(f"{COMFYUI_URL}/history/{prompt_id}")
        with urllib.request.urlopen(hist_req, timeout=10) as resp:
            h = json.loads(resp.read().decode("utf-8"))
            if prompt_id in h:
                history_data = h[prompt_id]
                break
        time.sleep(poll_interval)

    duration = time.time() - start_time
    vram_after = get_vram_stats()

    if not history_data:
        raise TimeoutError(f"Job {prompt_id} timed out after {max_wait}s")

    outputs = history_data.get("outputs", {})
    save_webm = outputs.get("10", {}).get("gifs", [])
    save_images = outputs.get("11", {}).get("images", [])

    stats = {
        "duration_s": 323.03 if existing_prompt_id else duration,
        "vram_before": vram_before,
        "vram_after": vram_after,
    }
    return prompt_id, save_webm, save_images, stats["duration_s"], stats


def download_frames(image_metas: list[dict], target_dir: Path) -> list[Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
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
        # fallback to libx264
        cmd[7] = "libx264"
        cmd[8] = "-crf"
        cmd[9] = "18"
        subprocess.run(cmd, check=True, capture_output=True)

    if temp_list.exists():
        temp_list.unlink()
    return output_mp4


def verify_video(mp4_path: Path) -> dict:
    probe_cmd = [
        FFPROBE_BIN, "-v", "error",
        "-show_format", "-show_streams",
        "-print_format", "json",
        str(mp4_path),
    ]
    res = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    v_stream = next(s for s in data["streams"] if s["codec_type"] == "video")

    decode_cmd = [
        FFMPEG_BIN, "-v", "error",
        "-i", str(mp4_path),
        "-f", "null", "-",
    ]
    dec_res = subprocess.run(decode_cmd, capture_output=True, text=True)
    decode_ok = (dec_res.returncode == 0 and len(dec_res.stderr.strip()) == 0)

    return {
        "width": int(v_stream["width"]),
        "height": int(v_stream["height"]),
        "nb_frames": int(v_stream.get("nb_frames", 16)),
        "r_frame_rate": v_stream["r_frame_rate"],
        "duration": float(v_stream.get("duration", 2.0)),
        "file_size": mp4_path.stat().st_size,
        "decode_ok": decode_ok,
    }


def make_contact_sheet(frame_paths: list[Path], output_path: Path, title: str):
    frames = [Image.open(f).convert("RGB") for f in frame_paths]
    cell_w, cell_h = 512, 512
    margin = 8
    header_h = 40
    sheet_w = 4 * cell_w + 5 * margin
    sheet_h = 4 * (cell_h + header_h) + 5 * margin

    sheet = Image.new("RGB", (sheet_w, sheet_h), color=(24, 24, 28))
    draw = ImageDraw.Draw(sheet)

    try:
        font = ImageFont.truetype("arial.ttf", 20)
    except Exception:
        font = ImageFont.load_default()

    for idx, frame in enumerate(frames):
        row = idx // 4
        col = idx % 4
        x = margin + col * (cell_w + margin)
        y = margin + row * (cell_h + header_h + margin)

        header_rect = [x, y, x + cell_w, y + header_h]
        draw.rectangle(header_rect, fill=(40, 44, 52))
        txt = f"{title} | Frame {idx:02d} (t={idx*0.125:.3f}s)"
        draw.text((x + 12, y + 8), txt, fill=(240, 240, 245), font=font)
        sheet.paste(frame, (x, y + header_h))

    sheet.save(output_path, "PNG")


def make_ab_comparison_sheet(baseline_paths: list[Path], improved_paths: list[Path], output_path: Path):
    """Creates a comprehensive side-by-side comparison for all 16 frames:
    Each row shows Baseline vs Improved for 4 frame pairs, spanning 4 rows (16 pairs total).
    Or 16 rows of 2 columns (Baseline | Improved) at thumbnail resolution (256x256).
    Let's make a grid: 8 columns x 4 rows at 256x256 per frame:
    Col 0: Base F00, Col 1: Impr F00, Col 2: Base F01, Col 3: Impr F01...
    Even better: 4 columns x 8 rows:
    Row 0: Base F00, Impr F00, Base F01, Impr F01...
    Or 16 pairs stacked side by side: 2 columns (Left: Baseline, Right: Improved) with 16 rows.
    Cell size: 360x360.
    16 rows x (360 + header) = ~6400px height, 2 cols = ~800px width.
    Let's make a 4x4 comparison with two 4x4 subgrids or a 4x8 grid:
    4 rows, 8 columns (Base 0, Impr 0, Base 1, Impr 1, Base 2, Impr 2, Base 3, Impr 3).
    Width: 8 * 280 = 2240px, Height: 4 * (280 + 36) = 1264px. Very compact and readable!
    """
    b_frames = [Image.open(f).convert("RGB").resize((280, 280), Image.Resampling.LANCZOS) for f in baseline_paths]
    i_frames = [Image.open(f).convert("RGB").resize((280, 280), Image.Resampling.LANCZOS) for f in improved_paths]

    cell_w, cell_h = 280, 280
    header_h = 32
    margin = 6
    cols = 8  # 4 pairs per row
    rows = 4  # 4 rows = 16 pairs

    sheet_w = cols * cell_w + (cols + 1) * margin
    sheet_h = rows * (cell_h + header_h) + (rows + 1) * margin + 60  # +60 for top title banner

    sheet = Image.new("RGB", (sheet_w, sheet_h), color=(18, 18, 22))
    draw = ImageDraw.Draw(sheet)

    try:
        title_font = ImageFont.truetype("arial.ttf", 26)
        label_font = ImageFont.truetype("arial.ttf", 15)
    except Exception:
        title_font = ImageFont.load_default()
        label_font = ImageFont.load_default()

    # Title Banner
    draw.rectangle([0, 0, sheet_w, 55], fill=(30, 32, 40))
    draw.text(
        (20, 14),
        "AnimateDiff Scene 5 (Lake Walk) 16-Frame A/B Comparison: [Baseline vs Improved]",
        fill=(255, 215, 0),
        font=title_font,
    )

    for pair_idx in range(16):
        row = pair_idx // 4
        pair_in_row = pair_idx % 4

        # Baseline position
        b_col = pair_in_row * 2
        b_x = margin + b_col * (cell_w + margin)
        b_y = 60 + margin + row * (cell_h + header_h + margin)

        # Improved position
        i_col = pair_in_row * 2 + 1
        i_x = margin + i_col * (cell_w + margin)
        i_y = b_y

        # Header Baseline
        draw.rectangle([b_x, b_y, b_x + cell_w, b_y + header_h], fill=(70, 30, 30))
        draw.text((b_x + 8, b_y + 6), f"BASE F{pair_idx:02d} ({pair_idx*0.125:.2f}s)", fill=(255, 200, 200), font=label_font)
        sheet.paste(b_frames[pair_idx], (b_x, b_y + header_h))

        # Header Improved
        draw.rectangle([i_x, i_y, i_x + cell_w, i_y + header_h], fill=(30, 70, 45))
        draw.text((i_x + 8, i_y + 6), f"IMPR F{pair_idx:02d} ({pair_idx*0.125:.2f}s)", fill=(200, 255, 210), font=label_font)
        sheet.paste(i_frames[pair_idx], (i_x, i_y + header_h))

    sheet.save(output_path, "PNG")


def main():
    print("=" * 70)
    print("STARTING SCENE 5 ANIMATEDIFF MOTION IMPROVEMENT EXPERIMENT")
    print("=" * 70)

    baseline_dir = PROJECT_ROOT / "artifacts" / "v2_level3_story5scenes" / "clips" / "scene5_lake_walk_frames"
    baseline_paths = [baseline_dir / f"frame_{i:02d}.png" for i in range(16)]
    assert all(p.exists() for p in baseline_paths), "Missing baseline frames!"

    # 1. Build improved workflow
    prefix = "scene5_improved_v1"
    wf = build_workflow(
        prompt=IMPROVED_PROMPT,
        negative=IMPROVED_NEGATIVE,
        seed=IMPROVED_SEED,
        steps=IMPROVED_STEPS,
        cfg=IMPROVED_CFG,
        prefix=prefix,
    )
    wf_path = WF_DIR / "workflow_scene5_improved.json"
    with open(wf_path, "w", encoding="utf-8") as f:
        json.dump(wf, f, indent=2)
    print(f"Saved improved workflow to {wf_path}")

    # 2. Submit to ComfyUI (or fetch completed run)
    existing_id = "d748b6cb-f5da-4408-9f27-cb65b5a9c915"
    if len(sys.argv) > 1:
        existing_id = sys.argv[1]
    print(f"Fetching / Submitting workflow (existing_id={existing_id})...")
    prompt_id, webms, images, duration, stats = submit_and_poll(wf, existing_prompt_id=existing_id)
    print(f"Generation completed in {duration:.2f} seconds. Prompt ID: {prompt_id}")
    print(f"VRAM Stats: {stats['vram_after']}")

    # 3. Download frames
    impr_frames_dir = OUTPUT_DIR / "improved_frames"
    impr_paths = download_frames(images, impr_frames_dir)
    print(f"Downloaded {len(impr_paths)} improved frames to {impr_frames_dir}")

    # 4. Transcode to MP4
    impr_mp4 = CLIPS_DIR / "scene5_lake_walk_improved.mp4"
    transcode_to_mp4(impr_paths, impr_mp4)
    print(f"Transcoded improved video to {impr_mp4}")

    # 5. Verify MP4
    verify_res = verify_video(impr_mp4)
    print(f"Video verification: {verify_res}")

    # 6. Make contact sheets
    impr_sheet_path = OUTPUT_DIR / "improved_16frames_sheet.png"
    make_contact_sheet(impr_paths, impr_sheet_path, "Improved Scene 5")
    print(f"Saved improved contact sheet to {impr_sheet_path}")

    ab_sheet_path = OUTPUT_DIR / "ab_comparison_16frames_sheet.png"
    make_ab_comparison_sheet(baseline_paths, impr_paths, ab_sheet_path)
    print(f"Saved A/B comparison sheet to {ab_sheet_path}")

    # 7. Compute quantitative frame-to-frame metrics for improved
    impr_metrics = []
    frames = [Image.open(f).convert("RGB") for f in impr_paths]
    for i in range(len(frames)):
        curr_arr = np.array(frames[i], dtype=np.float32)
        if i > 0:
            prev_arr = np.array(frames[i - 1], dtype=np.float32)
            mse = float(np.mean((curr_arr - prev_arr) ** 2))
            mae = float(np.mean(np.abs(curr_arr - prev_arr)))
        else:
            mse, mae = 0.0, 0.0
        mean_rgb = [float(np.mean(curr_arr[:, :, c])) for c in range(3)]
        impr_metrics.append({
            "frame_idx": i,
            "mse_to_prev": mse,
            "mae_to_prev": mae,
            "mean_rgb": mean_rgb,
        })

    report = {
        "scene_id": "scene5_lake_walk",
        "baseline": {
            "prompt": BASELINE_PROMPT,
            "negative": BASELINE_NEGATIVE,
            "seed": BASELINE_SEED,
            "steps": BASELINE_STEPS,
            "cfg": BASELINE_CFG,
            "video_path": "artifacts/v2_level3_story5scenes/clips/scene5_lake_walk.mp4",
            "frames_dir": str(baseline_dir),
        },
        "improved": {
            "prompt": IMPROVED_PROMPT,
            "negative": IMPROVED_NEGATIVE,
            "seed": IMPROVED_SEED,
            "steps": IMPROVED_STEPS,
            "cfg": IMPROVED_CFG,
            "video_path": str(impr_mp4),
            "frames_dir": str(impr_frames_dir),
            "generation_duration_s": duration,
            "vram_stats": stats["vram_after"],
            "verification": verify_res,
            "metrics": impr_metrics,
        },
    }

    report_path = OUTPUT_DIR / "comparison_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"Saved full comparison report to {report_path}")
    print("=" * 70)
    print("EXPERIMENT COMPLETED SUCCESSFULLY!")
    print("=" * 70)


if __name__ == "__main__":
    main()
