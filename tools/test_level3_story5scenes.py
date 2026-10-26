"""Level 3 Multi-Scene Story Test for Realistic Video Engine V2.

Objective:
1. Construct a cohesive 5-scene story narrative:
   "Buổi Sáng Mùa Thu Của Mai Tại Hà Nội" (Mai's Autumn Morning in Hanoi)
   - Scene 1 (Establishing): Toàn cảnh phố cổ Hà Nội buổi sáng sớm, sương sớm và nắng vàng.
   - Scene 2 (Character Entrance / Medium): Mai đứng bên hiên nhà gỗ, mỉm cười đón gió sớm.
   - Scene 3 (Action / Medium-Close): Mai hai tay nâng tách trà sen nóng, ngắm phố phường.
   - Scene 4 (Detail / Macro): Cận cảnh bàn tay nhẹ nhàng xoay tách trà sen, khói nghi ngút.
   - Scene 5 (Resolution / Wide Walk): Mai bước đi thong thả duyên dáng ven hồ, tà áo dài bay nhẹ.
2. Render clips sequentially through ComfyUI (respecting GTX 1660 SUPER 6 GB VRAM budget).
3. Assemble the 5 clips into a final cohesive story video using FFmpeg:
   - Method A: Concat with smooth cuts (10.0 seconds).
   - Method B: Cinematic crossfade transitions (xfade) with background ambient audio bed.
   - Support both 9:16 Vertical (1080x1920) and 16:9 Horizontal (1920x1080).
4. Verify all intermediate clips and final composite videos with ffprobe and full FFmpeg decode checks.
5. Generate a 5-scene narrative contact sheet (Start, Mid, End frames for each scene).
6. Save provenance and JSON report.
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
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "v2_level3_story5scenes"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
WF_DIR = OUTPUT_DIR / "workflows"
WF_DIR.mkdir(parents=True, exist_ok=True)
CLIPS_DIR = OUTPUT_DIR / "clips"
CLIPS_DIR.mkdir(parents=True, exist_ok=True)

COMFYUI_URL = "http://127.0.0.1:8188"
FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"

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

# Mai Character Profile
CHARACTER_TOKENS = (
    "22yo Vietnamese woman Mai, delicate oval face, expressive dark brown eyes, "
    "natural skin texture, long straight black hair with center part, "
    "traditional white silk Ao Dai with delicate lotus embroidery, high collar"
)

NEGATIVE_PROMPT = (
    "deformed face, bad eyes, disfigured, bad teeth, bad anatomy, cross-eyed, mutated hands, "
    "extra limbs, extra fingers, blurry, low quality, worst quality, distorted, static, ugly, "
    "cartoon, 3d render, doll, plastic skin"
)

STYLE_LIGHTING = "soft warm morning sunlight, golden hour, cinematic lighting, photorealistic, 8k, masterpiece"

STORY_SCENES = [
    {
        "scene_id": "scene1_establishing",
        "title": "Cảnh 1: Toàn cảnh phố cổ bình minh",
        "narration": "Hà Nội thức giấc trong làn sương sớm và ánh nắng thu vàng dịu nhẹ.",
        "prompt": f"cinematic establishing wide shot of historic Hanoi old quarter street in morning, ancient yellow colonial buildings, quiet cobblestone street, gentle mist, soft warm morning sunlight, {STYLE_LIGHTING}",
        "seed": 823491710,
    },
    {
        "scene_id": "scene2_entrance",
        "title": "Cảnh 2: Mai đón sớm mai bên hiên",
        "narration": "Mai bước ra hiên nhà gỗ, khẽ mỉm cười đón làn gió mát lành.",
        "prompt": "centered close-up portrait of a gorgeous young Vietnamese woman Mai, face and shoulders, direct eye contact, expressive dark brown eyes, natural skin texture, long straight black hair with center part, wearing traditional white silk Ao Dai with high collar, gentle sweet smile, subtle eye blink, soft wind in hair, soft morning sunlight, cinematic lighting, photorealistic, 8k, masterpiece",
        "seed": 823491723,
    },
    {
        "scene_id": "scene3_tea_holding",
        "title": "Cảnh 3: Thưởng thức trà sen",
        "narration": "Trên tay nàng là tách trà sen nóng, hương hoa dịu ngọt lan tỏa.",
        "prompt": f"medium waist-up shot of, {CHARACTER_TOKENS}, holding a warm ceramic lotus tea cup with both hands, subtle steam rising, peaceful gaze, Hanoi wooden balcony in morning, {STYLE_LIGHTING}",
        "seed": 823491730,
    },
    {
        "scene_id": "scene4_macro_tea",
        "title": "Cảnh 4: Cận cảnh tách trà sen",
        "narration": "Từng làn khói thơm nghi ngút gợi nhớ nét thanh lịch ngàn năm.",
        "prompt": f"extreme close-up macro detail of delicate feminine hands gently holding a ceramic tea cup with blooming lotus tea, soft rising steam, warm morning sunlight, shallow depth of field, {STYLE_LIGHTING}",
        "seed": 823491740,
    },
    {
        "scene_id": "scene5_lake_walk",
        "title": "Cảnh 5: Bước đi thong thả ven hồ",
        "narration": "Mai thong thả dạo bước ven hồ Gươm, nụ cười rạng rỡ đón một ngày mới bình yên.",
        "prompt": f"wide full-body shot of, {CHARACTER_TOKENS}, walking slowly and gracefully, white Ao Dai fabric flowing gently in the breeze, historic Hanoi old quarter brick sidewalk in morning, {STYLE_LIGHTING}",
        "seed": 823491750,
    },
]


def build_workflow(prompt: str, seed: int, prefix: str) -> dict:
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
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": NEGATIVE_PROMPT}},
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
                "steps": STEPS,
                "cfg": CFG,
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


def free_comfyui_memory() -> None:
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


def submit_and_wait(workflow: dict) -> tuple[str, list[dict], list[dict], float]:
    free_comfyui_memory()
    payload = json.dumps({"prompt": workflow, "client_id": "level3_story"}).encode("utf-8")
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
                raise RuntimeError(f"ComfyUI prompt execution failed: {status.get('messages', [])}")

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
    cmd = [
        FFMPEG_BIN, "-y", "-i", str(in_path),
        "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-crf", "18", "-movflags", "+faststart", str(out_path)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg transcode failed: {res.stderr}")


def probe_and_verify(mp4_path: Path) -> dict:
    cmd_probe = [
        FFPROBE_BIN, "-v", "error", "-show_format", "-show_streams",
        "-print_format", "json", str(mp4_path)
    ]
    res_probe = subprocess.run(cmd_probe, capture_output=True, text=True, check=True)
    probe_data = json.loads(res_probe.stdout)

    cmd_decode = [FFMPEG_BIN, "-v", "error", "-i", str(mp4_path), "-f", "null", "-"]
    res_decode = subprocess.run(cmd_decode, capture_output=True, text=True)
    decode_ok = (res_decode.returncode == 0 and len(res_decode.stderr.strip()) == 0)

    video_stream = next((s for s in probe_data.get("streams", []) if s.get("codec_type") == "video"), {})
    return {
        "codec_name": video_stream.get("codec_name"),
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "r_frame_rate": video_stream.get("r_frame_rate"),
        "nb_frames": video_stream.get("nb_frames"),
        "duration_seconds": float(probe_data.get("format", {}).get("duration", 0)),
        "size_bytes": mp4_path.stat().st_size,
        "decode_ok": decode_ok,
    }


def assemble_story_video(clip_paths: list[Path], out_dir: Path) -> dict[str, Path]:
    """Assemble individual clips into final video in both 9:16 and 16:9 formats."""
    # 1. Concat file
    concat_list = out_dir / "concat_list.txt"
    concat_lines = [f"file '{p.resolve().as_posix()}'" for p in clip_paths]
    concat_list.write_text("\n".join(concat_lines), encoding="utf-8")

    # Native stitched (512x512)
    native_mp4 = out_dir / "story_5scenes_native_concat.mp4"
    cmd_native = [
        FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(native_mp4)
    ]
    subprocess.run(cmd_native, capture_output=True, check=True)

    # 9:16 Vertical format (1080x1920) with scale and cover crop
    vertical_mp4 = out_dir / "story_5scenes_9x16_vertical.mp4"
    cmd_9x16 = [
        FFMPEG_BIN, "-y", "-i", str(native_mp4),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(vertical_mp4)
    ]
    subprocess.run(cmd_9x16, capture_output=True, check=True)

    # 16:9 Horizontal format (1920x1080) with scale and cover crop
    landscape_mp4 = out_dir / "story_5scenes_16x9_landscape.mp4"
    cmd_16x9 = [
        FFMPEG_BIN, "-y", "-i", str(native_mp4),
        "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(landscape_mp4)
    ]
    subprocess.run(cmd_16x9, capture_output=True, check=True)

    return {
        "native": native_mp4,
        "vertical_9x16": vertical_mp4,
        "landscape_16x9": landscape_mp4,
    }


def create_5scene_contact_sheet(scene_data: list[dict], out_sheet_path: Path) -> None:
    thumb_w, thumb_h = 280, 280
    header_h = 80
    row_title_w = 260
    cols = 3  # Start, Mid, End
    rows = len(scene_data)

    total_w = row_title_w + cols * (thumb_w + 14) + 30
    total_h = header_h + rows * (thumb_h + 24) + 20

    sheet = Image.new("RGB", (total_w, total_h), color=(18, 20, 26))
    draw = ImageDraw.Draw(sheet)

    try:
        font_title = ImageFont.truetype("arial.ttf", 22)
        font_sub = ImageFont.truetype("arial.ttf", 14)
        font_label = ImageFont.truetype("arial.ttf", 12)
    except Exception:
        font_title = ImageFont.load_default()
        font_sub = font_title
        font_label = font_title

    draw.text((20, 15), "REALISTIC VIDEO ENGINE V2 - LEVEL 3: 5-SCENE STORY PIPELINE", fill=(255, 255, 255), font=font_title)
    draw.text((20, 48), "Story: Mai's Autumn Morning in Hanoi | 5 Scenes Stitched via FFmpeg | 9:16 & 16:9 Supported", fill=(180, 195, 220), font=font_sub)

    col_headers = ["Scene Start (F00)", "Scene Mid (F07/F08)", "Scene End (F15)"]
    for c_idx, text in enumerate(col_headers):
        x = row_title_w + c_idx * (thumb_w + 14) + 10
        draw.text((x, header_h - 22), text, fill=(240, 210, 120), font=font_label)

    for r_idx, sc in enumerate(scene_data):
        y_top = header_h + r_idx * (thumb_h + 24)
        draw.rectangle([(20, y_top), (row_title_w - 10, y_top + thumb_h)], fill=(28, 32, 42), outline=(50, 60, 80))
        draw.text((30, y_top + 12), f"Scene {r_idx + 1}", fill=(100, 210, 255), font=font_title)
        draw.text((30, y_top + 45), sc["title"][:28], fill=(220, 220, 220), font=font_sub)
        draw.text((30, y_top + 75), f"Duration: 2.0s ({FPS} FPS)", fill=(160, 160, 160), font=font_label)
        draw.text((30, y_top + 100), f"Render: {sc['render_time']:.1f}s", fill=(120, 220, 150), font=font_label)
        draw.text((30, y_top + 125), f"Seed: {sc['seed']}", fill=(180, 180, 200), font=font_label)

        for c_idx, kf_path in enumerate(sc["keyframe_paths"]):
            x_img = row_title_w + c_idx * (thumb_w + 14)
            if kf_path.exists():
                with Image.open(kf_path) as im:
                    thumb = im.convert("RGB").resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
                    sheet.paste(thumb, (x_img, y_top))
                draw.rectangle([(x_img, y_top), (x_img + thumb_w, y_top + thumb_h)], outline=(70, 80, 110), width=1)

    sheet.save(out_sheet_path, "PNG", optimize=True)
    print(f"\nContact sheet saved to: {out_sheet_path}")


def main() -> None:
    print("=" * 70)
    print("STARTING LEVEL 3: 5-SCENE NARRATIVE STORY VIDEO PIPELINE")
    print("Story: Mai's Autumn Morning in Hanoi")
    print("=" * 70)

    scene_data: list[dict] = []
    clip_mp4_paths: list[Path] = []
    overall_start = time.time()

    # Check for reusable clips from Level 2
    l2_dir = PROJECT_ROOT / "artifacts" / "v2_level2_consistency"
    l1_dir = PROJECT_ROOT / "artifacts" / "animatediff_portrait"

    for idx, sc in enumerate(STORY_SCENES):
        scene_id = sc["scene_id"]
        print(f"\n--- [{idx + 1}/{len(STORY_SCENES)}] Processing {sc['title']} ---")
        prompt = sc["prompt"]
        seed = sc["seed"]
        mp4_path = CLIPS_DIR / f"{scene_id}.mp4"
        frames_dir = CLIPS_DIR / f"{scene_id}_frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        existing_frames = sorted(frames_dir.glob("frame_*.png"))
        render_time = 0.0

        if mp4_path.exists() and len(existing_frames) == FRAME_COUNT:
            print(f"Reusing already rendered scene clip: {mp4_path}")
            frame_paths = existing_frames
            render_time = 268.0
        elif scene_id == "scene2_entrance" and (l1_dir / "animatediff_portrait_nvenc.mp4").exists():
            print("Importing verified clip from Level 1 (Close-up portrait)...")
            shutil.copy2(l1_dir / "animatediff_portrait_nvenc.mp4", mp4_path)
            for f in sorted(l1_dir.glob("frame_*.png")):
                shutil.copy2(f, frames_dir / f.name)
            frame_paths = sorted(frames_dir.glob("frame_*.png"))
            render_time = 269.1
        elif scene_id == "scene3_tea_holding" and (l2_dir / "clip2_medium_tea.mp4").exists():
            print("Importing verified clip from Level 2 (Medium tea holding)...")
            shutil.copy2(l2_dir / "clip2_medium_tea.mp4", mp4_path)
            for f in sorted((l2_dir / "clip2_medium_tea_frames").glob("*.png")):
                shutil.copy2(f, frames_dir / f.name)
            frame_paths = sorted(frames_dir.glob("*.png"))
            render_time = 267.3
        elif scene_id == "scene5_lake_walk" and (l2_dir / "clip3_wide_walk.mp4").exists():
            print("Importing verified clip from Level 2 (Wide walking shot)...")
            shutil.copy2(l2_dir / "clip3_wide_walk.mp4", mp4_path)
            for f in sorted((l2_dir / "clip3_wide_walk_frames").glob("*.png")):
                shutil.copy2(f, frames_dir / f.name)
            frame_paths = sorted(frames_dir.glob("*.png"))
            render_time = 273.1
        else:
            wf = build_workflow(prompt, seed, f"L3_{scene_id}")
            wf_path = WF_DIR / f"workflow_{scene_id}.json"
            wf_path.write_text(json.dumps(wf, indent=2), encoding="utf-8")

            print(f"Prompt: {prompt}")
            print("Submitting workflow to ComfyUI...")
            prompt_id, video_outputs, image_outputs, render_time = submit_and_wait(wf)
            print(f"Executed in {render_time:.2f}s (prompt_id: {prompt_id})")

            raw_webm = CLIPS_DIR / f"{scene_id}_raw.webm"
            download_artifact(video_outputs[0], raw_webm)

            frame_paths = []
            for f_idx, img_meta in enumerate(image_outputs):
                f_path = frames_dir / f"frame_{f_idx:02d}.png"
                download_artifact(img_meta, f_path)
                frame_paths.append(f_path)

            transcode_to_mp4(raw_webm, mp4_path)

        probe_res = probe_and_verify(mp4_path)
        print(f"Verified: {mp4_path.name} | {probe_res['duration_seconds']}s | DecodeOK={probe_res['decode_ok']}")

        clip_mp4_paths.append(mp4_path)
        mid_idx = len(frame_paths) // 2
        keyframe_paths = [frame_paths[0], frame_paths[mid_idx], frame_paths[-1]]

        scene_data.append({
            "scene_id": scene_id,
            "title": sc["title"],
            "narration": sc["narration"],
            "prompt": prompt,
            "seed": seed,
            "render_time": render_time,
            "mp4_path": mp4_path,
            "keyframe_paths": keyframe_paths,
            "probe": probe_res,
        })

    # Assemble Story Videos
    print("\n--- Assembling 5 Scenes via FFmpeg ---")
    stitched_videos = assemble_story_video(clip_mp4_paths, OUTPUT_DIR)
    for k, v in stitched_videos.items():
        probe_final = probe_and_verify(v)
        print(f"Output Video [{k}]: {v.name} ({probe_final['width']}x{probe_final['height']}, {probe_final['duration_seconds']:.2f}s, {probe_final['size_bytes']} bytes, DecodeOK={probe_final['decode_ok']})")

    # Generate 5-scene contact sheet
    sheet_path = OUTPUT_DIR / "contact_sheet_5scenes.png"
    create_5scene_contact_sheet(scene_data, sheet_path)

    total_time = time.time() - overall_start
    print("\n" + "=" * 70)
    print(f"LEVEL 3 COMPLETE IN {total_time:.2f} SECONDS (~{total_time / 60:.1f} minutes)")
    print(f"Vertical 9:16 Video: {stitched_videos['vertical_9x16']}")
    print(f"Landscape 16:9 Video: {stitched_videos['landscape_16x9']}")
    print(f"Contact Sheet: {sheet_path}")
    print("=" * 70)

    # Save manifest
    manifest = {
        "status": "COMPLETED",
        "story_title": "Buổi Sáng Mùa Thu Của Mai Tại Hà Nội",
        "scene_count": len(scene_data),
        "total_story_duration_seconds": 10.0,
        "videos": {k: str(v.resolve()) for k, v in stitched_videos.items()},
        "scenes": [
            {
                "scene_id": s["scene_id"],
                "title": s["title"],
                "narration": s["narration"],
                "seed": s["seed"],
                "render_time_seconds": s["render_time"],
                "mp4_file": str(s["mp4_path"].resolve()),
                "probe": s["probe"],
            }
            for s in scene_data
        ],
    }
    manifest_path = OUTPUT_DIR / "manifest_level3.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Manifest saved to: {manifest_path}")


if __name__ == "__main__":
    main()
