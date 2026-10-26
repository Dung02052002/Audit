"""Forensic analysis script for film_mai_hanoi_9x16_vertical.mp4.

Extracts frames at critical timeline points (including 12s, 41s, 47s),
generates a comprehensive contact sheet with timecode labels,
and maps every section to its origin clip, prompt, and workflow.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path("D:/Project_Audit")
VIDEO_PATH = PROJECT_ROOT / "artifacts" / "v2_level4_longform60s" / "film_mai_hanoi_9x16_vertical.mp4"
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "root_cause_analysis"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
FRAMES_DIR = OUTPUT_DIR / "timeline_inspection_frames"
FRAMES_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG_BIN = "ffmpeg"

# Timestamps to sample across the 65.0s video:
# Scene 1: 0 - 8s (establishing) -> sample at 2s, 5s
# Scene 2: 8 - 16s (portrait) -> sample at 9s, 12s, 14s (TARGET: 12s!)
# Scene 3: 16 - 24s (tea) -> sample at 18s, 21s
# Scene 4: 24 - 32s (macro tea) -> sample at 26s, 29s
# Scene 5: 32 - 40s (walk) -> sample at 34s, 38s
# Scene 6: 40 - 48s (portrait repeat) -> sample at 41s, 44s, 47s (TARGET: 41s, 47s!)
# Scene 7: 48 - 56s (walk repeat) -> sample at 50s, 53s
# Scene 8: 56 - 65s (tea repeat) -> sample at 58s, 62s
TIMESTAMPS = [
    2.0, 5.0,
    9.0, 12.0, 14.0,
    18.0, 21.0,
    26.0, 29.0,
    34.0, 38.0,
    41.0, 44.0, 47.0,
    50.0, 53.0,
    58.0, 62.0,
]

def extract_frames():
    extracted_paths = []
    for t in TIMESTAMPS:
        out_f = FRAMES_DIR / f"frame_t{t:04.1f}s.png"
        cmd = [
            FFMPEG_BIN, "-y",
            "-ss", str(t),
            "-i", str(VIDEO_PATH),
            "-vframes", "1",
            "-q:v", "2",
            str(out_f)
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        extracted_paths.append((t, out_f))
        print(f"Extracted frame at t={t:.1f}s -> {out_f.name}")
    return extracted_paths

def build_contact_sheet(extracted_paths):
    # Vertical frames are 1080x1920 (aspect ratio 9:16)
    # Thumbnail size: 216 x 384
    thumb_w, thumb_h = 216, 384
    header_h = 36
    margin = 8
    cols = 6
    rows = 3  # 18 frames = 6 cols x 3 rows
    banner_h = 70

    sheet_w = cols * thumb_w + (cols + 1) * margin
    sheet_h = banner_h + rows * (thumb_h + header_h + margin) + margin

    sheet = Image.new("RGB", (sheet_w, sheet_h), color=(18, 20, 24))
    draw = ImageDraw.Draw(sheet)

    try:
        title_font = ImageFont.truetype("arial.ttf", 24)
        sub_font = ImageFont.truetype("arial.ttf", 14)
        label_font = ImageFont.truetype("arial.ttf", 13)
    except Exception:
        title_font = ImageFont.load_default()
        sub_font = ImageFont.load_default()
        label_font = ImageFont.load_default()

    # Banner
    draw.rectangle([0, 0, sheet_w, banner_h], fill=(30, 34, 44))
    draw.text((16, 12), "TIMELINE FORENSIC INSPECTION: film_mai_hanoi_9x16_vertical.mp4", fill=(255, 215, 0), font=title_font)
    draw.text((16, 42), "Full 65s Timeline Audit | Key Focus: t=12s (Scene 2), t=41s & 47s (Scene 6) Extreme Close-ups & Framing", fill=(200, 210, 225), font=sub_font)

    for idx, (t, p) in enumerate(extracted_paths):
        r = idx // cols
        c = idx % cols
        x = margin + c * (thumb_w + margin)
        y = banner_h + margin + r * (thumb_h + header_h + margin)

        # Header box: highlight 12s, 41s, 47s with alert color!
        is_alert = t in (12.0, 41.0, 47.0)
        box_color = (120, 30, 30) if is_alert else (45, 50, 65)
        text_color = (255, 200, 200) if is_alert else (230, 235, 245)

        draw.rectangle([x, y, x + thumb_w, y + header_h], fill=box_color)
        alert_tag = " [ALERT!]" if is_alert else ""
        draw.text((x + 6, y + 8), f"t={t:04.1f}s{alert_tag}", fill=text_color, font=label_font)

        with Image.open(p) as img:
            thumb = img.convert("RGB").resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
            sheet.paste(thumb, (x, y + header_h))

    out_sheet = OUTPUT_DIR / "timeline_forensic_18samples_sheet.png"
    sheet.save(out_sheet, "PNG")
    print(f"Saved forensic contact sheet to {out_sheet}")

def main():
    paths = extract_frames()
    build_contact_sheet(paths)

if __name__ == "__main__":
    main()
