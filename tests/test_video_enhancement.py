"""Tests for Video Enhancement & Frame Rate Interpolation Engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_youtube_agent.providers.video_enhancement import (
    SURVEYED_REPOSITORIES,
    EnhancedVideoResult,
    InterpolationMethod,
    VideoInterpolationRequest,
)


def test_video_interpolation_request_validations(tmp_path: Path) -> None:
    non_existent = tmp_path / "does_not_exist.mp4"
    with pytest.raises(FileNotFoundError):
        VideoInterpolationRequest(input_file=non_existent, output_file=tmp_path / "out.mp4")

    dummy_file = tmp_path / "dummy.mp4"
    dummy_file.write_bytes(b"dummy")

    with pytest.raises(ValueError, match="target_fps .* must be greater than source_fps"):
        VideoInterpolationRequest(
            input_file=dummy_file,
            output_file=tmp_path / "out.mp4",
            source_fps=24.0,
            target_fps=24.0,
        )


def test_enhanced_video_result_serialization(tmp_path: Path) -> None:
    in_p = tmp_path / "in.mp4"
    out_p = tmp_path / "out.mp4"
    res = EnhancedVideoResult(
        input_file=in_p,
        output_file=out_p,
        original_fps=8.0,
        result_fps=24.0,
        original_frame_count=16,
        result_frame_count=48,
        duration_seconds=2.0,
        elapsed_seconds=1.5,
        file_size_bytes=1024,
        decode_valid=True,
        method=InterpolationMethod.OPTICAL_FLOW_MCI,
        artifacts_detected={"ghosting": False, "flicker": False},
    )
    d = res.as_dict()
    assert d["original_fps"] == 8.0
    assert d["result_fps"] == 24.0
    assert d["decode_valid"] is True
    assert d["method"] == "optical_flow_mci"


def test_surveyed_repositories_registry() -> None:
    assert len(SURVEYED_REPOSITORIES) == 4
    repo_names = [r.repo_name for r in SURVEYED_REPOSITORIES]
    assert any("RIFE" in name for name in repo_names)
    assert any("Real-ESRGAN" in name for name in repo_names)
    assert any("VideoHelperSuite" in name for name in repo_names)
    assert any("LTX-Video" in name for name in repo_names)

    # Check hardware assessment for 6 GB GTX 1660 SUPER
    ltx = next(r for r in SURVEYED_REPOSITORIES if "LTX-Video" in r.repo_name)
    assert "INCOMPATIBLE_OOM" in ltx.hardware_status_gtx1660s
    assert ltx.vram_requirement_gb >= 10.0

    rife = next(r for r in SURVEYED_REPOSITORIES if "RIFE" in r.repo_name)
    assert "FEASIBLE_SEQUENTIAL" in rife.hardware_status_gtx1660s
    assert rife.vram_requirement_gb <= 2.0
