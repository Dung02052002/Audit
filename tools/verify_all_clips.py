"""Verify all baseline and improved clips with ffprobe and full ffmpeg decode."""

import json
import subprocess
from pathlib import Path

clips = [
    Path("artifacts/v2_level3_story5scenes/clips/scene5_lake_walk.mp4"),
    Path("artifacts/scene5_improvement/clips/scene5_lake_walk_improved.mp4"),
    Path("artifacts/scene5_improvement/clips/scene5_lake_walk_v2_fullbody.mp4"),
    Path("artifacts/scene5_improvement/clips/scene5_lake_walk_v3_minimal.mp4"),
]

for c in clips:
    if not c.exists():
        print(f"MISSING: {c}")
        continue
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-print_format", "json", str(c)],
        capture_output=True,
        text=True,
        check=True,
    )
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(c), "-f", "null", "-"], capture_output=True, text=True)
    data = json.loads(probe.stdout)
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    w = v["width"]
    h = v["height"]
    fps = v["r_frame_rate"]
    nb_frames = v.get("nb_frames", "N/A")
    duration = float(v.get("duration", 0.0))
    errs = dec.stderr.strip()
    print(f"{c.name}: {w}x{h} | fps={fps} | frames={nb_frames} | duration={duration:.3f}s | size={c.stat().st_size} bytes | decode_ok={len(errs) == 0}")
