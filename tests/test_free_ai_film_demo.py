"""Free AI film demo tool (tools/free_ai_film_demo.py, BUDGET-01 Goal B).

Development tool, not part of the package. Checks the pure parts: the voice
envelope, the subtitle cues, the step runner, the budget step (with the budget
layer injected, so these tests do not need it), the manifest honesty strings,
the Pillow frames and the ffmpeg encode. No network: the real ComfyUI and Edge
TTS run is manual.
"""

import importlib.util
import json
import math
import shutil
import struct
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "free_ai_film_demo", ROOT / "tools" / "free_ai_film_demo.py"
)
demo = importlib.util.module_from_spec(_spec)
sys.modules["free_ai_film_demo"] = demo
_spec.loader.exec_module(demo)


def write_wav(path: Path, segments: list[tuple[float, float]], rate: int = 24000):
    """A mono 16-bit WAV of (seconds, amplitude 0..1) sine segments at 220 Hz."""
    frames = bytearray()
    index = 0
    for seconds, amplitude in segments:
        for _ in range(round(seconds * rate)):
            value = amplitude * math.sin(2 * math.pi * 220 * index / rate)
            frames += struct.pack("<h", round(value * 32000))
            index += 1
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(bytes(frames))
    return path


# --- envelope and mouth -----------------------------------------------------


def test_envelope_is_quiet_in_silence_and_loud_in_speech(tmp_path):
    wav = write_wav(tmp_path / "v.wav", [(1.0, 0.0), (1.0, 0.5)])

    envelope = demo.rms_envelope(wav, 24)

    assert len(envelope) == 48
    assert all(0.0 <= value <= 1.0 for value in envelope)
    assert max(envelope[:24]) == 0.0
    assert min(envelope[30:]) > 0.9
    # Attack smoothing: the first loud frame has not reached the top yet.
    assert 0.0 < envelope[24] < envelope[26]


def test_envelope_release_closes_slowly(tmp_path):
    wav = write_wav(tmp_path / "v.wav", [(1.0, 0.5), (0.5, 0.0)])

    envelope = demo.rms_envelope(wav, 24)

    assert envelope[24] > 0.5  # the first silent frame is still closing
    assert envelope[24] > envelope[26] > envelope[30]


def test_envelope_of_pure_silence_is_zero(tmp_path):
    wav = write_wav(tmp_path / "v.wav", [(0.5, 0.0)])

    assert demo.rms_envelope(wav, 24) == [0.0] * 12


def test_mouth_openness_is_gated_clamped_and_zero_out_of_range():
    envelope = [0.0, 0.05, 0.5, 1.0]

    assert demo.mouth_openness(envelope, 0) == 0.0
    assert demo.mouth_openness(envelope, 1) == 0.0  # under the noise gate
    assert 0.0 < demo.mouth_openness(envelope, 2) < 1.0
    assert demo.mouth_openness(envelope, 3) == 1.0
    assert demo.mouth_openness(envelope, -1) == 0.0
    assert demo.mouth_openness(envelope, 4) == 0.0


# --- subtitles ----------------------------------------------------------------


def test_subtitle_cues_cover_the_voice_by_sentence_and_characters():
    cues = demo.subtitle_cues(demo.SCRIPT_TEXT, 15000)

    assert [cue.text for cue in cues] == demo.split_sentences(demo.SCRIPT_TEXT)
    assert len(cues) == 5
    assert cues[0].start_ms == 0
    assert cues[-1].end_ms == 15000
    for before, after in zip(cues, cues[1:], strict=False):
        assert before.end_ms == after.start_ms
    durations = {cue.text: cue.end_ms - cue.start_ms for cue in cues}
    longest = max(cues, key=lambda cue: len(cue.text)).text
    assert durations[longest] == max(durations.values())


def test_cue_at_finds_the_sentence_on_screen():
    cues = demo.subtitle_cues("Một hai. Ba bốn năm sáu.", 1000)

    assert demo.cue_at(cues, 0) == "Một hai."
    assert demo.cue_at(cues, 999) == "Ba bốn năm sáu."
    assert demo.cue_at(cues, 1000) is None


def test_script_is_a_short_vietnamese_greeting_and_story():
    words = demo.SCRIPT_TEXT.split()

    assert 45 <= len(words) <= 55
    assert "Xin chào" in demo.SCRIPT_TEXT
    assert "Quán Ăn Nhỏ" in demo.SCRIPT_TEXT


# --- step runner ----------------------------------------------------------


def make_runner(names=("one", "two", "three")):
    lines: list[str] = []
    return demo.StepRunner(names, log=lines.append), lines


def test_step_succeeds_only_with_an_existing_validated_file(tmp_path):
    runner, lines = make_runner(("one",))
    out = tmp_path / "out.txt"
    out.write_text("x", encoding="utf-8")

    result = runner.run("one", lambda: out, lambda path: path.read_text() == "x")

    record = runner.records["one"]
    assert result == out
    assert record.status is demo.StepStatus.SUCCESS
    assert record.output == str(out)
    assert record.started_at and record.finished_at
    assert runner.ok
    assert [line.split()[0] for line in lines] == ["PENDING", "RUNNING", "SUCCESS"]


def test_step_fails_without_a_file_or_when_validation_fails(tmp_path):
    runner, _ = make_runner(("missing", "invalid", "raises", "raising"))
    out = tmp_path / "out.txt"
    out.write_text("x", encoding="utf-8")

    def invalid(_path):
        raise ValueError("bad bytes")

    def boom():
        raise RuntimeError("ComfyUI unreachable")

    runner.run("missing", lambda: tmp_path / "nope.png", force=True)
    runner.run("invalid", lambda: out, lambda _path: False, force=True)
    runner.run("raises", lambda: out, invalid, force=True)
    runner.run("raising", boom, force=True)

    statuses = {name: r.status for name, r in runner.records.items()}
    assert set(statuses.values()) == {demo.StepStatus.FAILED}
    assert "bad bytes" in runner.records["raises"].detail
    assert "ComfyUI unreachable" in runner.records["raising"].detail
    assert not runner.ok


def test_failed_step_blocks_the_later_steps_without_running_them(tmp_path):
    runner, lines = make_runner()
    calls: list[str] = []

    def fail():
        calls.append("one")
        raise RuntimeError("no voice")

    def later():
        calls.append("two")
        return tmp_path

    runner.run("one", fail)
    assert runner.run("two", later) is None
    runner.run("three", later)

    assert calls == ["one"]
    assert runner.records["two"].status is demo.StepStatus.BLOCKED
    assert runner.records["two"].detail == "bước trước thất bại: one"
    assert runner.records["three"].status is demo.StepStatus.BLOCKED
    assert any(line.startswith("BLOCKED") for line in lines)


def test_step_blocked_exception_and_forced_step(tmp_path):
    runner, _ = make_runner(("budget", "manifest"))
    out = tmp_path / "manifest.json"
    out.write_text("{}", encoding="utf-8")

    def blocked():
        raise demo.StepBlocked("không có phông chữ")

    runner.run("budget", blocked)
    runner.run("manifest", lambda: out, force=True)

    assert runner.records["budget"].status is demo.StepStatus.BLOCKED
    assert runner.records["budget"].detail == "không có phông chữ"
    assert runner.records["manifest"].status is demo.StepStatus.SUCCESS
    assert not runner.ok


# --- budget step (layer injected) ----------------------------------------------


class FakeDecisionError(Exception):
    code = "budget.paid_provider_blocked_free_only"
    user_message = "Chế độ 'Chỉ miễn phí' đang bật"


@dataclass
class FakeDeclaration:
    provider: str
    cost_class: str


def fake_layer(result=None, error=None, calls=None):
    calls = calls if calls is not None else []

    class Guard:
        def preflight(self, providers):
            calls.append(("preflight", list(providers)))
            if error is not None:
                raise error
            return result

    class Container:
        def resolve(self, key):
            assert key is Guard
            return Guard()

    def build_container(settings):
        calls.append(("build_container", settings))
        return Container()

    def prepare_database(container):
        calls.append(("prepare_database", container))

    return demo.BudgetLayer(build_container, prepare_database, Guard, FakeDecisionError)


def test_budget_step_records_a_free_preflight(tmp_path, monkeypatch):
    calls: list = []
    result = SimpleNamespace(
        mode=SimpleNamespace(value="free_only"),
        status=SimpleNamespace(value="free"),
        declarations=(FakeDeclaration("comfyui", "local_free"),),
    )
    monkeypatch.setattr(
        demo, "import_budget_layer", lambda: fake_layer(result, None, calls)
    )
    runner, _ = make_runner(("budget_check",))
    state = demo.DemoState()

    runner.run(
        "budget_check",
        lambda: demo.run_budget_check("settings", ["comfy", "edge"], tmp_path, state),
        demo.validate_budget,
    )

    assert runner.records["budget_check"].status is demo.StepStatus.SUCCESS
    assert [name for name, _ in calls] == [
        "build_container",
        "prepare_database",
        "preflight",
    ]
    assert calls[2][1] == ["comfy", "edge"]
    assert state.budget["mode"] == "free_only"
    assert state.budget["status"] == "free"
    saved = json.loads((tmp_path / "budget_check.json").read_text(encoding="utf-8"))
    assert saved["declarations"] == [
        {"provider": "comfyui", "cost_class": "local_free"}
    ]


def test_budget_step_is_blocked_when_the_guard_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(
        demo, "import_budget_layer", lambda: fake_layer(error=FakeDecisionError("x"))
    )
    runner, _ = make_runner(("budget_check", "background_image"))

    runner.run(
        "budget_check",
        lambda: demo.run_budget_check(None, [], tmp_path, demo.DemoState()),
    )
    runner.run("background_image", lambda: tmp_path)

    record = runner.records["budget_check"]
    assert record.status is demo.StepStatus.BLOCKED
    assert "budget.paid_provider_blocked_free_only" in record.detail
    assert runner.records["background_image"].status is demo.StepStatus.BLOCKED


def test_budget_step_is_blocked_when_the_status_is_not_free(tmp_path, monkeypatch):
    result = SimpleNamespace(mode="paid_with_per_request_approval", status="blocked")
    result.declarations = ()
    monkeypatch.setattr(demo, "import_budget_layer", lambda: fake_layer(result))
    runner, _ = make_runner(("budget_check",))

    runner.run(
        "budget_check",
        lambda: demo.run_budget_check(None, [], tmp_path, demo.DemoState()),
        demo.validate_budget,
    )

    assert runner.records["budget_check"].status is demo.StepStatus.BLOCKED


def test_budget_step_fails_when_the_budget_layer_cannot_be_imported(
    tmp_path, monkeypatch
):
    # A None entry makes the import raise ImportError: never a silent skip.
    monkeypatch.setitem(
        sys.modules, "ai_youtube_agent.pipeline.paid_request_guard", None
    )
    runner, _ = make_runner(("budget_check",))

    runner.run(
        "budget_check",
        lambda: demo.run_budget_check(None, [], tmp_path, demo.DemoState()),
    )

    record = runner.records["budget_check"]
    assert record.status is demo.StepStatus.FAILED
    assert "paid_request_guard" in record.detail


# --- manifest --------------------------------------------------------------


def manifest_for(statuses):
    steps = [
        {"name": name, "status": status}
        for name, status in zip(demo.STEP_NAMES, statuses, strict=False)
    ]
    state = demo.DemoState(
        budget={"mode": "free_only", "status": "free", "declarations": []},
        comfy={"seed": 7, "prompt_id": "p-1", "checkpoint": "v1-5.safetensors"},
    )
    return demo.build_manifest(
        run_id="20261023-000000",
        created_at="2026-10-23T00:00:00Z",
        voice="vi-VN-HoaiMyNeural",
        checkpoint="ignored.safetensors",
        size=(720, 1280),
        fps=24,
        steps=steps,
        state=state,
    )


def test_manifest_states_the_methods_honestly_and_costs_nothing():
    manifest = manifest_for(["SUCCESS"] * 7)

    assert manifest["methods"] == {
        "background": (
            "Ảnh AI — ComfyUI cục bộ, Stable Diffusion 1.5 (v1-5.safetensors)"
        ),
        "motion": (
            "Hoạt hình 2D bằng phần mềm (Pillow, từng khung hình) — "
            "KHÔNG phải mô hình video AI"
        ),
        "voice": (
            "Microsoft Edge TTS vi-VN-HoaiMyNeural "
            "(dịch vụ trực tuyến miễn phí, không phải cục bộ)"
        ),
        "subtitles": "vẽ bằng Pillow",
    }
    assert manifest["cost"] == {"amount": "0", "paid_requests": 0}
    assert manifest["budget"] == {"mode": "free_only", "status": "free"}
    assert manifest["comfy"] == {
        "seed": 7,
        "prompt_id": "p-1",
        "checkpoint": "v1-5.safetensors",
    }
    assert manifest["overall_status"] == "SUCCESS"
    json.dumps(manifest, ensure_ascii=False)  # serialisable


def test_manifest_is_failed_unless_every_step_succeeded(tmp_path):
    manifest = manifest_for(["SUCCESS", "FAILED"] + ["BLOCKED"] * 5)

    assert manifest["overall_status"] == "FAILED"
    path = demo.write_manifest(manifest, tmp_path / "manifest.json")
    assert demo.validate_manifest(path)


# --- fonts and frames (Pillow) -------------------------------------------------


def test_configured_font_that_does_not_exist_blocks(tmp_path):
    with pytest.raises(demo.StepBlocked, match="phông chữ"):
        demo.resolve_font_files(tmp_path / "missing.ttf")


def test_no_vietnamese_font_blocks_instead_of_the_bitmap_font(tmp_path):
    with pytest.raises(demo.StepBlocked, match="tiếng Việt"):
        demo.resolve_font_files(None, fonts_dir=tmp_path)


@pytest.fixture
def scene():
    pil = pytest.importorskip("PIL.Image")
    try:
        fonts = demo.resolve_font_files(None)
    except demo.StepBlocked:
        pytest.skip("no Vietnamese-capable TrueType font on this machine")
    background = pil.new("RGB", (64, 96), (180, 140, 110))
    for x in range(64):
        background.putpixel((x, 40), (40, 90, 160))
    return demo.prepare_scene(background, (180, 320), fonts)


def test_frame_render_is_deterministic(scene):
    first = demo.render_frame(1.5, scene, 0.4, "Xin chào các bạn!", duration_s=14)
    second = demo.render_frame(1.5, scene, 0.4, "Xin chào các bạn!", duration_s=14)

    assert first.size == (180, 320)
    assert first.mode == "RGB"
    assert first.tobytes() == second.tobytes()


def test_mouth_envelope_changes_the_pixels(scene):
    closed = demo.render_frame(5.0, scene, 0.0, None, duration_s=14)
    open_ = demo.render_frame(5.0, scene, 1.0, None, duration_s=14)

    assert closed.tobytes() != open_.tobytes()


def test_motion_changes_frames_over_time(scene):
    early = demo.render_frame(0.5, scene, 0.0, None, duration_s=14)
    later = demo.render_frame(6.0, scene, 0.0, None, duration_s=14)

    assert early.tobytes() != later.tobytes()


def test_character_blinks_and_waves():
    fps = 24
    first_blink = round(0.8 * fps)
    assert demo.eye_openness(0.0, fps) == 1.0
    assert demo.eye_openness(first_blink / fps, fps) == 0.3
    assert demo.eye_openness((first_blink + 1) / fps, fps) == 0.0
    assert demo.eye_openness((first_blink + 2) / fps, fps) == 1.0
    assert demo.wave_amount(0.5, 15) == 1.0
    assert demo.wave_amount(7.0, 15) == 0.0
    assert demo.wave_amount(14.5, 15) == 1.0


# --- encode and validate (ffmpeg) ---------------------------------------------


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)
def test_frames_stream_to_ffmpeg_and_the_mp4_validates(tmp_path):
    pil = pytest.importorskip("PIL.Image")
    wav = write_wav(tmp_path / "voice.wav", [(0.3, 0.0), (0.9, 0.4)])
    size, fps, frames = (160, 288), 12, 18
    produced: list[int] = []

    def stream():
        for index in range(frames):
            produced.append(index)
            yield pil.new("RGB", size, (index * 10, 80, 120))

    out = demo.encode(
        stream(),
        wav,
        tmp_path / "out.mp4",
        size=size,
        fps=fps,
        log_path=tmp_path / "ffmpeg.log",
    )
    summary = demo.validate_mp4(out, expected_size=size, min_s=1.0, max_s=2.0)

    assert produced == list(range(frames))
    assert summary["ffprobe"]["video"]["codec"] == "h264"
    assert summary["ffprobe"]["audio"]["codec"] == "aac"
    assert (
        summary["ffprobe"]["video"]["width"],
        summary["ffprobe"]["video"]["height"],
    ) == size
    assert summary["size_bytes"] == out.stat().st_size
    with pytest.raises(ValueError, match="duration"):
        demo.validate_mp4(out, min_s=10.0, max_s=20.0)


def test_encode_rejects_a_frame_of_the_wrong_size(tmp_path):
    pil = pytest.importorskip("PIL.Image")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    wav = write_wav(tmp_path / "voice.wav", [(0.5, 0.2)])

    with pytest.raises(ValueError, match="frame 0"):
        demo.encode(
            iter([pil.new("RGB", (100, 100))]),
            wav,
            tmp_path / "out.mp4",
            size=(160, 288),
            fps=12,
        )
