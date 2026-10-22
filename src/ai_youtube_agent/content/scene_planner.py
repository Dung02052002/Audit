"""Scene Breakdown & Shot Planning Engine (Script-to-Film Refactor, Phase 4).

Transforms a ``StoryBible`` and ``ScriptAnalysisOutput`` into a structured
``SceneBreakdownPlan`` composed of ``ScenePlan`` and ``ShotPlan`` records:

- Every ``ScenePlan`` captures:
  - ``scene_id``, ``order_index``, ``section_index``, ``section_kind``
  - ``narrative_purpose`` (opening hook, rising action, turning point, climax,
    resolution/CTA)
  - ``character_ids``, ``location_id``, ``time_of_day``, ``lighting_mood``
  - ``primary_action``, ``narration_and_dialogue``, ``dialogue_segments``
  - ``shots`` (1..N ``ShotPlan`` records with ``shot_type``, ``camera_motion``,
    ``visual_prompt``, ``framing_rect``, and exact ``start_ms``/``end_ms``)
  - ``required_asset_kinds``, ``assigned_asset_ids``, ``provenance_ids``,
    ``rights_record_ids``, ``continuity_notes``, and ``status``
- Enforces visual continuity by injecting the exact character appearance/wardrobe
  and location setting/lighting from ``StoryBible`` into each
  ``ShotPlan.visual_prompt``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Final

from ai_youtube_agent.content.script import SectionKind
from ai_youtube_agent.content.story_bible import (
    CharacterSpec,
    DialogueSegment,
    LocationSpec,
    ProductionMode,
    RawSceneBeat,
    ScriptAnalysisOutput,
    StoryBible,
)
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.providers.video_render import PixelRect

SHORTS_CANVAS: Final[tuple[int, int]] = (1080, 1920)
LONGFORM_CANVAS: Final[tuple[int, int]] = (1920, 1080)


class ShotType(StrEnum):
    """Cinematic camera framing for a shot."""

    ESTABLISHING_SHOT = "establishing_shot"
    WIDE_SHOT = "wide_shot"
    MEDIUM_SHOT = "medium_shot"
    CLOSE_UP = "close_up"
    INSERT_SHOT = "insert_shot"


class CameraMotion(StrEnum):
    """Camera movement or simulated pan/zoom framing for a shot."""

    STATIC = "static"
    SLOW_PAN_RIGHT = "slow_pan_right"
    SLOW_PAN_LEFT = "slow_pan_left"
    ZOOM_IN = "zoom_in"
    ZOOM_OUT = "zoom_out"
    TRACKING = "tracking"


class SceneStatus(StrEnum):
    """Lifecycle status of an individual ScenePlan."""

    PLANNED = "planned"
    ASSETS_READY = "assets_ready"
    TIMELINE_BOUND = "timeline_bound"
    RENDERED = "rendered"
    FAILED = "failed"


@dataclass(frozen=True)
class ShotPlan:
    """One camera shot inside a ``ScenePlan``."""

    shot_id: str
    scene_id: str
    order_index: int
    shot_type: ShotType
    camera_motion: CameraMotion
    subject: str
    character_ids: tuple[str, ...]
    location_id: str
    visual_prompt: str
    start_ms: int
    end_ms: int
    duration_ms: int
    framing_rect: PixelRect
    assigned_asset_id: str | None = None

    def __post_init__(self) -> None:
        if not self.shot_id.strip():
            raise ValueError("shot_id must not be empty")
        if not self.scene_id.strip():
            raise ValueError("scene_id must not be empty")
        if not isinstance(self.shot_type, ShotType):
            raise TypeError("shot_type must be a ShotType")
        if not isinstance(self.camera_motion, CameraMotion):
            raise TypeError("camera_motion must be a CameraMotion")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("shot end_ms must be > start_ms >= 0")
        if self.duration_ms != self.end_ms - self.start_ms:
            raise ValueError("duration_ms must equal end_ms - start_ms")

    def with_asset(self, asset_id: str) -> ShotPlan:
        return replace(self, assigned_asset_id=asset_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "shot_id": self.shot_id,
            "scene_id": self.scene_id,
            "order_index": self.order_index,
            "shot_type": self.shot_type.value,
            "camera_motion": self.camera_motion.value,
            "subject": self.subject,
            "character_ids": list(self.character_ids),
            "location_id": self.location_id,
            "visual_prompt": self.visual_prompt,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_ms": self.duration_ms,
            "framing_rect": self.framing_rect.to_dict(),
            "assigned_asset_id": self.assigned_asset_id,
        }


@dataclass(frozen=True)
class ScenePlan:
    """Detailed cinematic breakdown for a single scene."""

    scene_id: str
    order_index: int
    section_index: int
    section_kind: SectionKind
    title: str
    narrative_purpose: str
    character_ids: tuple[str, ...]
    location_id: str
    time_of_day: str
    lighting_mood: str
    primary_action: str
    narration_and_dialogue: str
    dialogue_segments: tuple[DialogueSegment, ...]
    shots: tuple[ShotPlan, ...]
    start_ms: int
    end_ms: int
    target_duration_ms: int
    transition_in: str
    transition_out: str
    required_asset_kinds: tuple[str, ...]
    assigned_asset_ids: tuple[str, ...] = ()
    provenance_ids: tuple[str, ...] = ()
    rights_record_ids: tuple[str, ...] = ()
    continuity_notes: tuple[str, ...] = ()
    status: SceneStatus = SceneStatus.PLANNED

    def __post_init__(self) -> None:
        if not self.scene_id.strip():
            raise ValueError("scene_id must not be empty")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("scene end_ms must be > start_ms >= 0")
        if self.target_duration_ms != self.end_ms - self.start_ms:
            raise ValueError("target_duration_ms must equal end_ms - start_ms")
        if not self.shots:
            raise ValueError("every ScenePlan must contain at least one ShotPlan")

    def with_bound_assets(
        self,
        *,
        asset_ids: Sequence[str],
        provenance_ids: Sequence[str],
        rights_record_ids: Sequence[str],
        status: SceneStatus = SceneStatus.ASSETS_READY,
    ) -> ScenePlan:
        asset_tuple = tuple(asset_ids)
        primary_asset = asset_tuple[0] if asset_tuple else None
        updated_shots = tuple(
            shot.with_asset(
                asset_tuple[idx % len(asset_tuple)]
                if asset_tuple
                else (shot.assigned_asset_id or "")
            )
            if primary_asset is not None
            else shot
            for idx, shot in enumerate(self.shots)
        )
        return replace(
            self,
            shots=updated_shots,
            assigned_asset_ids=asset_tuple,
            provenance_ids=tuple(provenance_ids),
            rights_record_ids=tuple(rights_record_ids),
            status=status,
        )

    def with_status(self, status: SceneStatus) -> ScenePlan:
        return replace(self, status=status)

    @property
    def duration_ms(self) -> int:
        return self.target_duration_ms

    @property
    def visual_prompt(self) -> str:
        """The scene's image prompt: its first shot's prompt plus the
        continuity notes that keep characters and places consistent."""
        prompt = self.shots[0].visual_prompt
        if self.continuity_notes:
            prompt += " Continuity: " + "; ".join(self.continuity_notes)
        return prompt

    def as_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "order_index": self.order_index,
            "section_index": self.section_index,
            "section_kind": self.section_kind.value,
            "title": self.title,
            "narrative_purpose": self.narrative_purpose,
            "character_ids": list(self.character_ids),
            "location_id": self.location_id,
            "time_of_day": self.time_of_day,
            "lighting_mood": self.lighting_mood,
            "primary_action": self.primary_action,
            "narration_and_dialogue": self.narration_and_dialogue,
            "dialogue_segments": [seg.as_dict() for seg in self.dialogue_segments],
            "shots": [shot.as_dict() for shot in self.shots],
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "target_duration_ms": self.target_duration_ms,
            "transition_in": self.transition_in,
            "transition_out": self.transition_out,
            "required_asset_kinds": list(self.required_asset_kinds),
            "assigned_asset_ids": list(self.assigned_asset_ids),
            "provenance_ids": list(self.provenance_ids),
            "rights_record_ids": list(self.rights_record_ids),
            "continuity_notes": list(self.continuity_notes),
            "status": self.status.value,
        }


@dataclass(frozen=True)
class SceneBreakdownPlan:
    """Complete scene and shot breakdown for a Script-to-Film run."""

    story_id: str
    content_type: ContentType
    production_mode: ProductionMode
    canvas_width: int
    canvas_height: int
    total_duration_ms: int
    scenes: tuple[ScenePlan, ...]

    def __post_init__(self) -> None:
        if not self.scenes:
            raise ValueError("SceneBreakdownPlan must contain at least one scene")

    @property
    def canvas_size(self) -> tuple[int, int]:
        return self.canvas_width, self.canvas_height

    def with_scenes(self, scenes: Sequence[ScenePlan]) -> SceneBreakdownPlan:
        scenes_tuple = tuple(scenes)
        total = scenes_tuple[-1].end_ms if scenes_tuple else self.total_duration_ms
        return replace(self, scenes=scenes_tuple, total_duration_ms=total)

    def as_dict(self) -> dict[str, Any]:
        return {
            "story_id": self.story_id,
            "content_type": self.content_type.value,
            "production_mode": self.production_mode.value,
            "canvas_width": self.canvas_width,
            "canvas_height": self.canvas_height,
            "total_duration_ms": self.total_duration_ms,
            "scene_count": len(self.scenes),
            "shot_count": sum(len(s.shots) for s in self.scenes),
            "scenes": [s.as_dict() for s in self.scenes],
        }


def _framing_for_shot(
    shot_type: ShotType,
    canvas_width: int,
    canvas_height: int,
) -> PixelRect:
    """Compute a valid framing ``PixelRect``
    within ``(canvas_width, canvas_height)``.
    """
    # Full canvas background/shot rect is standard so background layer covers 100%,
    # while B-roll / inset shots can use framed rects. We return full canvas rect
    # so BACKGROUND role validation in Shorts/LongForm always covers the frame cleanly.
    return PixelRect(x=0, y=0, width=canvas_width, height=canvas_height)


def _narrative_purpose(index: int, total: int, kind: SectionKind) -> str:
    if kind in (SectionKind.HOOK, SectionKind.INTRO) or index == 0:
        return "Establish setting, central character, and opening narrative hook"
    if kind in (SectionKind.CTA, SectionKind.OUTRO) or index == total - 1:
        return "Deliver final resolution and closing call-to-action"
    if index == total - 2:
        return "Climax payoff and emotional resolution of the central arc"
    if index == 1:
        return "Inciting development and rising narrative tension"
    return f"Story progression beat ({index + 1} of {total})"


class SceneBreakdownPlanner:
    """Builds a ``SceneBreakdownPlan`` from a ``ScriptAnalysisOutput`` and optional
    timeline windows.
    """

    def plan(
        self,
        analysis: ScriptAnalysisOutput,
        *,
        scene_windows_ms: Sequence[tuple[int, int]] | None = None,
        canvas_width: int | None = None,
        canvas_height: int | None = None,
    ) -> SceneBreakdownPlan:
        bible = analysis.story_bible
        default_w, default_h = (
            SHORTS_CANVAS
            if bible.content_type is ContentType.SHORTS
            else LONGFORM_CANVAS
        )
        width = canvas_width or default_w
        height = canvas_height or default_h

        sections = analysis.sections
        beats = analysis.beats
        count = len(sections)

        if scene_windows_ms is not None and len(scene_windows_ms) == count:
            windows = list(scene_windows_ms)
        else:
            windows = []
            cursor = 0
            for sec in sections:
                dur_ms = max(2000, (sec.seconds or 5) * 1000)
                windows.append((cursor, cursor + dur_ms))
                cursor += dur_ms

        required_visual = (
            ("image", "voice", "subtitles")
            if bible.production_mode is ProductionMode.STORY_VIDEO
            else ("video_clip", "voice", "subtitles")
        )

        scenes: list[ScenePlan] = []
        for idx in range(count):
            section = sections[idx]
            beat = beats[min(idx, len(beats) - 1)]
            start_ms, end_ms = windows[idx]
            duration_ms = end_ms - start_ms
            scene_id = f"scene_{idx + 1:02d}"

            location = bible.location_by_id(beat.location_hint) or (
                bible.locations[0] if bible.locations else None
            )
            loc_id = location.location_id if location else "loc_default_1"
            loc_name = location.name if location else "Primary Stage"
            time_of_day = location.time_of_day if location else beat.time_of_day
            lighting_mood = (
                location.lighting_mood if location else "Balanced cinematic lighting"
            )

            active_chars: list[CharacterSpec] = []
            for cid in beat.character_ids:
                cspec = bible.character_by_id(cid)
                if cspec is not None:
                    active_chars.append(cspec)
            if not active_chars and bible.characters:
                active_chars = [bible.characters[0]]

            shots = self._plan_shots_for_scene(
                scene_id=scene_id,
                scene_index=idx,
                total_scenes=count,
                start_ms=start_ms,
                end_ms=end_ms,
                beat=beat,
                characters=active_chars,
                location=location,
                bible=bible,
                canvas_width=width,
                canvas_height=height,
            )

            continuity_notes: list[str] = []
            for char in active_chars:
                if char.role != "narrator":
                    continuity_notes.append(
                        f"Character {char.name} ({char.character_id}): "
                        f"{char.appearance}; wardrobe: {char.wardrobe}"
                    )
            continuity_notes.append(
                f"Location {loc_name} ({loc_id}): {time_of_day}, {lighting_mood}"
            )

            transition_in = "fade_in" if idx == 0 else "cross_dissolve"
            transition_out = "fade_out" if idx == count - 1 else "cut"

            scenes.append(
                ScenePlan(
                    scene_id=scene_id,
                    order_index=idx,
                    section_index=idx,
                    section_kind=section.kind,
                    title=section.title or beat.heading or f"Scene {idx + 1}",
                    narrative_purpose=_narrative_purpose(idx, count, section.kind),
                    character_ids=tuple(c.character_id for c in active_chars),
                    location_id=loc_id,
                    time_of_day=time_of_day,
                    lighting_mood=lighting_mood,
                    primary_action=beat.action_summary or section.text[:140],
                    narration_and_dialogue=section.text,
                    dialogue_segments=beat.segments,
                    shots=shots,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    target_duration_ms=duration_ms,
                    transition_in=transition_in,
                    transition_out=transition_out,
                    required_asset_kinds=required_visual,
                    continuity_notes=tuple(continuity_notes),
                    status=SceneStatus.PLANNED,
                )
            )

        total_duration_ms = scenes[-1].end_ms if scenes else 0
        return SceneBreakdownPlan(
            story_id=bible.story_id,
            content_type=bible.content_type,
            production_mode=bible.production_mode,
            canvas_width=width,
            canvas_height=height,
            total_duration_ms=total_duration_ms,
            scenes=tuple(scenes),
        )

    def _plan_shots_for_scene(
        self,
        *,
        scene_id: str,
        scene_index: int,
        total_scenes: int,
        start_ms: int,
        end_ms: int,
        beat: RawSceneBeat,
        characters: Sequence[CharacterSpec],
        location: LocationSpec | None,
        bible: StoryBible,
        canvas_width: int,
        canvas_height: int,
    ) -> tuple[ShotPlan, ...]:
        duration_ms = end_ms - start_ms
        # Determine how many shots fit naturally: 1 shot for short scenes (< 6s),
        # 2 shots for medium scenes (6..14s), 3 shots for longer scenes (>= 14s).
        if duration_ms < 6000:
            shot_count = 1
        elif duration_ms < 14000:
            shot_count = 2
        else:
            shot_count = 3

        visible_chars = [c for c in characters if c.role != "narrator"] or list(
            characters
        )
        char_summary = "; ".join(
            f"{c.name} ({c.appearance}, wearing {c.wardrobe})" for c in visible_chars
        )
        loc_summary = (
            f"{location.name} ({location.setting_description}, {location.time_of_day}, "
            f"{location.lighting_mood})"
            if location is not None
            else "Primary story setting with balanced cinematic lighting"
        )
        loc_id = location.location_id if location is not None else "loc_default_1"
        char_ids = tuple(c.character_id for c in visible_chars)

        shot_sequence: list[tuple[ShotType, CameraMotion]] = []
        if scene_index == 0:
            shot_sequence.append((ShotType.ESTABLISHING_SHOT, CameraMotion.ZOOM_IN))
            shot_sequence.append((ShotType.MEDIUM_SHOT, CameraMotion.SLOW_PAN_RIGHT))
            shot_sequence.append((ShotType.CLOSE_UP, CameraMotion.STATIC))
        elif scene_index == total_scenes - 1:
            shot_sequence.append((ShotType.MEDIUM_SHOT, CameraMotion.SLOW_PAN_LEFT))
            shot_sequence.append((ShotType.CLOSE_UP, CameraMotion.ZOOM_OUT))
            shot_sequence.append((ShotType.WIDE_SHOT, CameraMotion.ZOOM_OUT))
        else:
            shot_sequence.append((ShotType.WIDE_SHOT, CameraMotion.SLOW_PAN_RIGHT))
            shot_sequence.append((ShotType.MEDIUM_SHOT, CameraMotion.TRACKING))
            shot_sequence.append((ShotType.CLOSE_UP, CameraMotion.ZOOM_IN))

        shots: list[ShotPlan] = []
        base_dur = duration_ms // shot_count
        remainder = duration_ms % shot_count
        cursor = start_ms

        for s_idx in range(shot_count):
            this_dur = base_dur + (remainder if s_idx == shot_count - 1 else 0)
            shot_start = cursor
            shot_end = cursor + this_dur
            cursor = shot_end

            shot_type, motion = shot_sequence[s_idx % len(shot_sequence)]
            seg_text = (
                beat.segments[min(s_idx, len(beat.segments) - 1)].text
                if beat.segments
                else beat.spoken_text
            )
            prompt = (
                f"[{bible.visual_style} | {bible.aspect_ratio} | {shot_type.value} | "
                f"camera:{motion.value}] Setting: {loc_summary}. "
                f"Characters: {char_summary}. "
                f"Action/Beat: {seg_text[:180]}"
            )
            shots.append(
                ShotPlan(
                    shot_id=f"{scene_id}_shot_{s_idx + 1:02d}",
                    scene_id=scene_id,
                    order_index=s_idx,
                    shot_type=shot_type,
                    camera_motion=motion,
                    subject=visible_chars[0].name if visible_chars else "Scene Subject",
                    character_ids=char_ids,
                    location_id=loc_id,
                    visual_prompt=prompt,
                    start_ms=shot_start,
                    end_ms=shot_end,
                    duration_ms=this_dur,
                    framing_rect=_framing_for_shot(
                        shot_type, canvas_width, canvas_height
                    ),
                )
            )

        return tuple(shots)
