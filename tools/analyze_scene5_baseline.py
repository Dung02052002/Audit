"""Script to analyze all 16 frames of baseline scene5_lake_walk and generate contact sheet."""

import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import numpy as np

PROJECT_ROOT = Path("D:/Project_Audit")
FRAMES_DIR = PROJECT_ROOT / "artifacts" / "v2_level3_story5scenes" / "clips" / "scene5_lake_walk_frames"
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "scene5_improvement"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def analyze_frames():
    frame_files = sorted(list(FRAMES_DIR.glob("frame_*.png")))
    print(f"Found {len(frame_files)} frames in {FRAMES_DIR}")
    assert len(frame_files) == 16, f"Expected 16 frames, got {len(frame_files)}"

    # Load all frames
    frames = [Image.open(f).convert("RGB") for f in frame_files]
    
    # Calculate inter-frame metrics
    metrics = []
    for i in range(len(frames)):
        curr_arr = np.array(frames[i], dtype=np.float32)
        if i > 0:
            prev_arr = np.array(frames[i - 1], dtype=np.float32)
            mse = float(np.mean((curr_arr - prev_arr) ** 2))
            mae = float(np.mean(np.abs(curr_arr - prev_arr)))
            # Max pixel shift / delta
            max_delta = float(np.max(np.abs(curr_arr - prev_arr)))
        else:
            mse = 0.0
            mae = 0.0
            max_delta = 0.0
        
        # Color distribution (mean R, G, B)
        mean_rgb = [float(np.mean(curr_arr[:, :, c])) for c in range(3)]
        
        metrics.append({
            "frame_idx": i,
            "filename": frame_files[i].name,
            "timestamp_s": i * 0.125,
            "mse_to_prev": mse,
            "mae_to_prev": mae,
            "max_delta": max_delta,
            "mean_rgb": mean_rgb,
        })
        print(f"Frame {i:02d} ({i*0.125:.3f}s): MSE={mse:.2f}, MAE={mae:.2f}, MeanRGB=({mean_rgb[0]:.1f},{mean_rgb[1]:.1f},{mean_rgb[2]:.1f})")

    # Generate 4x4 Contact Sheet
    cell_w, cell_h = 512, 512
    margin = 8
    header_h = 40
    sheet_w = 4 * cell_w + 5 * margin
    sheet_h = 4 * (cell_h + header_h) + 5 * margin

    sheet = Image.new("RGB", (sheet_w, sheet_h), color=(24, 24, 28))
    draw = ImageDraw.Draw(sheet)
    
    try:
        font = ImageFont.truetype("arial.ttf", 22)
        subfont = ImageFont.truetype("arial.ttf", 16)
    except Exception:
        font = ImageFont.load_default()
        subfont = ImageFont.load_default()

    for idx, (frame, m) in enumerate(zip(frames, metrics)):
        row = idx // 4
        col = idx % 4
        x = margin + col * (cell_w + margin)
        y = margin + row * (cell_h + header_h + margin)

        # Header box
        header_rect = [x, y, x + cell_w, y + header_h]
        draw.rectangle(header_rect, fill=(40, 44, 52))
        
        # Title text
        txt = f"Frame {idx:02d} | t={m['timestamp_s']:.3f}s"
        if idx > 0:
            txt += f" | MAE={m['mae_to_prev']:.1f}"
        draw.text((x + 12, y + 8), txt, fill=(240, 240, 245), font=font)

        # Paste frame
        sheet.paste(frame, (x, y + header_h))

    contact_sheet_path = OUTPUT_DIR / "baseline_16frames_sheet.png"
    sheet.save(contact_sheet_path, "PNG")
    print(f"Saved contact sheet to {contact_sheet_path}")

    # Also save metrics JSON
    metrics_path = OUTPUT_DIR / "baseline_metrics.json"
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    print(f"Saved metrics to {metrics_path}")

if __name__ == "__main__":
    analyze_frames()
