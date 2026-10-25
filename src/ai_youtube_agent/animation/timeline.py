"""Timeline: the storyboard resolved to absolute milliseconds.

``resolve_timeline`` places the spoken lines (their real durations and word
times from ``tts``), then the actions, shots, transitions and sound effects of
every scene on one clock. A scene lasts ``max(min_duration_ms, last line end +
hold)``. A transition is centred on the boundary between two scenes and does
not shorten the film: the outgoing scene is drawn d/2 longer and the incoming
one starts d/2 earlier.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ai_youtube_agent.animation.errors import StoryboardError
from ai_youtube_agent.animation.storyboard import (
    SCENE_TAIL_HOLD_MS,
    ActionSpec,
    Anchor,
    CameraMoveSpec,
    FocusSpec,
    Framing,
    NarrationLine,
    Scene,
    Storyboard,
)
from ai_youtube_agent.animation.subtitles import WordTiming
from ai_youtube_agent.animation.tts import SpokenLine

WORD_CLEARANCE_MS: Final[int] = 100
# Sound effect of an action: (effect, fraction of the action duration).
ACTION_SFX: Final[dict[str, tuple[tuple[str, float], ...]]] = {
    "wipe_counter": (("cloth", 0.25),),
    "reach_take": (("cloth", 0.3),),
    "thumbs_up": (("cloth", 0.2),),
    "add_topping": (("clink", 0.5),),
    "ladle_to_bowl": (("clink", 0.55),),
    "place_bowl": (("bowl_place", 0.72),),
    "sit": (("chair", 0.6),),
}
TRANSITION_SFX: Final[dict[str, str]] = {
    "whip_pan": "whoosh",
    "steam_dissolve": "steam_hiss",
}


@dataclass(frozen=True)
class ResolvedLine:
    """A spoken line placed on the film clock; words carry absolute times."""

    line_id: str
    scene_id: str
    voice: str
    text: str
    start_ms: int
    end_ms: int
    pause_after_ms: int
    words: tuple[WordTiming, ...]
    timing_source: str
    wav_path: Path
    lead_trim_ms: int = 0


@dataclass(frozen=True)
class ResolvedAction:
    id: str
    scene_id: str
    character: str
    kind: str
    start_ms: int
    end_ms: int
    ease_in_ms: int
    ease_out_ms: int
    params: Mapping[str, Any]


@dataclass(frozen=True)
class ResolvedEffect:
    """A scenery effect (steam, glow, passers-by) with absolute times."""

    id: str
    scene_id: str
    kind: str
    start_ms: int
    end_ms: int
    params: Mapping[str, Any]


@dataclass(frozen=True)
class ResolvedMove:
    """A camera move with absolute times."""

    kind: str
    t0_ms: int
    t1_ms: int
    to: Mapping[str, Any]
    from_: Mapping[str, Any] | None
    ease: str | None


@dataclass(frozen=True)
class ResolvedShot:
    id: str
    scene_id: str
    start_ms: int
    end_ms: int
    framing: Framing
    focus: FocusSpec
    moves: tuple[ResolvedMove, ...]
    location: str


@dataclass(frozen=True)
class ResolvedScene:
    scene: Scene
    index: int
    start_ms: int
    end_ms: int
    # The frames drawn for the scene: it starts early and ends late by half of
    # the neighbouring transitions.
    render_start_ms: int
    render_end_ms: int

    @property
    def id(self) -> str:
        return self.scene.id

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class ResolvedTransition:
    from_scene: str
    to_scene: str
    kind: str
    boundary_ms: int
    start_ms: int
    end_ms: int
    direction: str | None

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class SfxEvent:
    kind: str
    t_ms: int
    source: str


@dataclass(frozen=True)
class ResolvedTimeline:
    storyboard: Storyboard
    scenes: tuple[ResolvedScene, ...]
    lines: tuple[ResolvedLine, ...]
    actions: tuple[ResolvedAction, ...]
    shots: tuple[ResolvedShot, ...]
    transitions: tuple[ResolvedTransition, ...]
    sfx: tuple[SfxEvent, ...]
    total_ms: int
    fps: int
    effects: tuple[ResolvedEffect, ...] = ()

    @property
    def frame_count(self) -> int:
        return math.ceil(self.total_ms * self.fps / 1000)

    def frame_time_ms(self, frame: int) -> float:
        return frame * 1000 / self.fps

    def scene_at(self, t_ms: float) -> ResolvedScene:
        for scene in self.scenes:
            if t_ms < scene.end_ms:
                return scene
        return self.scenes[-1]

    def shot_at(self, t_ms: float) -> ResolvedShot:
        scene = self.scene_at(t_ms)
        shots = [shot for shot in self.shots if shot.scene_id == scene.id]
        for shot in shots:
            if t_ms < shot.end_ms:
                return shot
        return shots[-1]

    def transition_at(self, t_ms: float) -> ResolvedTransition | None:
        for transition in self.transitions:
            if (
                transition.duration_ms > 0
                and transition.start_ms <= t_ms < transition.end_ms
            ):
                return transition
        return None

    def line(self, line_id: str) -> ResolvedLine:
        for line in self.lines:
            if line.line_id == line_id:
                return line
        raise KeyError(line_id)

    def actions_of(self, scene_id: str) -> tuple[ResolvedAction, ...]:
        return tuple(a for a in self.actions if a.scene_id == scene_id)

    def words(self) -> tuple[WordTiming, ...]:
        return tuple(word for line in self.lines for word in line.words)


def _spoken_index(
    spoken: Mapping[str, SpokenLine] | Sequence[SpokenLine],
) -> Mapping[str, SpokenLine]:
    if isinstance(spoken, Mapping):
        return spoken
    return {item.line_id: item for item in spoken}


def _anchor_time(
    anchor: Anchor,
    *,
    lines: Mapping[str, ResolvedLine],
    actions: Mapping[str, ResolvedAction],
    scene_start: int,
    scene_duration: int | None,
    for_line: bool,
) -> int | None:
    """Scene-relative start of ``anchor``, or ``None`` if it depends on something
    not placed yet. Absolute-time lines/actions are looked up by absolute clock."""
    offset = anchor.offset_ms
    if anchor.scene_ms is not None:
        return anchor.scene_ms + offset
    if anchor.scene_end_ms is not None:
        if scene_duration is None:
            return None
        return scene_duration - anchor.scene_end_ms + offset
    if anchor.after_action is not None:
        action = actions.get(anchor.after_action)
        return None if action is None else action.end_ms - scene_start + offset
    ref = lines.get(anchor.line or "")
    if ref is None:
        return None
    if anchor.word is not None:
        word = ref.words[anchor.word]
        edge = word.end_ms if anchor.edge == "end" else word.start_ms
        return edge - scene_start + offset
    if anchor.edge == "end":
        pause = ref.pause_after_ms if for_line else 0
        return ref.end_ms - scene_start + pause + offset
    return ref.start_ms - scene_start + offset


def _place_lines(
    scene: Scene,
    spoken: Mapping[str, SpokenLine],
    issues: list[str],
) -> tuple[dict[str, ResolvedLine], int]:
    """Lines with scene-relative times (start 0 = scene start) and the scene length."""
    placed: dict[str, ResolvedLine] = {}
    last_end = 0
    for line in scene.lines:
        audio = spoken.get(line.id)
        if audio is None:
            issues.append(f"no spoken audio for line '{line.id}'")
            continue
        start = _anchor_time(
            line.at,
            lines=placed,
            actions={},
            scene_start=0,
            scene_duration=None,
            for_line=True,
        )
        if start is None:
            issues.append(f"line '{line.id}': its anchor cannot be resolved")
            continue
        if start < 0:
            issues.append(f"line '{line.id}': starts before its scene ({start} ms)")
            continue
        if placed and start < last_end:
            issues.append(
                f"line '{line.id}': starts at {start} ms while the previous line "
                f"is still speaking until {last_end} ms"
            )
        placed[line.id] = _resolved_line(line, scene.id, audio, start)
        last_end = max(last_end, placed[line.id].end_ms)
    duration = max(scene.min_duration_ms, last_end + SCENE_TAIL_HOLD_MS)
    return placed, duration


def _resolved_line(
    line: NarrationLine, scene_id: str, audio: SpokenLine, start: int
) -> ResolvedLine:
    return ResolvedLine(
        line_id=line.id,
        scene_id=scene_id,
        voice=line.voice,
        text=line.text,
        start_ms=start,
        end_ms=start + audio.duration_ms,
        pause_after_ms=line.pause_after_ms,
        words=tuple(
            WordTiming(w.index, w.text, start + w.start_ms, start + w.end_ms)
            for w in audio.words
        ),
        timing_source=audio.timing_source,
        wav_path=audio.wav_path,
        lead_trim_ms=audio.lead_trim_ms,
    )


def _shift_line(line: ResolvedLine, delta: int) -> ResolvedLine:
    return ResolvedLine(
        line.line_id,
        line.scene_id,
        line.voice,
        line.text,
        line.start_ms + delta,
        line.end_ms + delta,
        line.pause_after_ms,
        tuple(word.shifted(delta) for word in line.words),
        line.timing_source,
        line.wav_path,
        line.lead_trim_ms,
    )


def _place_actions(
    scene: Scene,
    scene_start: int,
    duration: int,
    lines: Mapping[str, ResolvedLine],
    issues: list[str],
) -> list[ResolvedAction]:
    pending: list[ActionSpec] = list(scene.actions)
    placed: dict[str, ResolvedAction] = {}
    scene_end = scene_start + duration
    while pending:
        progressed = False
        for action in list(pending):
            relative = _anchor_time(
                action.at,
                lines=lines,
                actions=placed,
                scene_start=scene_start,
                scene_duration=duration,
                for_line=False,
            )
            if relative is None:
                continue
            pending.remove(action)
            progressed = True
            start = scene_start + relative
            if relative < 0 or start >= scene_end:
                issues.append(
                    f"action '{action.id}': starts at {relative} ms, outside its "
                    f"scene (0-{duration} ms)"
                )
                continue
            placed[action.id] = ResolvedAction(
                id=action.id,
                scene_id=scene.id,
                character=action.character,
                kind=action.kind,
                start_ms=start,
                end_ms=min(start + action.duration_ms, scene_end),
                ease_in_ms=action.ease_in_ms,
                ease_out_ms=action.ease_out_ms,
                params=dict(action.params),
            )
        if not progressed:
            names = ", ".join(a.id for a in pending)
            issues.append(f"actions with a circular or missing anchor: {names}")
            break
    return [placed[a.id] for a in scene.actions if a.id in placed]


def _place_effects(
    scene: Scene,
    scene_start: int,
    duration: int,
    lines: Mapping[str, ResolvedLine],
    actions: Sequence[ResolvedAction],
    issues: list[str],
) -> list[ResolvedEffect]:
    by_id = {action.id: action for action in actions}
    scene_end = scene_start + duration
    placed: list[ResolvedEffect] = []
    for effect in scene.effects:
        relative = _anchor_time(
            effect.at,
            lines=lines,
            actions=by_id,
            scene_start=scene_start,
            scene_duration=duration,
            for_line=False,
        )
        if relative is None or relative < 0 or scene_start + relative >= scene_end:
            issues.append(f"effect '{effect.id}': starts outside its scene")
            continue
        start = scene_start + relative
        placed.append(
            ResolvedEffect(
                effect.id,
                scene.id,
                effect.kind,
                start,
                min(start + effect.duration_ms, scene_end),
                dict(effect.params),
            )
        )
    return placed


def _resolve_move(move: CameraMoveSpec, shot_start: int) -> ResolvedMove:
    return ResolvedMove(
        kind=move.kind,
        t0_ms=shot_start + move.t0,
        t1_ms=shot_start + move.t1,
        to=dict(move.to),
        from_=dict(move.from_) if move.from_ is not None else None,
        ease=move.ease,
    )


def _action_sfx(action: ResolvedAction) -> list[SfxEvent]:
    events = [
        SfxEvent(
            kind,
            action.start_ms + round((action.end_ms - action.start_ms) * fraction),
            action.id,
        )
        for kind, fraction in ACTION_SFX.get(action.kind, ())
    ]
    extra = action.params.get("sfx")
    for kind in [extra] if isinstance(extra, str) else extra or []:
        events.append(SfxEvent(str(kind), action.start_ms, action.id))
    return events


def resolve_timeline(
    storyboard: Storyboard,
    spoken_lines: Mapping[str, SpokenLine] | Sequence[SpokenLine],
) -> ResolvedTimeline:
    """Place everything of ``storyboard`` on the film clock.

    ``spoken_lines`` are the results of ``tts.synthesize_lines``. Raises
    ``StoryboardError`` listing every problem: a missing line audio, speech that
    overlaps or runs into a transition, an action outside its scene, or a total
    outside ``format.duration_ms_range``.
    """
    spoken = _spoken_index(spoken_lines)
    issues: list[str] = []
    scene_lengths: list[tuple[Scene, dict[str, ResolvedLine], int]] = []
    for scene in storyboard.scenes:
        placed, duration = _place_lines(scene, spoken, issues)
        scene_lengths.append((scene, placed, duration))

    scenes: list[ResolvedScene] = []
    lines: list[ResolvedLine] = []
    actions: list[ResolvedAction] = []
    effects: list[ResolvedEffect] = []
    shots: list[ResolvedShot] = []
    sfx: list[SfxEvent] = []
    cursor = 0
    boundaries: list[int] = []
    for index, (scene, placed, duration) in enumerate(scene_lengths):
        start, end = cursor, cursor + duration
        absolute = {key: _shift_line(line, start) for key, line in placed.items()}
        lines.extend(absolute.values())
        scene_actions = _place_actions(scene, start, duration, absolute, issues)
        actions.extend(scene_actions)
        effects.extend(
            _place_effects(scene, start, duration, absolute, scene_actions, issues)
        )
        for action in scene_actions:
            sfx.extend(_action_sfx(action))
        starts = [start + shot.start_ms for shot in scene.shots]
        for position, shot in enumerate(scene.shots):
            shot_end = starts[position + 1] if position + 1 < len(starts) else end
            if starts[position] >= end:
                issues.append(f"shot '{shot.id}': starts after its scene ends")
                continue
            shots.append(
                ResolvedShot(
                    id=shot.id,
                    scene_id=scene.id,
                    start_ms=starts[position],
                    end_ms=min(shot_end, end),
                    framing=shot.framing,
                    focus=shot.focus,
                    moves=tuple(_resolve_move(m, starts[position]) for m in shot.moves),
                    location=scene.shot_location(shot),
                )
            )
        scenes.append(ResolvedScene(scene, index, start, end, start, end))
        boundaries.append(end)
        cursor = end

    transitions: list[ResolvedTransition] = []
    for index, scene in enumerate(storyboard.scenes[:-1]):
        spec = scene.transition_out
        kind = spec.kind if spec is not None else "cut"
        half = (spec.duration_ms if spec is not None else 0) // 2
        boundary = boundaries[index]
        transitions.append(
            ResolvedTransition(
                scene.id,
                storyboard.scenes[index + 1].id,
                kind,
                boundary,
                boundary - half,
                boundary + half,
                spec.direction if spec is not None else None,
            )
        )
        if kind in TRANSITION_SFX:
            sfx.append(
                SfxEvent(TRANSITION_SFX[kind], boundary - half, f"transition:{kind}")
            )
    for transition in transitions:
        clearance = transition.duration_ms // 2 + WORD_CLEARANCE_MS
        for word in (w for line in lines for w in line.words):
            if (
                word.end_ms > transition.boundary_ms - clearance
                and word.start_ms < transition.boundary_ms + clearance
            ):
                issues.append(
                    f"word '{word.text}' ({word.start_ms}-{word.end_ms} ms) is "
                    f"within {clearance} ms of the {transition.kind} between "
                    f"'{transition.from_scene}' and '{transition.to_scene}'"
                )

    scenes = _with_render_windows(scenes, transitions)
    total = cursor
    low, high = storyboard.format.duration_ms_range
    if not low <= total <= high:
        issues.append(f"the film lasts {total} ms, outside {low}-{high} ms")
    if issues:
        raise StoryboardError(issues)
    return ResolvedTimeline(
        storyboard=storyboard,
        scenes=tuple(scenes),
        lines=tuple(lines),
        actions=tuple(actions),
        shots=tuple(shots),
        transitions=tuple(transitions),
        sfx=tuple(sorted(sfx, key=lambda e: (e.t_ms, e.kind))),
        total_ms=total,
        fps=storyboard.format.fps,
        effects=tuple(effects),
    )


def _with_render_windows(
    scenes: list[ResolvedScene], transitions: Sequence[ResolvedTransition]
) -> list[ResolvedScene]:
    windowed: list[ResolvedScene] = []
    for scene in scenes:
        start, end = scene.start_ms, scene.end_ms
        if scene.index > 0:
            start = max(0, min(start, transitions[scene.index - 1].start_ms))
        if scene.index < len(transitions):
            end = max(end, transitions[scene.index].end_ms)
        windowed.append(
            ResolvedScene(
                scene.scene, scene.index, scene.start_ms, scene.end_ms, start, end
            )
        )
    return windowed


