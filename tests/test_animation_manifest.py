"""Animation manifest: honesty constants, building and strict validation."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from ai_youtube_agent.animation.backgrounds import POST_RESIZE_LABEL, BackgroundRecord
from ai_youtube_agent.animation.manifest import (
    BACKGROUND_BASE_METHOD,
    COST,
    MOTION_METHOD,
    MUSIC_METHOD,
    SUBTITLES_METHOD,
    background_method,
    build_manifest,
    file_sha256,
    validate_manifest,
    validate_manifest_file,
    voice_method,
    write_manifest,
)
from ai_youtube_agent.animation.timeline import resolve_timeline
from animation_fixtures import fake_spoken, shipped_storyboard


def record(location: str = "front", tier: str = "A", **changes) -> BackgroundRecord:
    base = BackgroundRecord(
        location_id=location,
        method="m",
        pass1_size=(576, 1024),
        pass2_size=(1080, 1920),
        final_size=(1080, 1920),
        tiled_vae=False,
        post_resize=None,
        seed=11,
        prompt_id="p1",
        elapsed_s=12.5,
        cache_hit=False,
        tier="A",
        checkpoint="v1-5-pruned-emaonly.safetensors",
        path=f"{location}.png",
        sha256="ab" * 32,
    )
    if tier == "B":
        base = replace(base, tier="B", tiled_vae=True)
    if tier == "C":
        base = replace(
            base,
            tier="C",
            tiled_vae=True,
            pass2_size=(810, 1440),
            post_resize=POST_RESIZE_LABEL,
        )
    return replace(base, **changes)


STEPS_OK = [
    {"name": "voice", "status": "SUCCESS"},
    {"name": "render", "status": "SUCCESS"},
]


def make_manifest(records=None, steps=None, budget=None, spoken=None, **extra):
    storyboard = shipped_storyboard()
    spoken = spoken if spoken is not None else fake_spoken(storyboard)
    records = records if records is not None else [record("front"), record("kitchen")]
    return build_manifest(
        run_id="20261101-120000",
        created_at="2026-11-01T12:00:00Z",
        storyboard=storyboard,
        steps=steps if steps is not None else STEPS_OK,
        budget=budget or {"mode": "free_only", "status": "free"},
        backgrounds={r.location_id: r for r in records},
        spoken=spoken,
        **extra,
    )


def test_honesty_constants() -> None:
    assert BACKGROUND_BASE_METHOD == (
        "ComfyUI SD1.5 two-pass hires-fix "
        "(txt2img 576x1024 -> img2img 1080x1920, denoise 0.5)"
    )
    assert MOTION_METHOD == "software 2D animation (code), not an AI video model"
    assert MUSIC_METHOD == (
        "synthesized by code, rights-clear, no samples or third-party tracks"
    )
    assert SUBTITLES_METHOD == "Pillow burn-in from Edge word-boundary timings"
    assert COST == {"amount": "0", "paid_requests": 0}
    assert POST_RESIZE_LABEL == "lanczos 1.333x, not a diffusion pass"


def test_a_good_manifest_is_valid_and_serialisable() -> None:
    manifest = make_manifest()

    assert validate_manifest(manifest) == []
    assert json.loads(json.dumps(manifest, ensure_ascii=False)) == manifest
    assert manifest["overall_status"] == "SUCCESS"
    assert manifest["cost"] == {"amount": "0", "paid_requests": 0}
    assert manifest["methods"]["motion"] == MOTION_METHOD
    assert manifest["methods"]["background"] == BACKGROUND_BASE_METHOD
    assert manifest["voice"]["timing_sources"] == {"word_boundary": 9}
    assert manifest["seeds"] == {"front": 11, "kitchen": 11}
    assert manifest["prompt_ids"] == {"front": "p1", "kitchen": "p1"}
    assert manifest["budget"] == {"mode": "free_only", "status": "free"}
    assert manifest["storyboard"]["lines"]["c2"] == "Ngon như ngày xưa đấy!"
    assert manifest["methods"]["voice"] == (
        "Edge TTS vi-VN-HoaiMyNeural, vi-VN-NamMinhNeural "
        "(online free service, not local)"
    )


def test_the_manifest_carries_steps_timeline_and_outputs() -> None:
    storyboard = shipped_storyboard()
    timeline = resolve_timeline(storyboard, fake_spoken(storyboard))

    manifest = make_manifest(
        timeline=timeline,
        audio_qc={"integrated_lufs": -14.1},
        validation={"problems": []},
        render={"size": "1080x1920", "fps": 30, "render_seconds": 301.5},
        outputs={"mp4": "film.mp4", "sha256": "cd" * 32},
    )

    assert manifest["steps"] == STEPS_OK
    assert manifest["timeline"]["total_ms"] == timeline.total_ms
    assert manifest["timeline"]["frame_count"] == timeline.frame_count
    assert [t["kind"] for t in manifest["timeline"]["transitions"]] == [
        "steam_dissolve",
        "whip_pan",
        "cut",
        "cut",
    ]
    assert manifest["audio_qc"] == {"integrated_lufs": -14.1}
    assert manifest["render"]["render_seconds"] == 301.5
    assert manifest["outputs"]["mp4"] == "film.mp4"
    assert validate_manifest(manifest) == []


def test_a_failed_step_makes_the_run_failed_but_still_honest() -> None:
    manifest = make_manifest(
        steps=[
            {"name": "voice", "status": "SUCCESS"},
            {"name": "x", "status": "FAILED"},
        ],
        budget={},
        records=[],
        spoken={},
    )

    assert manifest["overall_status"] == "FAILED"
    assert (
        validate_manifest(manifest) == []
    )  # no free budget needed to report a failure


def test_motion_must_be_exactly_the_honest_string() -> None:
    manifest = make_manifest()
    manifest["methods"]["motion"] = "AI video model"

    assert any("methods.motion" in p for p in validate_manifest(manifest))


def test_music_and_subtitles_strings_are_strict() -> None:
    manifest = make_manifest()
    manifest["methods"]["music"] = "licensed stock tracks"
    manifest["methods"]["subtitles"] = "ffmpeg drawtext"

    problems = validate_manifest(manifest)

    assert any("methods.music" in p for p in problems)
    assert any("methods.subtitles" in p for p in problems)


@pytest.mark.parametrize(
    "cost",
    [{"amount": "0.01", "paid_requests": 0}, {"amount": "0", "paid_requests": 1}, {}],
)
def test_cost_must_be_zero_and_unpaid(cost) -> None:
    manifest = make_manifest()
    manifest["cost"] = cost

    assert any(p.startswith("cost must be") for p in validate_manifest(manifest))


def test_timing_source_must_be_a_known_value_and_match_the_summary() -> None:
    manifest = make_manifest()
    manifest["voice"]["lines"][0]["timing_source"] = "guess"
    bad = make_manifest()
    bad["voice"]["timing_sources"] = {"word_boundary": 8, "sentence_estimate": 1}

    assert any("timing_source 'guess'" in p for p in validate_manifest(manifest))
    assert any("does not match" in p for p in validate_manifest(bad))


def test_estimated_timings_are_reported_not_hidden() -> None:
    storyboard = shipped_storyboard()
    spoken = fake_spoken(storyboard)
    spoken["n1"] = replace(spoken["n1"], timing_source="sentence_estimate")

    manifest = make_manifest(spoken=spoken)

    assert manifest["voice"]["timing_sources"] == {
        "sentence_estimate": 1,
        "word_boundary": 8,
    }
    assert validate_manifest(manifest) == []


def test_tier_b_and_c_are_described_and_cross_checked() -> None:
    manifest = make_manifest(records=[record("front", "B"), record("kitchen", "C")])

    text = manifest["methods"]["background"]
    assert text.startswith(BACKGROUND_BASE_METHOD)
    assert "tier B: explicit tiled VAE encode/decode" in text
    assert (
        f"tier C: pass 2 at 810x1440 with tiled VAE, post_resize: {POST_RESIZE_LABEL}"
        in text
    )
    assert validate_manifest(manifest) == []


def test_background_honesty_is_strict() -> None:
    tier_c = make_manifest(records=[record("front", "C")])
    tier_c["backgrounds"][0]["post_resize"] = "lanczos"
    resized_a = make_manifest(
        records=[record("front", "A", post_resize=POST_RESIZE_LABEL)]
    )
    untiled_b = make_manifest(records=[record("front", "B", tiled_vae=False)])
    hidden_c = make_manifest(records=[record("front", "C")])
    hidden_c["methods"]["background"] = BACKGROUND_BASE_METHOD
    wrong_base = make_manifest()
    wrong_base["methods"]["background"] = "SDXL img2img"

    assert any("label is wrong" in p for p in validate_manifest(tier_c))
    assert any("only on tier C" in p for p in validate_manifest(resized_a))
    assert any("tiled_vae" in p for p in validate_manifest(untiled_b))
    assert any("tier C post-resize" in p for p in validate_manifest(hidden_c))
    assert any("two-pass hires-fix" in p for p in validate_manifest(wrong_base))


def test_a_successful_run_needs_a_free_budget() -> None:
    manifest = make_manifest(budget={"mode": "free_only", "status": "blocked"})

    assert any("budget.status 'free'" in p for p in validate_manifest(manifest))


def test_the_voice_must_name_edge_as_an_online_service() -> None:
    manifest = make_manifest()
    manifest["methods"]["voice"] = "local voice"

    assert any("Edge TTS" in p for p in validate_manifest(manifest))
    assert validate_manifest({}) == ["methods is missing"]


def test_description_helpers() -> None:
    storyboard = shipped_storyboard()

    assert voice_method(storyboard.voices).startswith("Edge TTS vi-VN-HoaiMyNeural")
    assert background_method([]) == BACKGROUND_BASE_METHOD
    assert background_method([record("front", "A")]) == BACKGROUND_BASE_METHOD


def test_write_validate_file_and_hash(tmp_path: Path) -> None:
    manifest = make_manifest()
    path = write_manifest(manifest, tmp_path / "manifest.json")

    assert json.loads(path.read_text(encoding="utf-8")) == manifest
    assert validate_manifest_file(path)
    broken = copy.deepcopy(manifest)
    broken["methods"]["motion"] = "magic"
    bad = write_manifest(broken, tmp_path / "bad.json")
    assert not validate_manifest_file(bad)
    data = tmp_path / "data.bin"
    data.write_bytes(b"abc")
    assert file_sha256(data) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
