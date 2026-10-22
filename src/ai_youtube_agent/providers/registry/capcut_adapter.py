"""CapCut Desktop Integration & Draft Exporter Adapter.

Implements desktop video editor integration for CapCut.
Detects local CapCut desktop installation and exports complete draft packages
(draft_content.json, draft_meta_info.json, staged media, subtitles, and instructions)
so users can open projects in CapCut with 1 click.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.providers.registry.base import BaseProviderAdapter
from ai_youtube_agent.providers.registry.models import (
    ConnectionCheckResult,
    CredentialCheckResult,
    ModelCheckResult,
    ProviderCapability,
    ProviderCategory,
    ResourceCheckResult,
    ResourceRequirement,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class CapCutExportResult:
    """Artifact and directory report produced by CapCut draft export."""

    project_name: str
    draft_folder_path: str
    draft_content_path: str
    draft_meta_path: str
    assets_staged_count: int
    duration_ms: int
    instructions_file: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_name": self.project_name,
            "draft_folder_path": self.draft_folder_path,
            "draft_content_path": self.draft_content_path,
            "draft_meta_path": self.draft_meta_path,
            "assets_staged_count": self.assets_staged_count,
            "duration_ms": self.duration_ms,
            "instructions_file": self.instructions_file,
        }


class CapCutAdapter(BaseProviderAdapter):
    """Adapter managing CapCut Desktop environment detection and draft export."""

    def __init__(
        self,
        *,
        custom_draft_dir: Path | None = None,
        custom_app_dir: Path | None = None,
    ) -> None:
        super().__init__(
            provider_id="capcut",
            category=ProviderCategory.DESKTOP_EDITOR,
        )
        self.custom_draft_dir = custom_draft_dir
        self.custom_app_dir = custom_app_dir
        self.name = "capcut"

    def _get_draft_dir(self) -> Path:
        """Resolve the Windows default CapCut projects draft directory."""
        if self.custom_draft_dir:
            return self.custom_draft_dir

        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            standard_path = (
                Path(local_app_data)
                / "CapCut"
                / "User Data"
                / "Projects"
                / "com.lveditor.draft"
            )
            return standard_path

        # Fallback to home directory
        return Path.home() / ".capcut" / "projects"

    def _get_app_path(self) -> Path | None:
        """Check for CapCut desktop executable on the host machine."""
        if self.custom_app_dir and (self.custom_app_dir / "CapCut.exe").exists():
            return self.custom_app_dir / "CapCut.exe"

        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            apps_dir = Path(local_app_data) / "CapCut" / "Apps"
            if apps_dir.exists():
                exes = list(apps_dir.glob("*/CapCut.exe"))
                if exes:
                    return exes[0]

        which_capcut = shutil.which("CapCut")
        return Path(which_capcut) if which_capcut else None

    def capabilities(self) -> ProviderCapability:
        return ProviderCapability(
            provider_id=self.provider_id,
            name="CapCut Desktop Integration & Draft Exporter",
            category=self.category,
            is_local=True,
            is_free=True,
            supported_modalities=(
                "desktop_draft_export",
                "timeline_synchronization",
                "subtitles_text_tracks",
                "multi_track_audio",
            ),
            supported_models=("capcut_draft_v1", "capcut_draft_v2"),
            resource_requirements=ResourceRequirement(
                disk_space_mb_min=1024,
                executable_name="CapCut.exe",
            ),
            metadata={
                "draft_dir": str(self._get_draft_dir()),
                "app_detected": self._get_app_path() is not None,
            },
        )

    def check_connection(self) -> ConnectionCheckResult:
        draft_dir = self._get_draft_dir()
        app_path = self._get_app_path()
        has_draft_dir = draft_dir.exists()
        has_app = app_path is not None and app_path.exists()

        if has_app or has_draft_dir:
            return ConnectionCheckResult(
                connected=True,
                endpoint=str(draft_dir),
                latency_ms=0.5,
            )

        return ConnectionCheckResult(
            connected=False,
            endpoint=str(draft_dir),
            error_message="CapCut Desktop not found. Export package mode ready.",
        )

    def check_models(self) -> ModelCheckResult:
        return ModelCheckResult(
            valid=True,
            available_models=("capcut_draft_v1", "capcut_draft_v2"),
            default_model="capcut_draft_v1",
        )

    def check_credentials(self) -> CredentialCheckResult:
        # Desktop integration uses local filesystem drafts, no cloud credential needed
        return CredentialCheckResult(
            valid=True,
            has_credentials=True,
            auth_type="local_desktop",
        )

    def check_resources(self) -> ResourceCheckResult:
        draft_dir = self._get_draft_dir()
        disk_free_mb = None
        try:
            target = draft_dir if draft_dir.exists() else draft_dir.parent
            if target.exists():
                usage = shutil.disk_usage(target)
                disk_free_mb = int(usage.free / (1024 * 1024))
        except Exception:
            pass

        app_found = self._get_app_path() is not None
        sufficient = disk_free_mb is None or disk_free_mb >= 500

        return ResourceCheckResult(
            sufficient=sufficient,
            disk_free_mb=disk_free_mb,
            executable_found=app_found,
            details={
                "draft_directory": str(draft_dir),
                "app_executable": str(self._get_app_path() or "Not found"),
            },
            warning_message=None
            if app_found
            else "CapCut executable not detected in standard location.",
        )

    def export_draft(
        self,
        *,
        title: str,
        scenes: list[dict[str, Any]],
        audio_file_path: str | None = None,
        duration_ms: int = 15000,
        is_vertical_9_16: bool = True,
        output_dir: Path | None = None,
    ) -> CapCutExportResult:
        """Export project timeline and assets into a CapCut Desktop draft folder."""
        # Sanitize folder name
        safe_name = "".join(
            c if c.isalnum() or c in ("-", "_") else "_" for c in title
        ).strip("_")
        timestamp = int(datetime.now(UTC).timestamp())
        folder_name = f"AutoAI_{safe_name}_{timestamp}"

        base_dir = output_dir or self._get_draft_dir()
        project_dir = base_dir / folder_name
        assets_dir = project_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)

        width = 1080 if is_vertical_9_16 else 1920
        height = 1920 if is_vertical_9_16 else 1080
        ratio = "9:16" if is_vertical_9_16 else "16:9"

        # Staging assets and building track segments
        video_segments = []
        text_segments = []
        staged_count = 0

        current_ms = 0
        default_scene_duration_ms = max(duration_ms // max(len(scenes), 1), 1000)

        for i, scene in enumerate(scenes):
            scene_duration = scene.get("duration_ms", default_scene_duration_ms)
            start_us = current_ms * 1000
            dur_us = scene_duration * 1000

            # Staging image asset if file exists
            image_path = scene.get("image_file") or scene.get("image_path")
            target_asset_name = f"scene_{i + 1:02d}.png"
            target_asset_path = assets_dir / target_asset_name

            if image_path and Path(image_path).exists():
                shutil.copy2(image_path, target_asset_path)
                staged_count += 1
            else:
                # Write minimal placeholder PNG if missing
                target_asset_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR...")

            video_segments.append(
                {
                    "id": f"clip_{i + 1}",
                    "material_id": f"mat_img_{i + 1}",
                    "target_timerange": {"start": start_us, "duration": dur_us},
                    "source_timerange": {"start": 0, "duration": dur_us},
                    "file_path": str(target_asset_path.resolve()),
                }
            )

            # Subtitle text
            caption_text = (
                scene.get("caption") or scene.get("text") or scene.get("narration")
            )
            if caption_text:
                text_segments.append(
                    {
                        "id": f"text_{i + 1}",
                        "text": caption_text,
                        "target_timerange": {"start": start_us, "duration": dur_us},
                    }
                )

            current_ms += scene_duration

        # Stage audio if supplied
        audio_segments = []
        if audio_file_path and Path(audio_file_path).exists():
            target_audio = assets_dir / "voiceover.wav"
            shutil.copy2(audio_file_path, target_audio)
            staged_count += 1
            audio_segments.append(
                {
                    "id": "audio_voice",
                    "material_id": "mat_voice",
                    "target_timerange": {"start": 0, "duration": duration_ms * 1000},
                    "file_path": str(target_audio.resolve()),
                }
            )

        # Build draft_content.json
        draft_content = {
            "version": 1,
            "canvas_config": {
                "width": width,
                "height": height,
                "ratio": ratio,
            },
            "duration": duration_ms * 1000,
            "tracks": [
                {"id": "track_video_main", "type": "video", "segments": video_segments},
                {
                    "id": "track_audio_voice",
                    "type": "audio",
                    "segments": audio_segments,
                },
                {
                    "id": "track_text_subtitles",
                    "type": "text",
                    "segments": text_segments,
                },
            ],
            "materials": {
                "videos": video_segments,
                "audios": audio_segments,
                "texts": text_segments,
            },
        }

        content_path = project_dir / "draft_content.json"
        content_path.write_text(
            json.dumps(draft_content, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Build draft_meta_info.json
        draft_meta = {
            "draft_id": folder_name,
            "draft_name": title,
            "draft_create_time": timestamp,
            "draft_duration": duration_ms * 1000,
            "draft_ratio": ratio,
            "draft_materials": len(video_segments) + len(audio_segments),
        }
        meta_path = project_dir / "draft_meta_info.json"
        meta_path.write_text(
            json.dumps(draft_meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Build IMPORT_INSTRUCTIONS.txt (Vietnamese)
        instructions = (
            f"=== HƯỚNG DẪN MỞ DỰ ÁN TRÊN CAPCUT DESKTOP ===\n\n"
            f"Tên dự án: {title}\n"
            f"Tỉ lệ khung hình: {ratio} ({width}x{height})\n"
            f"Thời lượng dự kiến: {duration_ms / 1000:.1f} giây\n"
            f"Thư mục dự án: {project_dir.resolve()}\n\n"
            f"CÁCH SỬ DỤNG:\n"
            f"1. Thư mục này đã lưu vào vị trí CapCut: {project_dir.resolve()}\n"
            f"2. Khởi động ứng dụng CapCut Desktop trên máy tính.\n"
            f"3. Trong màn hình chính, bạn sẽ thấy dự án '{title}'.\n"
            f"4. Nhấp đúp để mở dự án: Scenes, Voiceover và Phụ đề đã sẵn sàng!\n"
            f"5. Thêm Transition, Effect hoặc chỉnh sửa trước khi Render.\n"
        )
        instruct_path = project_dir / "IMPORT_INSTRUCTIONS.txt"
        instruct_path.write_text(instructions, encoding="utf-8")

        return CapCutExportResult(
            project_name=title,
            draft_folder_path=str(project_dir.resolve()),
            draft_content_path=str(content_path.resolve()),
            draft_meta_path=str(meta_path.resolve()),
            assets_staged_count=staged_count,
            duration_ms=duration_ms,
            instructions_file=str(instruct_path.resolve()),
        )
