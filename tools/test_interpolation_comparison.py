"""Video Frame Interpolation (VFI) Benchmark and Comparison.

Objective:
1. Test motion-compensated frame interpolation (8 FPS -> 24 FPS, 3x frame rate) on:
   - Clip A: Close-up Portrait (facial motion, eyelid blink, hair drift)
   - Clip B: Medium Shot (hands, ceramic tea cup, steam, bodily posture)
2. Measure execution time and resource utilization.
3. Extract original vs interpolated frames.
4. Construct an A/B comparative contact sheet displaying:
   - Original Frame N
   - Synthesized In-between Frame N+1/3
   - Synthesized In-between Frame N+2/3
   - Original Frame N+1
5. Rigorously inspect for:
   - Ghosting / double-contour artifacts
   - Distortion of facial features, eyes, and teeth
   - Deformation of fingers and hands
   - Flicker and edge warping
6. Save ffprobe metadata and structured evaluation report.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path("D:/Project_Audit")
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "vfi_interpolation_test"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"

TEST_CLIPS = [
    {
        "id": "portrait_closeup",
        "name": "Chân dung cận cảnh (Portrait Eyelid & Hair)",
        "input_path": PROJECT_ROOT / "artifacts" / "animatediff_portrait" / "animatediff_portrait_nvenc.mp4",
        "focus_feature": "Eyes, eyelashes, skin texture, subtle hair motion",
    },
    {
        "id": "medium_tea",
        "name": "Trung cảnh tách trà (Medium Shot Hands & Steam)",
        "input_path": PROJECT_ROOT / "artifacts" / "v2_level2_consistency" / "clip2_medium_tea.mp4",
        "focus_feature": "Fingers, ceramic teacup rim, rising steam, torso",
    },
]


def interpolate_clip_8to24(in_mp4: Path, out_mp4: Path) -> tuple[float, dict]:
    """Run bidirectional optical-flow motion compensated interpolation (8 -> 24 FPS)."""
    start_t = time.time()
    cmd = [
        FFMPEG_BIN, "-y", "-i", str(in_mp4),
        "-vf", "minterpolate=fps=24:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(out_mp4)
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    elapsed = time.time() - start_t

    # Probe result
    cmd_probe = [
        FFPROBE_BIN, "-v", "error", "-show_format", "-show_streams",
        "-print_format", "json", str(out_mp4)
    ]
    res_probe = subprocess.run(cmd_probe, capture_output=True, text=True, check=True)
    probe_data = json.loads(res_probe.stdout)

    cmd_decode = [FFMPEG_BIN, "-v", "error", "-i", str(out_mp4), "-f", "null", "-"]
    res_decode = subprocess.run(cmd_decode, capture_output=True, text=True)
    decode_ok = (res_decode.returncode == 0 and len(res_decode.stderr.strip()) == 0)

    video_stream = next((s for s in probe_data.get("streams", []) if s.get("codec_type") == "video"), {})
    metrics = {
        "codec": video_stream.get("codec_name"),
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "fps": video_stream.get("r_frame_rate"),
        "nb_frames": int(video_stream.get("nb_frames", 0)),
        "duration": float(probe_data.get("format", {}).get("duration", 0)),
        "size_bytes": out_mp4.stat().st_size,
        "elapsed_seconds": elapsed,
        "decode_ok": decode_ok,
    }
    return elapsed, metrics


def extract_frames(video_path: Path, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG_BIN, "-y", "-i", str(video_path),
        str(out_dir / "f_%03d.png")
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return sorted(out_dir.glob("f_*.png"))


def build_vfi_contact_sheet(
    clip_id: str,
    title: str,
    orig_frames: list[Path],
    interp_frames: list[Path],
    out_sheet_path: Path,
) -> None:
    """Build a side-by-side progression contact sheet showing:
    Row 1: Frame sequence at 8 FPS (Original steps: F00, F01, F02, F03, F04)
    Row 2: Frame sequence at 24 FPS (Interpolated micro-steps: F00, F01, F02, F03, F04, F05, F06, F07, F08)
    """
    thumb_w, thumb_h = 240, 240
    header_h = 90
    title_w = 240

    # Show 5 original frames (Row 1) vs 9 interpolated frames (Row 2)
    cols = 9
    total_w = title_w + cols * (thumb_w + 12) + 30
    total_h = header_h + 2 * (thumb_h + 40) + 20

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

    draw.text((20, 15), f"VFI MOTION INTERPOLATION BENCHMARK: 8 FPS -> 24 FPS", fill=(255, 255, 255), font=font_title)
    draw.text((20, 48), f"Clip: {title} | Bidirectional Optical Flow Motion Compensation | Artifact Inspection", fill=(180, 195, 220), font=font_sub)

    # Row 1: Original 8 FPS frames (spaced out to align with corresponding time points)
    y_row1 = header_h + 10
    draw.rectangle([(20, y_row1), (title_w - 10, y_row1 + thumb_h)], fill=(32, 28, 48), outline=(70, 60, 90))
    draw.text((30, y_row1 + 20), "ORIGINAL", fill=(255, 180, 100), font=font_title)
    draw.text((30, y_row1 + 55), "Rate: 8.0 FPS", fill=(220, 220, 220), font=font_sub)
    draw.text((30, y_row1 + 85), "Step: 125ms / frame", fill=(180, 180, 180), font=font_label)
    draw.text((30, y_row1 + 120), "Source: AnimateDiff v2", fill=(160, 160, 160), font=font_label)

    # Display original frames F00, F01, F02, F03, F04 mapped to columns 0, 2, 4, 6, 8
    orig_indices = [0, 1, 2, 3, 4]
    for c_idx, o_idx in enumerate(orig_indices):
        col_pos = c_idx * 2  # spaced to show in-between slots
        x_img = title_w + col_pos * (thumb_w + 12)
        if o_idx < len(orig_frames):
            f_path = orig_frames[o_idx]
            with Image.open(f_path) as im:
                thumb = im.convert("RGB").resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
                sheet.paste(thumb, (x_img, y_row1))
            draw.rectangle([(x_img, y_row1), (x_img + thumb_w, y_row1 + thumb_h)], outline=(200, 150, 80), width=2)
            time_sec = o_idx * 0.125
            draw.text((x_img + 6, y_row1 + thumb_h + 6), f"Orig F{o_idx:02d} ({time_sec:.3f}s)", fill=(240, 200, 120), font=font_label)

    # Row 2: Interpolated 24 FPS frames
    y_row2 = header_h + thumb_h + 50
    draw.rectangle([(20, y_row2), (title_w - 10, y_row2 + thumb_h)], fill=(24, 38, 42), outline=(50, 80, 90))
    draw.text((30, y_row2 + 20), "INTERPOLATED", fill=(100, 220, 240), font=font_title)
    draw.text((30, y_row2 + 55), "Rate: 24.0 FPS (3x)", fill=(220, 220, 220), font=font_sub)
    draw.text((30, y_row2 + 85), "Step: 41.7ms / frame", fill=(180, 180, 180), font=font_label)
    draw.text((30, y_row2 + 120), "Motion: Bidirectional MCI", fill=(120, 220, 160), font=font_label)

    for i_idx in range(min(cols, len(interp_frames))):
        x_img = title_w + i_idx * (thumb_w + 12)
        f_path = interp_frames[i_idx]
        with Image.open(f_path) as im:
            thumb = im.convert("RGB").resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
            sheet.paste(thumb, (x_img, y_row2))

        # Color outline: gold for keyframes, cyan for newly synthesized in-betweens!
        is_synth = (i_idx % 3 != 0)
        outline_color = (60, 200, 240) if is_synth else (220, 180, 80)
        draw.rectangle([(x_img, y_row2), (x_img + thumb_w, y_row2 + thumb_h)], outline=outline_color, width=2)
        t_sec = i_idx * (1.0 / 24.0)
        label = f"{'Synth' if is_synth else 'Key'} F{i_idx:02d} ({t_sec:.3f}s)"
        draw.text((x_img + 6, y_row2 + thumb_h + 6), label, fill=outline_color, font=font_label)

    sheet.save(out_sheet_path, "PNG", optimize=True)
    print(f"Contact sheet saved to: {out_sheet_path}")


def main() -> None:
    print("=" * 70)
    print("STARTING VFI (FRAME INTERPOLATION) REAL EXPERIMENT (8 FPS -> 24 FPS)")
    print("=" * 70)

    results: list[dict] = []

    for item in TEST_CLIPS:
        c_id = item["id"]
        c_name = item["name"]
        in_path = item["input_path"]
        print(f"\n--- Testing {c_name} ---")
        print(f"Input Video: {in_path} ({in_path.stat().st_size} bytes)")

        # 1. Extract original frames
        orig_dir = OUTPUT_DIR / f"{c_id}_orig_frames"
        orig_frames = extract_frames(in_path, orig_dir)
        print(f"Extracted {len(orig_frames)} original frames (8 FPS).")

        # 2. Run 3x interpolation to 24 FPS
        out_mp4 = OUTPUT_DIR / f"{c_id}_24fps.mp4"
        print("Running motion interpolation (8 FPS -> 24 FPS)...")
        elapsed, metrics = interpolate_clip_8to24(in_path, out_mp4)
        print(f"Completed in {elapsed:.3f}s! ({metrics['nb_frames']} frames, {metrics['duration']:.2f}s, DecodeOK={metrics['decode_ok']})")

        # 3. Extract interpolated frames
        interp_dir = OUTPUT_DIR / f"{c_id}_interp_frames"
        interp_frames = extract_frames(out_mp4, interp_dir)
        print(f"Extracted {len(interp_frames)} interpolated frames (24 FPS).")

        # 4. Generate A/B comparative contact sheet
        sheet_path = OUTPUT_DIR / f"contact_sheet_vfi_{c_id}.png"
        build_vfi_contact_sheet(c_id, c_name, orig_frames, interp_frames, sheet_path)

        results.append({
            "clip_id": c_id,
            "name": c_name,
            "focus_feature": item["focus_feature"],
            "input_file": str(in_path.resolve()),
            "output_file": str(out_mp4.resolve()),
            "original_frames": len(orig_frames),
            "interpolated_frames": len(interp_frames),
            "contact_sheet": str(sheet_path.resolve()),
            "metrics": metrics,
        })

    report_path = OUTPUT_DIR / "vfi_benchmark_report.json"
    report_path.write_text(json.dumps({"status": "SUCCESS", "benchmarks": results}, indent=2), encoding="utf-8")
    print(f"\nVFI Benchmark Report saved to: {report_path}")
    print("=" * 70)


if __name__ == "__main__":
    main()
