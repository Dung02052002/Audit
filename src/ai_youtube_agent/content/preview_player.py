"""Preview Player component and service.

(Prompt Pack v8, prompt L-132), context C11 Review.

Builds the actual HTML5 video playback component and configuration model for
previewing an exact rendered ``VIDEO`` artifact version:
- Probes the rendered MP4 file via ``probe_mp4`` for exact duration and container
  metadata.
- Resolves canvas dimensions (9:16 vertical for Shorts, 16:9 horizontal for LongForm),
  frame rate, safe area guides, and chapter seek markers.
- Converts SRT subtitles to WebVTT on demand via ``srt_to_vtt`` so browser
  ``<track kind="subtitles">`` elements play both SRT and WebVTT caption tracks.
- Renders a self-contained, XSS-safe HTML5 video player component via
  ``render_preview_player_html`` with version/checksum badges, safe-area guide
  overlay, playback speed controls, and chapter scrubbing buttons.
"""

import html
import re
from dataclasses import dataclass
from typing import Any, Final

from ai_youtube_agent.content.longform_metadata import LongFormMetadata
from ai_youtube_agent.content.longform_timeline import format_chapter_timestamp
from ai_youtube_agent.content.preview_artifact import (
    PreviewArtifactDescriptor,
    PreviewArtifactService,
    PreviewError,
    PreviewErrorCode,
    PreviewSnapshot,
)
from ai_youtube_agent.content.shorts_metadata import ShortsMetadata
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import ArtifactRepository
from ai_youtube_agent.core.db.repositories.longform import LongFormProjectRepository
from ai_youtube_agent.core.db.repositories.shorts import ShortsProjectRepository
from ai_youtube_agent.providers.video_render import probe_mp4

_SRT_TIMESTAMP_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(\d{2}:\d{2}:\d{2}),(\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}),(\d{3})(.*)$"
)


@dataclass(frozen=True)
class SafeAreaBox:
    """Safe-area overlay guide in permille (0..1000) of canvas dimensions."""

    top_permille: int
    bottom_permille: int
    left_permille: int
    right_permille: int

    def as_dict(self) -> dict[str, int]:
        return {
            "top_permille": self.top_permille,
            "bottom_permille": self.bottom_permille,
            "left_permille": self.left_permille,
            "right_permille": self.right_permille,
        }


@dataclass(frozen=True)
class PlayerChapterMarker:
    """Interactive chapter marker for seeking within the preview player."""

    index: int
    title: str
    start_ms: int
    end_ms: int
    timestamp_label: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "timestamp_label": self.timestamp_label,
        }


@dataclass(frozen=True)
class PreviewPlayerConfig:
    """Structured configuration for the video preview player component (#132)."""

    content_item_id: str
    channel_id: str
    content_type: ContentType
    title: str
    aspect_ratio: str
    width: int
    height: int
    fps: int
    duration_ms: int
    timescale: int
    video: PreviewArtifactDescriptor
    video_src: str
    poster_src: str | None
    subtitle_track_src: str | None
    language: str
    safe_area: SafeAreaBox
    chapters: tuple[PlayerChapterMarker, ...]
    playback_rates: tuple[float, ...]
    bundle_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_item_id": self.content_item_id,
            "channel_id": self.channel_id,
            "content_type": self.content_type.value,
            "title": self.title,
            "aspect_ratio": self.aspect_ratio,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "duration_ms": self.duration_ms,
            "timescale": self.timescale,
            "video": self.video.as_dict(),
            "video_src": self.video_src,
            "poster_src": self.poster_src,
            "subtitle_track_src": self.subtitle_track_src,
            "language": self.language,
            "safe_area": self.safe_area.as_dict(),
            "chapters": [c.as_dict() for c in self.chapters],
            "playback_rates": list(self.playback_rates),
            "bundle_sha256": self.bundle_sha256,
        }


def srt_to_vtt(srt_text: str) -> str:
    """Convert SubRip (SRT) subtitle text into WebVTT for HTML5 ``<track>`` playback."""
    stripped = (
        srt_text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    )
    if stripped.startswith("WEBVTT"):
        return stripped + "\n"

    out_lines: list[str] = ["WEBVTT", ""]
    for line in stripped.split("\n"):
        match = _SRT_TIMESTAMP_LINE_RE.match(line.strip())
        if match is not None:
            start_hms, start_ms, end_hms, end_ms, rest = match.groups()
            out_lines.append(f"{start_hms}.{start_ms} --> {end_hms}.{end_ms}{rest}")
        else:
            out_lines.append(line)
    return "\n".join(out_lines).rstrip() + "\n"


def render_preview_player_html(config: PreviewPlayerConfig) -> str:
    """Render the self-contained HTML5 video player component for ``config``."""
    esc_item_id = html.escape(config.content_item_id, quote=True)
    esc_title = html.escape(config.title, quote=True)
    esc_video_id = html.escape(config.video.artifact_id, quote=True)
    esc_video_sha = html.escape(config.video.sha256, quote=True)
    esc_bundle_sha = html.escape(config.bundle_sha256, quote=True)
    esc_video_src = html.escape(config.video_src, quote=True)
    esc_aspect = html.escape(config.aspect_ratio, quote=True)
    esc_lang = html.escape(config.language, quote=True)

    poster_attr = (
        f' poster="{html.escape(config.poster_src, quote=True)}"'
        if config.poster_src is not None
        else ""
    )

    track_html = ""
    if config.subtitle_track_src is not None:
        esc_track_src = html.escape(config.subtitle_track_src, quote=True)
        track_html = (
            f'\n      <track kind="subtitles" src="{esc_track_src}" '
            f'srclang="{esc_lang}" label="{esc_lang}" default />'
        )

    top_pct = config.safe_area.top_permille / 10.0
    bottom_pct = config.safe_area.bottom_permille / 10.0
    left_pct = config.safe_area.left_permille / 10.0
    right_pct = config.safe_area.right_permille / 10.0

    chapters_html_items: list[str] = []
    for ch in config.chapters:
        esc_ch_title = html.escape(ch.title, quote=True)
        esc_ch_label = html.escape(ch.timestamp_label, quote=True)
        chapters_html_items.append(
            f'      <li><button type="button" class="chapter-seek-btn" '
            f'data-seek-ms="{ch.start_ms}" data-chapter-index="{ch.index}">'
            f'<span class="chapter-time">{esc_ch_label}</span> '
            f'<span class="chapter-title">{esc_ch_title}</span></button></li>'
        )
    chapters_section = ""
    if chapters_html_items:
        chapters_section = (
            '\n  <nav class="preview-player-chapters" aria-label="Video Chapters">\n'
            '    <ul class="chapter-list">\n'
            + "\n".join(chapters_html_items)
            + "\n    </ul>\n  </nav>"
        )

    rate_options = "\n".join(
        f'        <option value="{rate}"{" selected" if rate == 1.0 else ""}'
        f">{rate}x</option>"
        for rate in config.playback_rates
    )

    return f"""<section class="preview-player-component" \
data-content-item-id="{esc_item_id}" data-content-type="{config.content_type.value}" \
data-aspect-ratio="{esc_aspect}" data-video-artifact-id="{esc_video_id}" \
data-video-version="{config.video.version}" data-video-sha256="{esc_video_sha}" \
data-bundle-sha256="{esc_bundle_sha}">
  <header class="preview-player-header">
    <h2 class="preview-player-title">{esc_title}</h2>
    <div class="preview-player-badges">
      <span class="badge badge-format">{esc_aspect} ({config.width}x{config.height} \
@ {config.fps}fps)</span>
      <span class="badge badge-version">v{config.video.version}</span>
      <span class="badge badge-sha256" title="{esc_video_sha}">sha256:{
        esc_video_sha[:12]
    }</span>
    </div>
  </header>
  <div class="preview-player-stage" \
style="aspect-ratio: {config.width} / {config.height}; max-width: {config.width}px;">
    <video id="preview-video-player" class="preview-video-element" \
width="{config.width}" height="{config.height}" controls preload="metadata" \
playsinline data-duration-ms="{config.duration_ms}"{poster_attr}>
      <source src="{esc_video_src}" type="{
        html.escape(config.video.media_type, quote=True)
    }" />{track_html}
    </video>
    <div class="preview-safe-area-guide" data-safe-area-overlay \
style="top: {top_pct:.1f}%; bottom: {bottom_pct:.1f}%; left: {left_pct:.1f}%; \
right: {right_pct:.1f}%; pointer-events: none;"></div>
  </div>
  <div class="preview-player-controls" role="toolbar" \
aria-label="Preview Player Controls">
    <span class="preview-timecode" data-timecode-display>00:00.000 / {
        format_chapter_timestamp(config.duration_ms)
    }</span>
    <label class="preview-rate-control">Speed
      <select data-playback-rate-select>
{rate_options}
      </select>
    </label>
    <button type="button" data-safe-area-toggle \
aria-pressed="true">Toggle Safe Area</button>
  </div>{chapters_section}
  <script>
    (function() {{
      var root = document.currentScript.parentElement;
      var video = root.querySelector('#preview-video-player');
      var rateSelect = root.querySelector('[data-playback-rate-select]');
      var safeToggle = root.querySelector('[data-safe-area-toggle]');
      var safeOverlay = root.querySelector('[data-safe-area-overlay]');
      if (rateSelect && video) {{
        rateSelect.addEventListener('change', function() {{
          video.playbackRate = parseFloat(rateSelect.value) || 1.0;
        }});
      }}
      if (safeToggle && safeOverlay) {{
        safeToggle.addEventListener('click', function() {{
          var hidden = safeOverlay.style.display === 'none';
          safeOverlay.style.display = hidden ? '' : 'none';
          safeToggle.setAttribute('aria-pressed', hidden ? 'true' : 'false');
        }});
      }}
      var seekButtons = root.querySelectorAll('[data-seek-ms]');
      seekButtons.forEach(function(btn) {{
        btn.addEventListener('click', function() {{
          if (video) {{
            video.currentTime = (parseInt(btn.getAttribute('data-seek-ms'), 10) || 0) \
/ 1000.0;
            video.play();
          }}
        }});
      }});
    }})();
  </script>
</section>
"""


class PreviewPlayerService:
    """Builds the video playback configuration and HTML5 player component (#132)."""

    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        artifact_service: PreviewArtifactService | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._artifacts = artifact_service or PreviewArtifactService(database, storage)

    def build_player(
        self,
        content_item_id: str,
        *,
        video_version: int | None = None,
    ) -> PreviewPlayerConfig:
        """Build ``PreviewPlayerConfig`` bound to the exact rendered artifact.

        The artifact version is the exact one rendered.
        """
        snapshot: PreviewSnapshot = self._artifacts.get_snapshot(
            content_item_id, video_version=video_version
        )
        with self._database.transaction() as connection:
            video_artifact = ArtifactRepository(connection).get(
                snapshot.video.artifact_id
            )
            assert video_artifact is not None
            strategy = StrategyProfileRepository(connection).get_by_channel(
                snapshot.channel_id
            )
            shorts_proj = (
                ShortsProjectRepository(connection).get_by_content_item(content_item_id)
                if snapshot.content_type == ContentType.SHORTS
                else None
            )
            lf_proj = (
                LongFormProjectRepository(connection).get_by_content_item(
                    content_item_id
                )
                if snapshot.content_type == ContentType.LONGFORM
                else None
            )
            meta_artifact = (
                ArtifactRepository(connection).get(snapshot.metadata.artifact_id)
                if snapshot.metadata is not None
                else None
            )

        video_bytes = self._storage.read(video_artifact.uri)
        try:
            mp4_info = probe_mp4(video_bytes)
        except Exception as error:
            raise PreviewError(
                f"rendered video failed MP4 probe: {error}",
                code=PreviewErrorCode.ARTIFACT_CORRUPTED,
            ) from error

        language = "en"
        if strategy is not None and strategy.languages is not None:
            language = strategy.languages.primary

        if snapshot.content_type == ContentType.SHORTS:
            aspect_ratio = "9:16"
            width, height = 1080, 1920
            fps = 30
            safe_area = SafeAreaBox(150, 220, 60, 120)
            if shorts_proj is not None:
                width, height = shorts_proj.config.canvas_size
                fps = shorts_proj.config.fps
                sa = shorts_proj.config.safe_area
                safe_area = SafeAreaBox(
                    sa.top,
                    sa.bottom,
                    sa.left,
                    sa.right,
                )
        else:
            aspect_ratio = "16:9"
            width, height = 1920, 1080
            fps = 30
            safe_area = SafeAreaBox(100, 150, 60, 60)
            if lf_proj is not None:
                width, height = lf_proj.config.canvas_size
                fps = lf_proj.config.fps
                sa = lf_proj.config.safe_area
                safe_area = SafeAreaBox(
                    sa.top,
                    sa.bottom,
                    sa.left,
                    sa.right,
                )

        title = snapshot.title
        chapters: list[PlayerChapterMarker] = []
        if meta_artifact is not None:
            try:
                raw_meta = self._storage.read(meta_artifact.uri)
                if snapshot.content_type == ContentType.SHORTS:
                    s_meta = ShortsMetadata.from_json_bytes(raw_meta)
                    title = s_meta.title
                    language = s_meta.language
                else:
                    l_meta = LongFormMetadata.from_bytes(raw_meta)
                    title = l_meta.title
                    language = l_meta.language
                    for idx, ch in enumerate(l_meta.chapters, start=1):
                        chapters.append(
                            PlayerChapterMarker(
                                index=idx,
                                title=ch.title,
                                start_ms=ch.start_ms,
                                end_ms=ch.end_ms,
                                timestamp_label=format_chapter_timestamp(ch.start_ms),
                            )
                        )
            except Exception:
                pass

        if not chapters and lf_proj is not None and lf_proj.timeline.chapters:
            for idx, ch in enumerate(lf_proj.timeline.chapters, start=1):
                chapters.append(
                    PlayerChapterMarker(
                        index=idx,
                        title=ch.title,
                        start_ms=ch.start_ms,
                        end_ms=ch.end_ms,
                        timestamp_label=format_chapter_timestamp(ch.start_ms),
                    )
                )

        video_src = (
            f"{snapshot.video.stream_url}"
            f"?expected_version={snapshot.video.version}"
            f"&expected_sha256={snapshot.video.sha256}"
        )
        poster_src = (
            f"{snapshot.thumbnail.stream_url}"
            f"?expected_version={snapshot.thumbnail.version}"
            f"&expected_sha256={snapshot.thumbnail.sha256}"
            if snapshot.thumbnail is not None
            else None
        )
        subtitle_track_src = (
            f"/previews/{content_item_id}/subtitles.vtt"
            f"?artifact_id={snapshot.subtitles.artifact_id}"
            f"&expected_sha256={snapshot.subtitles.sha256}"
            if snapshot.subtitles is not None
            else None
        )

        return PreviewPlayerConfig(
            content_item_id=snapshot.content_item_id,
            channel_id=snapshot.channel_id,
            content_type=snapshot.content_type,
            title=title,
            aspect_ratio=aspect_ratio,
            width=width,
            height=height,
            fps=fps,
            duration_ms=mp4_info.duration_ms,
            timescale=mp4_info.timescale,
            video=snapshot.video,
            video_src=video_src,
            poster_src=poster_src,
            subtitle_track_src=subtitle_track_src,
            language=language,
            safe_area=safe_area,
            chapters=tuple(chapters),
            playback_rates=(0.5, 1.0, 1.25, 1.5, 2.0),
            bundle_sha256=snapshot.bundle_sha256,
        )

    def render_player_html(
        self,
        content_item_id: str,
        *,
        video_version: int | None = None,
    ) -> str:
        config = self.build_player(content_item_id, video_version=video_version)
        return render_preview_player_html(config)

    def subtitles_as_vtt(
        self,
        content_item_id: str,
        *,
        artifact_id: str | None = None,
        expected_sha256: str | None = None,
    ) -> tuple[PreviewArtifactDescriptor, str]:
        """Return the item's subtitle artifact converted to WebVTT text.

        For the player ``<track>``.
        """
        if artifact_id is not None:
            desc = self._artifacts.get_artifact(
                content_item_id,
                artifact_id,
                expected_sha256=expected_sha256,
            )
            if desc.kind is not ArtifactKind.SUBTITLES:
                raise PreviewError(
                    f"artifact {artifact_id} is {desc.kind.value}, expected subtitles",
                    code=PreviewErrorCode.ARTIFACT_MISMATCH,
                )
        else:
            snapshot = self._artifacts.get_snapshot(content_item_id)
            if snapshot.subtitles is None:
                raise PreviewError(
                    f"no subtitles artifact exists for item {content_item_id}",
                    code=PreviewErrorCode.ARTIFACT_NOT_FOUND,
                )
            desc = snapshot.subtitles
            if expected_sha256 is not None and desc.sha256 != expected_sha256.lower():
                raise PreviewError(
                    "subtitles sha256 does not match expected_sha256",
                    code=PreviewErrorCode.ARTIFACT_MISMATCH,
                )

        stream = self._artifacts.stream_artifact(content_item_id, desc.artifact_id)
        try:
            text = stream.data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PreviewError(
                f"subtitles artifact {desc.artifact_id} is not valid UTF-8",
                code=PreviewErrorCode.ARTIFACT_CORRUPTED,
            ) from error

        return (desc, srt_to_vtt(text))
