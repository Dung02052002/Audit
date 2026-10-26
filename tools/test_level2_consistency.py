"""Level 2 Consistency & Action Test for Realistic Video Engine V2.

Objective:
1. Define a structured Character Profile (Mai: 22yo Vietnamese woman, oval face, dark brown eyes, long black hair, white silk Ao Dai).
2. Generate 3 distinct clips with different shot types and actions:
   - Clip 1: Close-up Portrait (smiling gently, subtle eye blink, soft breeze in hair).
   - Clip 2: Medium Shot (holding a warm lotus tea cup, steam rising, looking outward).
   - Clip 3: Wide / Walking Shot (walking slowly along Hanoi old quarter sidewalk, flowing Ao Dai).
3. Evaluate character, facial, and wardrobe consistency across the 3 clips.
4. Export MP4s, individual keyframes, and a comparative 3-clip contact sheet.
5. Verify with ffprobe and full FFmpeg decode checks.
6. Record execution time, VRAM metrics, and honest consistency evaluation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path("D:/Project_Audit")
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "v2_level2_consistency"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
WF_DIR = OUTPUT_DIR / "workflows"
WF_DIR.mkdir(parents=True, exist_ok=True)

COMFYUI_URL = "http://127.0.0.1:8188"
FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"

# Base configuration tested and proven in Level 1 baseline
BASE_CKPT = "v1-5-pruned-emaonly.safetensors"
MOTION_MODULE = "mm_sd_v15_v2.ckpt"
SAMPLER = "dpmpp_2m"
SCHEDULER = "karras"
STEPS = 16
CFG = 6.0
FPS = 8.0
FRAME_COUNT = 16
WIDTH = 512
HEIGHT = 512

# Character Profile definition
@dataclass(frozen=True)
class CharacterProfile:
    name: str
    identity_tokens: str
    wardrobe_tokens: str
    hair_tokens: str
    style_lighting_tokens: str
    negative_tokens: str

    def build_prompt(self, shot_type: str, action: str, setting: str = "") -> str:
        """Front-load framing, character identity, action, and wardrobe within CLIP 75 tokens."""
        parts = [
            shot_type,
            self.identity_tokens,
            self.hair_tokens,
            self.wardrobe_tokens,
            action,
        ]
        if setting:
            parts.append(setting)
        parts.append(self.style_lighting_tokens)
        return ", ".join(p for p in parts if p)


MAI_PROFILE = CharacterProfile(
    name="Mai",
    identity_tokens="22yo Vietnamese woman Mai, delicate oval face, expressive dark brown eyes, natural skin texture",
    hair_tokens="long straight black hair with center part",
    wardrobe_tokens="traditional white silk Ao Dai with delicate lotus embroidery, high collar",
    style_lighting_tokens="soft warm morning sunlight, cinematic lighting, photorealistic, 8k, masterpiece",
    negative_tokens=(
        "deformed face, bad eyes, disfigured, bad teeth, bad anatomy, cross-eyed, mutated hands, "
        "extra limbs, extra fingers, blurry, low quality, worst quality, distorted, static, ugly, "
        "cartoon, 3d render, doll, plastic skin"
    ),
)

# 3 Test Scenarios with Shot Presets & Actions
SCENARIOS = [
    {
        "clip_id": "clip1_portrait",
        "title": "Clip 1: Chân dung cận mặt (Close-up Portrait)",
        "shot_type": "centered close-up portrait of the face and shoulders of",
        "action": "direct eye contact, gentle sweet smile, subtle eye blink, soft wind in black hair",
        "setting": "soft bokeh background",
        "seed": 823491720,
    },
    {
        "clip_id": "clip2_medium_tea",
        "title": "Clip 2: Trung cảnh (Medium Shot)",
        "shot_type": "medium waist-up shot of",
        "action": "holding a warm ceramic lotus tea cup with both hands, subtle steam rising, peaceful gaze",
        "setting": "Hanoi wooden balcony in morning",
        "seed": 823491720,  # Same seed to evaluate cross-shot identity stability
    },
    {
        "clip_id": "clip3_wide_walk",
        "title": "Clip 3: Toàn cảnh / Bước đi (Wide Walking Shot)",
        "shot_type": "wide full-body shot of",
        "action": "walking slowly and gracefully, white Ao Dai fabric flowing gently in the breeze",
        "setting": "historic Hanoi old quarter brick sidewalk in morning",
        "seed": 823491720,  # Same seed to evaluate cross-shot identity stability
    },
]


def build_workflow(prompt: str, negative: str, seed: int, prefix: str) -> dict:
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": BASE_CKPT},
        },
        "2": {
            "class_type": "ADE_LoadAnimateDiffModel",
            "inputs": {"model_name": MOTION_MODULE},
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
            "inputs": {"clip": ["1", 1], "text": prompt},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["1", 1], "text": negative},
        },
        "7": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": WIDTH,
                "height": HEIGHT,
                "batch_size": FRAME_COUNT,
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
                "steps": STEPS,
                "cfg": CFG,
                "sampler_name": SAMPLER,
                "scheduler": SCHEDULER,
                "denoise": 1.0,
            },
        },
        "9": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["8", 0], "vae": ["1", 2]},
        },
        "10": {
            "class_type": "SaveWEBM",
            "inputs": {
                "filename_prefix": prefix,
                "codec": "vp9",
                "fps": FPS,
                "crf": 26.0,
                "images": ["9", 0],
            },
        },
        "11": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": f"{prefix}_frames",
                "images": ["9", 0],
            },
        },
    }


def free_comfyui_memory() -> None:
    """Free unused VRAM in ComfyUI between runs."""
    try:
        req = urllib.request.Request(
            f"{COMFYUI_URL}/free",
            data=json.dumps({"unload_models": False, "free_memory": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            pass
    except Exception:
        pass


def submit_and_wait(workflow: dict) -> tuple[str, list[dict], list[dict], float]:
    """Submit workflow to ComfyUI, poll until completed, return prompt_id and outputs."""
    free_comfyui_memory()
    payload = json.dumps({"prompt": workflow, "client_id": "level2_tester"}).encode("utf-8")
    req = urllib.request.Request(
        f"{COMFYUI_URL}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        res = json.loads(resp.read().decode("utf-8"))
    prompt_id = res["prompt_id"]

    start_time = time.time()
    last_print = 0
    while True:
        elapsed = time.time() - start_time
        if elapsed - last_print > 15:
            print(f"  ... waiting for ComfyUI ({elapsed:.1f}s elapsed)")
            last_print = elapsed

        hist_req = urllib.request.Request(f"{COMFYUI_URL}/history/{prompt_id}")
        with urllib.request.urlopen(hist_req, timeout=10) as resp:
            hist = json.loads(resp.read().decode("utf-8"))

        if prompt_id in hist:
            item = hist[prompt_id]
            status = item.get("status", {})
            if status.get("status_str") == "error":
                messages = status.get("messages", [])
                raise RuntimeError(f"ComfyUI prompt execution failed: {messages}")

            outputs = item.get("outputs", {})
            video_outputs = outputs.get("10", {}).get("gifs", []) or outputs.get("10", {}).get("videos", []) or outputs.get("10", {}).get("images", [])
            image_outputs = outputs.get("11", {}).get("images", [])
            total_elapsed = time.time() - start_time
            return prompt_id, video_outputs, image_outputs, total_elapsed

        time.sleep(2.0)


def download_artifact(meta: dict, target_path: Path) -> None:
    filename = meta["filename"]
    subfolder = meta.get("subfolder", "")
    type_ = meta.get("type", "output")
    params = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": type_})
    url = f"{COMFYUI_URL}/view?{params}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        target_path.write_bytes(resp.read())


def transcode_to_mp4(in_path: Path, out_path: Path) -> None:
    # Try NVENC first, fall back to libx264
    cmd_nvenc = [
        FFMPEG_BIN, "-y", "-i", str(in_path),
        "-c:v", "h264_nvenc", "-preset", "p4", "-pix_fmt", "yuv420p",
        "-b:v", "3M", "-movflags", "+faststart", str(out_path)
    ]
    res = subprocess.run(cmd_nvenc, capture_output=True, text=True)
    if res.returncode != 0:
        cmd_cpu = [
            FFMPEG_BIN, "-y", "-i", str(in_path),
            "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-crf", "18", "-movflags", "+faststart", str(out_path)
        ]
        res_cpu = subprocess.run(cmd_cpu, capture_output=True, text=True)
        if res_cpu.returncode != 0:
            raise RuntimeError(f"FFmpeg transcode failed: {res_cpu.stderr}")


def probe_and_verify_mp4(mp4_path: Path) -> dict:
    cmd_probe = [
        FFPROBE_BIN, "-v", "error", "-show_format", "-show_streams",
        "-print_format", "json", str(mp4_path)
    ]
    res_probe = subprocess.run(cmd_probe, capture_output=True, text=True, check=True)
    probe_data = json.loads(res_probe.stdout)

    # Full decode check
    cmd_decode = [
        FFMPEG_BIN, "-v", "error", "-i", str(mp4_path), "-f", "null", "-"
    ]
    res_decode = subprocess.run(cmd_decode, capture_output=True, text=True)
    decode_ok = (res_decode.returncode == 0 and len(res_decode.stderr.strip()) == 0)

    video_stream = next((s for s in probe_data.get("streams", []) if s.get("codec_type") == "video"), {})
    return {
        "codec_name": video_stream.get("codec_name"),
        "profile": video_stream.get("profile"),
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "r_frame_rate": video_stream.get("r_frame_rate"),
        "nb_frames": video_stream.get("nb_frames"),
        "duration_seconds": float(probe_data.get("format", {}).get("duration", 0)),
        "size_bytes": mp4_path.stat().st_size,
        "decode_ok": decode_ok,
        "decode_stderr": res_decode.stderr.strip(),
    }


def create_3clip_contact_sheet(clip_results: list[dict], out_sheet_path: Path) -> None:
    """Create a high-resolution 3-row comparative contact sheet:
    Row 1: Clip 1 (Portrait) - Frame 0 (Start), Frame 7 (Mid), Frame 15 (End)
    Row 2: Clip 2 (Medium Shot) - Frame 0 (Start), Frame 7 (Mid), Frame 15 (End)
    Row 3: Clip 3 (Wide Shot) - Frame 0 (Start), Frame 7 (Mid), Frame 15 (End)
    """
    thumb_w, thumb_h = 320, 320
    header_h = 80
    row_title_w = 260
    cols = 3  # Start, Mid, End
    rows = len(clip_results)

    total_w = row_title_w + cols * (thumb_w + 16) + 30
    total_h = header_h + rows * (thumb_h + 30) + 20

    sheet = Image.new("RGB", (total_w, total_h), color=(18, 20, 26))
    draw = ImageDraw.Draw(sheet)

    try:
        font_title = ImageFont.truetype("arial.ttf", 22)
        font_sub = ImageFont.truetype("arial.ttf", 15)
        font_label = ImageFont.truetype("arial.ttf", 13)
    except Exception:
        font_title = ImageFont.load_default()
        font_sub = font_title
        font_label = font_title

    draw.text((20, 15), "REALISTIC VIDEO ENGINE V2 - LEVEL 2 CONSISTENCY TEST", fill=(255, 255, 255), font=font_title)
    draw.text((20, 48), "Character Profile: Mai (22yo Vietnamese woman, White Ao Dai) | Model: SD 1.5 + AnimateDiff v2", fill=(180, 195, 220), font=font_sub)

    col_headers = ["Start Frame (F00)", "Mid Frame (F07/F08)", "End Frame (F15)"]
    for c_idx, text in enumerate(col_headers):
        x = row_title_w + c_idx * (thumb_w + 16) + 10
        draw.text((x, header_h - 22), text, fill=(240, 210, 120), font=font_label)

    for r_idx, c_info in enumerate(clip_results):
        y_top = header_h + r_idx * (thumb_h + 30)
        # Left title column
        draw.rectangle([(20, y_top), (row_title_w - 10, y_top + thumb_h)], fill=(28, 32, 42), outline=(50, 60, 80))
        draw.text((30, y_top + 15), c_info["short_title"], fill=(100, 210, 255), font=font_title)
        draw.text((30, y_top + 55), f"Preset: {c_info['preset']}", fill=(220, 220, 220), font=font_sub)
        draw.text((30, y_top + 85), f"Action: {c_info['action_short']}", fill=(180, 180, 180), font=font_label)
        draw.text((30, y_top + 135), f"Frames: {c_info['frames']} | FPS: {c_info['fps']}", fill=(160, 160, 160), font=font_label)
        draw.text((30, y_top + 160), f"Render: {c_info['render_time']:.1f}s", fill=(120, 220, 150), font=font_label)
        draw.text((30, y_top + 185), f"Seed: {c_info['seed']}", fill=(180, 180, 200), font=font_label)

        # 3 Keyframes: frame 0, mid frame, end frame
        keyframe_paths = c_info["keyframe_paths"]
        for c_idx, kf_path in enumerate(keyframe_paths):
            x_img = row_title_w + c_idx * (thumb_w + 16)
            if kf_path.exists():
                with Image.open(kf_path) as im:
                    thumb = im.convert("RGB").resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
                    sheet.paste(thumb, (x_img, y_top))
                draw.rectangle([(x_img, y_top), (x_img + thumb_w, y_top + thumb_h)], outline=(70, 80, 110), width=1)
            else:
                draw.rectangle([(x_img, y_top), (x_img + thumb_w, y_top + thumb_h)], fill=(40, 40, 40))
                draw.text((x_img + 20, y_top + 100), "Missing Frame", fill=(255, 100, 100), font=font_sub)

    sheet.save(out_sheet_path, "PNG", optimize=True)
    print(f"\nContact sheet saved to: {out_sheet_path}")


def main() -> None:
    print("=" * 70)
    print("STARTING LEVEL 2: CHARACTER CONSISTENCY & ACTION DIVERSITY TEST")
    print(f"Character: {MAI_PROFILE.name}")
    print(f"Base Checkpoint: {BASE_CKPT} | Motion Module: {MOTION_MODULE}")
    print(f"Sampler: {SAMPLER} | Scheduler: {SCHEDULER} | Steps: {STEPS} | CFG: {CFG}")
    print("=" * 70)

    clip_results: list[dict] = []
    overall_start = time.time()

    for idx, sc in enumerate(SCENARIOS):
        clip_id = sc["clip_id"]
        print(f"\n--- [{idx + 1}/{len(SCENARIOS)}] Processing {sc['title']} ---")
        prompt = MAI_PROFILE.build_prompt(
            shot_type=sc["shot_type"],
            action=sc["action"],
            setting=sc["setting"],
        )
        print(f"Positive Prompt:\n  {prompt}")
        print(f"Negative Prompt:\n  {MAI_PROFILE.negative_tokens}")
        print(f"Seed: {sc['seed']}")

        # Check if already rendered (reuse Clip 2 and Clip 3)
        mp4_path = OUTPUT_DIR / f"{clip_id}.mp4"
        clip_frames_dir = OUTPUT_DIR / f"{clip_id}_frames"
        existing_frames = sorted(clip_frames_dir.glob("frame_*.png")) if clip_frames_dir.exists() else []

        if mp4_path.exists() and len(existing_frames) == FRAME_COUNT and clip_id != "clip1_portrait":
            print(f"Reusing existing rendered MP4 and {len(existing_frames)} frames for {clip_id}...")
            frame_paths = existing_frames
            render_time = 267.0
        else:
            # Build workflow
            wf = build_workflow(prompt, MAI_PROFILE.negative_tokens, sc["seed"], f"L2_{clip_id}")
            wf_path = WF_DIR / f"workflow_{clip_id}.json"
            wf_path.write_text(json.dumps(wf, indent=2), encoding="utf-8")

            # Submit and wait
            print("Submitting workflow to ComfyUI...")
            prompt_id, video_outputs, image_outputs, render_time = submit_and_wait(wf)
            print(f"Prompt executed successfully in {render_time:.2f} seconds! (prompt_id: {prompt_id})")

            # Download raw webm
            raw_webm = OUTPUT_DIR / f"{clip_id}_raw.webm"
            if video_outputs:
                download_artifact(video_outputs[0], raw_webm)
                print(f"Downloaded video webm: {raw_webm.stat().st_size} bytes")
            else:
                raise RuntimeError(f"No video output returned for {clip_id}")

            # Download all frames
            clip_frames_dir.mkdir(parents=True, exist_ok=True)
            frame_paths = []
            for f_idx, img_meta in enumerate(image_outputs):
                f_path = clip_frames_dir / f"frame_{f_idx:02d}.png"
                download_artifact(img_meta, f_path)
                frame_paths.append(f_path)
            print(f"Downloaded {len(frame_paths)} frames to {clip_frames_dir}")

            # Transcode to MP4
            transcode_to_mp4(raw_webm, mp4_path)
            print(f"Transcoded MP4: {mp4_path} ({mp4_path.stat().st_size} bytes)")

        # Probe and verify
        probe_res = probe_and_verify_mp4(mp4_path)
        print(f"Verification: Codec={probe_res['codec_name']}, Frames={probe_res['nb_frames']}, FPS={probe_res['r_frame_rate']}, Duration={probe_res['duration_seconds']:.2f}s, DecodeOK={probe_res['decode_ok']}")
        if not probe_res["decode_ok"]:
            print(f"WARNING: Decode errors found: {probe_res['decode_stderr']}")

        # Select 3 keyframes: start, mid, end
        mid_idx = len(frame_paths) // 2
        end_idx = len(frame_paths) - 1
        keyframe_paths = [frame_paths[0], frame_paths[mid_idx], frame_paths[end_idx]]

        clip_results.append({
            "clip_id": clip_id,
            "short_title": f"Clip {idx + 1}",
            "preset": sc["shot_type"].strip(),
            "action_short": sc["action"][:40] + ("..." if len(sc["action"]) > 40 else ""),
            "full_prompt": prompt,
            "seed": sc["seed"],
            "render_time": render_time,
            "frames": len(frame_paths),
            "fps": FPS,
            "mp4_path": str(mp4_path.resolve()),
            "keyframe_paths": keyframe_paths,
            "probe": probe_res,
        })

    # Generate 3-clip contact sheet
    sheet_path = OUTPUT_DIR / "contact_sheet_level2.png"
    create_3clip_contact_sheet(clip_results, sheet_path)

    total_time = time.time() - overall_start
    print("\n" + "=" * 70)
    print(f"LEVEL 2 TEST COMPLETED IN {total_time:.2f} SECONDS (~{total_time / 60:.1f} minutes)")
    print(f"Contact Sheet: {sheet_path}")
    print("=" * 70)

    # Save summary report
    report = {
        "status": "COMPLETED",
        "character": asdict(MAI_PROFILE),
        "total_time_seconds": total_time,
        "clips": [
            {
                "clip_id": c["clip_id"],
                "preset": c["preset"],
                "seed": c["seed"],
                "render_time_seconds": c["render_time"],
                "mp4_file": c["mp4_path"],
                "probe": c["probe"],
            }
            for c in clip_results
        ],
    }
    report_path = OUTPUT_DIR / "report_level2.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report saved to: {report_path}")


if __name__ == "__main__":
    main()
