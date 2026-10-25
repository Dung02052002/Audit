"""Animation film tool (tools/animation_film.py, ANIM-01): steps, run dir, manifest.

Development tool, not part of the package. The services are fakes (no ComfyUI, no
Edge TTS, no network): the slow steps are replaced where the tool imports them, so
these tests check the wiring, the step statuses, the output files and the honesty
of the manifest. The real run is manual.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_youtube_agent.animation import audio_mix, audio_qc, validate
from ai_youtube_agent.animation import backgrounds as backgrounds_module
from ai_youtube_agent.animation import tts as tts_module
from ai_youtube_agent.animation.errors import AnimationError
from ai_youtube_agent.animation.manifest import validate_manifest
from ai_youtube_agent.animation.pipeline_steps import StepBlocked
from ai_youtube_agent.animation.timeline import resolve_timeline
from animation_fixtures import fake_spoken, shipped_storyboard

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "animation_film", ROOT / "tools" / "animation_film.py"
)
tool = importlib.util.module_from_spec(_spec)
sys.modules["animation_film"] = tool
_spec.loader.exec_module(tool)

STORYBOARD = ROOT / "tools" / "storyboards" / "quan_an_nho_shorts.json"
NOW = datetime(2026, 10, 23, 12, 0, 0)


def args_for(tmp_path: Path, *extra: str):
    return tool.parse_args(
        [
            "--storyboard",
            str(STORYBOARD),
            "--out-root",
            str(tmp_path / "out"),
            *extra,
        ]
    )


def free_budget(settings, providers, run_dir, record):
    record.update(mode="free_only", status="free", declarations=[])
    path = run_dir / "budget_check.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def blocked_budget(settings, providers, run_dir, record):
    raise StepBlocked("PAID_REQUEST_BLOCKED: the budget refuses")


SETTINGS = SimpleNamespace(ffmpeg_binary="ffmpeg", render_font_file=None)
SERVICES = (SimpleNamespace(provider_id="comfyui"), SimpleNamespace(provider_id="edge"))


def statuses(run_dir: Path) -> dict[str, str]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    return {step["name"]: step["status"] for step in manifest["steps"]}


def only_run_dir(tmp_path: Path) -> Path:
    runs = [p for p in (tmp_path / "out").iterdir() if p.is_dir()]
    assert len(runs) == 1
    return runs[0]


# --- arguments ----------------------------------------------------------------------


def test_defaults_match_the_shipped_setup() -> None:
    args = tool.parse_args([])

    assert args.storyboard == Path("tools/storyboards/quan_an_nho_shorts.json")
    assert args.out_root == Path("artifacts/animation_film")
    assert args.comfy_url == "http://127.0.0.1:8188"
    assert args.checkpoint == "v1-5-pruned-emaonly.safetensors"
    assert args.cache_dir is None and args.size is None and args.fps is None
    assert not args.stills_only and not args.contact_sheet and not args.strict


def test_options_are_parsed() -> None:
    args = tool.parse_args(
        [
            "--size",
            "540x960",
            "--stills",
            "0.5,1.5",
            "--stills-only",
            "--contact-sheet",
            "--steps",
            "voice, audio",
            "--strict",
            "--checkpoint",
            "other.safetensors",
        ]
    )

    assert args.size == (540, 960) and args.stills == [0.5, 1.5]
    assert args.stills_only and args.contact_sheet and args.strict
    assert args.steps == ["voice", "audio"] and args.checkpoint == "other.safetensors"


@pytest.mark.parametrize(
    "argv",
    [
        ["--size", "1080"],
        ["--size", "1081x1920"],
        ["--stills", "a,b"],
        ["--steps", "voice,unknown"],
    ],
)
def test_bad_options_exit_with_a_usage_error(argv, capsys) -> None:
    with pytest.raises(SystemExit):
        tool.parse_args(argv)

    assert "error" in capsys.readouterr().err


def test_the_step_selection() -> None:
    full = tool.choose_steps(tool.parse_args([]))
    assert full == list(tool.FULL_STEPS) and full[-1] == "manifest"
    assert full.index("stills") < full.index("audio") < full.index("render")
    assert full.index("render") < full.index("validate") < full.index("manifest")

    stills = tool.choose_steps(tool.parse_args(["--stills-only"]))
    assert "audio" not in stills and "render" not in stills and "validate" not in stills
    assert stills[-1] == "manifest"

    chosen = tool.choose_steps(tool.parse_args(["--steps", "render,voice"]))
    assert chosen == ["voice", "render", "manifest"]  # canonical order, manifest always


# --- run directory ------------------------------------------------------------------


def test_a_run_directory_is_never_reused(tmp_path: Path) -> None:
    first_id, first = tool.make_run_dir(tmp_path, NOW)
    (first / "keep.txt").write_text("old run", encoding="utf-8")
    second_id, second = tool.make_run_dir(tmp_path, NOW)
    third_id, third = tool.make_run_dir(tmp_path, NOW)

    assert first_id == "20261023-120000" and second_id == "20261023-120000-2"
    assert third_id == "20261023-120000-3"
    assert len({first, second, third}) == 3
    assert (first / "keep.txt").read_text(encoding="utf-8") == "old run"
    assert not any(second.iterdir())


# --- failures still write the manifest ----------------------------------------------


def test_a_refused_budget_blocks_every_later_step_but_the_manifest_is_written(
    tmp_path: Path,
) -> None:
    code = tool.run(
        args_for(tmp_path, "--stills-only"),
        SETTINGS,
        *SERVICES,
        budget_check=blocked_budget,
        now=NOW,
    )

    run_dir = only_run_dir(tmp_path)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert code == 1 and manifest["overall_status"] == "FAILED"
    got = statuses(run_dir)
    assert got["budget_check"] == "BLOCKED"
    assert all(
        got[name] == "BLOCKED"
        for name in got
        if name not in ("budget_check", "manifest")
    )
    assert got["manifest"] == "SUCCESS"
    assert "budget refuses" in manifest["steps"][0]["detail"]
    assert manifest["cost"] == {"amount": "0", "paid_requests": 0}
    assert (run_dir / "run.log").read_text(encoding="utf-8").count("BLOCKED") >= 3
    assert not list(run_dir.glob("*.mp4")) and not list(run_dir.glob("still_*.png"))


def test_a_failed_voice_step_blocks_the_rest_and_says_why(
    tmp_path: Path, monkeypatch
) -> None:
    def broken(*_args, **_kwargs):
        raise AnimationError("Edge TTS failed for line 'n1' after 3 attempt(s)")

    monkeypatch.setattr(tts_module, "synthesize_lines", broken)

    code = tool.run(
        args_for(tmp_path, "--stills-only"),
        SETTINGS,
        *SERVICES,
        budget_check=free_budget,
        now=NOW,
    )

    run_dir = only_run_dir(tmp_path)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    got = statuses(run_dir)
    assert code == 1
    assert got["budget_check"] == "SUCCESS" and got["storyboard"] == "SUCCESS"
    assert got["voice"] == "FAILED" and got["backgrounds"] == "BLOCKED"
    assert "Edge TTS failed for line 'n1'" in manifest["steps"][2]["detail"]
    assert manifest["budget"] == {"mode": "free_only", "status": "free"}


def test_selecting_a_step_without_its_inputs_is_blocked_not_a_crash(
    tmp_path: Path,
) -> None:
    code = tool.run(
        args_for(tmp_path, "--steps", "render"),
        SETTINGS,
        *SERVICES,
        budget_check=free_budget,
        now=NOW,
    )

    run_dir = only_run_dir(tmp_path)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert code == 1
    assert [s["name"] for s in manifest["steps"]] == ["render", "manifest"]
    assert manifest["steps"][0]["status"] == "BLOCKED"
    assert "has not succeeded in this run" in manifest["steps"][0]["detail"]


def test_a_different_frame_rate_than_the_storyboard_is_refused(tmp_path: Path) -> None:
    code = tool.run(
        args_for(tmp_path, "--steps", "storyboard", "--fps", "24"),
        SETTINGS,
        *SERVICES,
        budget_check=free_budget,
        now=NOW,
    )

    run_dir = only_run_dir(tmp_path)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert code == 1 and manifest["steps"][0]["status"] == "BLOCKED"
    assert "differs from the storyboard (30)" in manifest["steps"][0]["detail"]


# --- the stills-only run with fakes -------------------------------------------------


@pytest.fixture
def fakes(monkeypatch, tmp_path: Path):
    """Edge TTS and ComfyUI replaced by deterministic fakes in the tool's imports."""
    pytest.importorskip("PIL.Image")
    from PIL import Image

    storyboard = shipped_storyboard()
    calls = {"voice": 0, "backgrounds": 0}

    def synthesize(edge, board, ffmpeg, out_dir, cache_dir, **_kwargs):
        calls["voice"] += 1
        return fake_spoken(board)

    def generate(comfy, board, out_dir, cache_dir, **_kwargs):
        calls["backgrounds"] += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        records = {}
        for location in board.locations:
            plate = Image.new("RGB", (1080, 1920), (150, 176, 170))
            for y in range(0, 1920, 8):
                plate.paste((140 + y // 40, 170, 160 - y // 60), (0, y, 1080, y + 8))
            path = out_dir / f"{location.id}.png"
            plate.save(path)
            records[location.id] = backgrounds_module.BackgroundRecord(
                location_id=location.id,
                method="fake",
                pass1_size=(576, 1024),
                pass2_size=(1080, 1920),
                final_size=(1080, 1920),
                tiled_vae=False,
                post_resize=None,
                seed=location.seed,
                prompt_id=f"prompt-{location.id}",
                elapsed_s=1.0,
                cache_hit=False,
                tier="A",
                checkpoint="v1-5-pruned-emaonly.safetensors",
                path=str(path),
                sha256="0" * 64,
            )
        return records

    monkeypatch.setattr(tts_module, "synthesize_lines", synthesize)
    monkeypatch.setattr(backgrounds_module, "generate_backgrounds", generate)
    return SimpleNamespace(storyboard=storyboard, calls=calls)


def test_a_stills_only_run_writes_stills_subtitles_timeline_and_an_honest_manifest(
    tmp_path: Path, fakes
) -> None:
    code = tool.run(
        args_for(
            tmp_path,
            "--stills-only",
            "--size",
            "270x480",
            "--stills",
            "1.0",
            "--contact-sheet",
        ),
        SETTINGS,
        *SERVICES,
        budget_check=free_budget,
        now=NOW,
    )

    run_dir = only_run_dir(tmp_path)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert code == 0 and manifest["overall_status"] == "SUCCESS"
    assert list(statuses(run_dir)) == list(tool.STILLS_ONLY_STEPS)
    assert set(statuses(run_dir).values()) == {"SUCCESS"}
    for name in (
        "still_start.png",
        "still_middle.png",
        "still_end.png",
        "still_t1s.png",
        "contact_sheet.png",
        "quan_an_nho_shorts.srt",
        "quan_an_nho_shorts.vtt",
        "timeline.json",
        "voice.json",
        "backgrounds.json",
        "storyboard.json",
        "budget_check.json",
        "run.log",
    ):
        assert (run_dir / name).is_file(), name
    assert not list(run_dir.glob("*.mp4")) and not (run_dir / "mix.wav").exists()
    assert fakes.calls == {"voice": 1, "backgrounds": 1}
    # honesty: the manifest passes the strict checker and says what it did
    assert validate_manifest(manifest) == []
    assert (
        manifest["methods"]["motion"]
        == "software 2D animation (code), not an AI video model"
    )
    assert manifest["cost"] == {"amount": "0", "paid_requests": 0}
    assert manifest["timeline"]["frame_count"] > 400
    assert manifest["outputs"]["contact_sheet"].endswith("contact_sheet.png")
    assert len(manifest["outputs"]["stills"]) == 4
    srt = (run_dir / "quan_an_nho_shorts.srt").read_text(encoding="utf-8")
    assert srt.startswith("1\n00:00:00,") and "Mai" in srt
    timeline = json.loads((run_dir / "timeline.json").read_text(encoding="utf-8"))
    assert [s["id"] for s in timeline["scenes"]][0] == "s1_hook"
    assert any(e["kind"] == "door_bell" for e in timeline["sfx"])


def test_two_runs_never_share_a_directory(tmp_path: Path, fakes) -> None:
    for _ in range(2):
        tool.run(
            args_for(tmp_path, "--steps", "storyboard"),
            SETTINGS,
            *SERVICES,
            budget_check=free_budget,
            now=NOW,
        )

    runs = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert runs == ["20261023-120000", "20261023-120000-2"]
    for name in runs:
        assert (tmp_path / "out" / name / "manifest.json").exists()


def test_the_audio_step_masters_the_mix_and_checks_its_loudness(
    tmp_path: Path, fakes, monkeypatch
) -> None:
    def build(timeline, out_dir, **kwargs):
        mix = out_dir / "mix.wav"
        mix.write_bytes(b"RIFF")
        for name in ("narration", "music", "ambience", "sfx"):
            (out_dir / f"{name}.wav").write_bytes(b"RIFF")
        return audio_mix.AudioBuild(
            narration=out_dir / "narration.wav",
            music=out_dir / "music.wav",
            ambience=out_dir / "ambience.wav",
            sfx=out_dir / "sfx.wav",
            mix=mix,
            windows=[(300.0, 2500.0)],
            total_ms=28300.0,
            duck_depth_db=9.0,
        )

    seen = {}
    monkeypatch.setattr(audio_mix, "build_audio", build)
    monkeypatch.setattr(
        audio_qc,
        "measure_loudness",
        lambda path, ffmpeg, **_k: seen.setdefault(
            "l", audio_qc.LoudnessReport(-14.0, -1.5, 3.0)
        ),
    )

    code = tool.run(
        args_for(
            tmp_path,
            "--steps",
            "budget_check,storyboard,voice,backgrounds,timeline_subtitles,audio",
        ),
        SETTINGS,
        *SERVICES,
        budget_check=free_budget,
        now=NOW,
    )

    run_dir = only_run_dir(tmp_path)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert code == 0 and statuses(run_dir)["audio"] == "SUCCESS"
    assert manifest["render"]["audio"]["mix"].endswith("mix.wav")
    assert manifest["outputs"]["mix"].endswith("mix.wav")


def test_the_audio_step_fails_when_the_master_misses_the_target(
    tmp_path: Path, fakes, monkeypatch
) -> None:
    def build(timeline, out_dir, **kwargs):
        (out_dir / "mix.wav").write_bytes(b"RIFF")
        return audio_mix.AudioBuild(
            narration=out_dir / "n.wav",
            music=out_dir / "m.wav",
            ambience=out_dir / "a.wav",
            sfx=out_dir / "s.wav",
            mix=out_dir / "mix.wav",
            windows=[(0.0, 1.0)],
            total_ms=1.0,
        )

    monkeypatch.setattr(audio_mix, "build_audio", build)
    monkeypatch.setattr(
        audio_qc,
        "measure_loudness",
        lambda *a, **k: audio_qc.LoudnessReport(-20.0, 0.0, 3.0),
    )

    code = tool.run(
        args_for(
            tmp_path,
            "--steps",
            "budget_check,storyboard,voice,backgrounds,timeline_subtitles,audio",
        ),
        SETTINGS,
        *SERVICES,
        budget_check=free_budget,
        now=NOW,
    )

    run_dir = only_run_dir(tmp_path)
    got = statuses(run_dir)
    assert code == 1 and got["audio"] == "FAILED"
    assert (
        "did not pass validation"
        in json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["steps"][
            5
        ]["detail"]
    )


# --- the validate step with canned measurements -------------------------------------


def pipeline_for_validation(tmp_path: Path, *, strict: bool, motion_value: float):
    storyboard = shipped_storyboard()
    timeline = resolve_timeline(storyboard, fake_spoken(storyboard))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state = tool.FilmState(run_dir=run_dir)
    state.storyboard, state.timeline = storyboard, timeline
    state.mp4 = run_dir / "film.mp4"
    state.mp4.write_bytes(b"x")
    state.audio = audio_mix.AudioBuild(
        narration=run_dir / "n.wav",
        music=run_dir / "m.wav",
        ambience=run_dir / "a.wav",
        sfx=run_dir / "s.wav",
        mix=run_dir / "mix.wav",
        windows=[(0.0, 1000.0)],
        total_ms=1000.0,
    )
    state.composition = validate.CompositionTally(
        size=(1080, 1920), safe_area=(64, 230, 950, 1536)
    )
    state.done = {"render", "audio"}
    args = tool.parse_args(
        ["--out-root", str(tmp_path)] + (["--strict"] if strict else [])
    )
    pipeline = tool.Pipeline(args, SETTINGS, *SERVICES, state, lambda _m: None)
    probe = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "profile": "High",
                "width": 1080,
                "height": 1920,
                "pix_fmt": "yuv420p",
                "sample_aspect_ratio": "1:1",
                "r_frame_rate": "30/1",
                "nb_frames": str(timeline.frame_count),
                "duration": "28.3",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
                "duration": "28.3",
            },
        ],
        "format": {"duration": "28.3"},
    }
    return pipeline, state, probe, timeline


def patch_measurements(
    monkeypatch, pipeline, probe, timeline, motion_value, qc_problems
):
    monkeypatch.setattr(validate, "probe_media", lambda *a, **k: probe)
    monkeypatch.setattr(validate, "faststart_ok", lambda path: True)
    monkeypatch.setattr(validate, "decode_errors", lambda *a, **k: [])
    seconds = timeline.total_ms / 1000
    monkeypatch.setattr(
        validate,
        "motion_series",
        lambda *a, **k: [(i / 30, motion_value) for i in range(int(seconds * 30))],
    )
    colours = [
        (200, 20, 20),
        (20, 200, 20),
        (20, 20, 200),
        (200, 200, 20),
        (20, 200, 200),
    ]
    order = iter(colours)
    monkeypatch.setattr(
        validate,
        "thumbnail",
        lambda *a, **k: (
            bytes(next(order)) * (validate.THUMB_SIZE[0] * validate.THUMB_SIZE[1])
        ),
    )
    result = audio_qc.AudioQcResult(problems=list(qc_problems))
    monkeypatch.setattr(pipeline, "measure_final_audio", lambda *a, **k: result)


def test_validation_passes_with_good_measurements_and_writes_both_reports(
    tmp_path: Path, monkeypatch
) -> None:
    pipeline, state, probe, timeline = pipeline_for_validation(
        tmp_path, strict=True, motion_value=2.0
    )
    patch_measurements(monkeypatch, pipeline, probe, timeline, 2.0, [])

    path = pipeline.run_validate()

    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["passed"] and report["all_checks_passed"] and report["strict"]
    assert set(report["checks"]) == {
        "container",
        "decode",
        "motion",
        "scenes",
        "composition",
        "audio",
    }
    assert (state.run_dir / "audio_qc.json").is_file()
    assert state.validation["passed"] is True


def test_a_soft_problem_is_a_warning_unless_strict(tmp_path: Path, monkeypatch) -> None:
    pipeline, state, probe, timeline = pipeline_for_validation(
        tmp_path, strict=False, motion_value=0.0
    )
    patch_measurements(
        monkeypatch, pipeline, probe, timeline, 0.0, ["1 clipped samples"]
    )

    path = pipeline.run_validate()  # frozen picture and clipping: warnings only

    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["passed"] is True and report["all_checks_passed"] is False
    assert any("frozen" in w for w in report["warnings"])
    assert any("clipped" in w for w in report["warnings"])


def test_strict_mode_turns_a_soft_problem_into_a_failed_step(
    tmp_path: Path, monkeypatch
) -> None:
    pipeline, state, probe, timeline = pipeline_for_validation(
        tmp_path, strict=True, motion_value=0.0
    )
    patch_measurements(monkeypatch, pipeline, probe, timeline, 0.0, [])

    with pytest.raises(AnimationError, match="frozen"):
        pipeline.run_validate()

    written = json.loads(
        (state.run_dir / "validation.json").read_text(encoding="utf-8")
    )
    assert written["passed"] is False and written["strict"] is True


def test_a_container_problem_always_fails_the_step(tmp_path: Path, monkeypatch) -> None:
    pipeline, state, probe, timeline = pipeline_for_validation(
        tmp_path, strict=False, motion_value=2.0
    )
    probe["streams"][0]["nb_frames"] = "10"
    patch_measurements(monkeypatch, pipeline, probe, timeline, 2.0, [])

    with pytest.raises(AnimationError, match="has 10 frames"):
        pipeline.run_validate()


def test_the_manifest_carries_the_validation_and_the_audio_qc(
    tmp_path: Path, monkeypatch
) -> None:
    pipeline, state, probe, timeline = pipeline_for_validation(
        tmp_path, strict=False, motion_value=2.0
    )
    patch_measurements(monkeypatch, pipeline, probe, timeline, 2.0, [])
    pipeline.run_validate()
    runner = tool.StepRunner(["validate"], log=lambda _m: None)

    manifest = tool.build_run_manifest(
        "r1", "2026-10-23T12:00:00Z", runner, state, pipeline
    )

    assert manifest["validation"]["passed"] is True
    assert manifest["audio_qc"]["passed"] is True
    assert manifest["outputs"]["validation"].endswith("validation.json")
    assert manifest["outputs"]["audio_qc"].endswith("audio_qc.json")
    assert manifest["methods"]["music"].startswith("synthesized by code")
