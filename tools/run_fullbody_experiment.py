"""Full-body motion stabilization experiment for Scene 5 (Lake Walk).

Strictly maintains full-body framing (head to toe, feet on sidewalk) while
testing motion stabilization, posture anchoring, and comparing against baseline.
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
CLIPS_DIR = OUTPUT_DIR / "clips"
WF_DIR = OUTPUT_DIR / "workflows"
FRAMES_V2_DIR = OUTPUT_DIR / "improved_v2_fullbody_frames"

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

# Explicit full-body anchoring prompt
FULLBODY_PROMPT = (
    "wide full-body shot, wide angle, head to toe, full length, "
    "22yo Vietnamese woman Mai standing with upright straight posture, "
    "taking a slow graceful step on brick sidewalk, feet on ground, "
    "natural hands by side, traditional white silk Ao Dai with long pants, "
    "long straight black hair, stable fixed camera, historic Hanoi old quarter brick wall background, "
    "soft warm morning sunlight, cinematic lighting, photorealistic, 8k, masterpiece"
)

# Targeted negative prompt with anti-crop and anti-contortion tokens
FULLBODY_NEGATIVE = (
    "cropped, cut off, headshot, close up, medium shot, out of frame, "
    "leaning back, backward bend, arching spine, contorted body, distorted neck, falling backwards, "
    "floating feet, flying, running, deformed feet, bad shoes, mutated hands, extra limbs, extra fingers, "
    "deformed face, bad eyes, disfigured, bad teeth, bad anatomy, blurry, low quality, worst quality, "
    "distorted, ugly, cartoon, 3d render, doll, plastic skin"
)

STEPS = 16
CFG = 5.5
SEED = 823491750


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


def submit_and_poll(workflow: dict) -> tuple[str, list[dict], list[dict], float, dict]:
    free_memory()
    vram_before = get_vram_stats()

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
        "duration_s": duration,
        "vram_before": vram_before,
        "vram_after": vram_after,
    }
    return prompt_id, save_webm, save_images, duration, stats


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


def make_ab_comparison_sheet(baseline_paths: list[Path], improved_paths: list[Path], output_path: Path, title: str):
    b_frames = [Image.open(f).convert("RGB").resize((280, 280), Image.Resampling.LANCZOS) for f in baseline_paths]
    i_frames = [Image.open(f).convert("RGB").resize((280, 280), Image.Resampling.LANCZOS) for f in improved_paths]

    cell_w, cell_h = 280, 280
    header_h = 32
    margin = 6
    cols = 8  # 4 pairs per row
    rows = 4  # 4 rows = 16 pairs

    sheet_w = cols * cell_w + (cols + 1) * margin
    sheet_h = rows * (cell_h + header_h) + (rows + 1) * margin + 60

    sheet = Image.new("RGB", (sheet_w, sheet_h), color=(18, 18, 22))
    draw = ImageDraw.Draw(sheet)

    try:
        title_font = ImageFont.truetype("arial.ttf", 24)
        label_font = ImageFont.truetype("arial.ttf", 14)
    except Exception:
        title_font = ImageFont.load_default()
        label_font = ImageFont.load_default()

    # Title Banner
    draw.rectangle([0, 0, sheet_w, 55], fill=(30, 32, 40))
    draw.text((20, 14), title, fill=(255, 215, 0), font=title_font)

    for pair_idx in range(16):
        row = pair_idx // 4
        pair_in_row = pair_idx % 4

        b_col = pair_in_row * 2
        b_x = margin + b_col * (cell_w + margin)
        b_y = 60 + margin + row * (cell_h + header_h + margin)

        i_col = pair_in_row * 2 + 1
        i_x = margin + i_col * (cell_w + margin)
        i_y = b_y

        draw.rectangle([b_x, b_y, b_x + cell_w, b_y + header_h], fill=(70, 30, 30))
        draw.text((b_x + 8, b_y + 6), f"BASELINE F{pair_idx:02d} ({pair_idx*0.125:.2f}s)", fill=(255, 200, 200), font=label_font)
        sheet.paste(b_frames[pair_idx], (b_x, b_y + header_h))

        draw.rectangle([i_x, i_y, i_x + cell_w, i_y + header_h], fill=(30, 70, 45))
        draw.text((i_x + 8, i_y + 6), f"V2 FULLBODY F{pair_idx:02d} ({pair_idx*0.125:.2f}s)", fill=(200, 255, 210), font=label_font)
        sheet.paste(i_frames[pair_idx], (i_x, i_y + header_h))

    sheet.save(output_path, "PNG")


def main():
    print("=" * 70)
    print("STARTING FULL-BODY STABILIZATION EXPERIMENT (SCENE 5)")
    print("=" * 70)

    baseline_dir = PROJECT_ROOT / "artifacts" / "v2_level3_story5scenes" / "clips" / "scene5_lake_walk_frames"
    baseline_paths = [baseline_dir / f"frame_{i:02d}.png" for i in range(16)]

    prefix = "scene5_fullbody_v2"
    wf = build_workflow(
        prompt=FULLBODY_PROMPT,
        negative=FULLBODY_NEGATIVE,
        seed=SEED,
        steps=STEPS,
        cfg=CFG,
        prefix=prefix,
    )
    wf_path = WF_DIR / "workflow_scene5_fullbody_v2.json"
    with open(wf_path, "w", encoding="utf-8") as f:
        json.dump(wf, f, indent=2)
    print(f"Saved fullbody workflow to {wf_path}")

    print("Submitting fullbody workflow to ComfyUI (16 steps, CFG 5.5)...")
    prompt_id, webms, images, duration, stats = submit_and_poll(wf)
    print(f"Generation completed in {duration:.2f} seconds. Prompt ID: {prompt_id}")
    print(f"VRAM Stats: {stats['vram_after']}")

    v2_paths = download_frames(images, FRAMES_V2_DIR)
    print(f"Downloaded {len(v2_paths)} frames to {FRAMES_V2_DIR}")

    v2_mp4 = CLIPS_DIR / "scene5_lake_walk_v2_fullbody.mp4"
    transcode_to_mp4(v2_paths, v2_mp4)
    print(f"Transcoded fullbody video to {v2_mp4}")

    verify_res = verify_video(v2_mp4)
    print(f"Verification: {verify_res}")

    v2_sheet_path = OUTPUT_DIR / "v2_fullbody_16frames_sheet.png"
    make_contact_sheet(v2_paths, v2_sheet_path, "V2 Full-Body Stabilized Scene 5")
    print(f"Saved fullbody contact sheet to {v2_sheet_path}")

    ab_v2_sheet = OUTPUT_DIR / "ab_comparison_baseline_vs_fullbody_v2.png"
    make_ab_comparison_sheet(
        baseline_paths,
        v2_paths,
        ab_v2_sheet,
        "Scene 5 Full-Body 16-Frame Comparison: [Baseline vs V2 Full-Body Stabilized]",
    )
    print(f"Saved Baseline vs V2 comparison sheet to {ab_v2_sheet}")

    # Metrics
    metrics = []
    frames = [Image.open(f).convert("RGB") for f in v2_paths]
    for i in range(len(frames)):
        curr_arr = np.array(frames[i], dtype=np.float32)
        if i > 0:
            prev_arr = np.array(frames[i - 1], dtype=np.float32)
            mse = float(np.mean((curr_arr - prev_arr) ** 2))
            mae = float(np.mean(np.abs(curr_arr - prev_arr)))
        else:
            mse, mae = 0.0, 0.0
        mean_rgb = [float(np.mean(curr_arr[:, :, c])) for c in range(3)]
        metrics.append({
            "frame_idx": i,
            "mse_to_prev": mse,
            "mae_to_prev": mae,
            "mean_rgb": mean_rgb,
        })

    report = {
        "experiment": "fullbody_motion_stabilization_v2",
        "prompt": FULLBODY_PROMPT,
        "negative": FULLBODY_NEGATIVE,
        "seed": SEED,
        "steps": STEPS,
        "cfg": CFG,
        "duration_s": duration,
        "vram_stats": stats["vram_after"],
        "verification": verify_res,
        "metrics": metrics,
    }
    with open(OUTPUT_DIR / "v2_fullbody_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print("=" * 70)
    print("FULL-BODY EXPERIMENT COMPLETE!")
    print("=" * 70)


if __name__ == "__main__":
    main()
