"""End-to-end verification of ComfyUIAdapter (Step 6) + Demo Video Generation (Step 7).

Resumable: reuses verified real ComfyUI images + metadata already generated in OUT_ROOT,
synthesizes Vietnamese narration/dialogue via EdgeTTSAdapter, and renders the final MP4.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.providers.image_generation import ImageGenerationRequest
from ai_youtube_agent.providers.media_probe import probe_image_bytes, probe_mp3_bytes
from ai_youtube_agent.providers.registry.comfyui_adapter import (
    ComfyUIAdapter,
    derive_seed,
)
from ai_youtube_agent.providers.registry.tts_adapters import EdgeTTSAdapter
from ai_youtube_agent.providers.speech_synthesis import (
    SpeechRequest,
    SpeechSynthesisError,
)

LOW_PRIORITY = 0x00004000 if sys.platform == "win32" else 0
CHECKPOINT = "v1-5-pruned-emaonly.safetensors"
COMFY_DIR = Path("D:/ComfyUI")
COMFY_PYTHON = COMFY_DIR / ".venv" / "Scripts" / "python.exe"
COMFY_URL = "http://127.0.0.1:8188"

TIMESTAMP = os.environ.get("DEMO_RUN_ID", "20261023-001304")
OUT_ROOT = Path("artifacts/comfyui_demo_run") / TIMESTAMP
STORAGE_DIR = OUT_ROOT / "artifact_storage"
IMAGES_DIR = OUT_ROOT / "images"
AUDIO_DIR = OUT_ROOT / "audio"
CLIPS_DIR = OUT_ROOT / "clips"
LOGS_DIR = OUT_ROOT / "logs"

W, H, FPS = 1280, 720, 24
GEN_W, GEN_H = 896, 640
SR = 24000
GAP_S, SCENE_PAD_S, FADE_S = 0.35, 0.65, 0.35

LAN_DESC = (
    "a 20-year-old Vietnamese woman, shoulder-length black hair in a low ponytail, "
    "friendly face, white t-shirt, dark green apron"
)
TUAN_DESC = (
    "a 35-year-old Vietnamese man, short black hair, warm smile, grey polo shirt, "
    "dark green apron"
)
PHO_SHOP = (
    "traditional Vietnamese pho noodle shop on a Hanoi street corner, stainless steel "
    "tables, steaming broth pots, warm lanterns"
)
PHOTO_STYLE = (
    "cinematic film still, realistic photography, 35mm lens, natural lighting, "
    "shallow depth of field, high detail"
)

CHARACTERS = {
    "lan": {
        "name": "Lan",
        "seed_key": "4101",
        "prompt": f"portrait of {LAN_DESC}, neutral warm background, {PHOTO_STYLE}",
    },
    "tuan": {
        "name": "Anh Tuấn",
        "seed_key": "4202",
        "prompt": f"portrait of {TUAN_DESC}, neutral warm background, {PHOTO_STYLE}",
    },
}

SCENES = [
    {
        "id": "scene_01",
        "seed_key": "5101",
        "title": "Bình minh trước quán phở",
        "motion": "zoom_in",
        "prompt": (
            f"{LAN_DESC} standing in front of a {PHO_SHOP} at dawn, soft morning "
            f"mist, wide cinematic shot, {PHOTO_STYLE}"
        ),
        "lines": [
            (
                "narrator",
                "vi-VN-NamMinhNeural",
                "Sáu giờ sáng trên góc phố Hà Nội, hơi nước từ nồi phở đầu ngày đã "
                "tỏa hương ấm áp. Hôm nay là buổi làm thêm đầu tiên của Lan.",
            ),
        ],
    },
    {
        "id": "scene_02",
        "seed_key": "5202",
        "title": "Chiếc tạp dề đầu tiên",
        "motion": "pan_right",
        "prompt": (
            f"{TUAN_DESC} handing a dark green apron to {LAN_DESC} inside a "
            f"{PHO_SHOP}, warm morning sunlight, medium shot, {PHOTO_STYLE}"
        ),
        "lines": [
            (
                "tuan",
                "vi-VN-NamMinhNeural",
                "Em là Lan phải không? Đeo tạp dề vào nhé, khách quen buổi sáng sắp "
                "ghé rồi đấy.",
            ),
            (
                "lan",
                "vi-VN-HoaiMyNeural",
                "Dạ, em chào anh Tuấn. Em đã sẵn sàng rồi ạ!",
            ),
        ],
    },
    {
        "id": "scene_03",
        "seed_key": "5303",
        "title": "Giờ cao điểm bận rộn",
        "motion": "zoom_out",
        "prompt": (
            f"busy morning rush inside a {PHO_SHOP}, {LAN_DESC} carrying a tray of "
            f"steaming pho bowls, lively atmosphere, medium wide shot, {PHOTO_STYLE}"
        ),
        "lines": [
            (
                "narrator",
                "vi-VN-NamMinhNeural",
                "Giờ cao điểm ập đến rất nhanh. Tiếng gọi món rộn ràng khiến cô "
                "sinh viên trẻ thoáng chút bối rối.",
            ),
            (
                "lan",
                "vi-VN-HoaiMyNeural",
                "Dạ phở tái nạm bàn số ba có ngay đây ạ!",
            ),
        ],
    },
    {
        "id": "scene_04",
        "seed_key": "5404",
        "title": "Lời động viên ấm áp",
        "motion": "pan_left",
        "prompt": (
            f"{TUAN_DESC} smiling kindly and encouraging {LAN_DESC} next to a clean "
            f"table in a {PHO_SHOP}, warm reassuring light, medium close-up, "
            f"{PHOTO_STYLE}"
        ),
        "lines": [
            (
                "tuan",
                "vi-VN-NamMinhNeural",
                "Làm tốt lắm. Ngày đầu ai cũng bỡ ngỡ, cứ bình tĩnh và giữ nụ cười "
                "với khách là được.",
            ),
        ],
    },
    {
        "id": "scene_05",
        "seed_key": "5505",
        "title": "Nụ cười cuối buổi sáng",
        "motion": "zoom_in",
        "prompt": (
            f"{LAN_DESC} smiling happily in a sunlit {PHO_SHOP} after the morning "
            f"rush, golden warm light, cinematic wide shot, {PHOTO_STYLE}"
        ),
        "lines": [
            (
                "narrator",
                "vi-VN-NamMinhNeural",
                "Buổi sáng khép lại trong nắng ấm. Lan nhận ra sự tận tâm chính là "
                "gia vị đặc biệt nhất của quán nhỏ.",
            ),
            (
                "lan",
                "vi-VN-HoaiMyNeural",
                "Sáng mai em lại đến sớm phụ anh nữa nhé!",
            ),
        ],
    },
]


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOGS_DIR / "demo.log", "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def uri_to_path(uri: str) -> Path:
    parts = urlsplit(uri)
    return Path(url2pathname(parts.path)).resolve()


def run_cmd(cmd: list[str], log_name: str, cwd: Path | None = None) -> None:
    with open(LOGS_DIR / f"{log_name}.log", "a", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(cmd) + "\n")
        handle.flush()
        rc = subprocess.run(
            cmd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            creationflags=LOW_PRIORITY,
            check=False,
        ).returncode
    if rc != 0:
        raise RuntimeError(f"{log_name} failed with exit code {rc}")


def ffprobe_json(path: Path) -> dict:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        creationflags=LOW_PRIORITY,
        check=True,
    )
    return json.loads(proc.stdout)


def find_existing_meta(prefix: str) -> dict | None:
    matches = sorted(IMAGES_DIR.glob(f"{prefix}_*.meta.json"))
    if not matches:
        return None
    meta = json.loads(matches[-1].read_text(encoding="utf-8"))
    return meta


def start_comfyui_server() -> subprocess.Popen | None:
    probe_adapter = ComfyUIAdapter(
        base_url=COMFY_URL, default_model=CHECKPOINT, timeout_seconds=2.0
    )
    if probe_adapter.check_connection().connected:
        log("ComfyUI server is already running on 127.0.0.1:8188")
        return None

    log("Starting ComfyUI server on 127.0.0.1:8188 (--lowvram --fp16-unet --fp32-vae)...")
    server_log = open(LOGS_DIR / "comfyui_server.log", "a", encoding="utf-8")
    proc = subprocess.Popen(
        [
            str(COMFY_PYTHON),
            "main.py",
            "--listen",
            "127.0.0.1",
            "--port",
            "8188",
            "--lowvram",
            "--fp16-unet",
            "--fp32-vae",
            "--disable-auto-launch",
        ],
        cwd=str(COMFY_DIR),
        stdout=server_log,
        stderr=subprocess.STDOUT,
        creationflags=LOW_PRIORITY,
    )
    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"ComfyUI exited early with code {proc.returncode}; see logs/comfyui_server.log"
            )
        conn = probe_adapter.check_connection()
        if conn.connected:
            log(f"ComfyUI server ready (latency={conn.latency_ms}ms)")
            return proc
        time.sleep(2.0)
    proc.terminate()
    raise RuntimeError("Timed out waiting for ComfyUI server to start")


def synthesize_ambient_music(path: Path, duration_s: float) -> None:
    total_samples = int(SR * duration_s)
    buf = bytearray()
    freqs = (220.0, 277.18, 329.63, 440.0)
    for idx in range(total_samples):
        t = idx / SR
        env = 1.0
        if t < 1.5:
            env = t / 1.5
        elif t > duration_s - 2.0:
            env = max(0.0, (duration_s - t) / 2.0)
        val = sum(
            math.sin(2.0 * math.pi * f * t) * (0.22 / len(freqs)) for f in freqs
        )
        sample = max(-32767, min(32767, int(val * env * 32767)))
        buf += struct.pack("<h", sample)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SR)
        writer.writeframes(bytes(buf))


def format_srt_timestamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    return (
        f"{ms // 3_600_000:02d}:{(ms // 60_000) % 60:02d}:"
        f"{(ms // 1000) % 60:02d},{ms % 1000:03d}"
    )


def render_scene_clip(
    image_path: Path, clip_path: Path, frames: int, motion: str
) -> None:
    if motion == "zoom_in":
        z_expr = "min(zoom+0.0008,1.18)"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    elif motion == "zoom_out":
        z_expr = "if(eq(on,1),1.18,max(1.0,zoom-0.0008))"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    elif motion == "pan_right":
        z_expr = "1.14"
        x_expr = f"(iw-iw/zoom)*on/{max(1, frames)}"
        y_expr = "ih/2-(ih/zoom/2)"
    else:
        z_expr = "1.14"
        x_expr = f"(iw-iw/zoom)*(1-on/{max(1, frames)})"
        y_expr = "ih/2-(ih/zoom/2)"

    vf = (
        f"scale=2560:1440:flags=lanczos,"
        f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d={frames}:s={W}x{H}:fps={FPS},"
        f"format=yuv420p"
    )
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-threads",
            "2",
            "-loop",
            "1",
            "-i",
            str(image_path),
            "-vf",
            vf,
            "-frames:v",
            str(frames),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(clip_path),
        ],
        "render_clips",
    )


def main() -> int:
    for d in (OUT_ROOT, STORAGE_DIR, IMAGES_DIR, AUDIO_DIR, CLIPS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)

    server_proc: subprocess.Popen | None = None
    try:
        storage = LocalFileStorage(STORAGE_DIR)
        comfy = ComfyUIAdapter(
            base_url=COMFY_URL,
            default_model=CHECKPOINT,
            timeout_seconds=10.0,
            poll_interval_seconds=0.5,
            job_timeout_seconds=240.0,
            asset_output_dir=IMAGES_DIR,
        )

        # Check if all 8 ComfyUI images already exist from the first run
        step6_stored_path = STORAGE_DIR / "comfyui_step6_verify" / "thumbnail" / "v1.png"
        step6_meta = find_existing_meta("step6_smoke_shot_01")
        if step6_stored_path.exists() and step6_meta is not None:
            step6_probe = probe_image_bytes(step6_stored_path.read_bytes())
            step6_info = {
                "prompt_id": step6_meta["prompt_id"],
                "seed": step6_meta["seed"],
                "checkpoint": step6_meta["checkpoint"],
                "stored_uri": step6_stored_path.resolve().as_uri(),
                "stored_path": str(step6_stored_path.resolve()),
                "size_bytes": step6_stored_path.stat().st_size,
                "width": step6_probe.width,
                "height": step6_probe.height,
                "sha256": step6_meta["sha256"],
                "provider": step6_meta["provider"],
                "fallback_enabled": False,
            }
            log(f"Step 6 reused verified ComfyUI image: {step6_stored_path}")
        else:
            server_proc = start_comfyui_server()
            step6_req = ImageGenerationRequest(
                scene_id="step6_smoke",
                shot_id="shot_01",
                prompt=f"a steaming bowl of Vietnamese beef pho on a wooden table, morning light, {PHOTO_STYLE}",
                width=640,
                height=640,
                style="cinematic_realism",
                seed_key="6001",
            )
            step6_img, step6_job, step6_stored = comfy.generate_and_store(
                step6_req,
                storage,
                content_item_id="comfyui_step6_verify",
                version=1,
                kind=ArtifactKind.THUMBNAIL,
            )
            step6_disk_path = uri_to_path(step6_stored.uri)
            step6_probe = probe_image_bytes(step6_disk_path.read_bytes())
            step6_info = {
                "prompt_id": step6_job.prompt_id,
                "seed": step6_job.seed,
                "checkpoint": step6_job.checkpoint,
                "stored_uri": step6_stored.uri,
                "stored_path": str(step6_disk_path),
                "size_bytes": step6_stored.size_bytes,
                "width": step6_probe.width,
                "height": step6_probe.height,
                "sha256": step6_img.sha256,
                "provider": step6_img.provider,
                "fallback_enabled": comfy.fallback_enabled,
            }

        image_records: dict[str, dict] = {}
        version_counter = 1

        for char_id, char_info in CHARACTERS.items():
            friendly = IMAGES_DIR / f"ref_{char_id}.png"
            meta = find_existing_meta(f"ref_{char_id}_portrait")
            art_path = STORAGE_DIR / "demo_pho_som_assets" / "thumbnail" / f"v{version_counter}.png"
            if friendly.exists() and meta is not None and art_path.exists():
                probe_image_bytes(friendly.read_bytes())
                image_records[f"ref_{char_id}"] = {
                    "prompt_id": meta["prompt_id"],
                    "seed": meta["seed"],
                    "provider": meta["provider"],
                    "model": meta["checkpoint"],
                    "width": meta["width"],
                    "height": meta["height"],
                    "sha256": meta["sha256"],
                    "size_bytes": meta["size_bytes"],
                    "artifact_uri": art_path.resolve().as_uri(),
                    "image_file": str(friendly.resolve()),
                }
                version_counter += 1
                log(f"Reused verified ComfyUI character ref_{char_id}: prompt_id={meta['prompt_id']}")
                continue

            if server_proc is None:
                server_proc = start_comfyui_server()
            req = ImageGenerationRequest(
                scene_id=f"ref_{char_id}",
                shot_id="portrait",
                prompt=char_info["prompt"],
                width=640,
                height=640,
                style="character_reference",
                seed_key=char_info["seed_key"],
            )
            img, job, stored = comfy.generate_and_store(
                req,
                storage,
                content_item_id="demo_pho_som_assets",
                version=version_counter,
                kind=ArtifactKind.THUMBNAIL,
            )
            version_counter += 1
            friendly.write_bytes(img.data)
            image_records[f"ref_{char_id}"] = {
                "prompt_id": job.prompt_id,
                "seed": job.seed,
                "provider": img.provider,
                "model": img.model_name,
                "width": img.width,
                "height": img.height,
                "sha256": img.sha256,
                "size_bytes": len(img.data),
                "artifact_uri": stored.uri,
                "image_file": str(friendly.resolve()),
            }

        for scene in SCENES:
            friendly = IMAGES_DIR / f"{scene['id']}.png"
            meta = find_existing_meta(f"{scene['id']}_main")
            art_path = STORAGE_DIR / "demo_pho_som_assets" / "thumbnail" / f"v{version_counter}.png"
            if friendly.exists() and meta is not None and art_path.exists():
                probe_image_bytes(friendly.read_bytes())
                image_records[scene["id"]] = {
                    "prompt_id": meta["prompt_id"],
                    "seed": meta["seed"],
                    "provider": meta["provider"],
                    "model": meta["checkpoint"],
                    "width": meta["width"],
                    "height": meta["height"],
                    "sha256": meta["sha256"],
                    "size_bytes": meta["size_bytes"],
                    "artifact_uri": art_path.resolve().as_uri(),
                    "image_file": str(friendly.resolve()),
                }
                version_counter += 1
                log(f"Reused verified ComfyUI {scene['id']}: prompt_id={meta['prompt_id']}")
                continue

            if server_proc is None:
                server_proc = start_comfyui_server()
            req = ImageGenerationRequest(
                scene_id=scene["id"],
                shot_id="main",
                prompt=scene["prompt"],
                width=GEN_W,
                height=GEN_H,
                style="cinematic_story",
                seed_key=scene["seed_key"],
            )
            img, job, stored = comfy.generate_and_store(
                req,
                storage,
                content_item_id="demo_pho_som_assets",
                version=version_counter,
                kind=ArtifactKind.THUMBNAIL,
            )
            version_counter += 1
            friendly.write_bytes(img.data)
            image_records[scene["id"]] = {
                "prompt_id": job.prompt_id,
                "seed": job.seed,
                "provider": img.provider,
                "model": img.model_name,
                "width": img.width,
                "height": img.height,
                "sha256": img.sha256,
                "size_bytes": len(img.data),
                "artifact_uri": stored.uri,
                "image_file": str(friendly.resolve()),
            }

        # ------------------------------------------------------------------
        # STEP 7.2: Vietnamese Narration & Dialogue via EdgeTTSAdapter
        # ------------------------------------------------------------------
        edge_mp3 = EdgeTTSAdapter(default_voice="vi-VN-HoaiMyNeural")
        audio_segments: list[dict] = []

        for scene in SCENES:
            for idx, (speaker, voice_id, text) in enumerate(scene["lines"]):
                stem = f"{scene['id']}_{idx}_{speaker}"
                mp3_path = (AUDIO_DIR / f"{stem}.mp3").resolve()
                wav_path = (AUDIO_DIR / f"{stem}.wav").resolve()
                req = SpeechRequest(text=text, voice_id=voice_id, language="vi-VN")

                if mp3_path.exists() and mp3_path.stat().st_size > 500:
                    mp3_bytes = mp3_path.read_bytes()
                    mp3_probe = probe_mp3_bytes(mp3_bytes)
                else:
                    last_err: Exception | None = None
                    for attempt in range(1, 5):
                        try:
                            mp3_speech, mp3_path = edge_mp3.synthesize_to_file(
                                req, mp3_path
                            )
                            mp3_bytes = mp3_speech.audio
                            mp3_probe = probe_mp3_bytes(mp3_bytes)
                            assert mp3_speech.provider == "edge_tts"
                            break
                        except SpeechSynthesisError as exc:
                            last_err = exc
                            log(f"EdgeTTS {stem} attempt {attempt} failed: {exc}; waiting {2 * attempt}s...")
                            time.sleep(2.0 * attempt)
                    else:
                        raise RuntimeError(f"EdgeTTS failed for {stem}: {last_err}")
                    time.sleep(1.2)

                wav_bytes, wav_duration_ms = edge_mp3._transcode_mp3_to_wav(mp3_bytes)
                wav_path.write_bytes(wav_bytes)

                audio_segments.append(
                    {
                        "scene_id": scene["id"],
                        "speaker": speaker,
                        "voice_id": voice_id,
                        "text": text,
                        "mp3_file": str(mp3_path),
                        "wav_file": str(wav_path),
                        "mp3_bytes": len(mp3_bytes),
                        "wav_bytes": len(wav_bytes),
                        "duration_ms": wav_duration_ms,
                        "mp3_frames": mp3_probe.frames,
                    }
                )
                log(
                    f"EdgeTTS {stem}: voice={voice_id}, duration_ms={wav_duration_ms}, "
                    f"mp3_bytes={len(mp3_bytes)}"
                )

        # ------------------------------------------------------------------
        # STEP 7.3: Timeline, Subtitles (.srt), and Voice Track Assembly
        # ------------------------------------------------------------------
        pcm_all = bytearray()
        cursor_s = 0.0
        srt_entries: list[tuple[float, float, str]] = []
        scene_timings: list[dict] = []

        for scene in SCENES:
            sc_start = cursor_s
            pcm_all += b"\x00\x00" * int(SR * FADE_S)
            cursor_s += FADE_S
            sc_segs = [s for s in audio_segments if s["scene_id"] == scene["id"]]
            for seg in sc_segs:
                with wave.open(seg["wav_file"], "rb") as reader:
                    frames_bytes = reader.readframes(reader.getnframes())
                seg_dur = len(frames_bytes) / 2 / SR
                seg_start = cursor_s
                seg_end = cursor_s + seg_dur
                pcm_all += frames_bytes
                cursor_s = seg_end

                speaker_label = (
                    "Người dẫn"
                    if seg["speaker"] == "narrator"
                    else CHARACTERS[seg["speaker"]]["name"]
                )
                caption_line = (
                    seg["text"]
                    if seg["speaker"] == "narrator"
                    else f"{speaker_label}: {seg['text']}"
                )
                srt_entries.append((seg_start, seg_end, caption_line))
                pcm_all += b"\x00\x00" * int(SR * GAP_S)
                cursor_s += GAP_S

            pcm_all += b"\x00\x00" * int(SR * SCENE_PAD_S)
            cursor_s += SCENE_PAD_S
            n_frames = round((cursor_s - sc_start) * FPS)
            aligned_end = sc_start + (n_frames / FPS)
            pad_samples = int(round((aligned_end - cursor_s) * SR))
            if pad_samples > 0:
                pcm_all += b"\x00\x00" * pad_samples
            cursor_s = aligned_end
            scene_timings.append(
                {
                    "scene_id": scene["id"],
                    "title": scene["title"],
                    "motion": scene["motion"],
                    "start_s": round(sc_start, 3),
                    "end_s": round(cursor_s, 3),
                    "duration_s": round(cursor_s - sc_start, 3),
                    "frames": n_frames,
                }
            )

        voice_track_path = AUDIO_DIR / "voice_track.wav"
        with wave.open(str(voice_track_path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(SR)
            writer.writeframes(bytes(pcm_all))

        music_path = AUDIO_DIR / "ambient_music.wav"
        synthesize_ambient_music(music_path, cursor_s)

        srt_path = OUT_ROOT / "subtitles.vi.srt"
        with open(srt_path, "w", encoding="utf-8") as handle:
            for idx, (st, et, line) in enumerate(srt_entries, 1):
                handle.write(
                    f"{idx}\n{format_srt_timestamp(st)} --> {format_srt_timestamp(et)}\n{line}\n\n"
                )

        # ------------------------------------------------------------------
        # STEP 7.4: Render Ken Burns Scene Clips & Final Muxed MP4 with Subtitles
        # ------------------------------------------------------------------
        concat_lines: list[str] = []
        for timing in scene_timings:
            sc_id = timing["scene_id"]
            img_path = IMAGES_DIR / f"{sc_id}.png"
            clip_path = CLIPS_DIR / f"{sc_id}.mp4"
            render_scene_clip(img_path, clip_path, timing["frames"], timing["motion"])
            concat_lines.append(f"file '{clip_path.resolve().as_posix()}'")
            log(f"Rendered clip {sc_id}: {timing['frames']} frames ({timing['duration_s']}s)")

        concat_file = CLIPS_DIR / "concat.txt"
        concat_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

        final_mp4 = OUT_ROOT / "demo_pho_som_comfyui.mp4"
        srt_escaped = srt_path.resolve().as_posix().replace(":", r"\:")
        sub_filter = (
            f"subtitles='{srt_escaped}':charenc=UTF-8:"
            "force_style='FontName=Arial,FontSize=18,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00101010,BackColour=&H66000000,BorderStyle=3,Outline=1.5,"
            "Shadow=0,MarginV=24'"
        )
        run_cmd(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-threads",
                "2",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-i",
                str(voice_track_path),
                "-i",
                str(music_path),
                "-filter_complex",
                f"[0:v]{sub_filter}[v];[1:a]volume=1.0[va];[2:a]volume=0.10[ma];"
                "[va][ma]amix=inputs=2:duration=first:dropout_transition=0[a]",
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-movflags",
                "+faststart",
                str(final_mp4),
            ],
            "final_mux",
        )

        # Full decode verification + ffprobe inspection
        run_cmd(
            ["ffmpeg", "-v", "error", "-threads", "2", "-i", str(final_mp4), "-f", "null", "-"],
            "verify_decode",
        )
        probe_info = ffprobe_json(final_mp4)
        video_sha256 = hashlib.sha256(final_mp4.read_bytes()).hexdigest()

        manifest = {
            "timestamp": TIMESTAMP,
            "output_dir": str(OUT_ROOT.resolve()),
            "step6_verification": step6_info,
            "step7_demo_video": {
                "video_path": str(final_mp4.resolve()),
                "video_size_bytes": final_mp4.stat().st_size,
                "video_sha256": video_sha256,
                "subtitles_path": str(srt_path.resolve()),
                "voice_track_path": str(voice_track_path.resolve()),
                "total_duration_seconds": round(cursor_s, 3),
                "ffprobe": probe_info,
                "images": image_records,
                "audio_segments": audio_segments,
                "scene_timings": scene_timings,
            },
        }
        manifest_path = OUT_ROOT / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log(
            f"SUCCESS! Final MP4: {final_mp4.resolve()} "
            f"({final_mp4.stat().st_size} bytes, duration={cursor_s:.2f}s)"
        )
        return 0
    finally:
        if server_proc is not None and server_proc.poll() is None:
            log("Stopping managed ComfyUI server process to free GPU VRAM...")
            server_proc.terminate()
            try:
                server_proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server_proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
