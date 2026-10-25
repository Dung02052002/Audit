"""Animation step runner, budget preflight and fonts, and the PIL-free pure modules."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from ai_youtube_agent.animation import pipeline_steps as steps
from ai_youtube_agent.animation.fonts import FontFiles, resolve_font_files

PURE_MODULES = (
    "ai_youtube_agent.animation",
    "ai_youtube_agent.animation.errors",
    "ai_youtube_agent.animation.easing",
    "ai_youtube_agent.animation.storyboard",
    "ai_youtube_agent.animation.timeline",
    "ai_youtube_agent.animation.camera",
    "ai_youtube_agent.animation.subtitles",
    "ai_youtube_agent.animation.tts",
    "ai_youtube_agent.animation.backgrounds",
    "ai_youtube_agent.animation.manifest",
    "ai_youtube_agent.animation.pipeline_steps",
    "ai_youtube_agent.animation.fonts",
    "ai_youtube_agent.animation.rig",
    "ai_youtube_agent.animation.actions",
    "ai_youtube_agent.animation.transitions",
    "ai_youtube_agent.animation.encode",
)


def make_runner(names=("one", "two", "three")):
    lines: list[str] = []
    return steps.StepRunner(names, log=lines.append), lines


# --- pure modules never need Pillow --------------------------------------------


def test_pure_modules_import_and_work_with_pillow_blocked() -> None:
    code = textwrap.dedent(
        f"""
        import importlib, sys

        class BlockPillow:
            def find_spec(self, name, path=None, target=None):
                if name == "PIL" or name.startswith("PIL."):
                    raise ImportError("Pillow is blocked in this test")

        sys.meta_path.insert(0, BlockPillow())
        for name in {PURE_MODULES!r}:
            importlib.import_module(name)

        from ai_youtube_agent.animation.storyboard import load_storyboard
        from ai_youtube_agent.animation.subtitles import (
            CueSource, build_cues, align_boundaries, TimingEvent, layout_cue,
            contrast_ratio,
        )
        from ai_youtube_agent.animation.camera import headroom_center
        from ai_youtube_agent.animation.backgrounds import validate_plate
        from ai_youtube_agent.animation.rig import Pose, solve_skeleton, walk_cycle
        from ai_youtube_agent.animation.actions import ACTION_REGISTRY, Choreography
        from ai_youtube_agent.animation.transitions import whip_offset
        from ai_youtube_agent.animation.encode import build_ffmpeg_command

        words, _ = align_boundaries(
            "Ngon quá!", [TimingEvent("WordBoundary", 0, 200, "Ngon"),
                          TimingEvent("WordBoundary", 250, 500, "quá")])
        cue = build_cues([CueSource("a", words)])[0]
        layout_cue(cue, lambda text, size: len(text) * size * 0.5)
        assert contrast_ratio((255, 255, 255), (0, 0, 0)) > 20
        assert headroom_center((540, 800), 2.7)
        assert len(ACTION_REGISTRY) == 28 and whip_offset(1.0, 100, "left") == -100
        assert solve_skeleton(Pose(yaw=0.5)).near == "right"
        assert walk_cycle(0.3, 300.0).bob <= 0
        assert "-c:v" in build_ffmpeg_command("o.mp4", size=(64, 112), fps=30)
        assert Choreography
        loaded = [name for name in sys.modules
                  if name == "PIL" or name.startswith("PIL.")]
        assert not loaded, loaded
        print("ok")
        """
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# --- the step runner -----------------------------------------------------------


def test_steps_succeed_in_order_and_record_their_output(tmp_path) -> None:
    runner, lines = make_runner(("a", "b"))
    out = tmp_path / "a.txt"
    out.write_text("x", encoding="utf-8")

    result = runner.run("a", lambda: out, lambda path: path.read_text() == "x")
    runner.run("b", lambda: out)

    assert result == out
    assert runner.ok
    assert runner.records["a"].status is steps.StepStatus.SUCCESS
    assert runner.records["a"].output == str(out)
    assert lines[0].startswith("PENDING") and any(
        line.startswith("SUCCESS") for line in lines
    )
    assert [r["name"] for r in runner.as_list()] == ["a", "b"]


def test_a_failed_step_blocks_the_rest_but_a_forced_step_runs(tmp_path) -> None:
    runner, _ = make_runner()
    out = tmp_path / "o.txt"
    out.write_text("x", encoding="utf-8")

    def fail():
        raise RuntimeError("no voice")

    runner.run("one", fail)
    assert runner.run("two", lambda: out) is None
    runner.run("three", lambda: out, force=True)

    assert runner.records["one"].detail == "RuntimeError: no voice"
    assert runner.records["two"].status is steps.StepStatus.BLOCKED
    assert runner.records["two"].detail == "previous step failed: one"
    assert runner.records["three"].status is steps.StepStatus.SUCCESS
    assert not runner.ok


def test_a_step_needs_an_existing_file_and_a_passing_validator(tmp_path) -> None:
    runner, _ = make_runner(("missing", "invalid", "blocked"))
    out = tmp_path / "o.txt"
    out.write_text("x", encoding="utf-8")

    def blocked():
        raise steps.StepBlocked("no font")

    runner.run("missing", lambda: tmp_path / "nope", force=True)
    runner.run("invalid", lambda: out, lambda _p: False, force=True)
    runner.run("blocked", blocked, force=True)

    assert runner.records["missing"].status is steps.StepStatus.FAILED
    assert runner.records["invalid"].status is steps.StepStatus.FAILED
    assert runner.records["blocked"].status is steps.StepStatus.BLOCKED
    assert runner.records["blocked"].detail == "no font"


def test_run_log_prints_and_appends(tmp_path, capsys) -> None:
    log = steps.RunLog(tmp_path / "run.log")

    log("hello")
    log("world")

    assert "hello" in capsys.readouterr().out
    assert (tmp_path / "run.log").read_text(encoding="utf-8").count("\n") == 2


# --- budget --------------------------------------------------------------------


class FakeDecisionError(Exception):
    code = "budget.paid_provider_blocked_free_only"
    user_message = "Free only mode is on"


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

    return steps.BudgetLayer(
        build_container, prepare_database, Guard, FakeDecisionError
    )


def test_budget_check_records_a_free_preflight(tmp_path, monkeypatch) -> None:
    calls: list = []
    result = SimpleNamespace(
        mode=SimpleNamespace(value="free_only"),
        status=SimpleNamespace(value="free"),
        declarations=(FakeDeclaration("comfyui", "local_free"),),
    )
    monkeypatch.setattr(
        steps, "import_budget_layer", lambda: fake_layer(result, None, calls)
    )
    record: dict = {}

    path = steps.run_budget_check("settings", ["comfy", "edge"], tmp_path, record)

    assert steps.validate_budget(path)
    assert [name for name, _ in calls] == [
        "build_container",
        "prepare_database",
        "preflight",
    ]
    assert record["mode"] == "free_only" and record["status"] == "free"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["declarations"] == [
        {"provider": "comfyui", "cost_class": "local_free"}
    ]


def test_budget_check_blocks_when_the_guard_refuses_or_is_not_free(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        steps, "import_budget_layer", lambda: fake_layer(error=FakeDecisionError("x"))
    )
    with pytest.raises(
        steps.StepBlocked, match="budget.paid_provider_blocked_free_only"
    ):
        steps.run_budget_check(None, [], tmp_path, {})

    paid = SimpleNamespace(mode="paid", status="blocked", declarations=())
    monkeypatch.setattr(steps, "import_budget_layer", lambda: fake_layer(paid))
    with pytest.raises(steps.StepBlocked, match="not 'free'"):
        steps.run_budget_check(None, [], tmp_path, {})


def test_plain_converts_enums_dataclasses_and_sets() -> None:
    value = {
        "a": FakeDeclaration("x", "y"),
        "b": (1, 2),
        "c": SimpleNamespace(value="v"),
    }

    assert steps.plain(value) == {
        "a": {"provider": "x", "cost_class": "y"},
        "b": [1, 2],
        "c": "v",
    }


# --- fonts ---------------------------------------------------------------------


def test_a_configured_font_that_does_not_exist_blocks(tmp_path) -> None:
    with pytest.raises(steps.StepBlocked, match="configured font was not found"):
        resolve_font_files(tmp_path / "missing.ttf")


def test_no_vietnamese_font_blocks_instead_of_the_bitmap_font(tmp_path) -> None:
    with pytest.raises(steps.StepBlocked, match="Vietnamese glyphs"):
        resolve_font_files(None, fonts_dir=tmp_path)


def test_a_system_font_with_vietnamese_glyphs_is_found() -> None:
    pytest.importorskip("PIL.Image")
    try:
        fonts = resolve_font_files(None)
    except steps.StepBlocked:
        pytest.skip("no Vietnamese-capable TrueType font on this machine")

    assert isinstance(fonts, FontFiles)
    assert fonts.regular.is_file() and fonts.bold.is_file()
