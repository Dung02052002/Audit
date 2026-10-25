"""Frame renderer: the film as a pure function of the frame index.

``FrameRenderer.render(i)`` composes, in this order: the AI plate in the camera
(parallax crop, depth-of-field blur), the drawn floor, soft contact shadows,
furniture, props and characters sorted by their ground line (all vectors drawn
at the exact scale of the frame), steam and light, the colour grade, vignette
and grain; two scenes blended during a transition; the word-timed subtitles in
screen space; and the final fade. Nothing is random per run: seeds come from
ids, so a frame depends only on its index.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from PIL import Image, ImageChops, ImageDraw

from ai_youtube_agent.animation.actions import (
    BOWL_H,
    HOLDS,
    POT_H,
    PROP_SCALE,
    CharacterFrame,
    Choreography,
    Envelope,
    PropState,
)
from ai_youtube_agent.animation.camera import (
    STAGE_H,
    STAGE_W,
    CameraState,
    ShotCamera,
    build_shot_camera,
    world_to_screen,
)
from ai_youtube_agent.animation.characters import (
    CharacterRender,
    HeldItem,
    draw_character,
    paste_sprite,
)
from ai_youtube_agent.animation.easing import clamp, lerp, smoothstep
from ai_youtube_agent.animation.errors import AnimationError
from ai_youtube_agent.animation.fonts import FontFiles
from ai_youtube_agent.animation.props import (
    PropLook,
    prop_extent,
    render_prop,
)
from ai_youtube_agent.animation.rig import RIG, get_style
from ai_youtube_agent.animation.scenery import (
    CREAM_HAZE,
    GROUND_TOP,
    PlateInfo,
    apply_grade,
    broth_arc,
    draw_dust,
    draw_floor,
    draw_glow,
    draw_shadow,
    draw_steam,
    draw_walker,
    dust_motes,
    floor_colour,
    grain_tiles,
    passersby,
    prepare_plate,
    render_plate,
    steam_puffs,
    vignette,
)
from ai_youtube_agent.animation.storyboard import FocusSpec
from ai_youtube_agent.animation.subtitles import (
    CueSource,
    SubtitleCue,
    build_cues,
    cue_at,
    font_measure,
    layout_cue,
    render_cue,
)
from ai_youtube_agent.animation.timeline import (
    ResolvedEffect,
    ResolvedShot,
    ResolvedTimeline,
    ResolvedTransition,
)
from ai_youtube_agent.animation.transitions import blend_frames, transition_progress

Point = tuple[float, float]
Box = tuple[int, int, int, int]
FADE_RGB: Final[tuple[int, int, int]] = (14, 9, 7)
SIGN_TEXT: Final[str] = "Quán Ăn Nhỏ"
HANGING_KINDS: Final[frozenset[str]] = frozenset({"lantern", "sign", "hanging_ladle"})


@dataclass(frozen=True)
class NamedBox:
    """A box in frame pixels with a name (a character id or a text label)."""

    name: str
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def area(self) -> int:
        return max(0, self.x1 - self.x0) * max(0, self.y1 - self.y0)

    def intersects(self, other: tuple[int, int, int, int]) -> bool:
        return not (
            self.x1 <= other[0]
            or other[2] <= self.x0
            or self.y1 <= other[1]
            or other[3] <= self.y0
        )


@dataclass(frozen=True)
class FrameMeta:
    """What a frame shows, for the composition checks of the validator."""

    frame: int
    t_ms: float
    scene_id: str
    shot_id: str
    transition: str | None
    characters: tuple[NamedBox, ...]
    faces: tuple[NamedBox, ...]
    text_boxes: tuple[NamedBox, ...]
    caption: Box | None
    fade: float
    elapsed_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        def boxes(items: tuple[NamedBox, ...]) -> list[dict[str, Any]]:
            return [{"name": b.name, "box": [b.x0, b.y0, b.x1, b.y1]} for b in items]

        return {
            "frame": self.frame,
            "t_ms": round(self.t_ms, 2),
            "scene": self.scene_id,
            "shot": self.shot_id,
            "transition": self.transition,
            "characters": boxes(self.characters),
            "faces": boxes(self.faces),
            "text": boxes(self.text_boxes),
            "caption": list(self.caption) if self.caption else None,
            "fade": round(self.fade, 3),
        }


@dataclass(frozen=True)
class RenderAssets:
    """Everything the renderer reads from disk or earlier steps.

    ``plates`` maps a location id to its 1080x1920 plate (a path or a PIL
    image), ``fonts`` is the Vietnamese-capable font pair, ``envelopes`` the
    loudness of spoken lines by id (optional: the mouth then follows the word
    times only).
    """

    plates: Mapping[str, Any]
    fonts: FontFiles
    envelopes: Mapping[str, Envelope] = field(default_factory=dict)
    sign_text: str = SIGN_TEXT


@dataclass
class _LayerMeta:
    characters: list[NamedBox] = field(default_factory=list)
    faces: list[NamedBox] = field(default_factory=list)
    text_boxes: list[NamedBox] = field(default_factory=list)
    shot_id: str = ""
    scene_id: str = ""


def _seed(*parts: str) -> int:
    return zlib.crc32("|".join(parts).encode("utf-8"))


def _clip_box(
    box: tuple[float, float, float, float], size: tuple[int, int]
) -> Box | None:
    x0, y0 = max(0, math.floor(box[0])), max(0, math.floor(box[1]))
    x1, y1 = min(size[0], math.ceil(box[2])), min(size[1], math.ceil(box[3]))
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


class FrameRenderer:
    """Renders any frame of a resolved timeline, independent of the others."""

    def __init__(
        self,
        timeline: ResolvedTimeline,
        assets: RenderAssets,
        *,
        size: tuple[int, int] = (STAGE_W, STAGE_H),
        fps: int | None = None,
        voice_characters: Mapping[str, str] | None = None,
        preferred_ss: float = 2.0,
    ) -> None:
        if size[0] * STAGE_H != size[1] * STAGE_W:
            raise AnimationError(f"the frame size {size} is not 9:16")
        self.timeline = timeline
        self.assets = assets
        self.size = size
        self.fps = fps or timeline.fps
        self.preferred_ss = preferred_ss
        self.fs = size[0] / STAGE_W
        self.choreo = Choreography(
            timeline,
            voice_characters=voice_characters,
            envelopes=assets.envelopes,
        )
        self._plates: dict[str, PlateInfo] = {}
        self._cameras: dict[str, ShotCamera] = {}
        self._cues: tuple[SubtitleCue, ...] = build_cues(
            [CueSource(line.line_id, line.words) for line in timeline.lines]
        )
        self._measure = font_measure(assets.fonts.bold)
        self._layouts: dict[int, Any] = {}
        self._cue_images: dict[tuple[int, int | None], tuple[Any, tuple[int, int]]] = {}
        self._scene_shots: dict[str, tuple[ResolvedShot, ...]] = {}
        for shot in timeline.shots:
            self._scene_shots.setdefault(shot.scene_id, ())
            self._scene_shots[shot.scene_id] += (shot,)

    # --- public API ----------------------------------------

    @property
    def frame_count(self) -> int:
        return self.timeline.frame_count

    @property
    def cues(self) -> tuple[SubtitleCue, ...]:
        return self._cues

    def render(self, index: int) -> tuple[Any, FrameMeta]:
        """The RGB frame ``index`` and what it shows."""
        return self.render_time(self.timeline.frame_time_ms(index), index)

    def render_time(
        self, t_ms: float, index: int | None = None
    ) -> tuple[Any, FrameMeta]:
        index = round(t_ms * self.fps / 1000) if index is None else index
        transition = self.timeline.transition_at(t_ms)
        if transition is not None and transition.kind != "cut":
            frame, meta = self._blend(transition, t_ms)
            transition_kind: str | None = transition.kind
        else:
            scene = self.timeline.scene_at(t_ms)
            frame, meta = self._layer(scene.id, t_ms)
            transition_kind = None
        caption = self._draw_subtitle(frame, t_ms)
        fade = self._fade(t_ms)
        if fade < 1.0:
            frame = Image.blend(Image.new("RGB", frame.size, FADE_RGB), frame, fade)
        return frame, FrameMeta(
            frame=index,
            t_ms=t_ms,
            scene_id=meta.scene_id,
            shot_id=meta.shot_id,
            transition=transition_kind,
            characters=tuple(meta.characters),
            faces=tuple(meta.faces),
            text_boxes=tuple(meta.text_boxes),
            caption=caption,
            fade=fade,
        )

    def render_stills(
        self, times_ms: Sequence[float]
    ) -> list[tuple[float, Any, FrameMeta]]:
        """Frames at the given times (the nearest frame of each)."""
        stills = []
        for t in times_ms:
            index = min(self.frame_count - 1, max(0, round(t * self.fps / 1000)))
            image, meta = self.render(index)
            stills.append((self.timeline.frame_time_ms(index), image, meta))
        return stills

    def contact_sheet(
        self,
        times_ms: Sequence[float] | None = None,
        *,
        columns: int = 6,
        thumb_width: int = 200,
        label: bool = True,
    ) -> Any:
        """One image with a thumbnail per time (default: a frame every 1.5 s)."""
        if times_ms is None:
            times_ms = [
                i * 1500.0 for i in range(int(self.timeline.total_ms // 1500) + 1)
            ]
            times_ms = [*times_ms, self.timeline.total_ms - 40]
        stills = self.render_stills(times_ms)
        thumb_height = round(thumb_width * self.size[1] / self.size[0])
        rows = math.ceil(len(stills) / columns)
        pad = 8
        label_h = 20 if label else 0
        sheet = Image.new(
            "RGB",
            (
                columns * (thumb_width + pad) + pad,
                rows * (thumb_height + label_h + pad) + pad,
            ),
            (28, 24, 22),
        )
        draw = ImageDraw.Draw(sheet)
        for position, (t, image, meta) in enumerate(stills):
            col, row = position % columns, position // columns
            x = pad + col * (thumb_width + pad)
            y = pad + row * (thumb_height + label_h + pad)
            sheet.paste(
                image.resize((thumb_width, thumb_height), Image.Resampling.LANCZOS),
                (x, y + label_h),
            )
            if label:
                draw.text(
                    (x + 2, y + 3),
                    f"{t / 1000:5.2f}s {meta.shot_id}",
                    fill=(235, 226, 210),
                )
        return sheet

    # --- cameras ----------------------------------------

    def _shot_of(self, scene_id: str, t_ms: float) -> ResolvedShot:
        shots = self._scene_shots[scene_id]
        for shot in shots:
            if t_ms < shot.end_ms:
                return shot
        return shots[-1]

    def _camera(self, shot: ResolvedShot) -> ShotCamera:
        camera = self._cameras.get(shot.id)
        if camera is None:
            camera = build_shot_camera(
                shot,
                lambda t, s=shot: self._focus_point(s, t),
                self.fps,
                seed=_seed(shot.id),
            )
            self._cameras[shot.id] = camera
        return camera

    def _focus_point(self, shot: ResolvedShot, t_ms: float) -> Point:
        focus: FocusSpec = shot.focus
        scene_id = shot.scene_id
        runtime = self.choreo.scene(scene_id)
        names = list(focus.characters) or ([focus.character] if focus.character else [])
        visible = set(self.choreo.visible_characters(scene_id, shot.location))
        names = [n for n in names if n in visible] or names
        if names:
            points = []
            for name in names:
                if name not in runtime.tracks:
                    continue
                x, y = runtime.tracks[name].position(t_ms)
                scale = get_style(runtime.tracks[name].rig).scale
                yaw = runtime.tracks[name].yaw(t_ms)
                sit = runtime.tracks[name].sit(t_ms)
                forward = 40.0 if yaw >= 0 else -40.0
                if focus.part == "head":
                    height = -(RIG.hip_y + RIG.head_center_y) - RIG.sit_drop * sit
                    points.append((x, y - height * scale))
                elif focus.part == "hands":
                    points.append(
                        (x + forward * scale, y - (300 - RIG.sit_drop * sit) * scale)
                    )
                else:
                    points.append((x, y - (330 - RIG.sit_drop * sit * 0.6) * scale))
            if points:
                return (
                    sum(p[0] for p in points) / len(points),
                    sum(p[1] for p in points) / len(points),
                )
        if focus.prop and focus.prop in runtime.props:
            prop = runtime.props[focus.prop]
            lift = POT_H + 30 if prop.kind == "pot" else 60
            return (prop.x, prop.y - lift)
        if focus.x is not None and focus.y is not None:
            return (focus.x * STAGE_W, focus.y * STAGE_H)
        return (STAGE_W / 2, STAGE_H / 2)

    # --- plates ----------------------------------------

    def _plate(self, location: str) -> PlateInfo:
        info = self._plates.get(location)
        if info is None:
            source = self.assets.plates.get(location)
            if source is None:
                raise AnimationError(f"no background plate for location '{location}'")
            image = source if hasattr(source, "convert") else Image.open(Path(source))
            info = prepare_plate(image)
            self._plates[location] = info
        return info

    # --- one scene layer ----------------------------------------

    def _to_screen(self, cam: CameraState, point: Point) -> Point:
        return world_to_screen(cam, point, self.size)

    def _layer(self, scene_id: str, t_ms: float) -> tuple[Any, _LayerMeta]:
        resolved = next(s for s in self.timeline.scenes if s.id == scene_id)
        scene = resolved.scene
        runtime = self.choreo.scene(scene_id)
        shot = self._shot_of(scene_id, t_ms)
        location = shot.location
        cam = self._camera(shot).state_at(t_ms)
        k = cam.zoom * self.fs
        info = self._plate(location)
        frame = render_plate(info, cam, self.size)
        meta = _LayerMeta(shot_id=shot.id, scene_id=scene_id)
        effects = [e for e in self.timeline.effects if e.scene_id == scene_id]

        if scene.grade == "warm":
            draw_dust(frame, dust_motes(t_ms, _seed(scene_id, "dust")), cam)
        for effect in effects:
            if (
                effect.kind == "passersby"
                and effect.start_ms <= t_ms <= effect.end_ms + 1500
            ):
                for walker in passersby(
                    t_ms,
                    effect.start_ms,
                    effect.end_ms,
                    int(effect.params.get("count", 2)),
                    _seed(effect.id),
                ):
                    draw_walker(frame, walker, cam, t_ms)
        draw_floor(frame, cam, location, floor_colour(info, location))

        scene_effects = self._scene_effects(scene_id, runtime, effects)
        drawables = self._collect_drawables(
            scene_id, runtime, location, t_ms, scene_effects
        )
        self._draw_shadows(frame, drawables, cam, k)
        lantern_glow = self._effect_level(effects, "lantern_glow", t_ms)
        pour_targets: list[tuple[Any, ...]] = []
        for _z, kind, payload in sorted(drawables, key=lambda item: item[0]):
            if kind == "prop":
                self._draw_prop(
                    frame, payload, scene, runtime, cam, k, t_ms, lantern_glow, meta
                )
            elif kind == "steam":
                self._draw_steam(frame, payload, runtime, cam, k, t_ms, scene_id)
            else:
                render = self._draw_character(
                    frame, payload, runtime, cam, k, t_ms, meta, location
                )
                if render is not None:
                    pour_targets.append((payload, render))
        self._draw_effects(
            frame,
            scene_effects,
            runtime,
            cam,
            k,
            t_ms,
            pour_targets,
            lantern_glow,
            scene_id,
        )
        frame = apply_grade(frame, scene.grade)
        frame = ImageChops.multiply(frame, vignette(self.size))
        tiles = grain_tiles(self.size)
        tile = tiles[round(t_ms * self.fps / 1000) % len(tiles)]
        frame = ImageChops.add(frame, tile, 1.0, -128)
        return frame, meta

    # --- drawables ----------------------------------------

    def _scene_effects(
        self, scene_id: str, runtime: Any, effects: Sequence[ResolvedEffect]
    ) -> list[ResolvedEffect]:
        """The scene's effects plus a steady steam for every steaming pot."""
        scene_effects = list(effects)
        steamed = {e.params.get("prop") for e in effects if e.kind == "steam_rise"}
        for prop in runtime.props.values():
            if (
                prop.kind == "pot"
                and prop.state == "steaming"
                and prop.id not in steamed
            ):
                scene_effects.append(
                    ResolvedEffect(
                        f"{scene_id}:{prop.id}:steam",
                        scene_id,
                        "steam_rise",
                        int(runtime.start_ms),
                        int(runtime.end_ms),
                        {"prop": prop.id},
                    )
                )
        return scene_effects

    def _collect_drawables(
        self,
        scene_id: str,
        runtime: Any,
        location: str,
        t_ms: float,
        effects: Sequence[ResolvedEffect] = (),
    ) -> list[tuple[float, str, Any]]:
        items: list[tuple[float, str, Any]] = []
        for name in self.choreo.visible_characters(scene_id, location):
            if name not in runtime.tracks:
                continue
            frame_data = self.choreo.character_frame(scene_id, name, t_ms)
            items.append((frame_data.y + 0.1, "char", frame_data))
        for prop_id, prop in runtime.props.items():
            state = self.choreo.prop_state(scene_id, prop_id, t_ms)
            if state.holder is not None:
                continue
            if state.location not in (None, location):
                continue
            if prop.kind in HANGING_KINDS:
                z = -1.0
            elif state.prop.host and state.prop.host in runtime.props:
                host = runtime.props[state.prop.host]
                z = host.y + (0.6 if prop.kind == "ladle" else 0.5)
            else:
                z = state.y
                if prop.kind == "counter":
                    z += 40.0
            items.append((z, "prop", state))
        for effect in effects:
            if effect.kind not in ("steam_rise", "steam_burst"):
                continue
            prop_id = str(effect.params.get("prop", ""))
            if prop_id not in runtime.props:
                continue
            state = self.choreo.prop_state(scene_id, prop_id, t_ms)
            if state.holder is not None:
                carrier = self.choreo.character_position(scene_id, state.holder, t_ms)
                z = carrier[1] + 0.2
            elif state.prop.host and state.prop.host in runtime.props:
                z = runtime.props[state.prop.host].y + 0.7
            else:
                z = state.y + 0.7
            items.append((z, "steam", effect))
        return items

    def _draw_shadows(
        self,
        frame: Any,
        drawables: Sequence[tuple[float, str, Any]],
        cam: CameraState,
        k: float,
    ) -> None:
        for _z, kind, payload in drawables:
            if kind == "char":
                data: CharacterFrame = payload
                style = get_style(data.rig)
                x, y = self._to_screen(cam, (data.x, data.y))
                widen = 1.0 + 0.3 * data.sitting
                if data.sitting < 0.5:
                    draw_shadow(
                        frame,
                        (x + 6 * k, y),
                        86 * k * style.scale * widen,
                        15 * k * style.scale,
                        0.42,
                    )
            elif kind == "prop":
                state: PropState = payload
                kind_name = state.prop.kind
                if kind_name in ("stool",):
                    x, y = self._to_screen(cam, (state.x, state.y))
                    draw_shadow(
                        frame, (x, y), 62 * k * PROP_SCALE, 13 * k * PROP_SCALE, 0.4
                    )
                elif kind_name in ("table", "counter", "stove"):
                    half = prop_extent(kind_name)[2] * 0.86 * PROP_SCALE
                    x, y = self._to_screen(cam, (state.x, state.y))
                    draw_shadow(frame, (x, y), half * k, 18 * k * PROP_SCALE, 0.4)

    def _draw_prop(
        self,
        frame: Any,
        state: PropState,
        scene: Any,
        runtime: Any,
        cam: CameraState,
        k: float,
        t_ms: float,
        lantern_glow: float,
        meta: _LayerMeta,
    ) -> None:
        prop = state.prop
        anchor = self._to_screen(cam, (state.x, state.y))
        look = PropLook(
            broth=state.broth,
            beef=state.beef,
            herbs=state.herbs,
            scoop=state.scoop,
            t_s=t_ms / 1000,
            bubbling="bubbling" in scene.ambience,
        )
        angle = state.angle
        if prop.kind == "stove":
            look = PropLook(t_s=t_ms / 1000, glow=1.0)
        elif prop.kind == "lantern":
            sway = 2.4 * math.sin(math.tau * 0.33 * t_ms / 1000 + _seed(prop.id) % 7)
            glow = lantern_glow if prop.state == "dim" else 1.0
            look = PropLook(t_s=t_ms / 1000, glow=glow, sway=sway)
            angle = sway
            if glow > 0.02:
                centre = self._to_screen(cam, (state.x, state.y + 108 * PROP_SCALE))
                flick = 1 + 0.06 * math.sin(t_ms / 1000 * 9 + _seed(prop.id) % 5)
                draw_glow(frame, centre, 260 * k * flick, (255, 168, 80), 0.34 * glow)
        elif prop.kind == "sign":
            look = PropLook(
                t_s=t_ms / 1000,
                text=self.assets.sign_text,
                font_file=str(self.assets.fonts.bold),
            )
            angle = 0.7 * math.sin(math.tau * 0.22 * t_ms / 1000)
            box = _clip_box(self._sign_text_box(anchor, k), self.size)
            if box:
                meta.text_boxes.append(NamedBox("sign", *box))
        elif prop.kind == "hanging_ladle":
            angle = 1.6 * math.sin(math.tau * 0.3 * t_ms / 1000 + 1.0)
        sprite = render_prop(
            prop.kind,
            anchor,
            k,
            self.size,
            look,
            angle=angle,
            preferred_ss=self.preferred_ss,
        )
        if sprite is not None:
            image, left, top = sprite
            frame.paste(image, (left, top), image)

    def _sign_text_box(
        self, anchor: Point, k: float
    ) -> tuple[float, float, float, float]:
        """Screen box of the lettering on the sign (the only in-world text)."""
        from PIL import ImageFont

        px = max(8, round(62 * PROP_SCALE * k))
        font = ImageFont.truetype(str(self.assets.fonts.bold), px)
        left, top, right, bottom = font.getbbox(self.assets.sign_text)
        cx, cy = anchor[0], anchor[1] + 90 * k * PROP_SCALE
        width, height = right - left, bottom - top
        return (cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2)

    def _held_items(
        self, data: CharacterFrame, t_ms: float, scene_id: str
    ) -> list[HeldItem]:
        items: list[HeldItem] = []
        runtime = self.choreo.scene(scene_id)
        sf = 1.0 if data.pose.yaw >= 0 else -1.0
        for prop_id in runtime.props:
            state = self.choreo.prop_state(scene_id, prop_id, t_ms)
            if state.holder != data.character or state.hand is None:
                continue
            kind = "ladle" if state.prop.kind == "hanging_ladle" else state.prop.kind
            look = PropLook(
                broth=state.broth,
                beef=state.beef,
                herbs=state.herbs,
                scoop=state.scoop,
                t_s=t_ms / 1000,
                flip=sf,
            )
            angle = self.choreo.held_angle(scene_id, prop_id, t_ms, data.character)
            items.append(HeldItem(kind, state.hand, angle, look))
        if data.pose.tool:
            items.append(
                HeldItem(
                    data.pose.tool,
                    data.pose.tool_hand,
                    data.pose.tool_angle,
                    PropLook(t_s=t_ms / 1000, flip=sf, scoop=0.0),
                )
            )
        return items

    def _draw_character(
        self,
        frame: Any,
        data: CharacterFrame,
        runtime: Any,
        cam: CameraState,
        k: float,
        t_ms: float,
        meta: _LayerMeta,
        location: str,
    ) -> CharacterRender | None:
        style = get_style(data.rig)
        anchor = self._to_screen(cam, (data.x, data.y))
        held = self._held_items(data, t_ms, runtime.scene_id)
        render = draw_character(
            data,
            style,
            anchor,
            k,
            self.size,
            held=held,
            preferred_ss=self.preferred_ss,
        )
        if render is None:
            return None
        paste_sprite(frame, render)
        face = _clip_box(render.face_box, self.size)
        body = _clip_box(render.body_box, self.size)
        if face:
            meta.faces.append(NamedBox(data.character, *face))
        if body:
            meta.characters.append(NamedBox(data.character, *body))
        return render

    # --- effects ----------------------------------------

    @staticmethod
    def _effect_level(
        effects: Sequence[ResolvedEffect], kind: str, t_ms: float
    ) -> float:
        level = 0.0
        for effect in effects:
            if effect.kind == kind:
                level = max(
                    level, smoothstep(effect.start_ms, effect.start_ms + 700, t_ms)
                )
        return level

    def _prop_anchor(self, scene_id: str, prop_id: str, t_ms: float) -> Point:
        """Stage position of a prop's anchor, also while it is in a hand."""
        state = self.choreo.prop_state(scene_id, prop_id, t_ms)
        if state.holder is None or state.hand is None:
            return (state.x, state.y)
        hand = self.choreo.character_world_hand(
            scene_id, state.holder, state.hand, t_ms
        )
        spec = HOLDS.get(state.prop.kind)
        frame_data = self.choreo.character_frame(scene_id, state.holder, t_ms)
        sf = 1.0 if frame_data.pose.yaw >= 0 else -1.0
        scale = frame_data.scale
        if spec is None:
            return hand
        return (hand[0] - sf * spec.grip[0] * scale, hand[1] - spec.grip[1] * scale)

    def _draw_effects(
        self,
        frame: Any,
        effects: Sequence[ResolvedEffect],
        runtime: Any,
        cam: CameraState,
        k: float,
        t_ms: float,
        characters: Sequence[tuple[CharacterFrame, CharacterRender]],
        lantern_glow: float,
        scene_id: str,
    ) -> None:
        scene_effects = effects
        for effect in scene_effects:
            if (
                effect.kind == "broth_splash"
                and effect.start_ms <= t_ms <= effect.end_ms + 300
            ):
                self._draw_splash(frame, effect, runtime, cam, k, t_ms, scene_id)
        self._draw_pours(frame, runtime, cam, k, t_ms, characters)
        stove = next((p for p in runtime.props.values() if p.kind == "stove"), None)
        if stove is not None:
            flick = 0.85 + 0.15 * math.sin(t_ms / 1000 * 11) * math.sin(
                t_ms / 1000 * 6.3
            )
            centre = self._to_screen(cam, (stove.x, stove.y - 250))
            draw_glow(frame, centre, 300 * k, (255, 150, 64), 0.2 * flick)

    def _draw_steam(
        self,
        frame: Any,
        effect: ResolvedEffect,
        runtime: Any,
        cam: CameraState,
        k: float,
        t_ms: float,
        scene_id: str,
    ) -> None:
        prop_id = str(effect.params.get("prop", ""))
        prop = runtime.props[prop_id]
        puffs = steam_puffs(
            t_ms,
            effect.start_ms,
            effect.end_ms,
            burst=effect.kind == "steam_burst",
            seed=_seed(effect.id),
            scale=(1.0 if prop.kind == "pot" else 0.6) * PROP_SCALE,
            strength=1.0 if prop.kind == "pot" else 0.6,
        )
        if not puffs:
            return
        emit_height = POT_H if prop.kind == "pot" else BOWL_H
        if prop.kind == "pot":
            origin = self._to_screen(cam, (prop.x, prop.y - emit_height))
            draw_steam(frame, puffs, origin, k)
            return
        # puffs keep the place of the emitter at their birth (a bowl that moves)
        for puff in puffs:
            spot = self._prop_anchor(scene_id, prop_id, max(0.0, puff.born_ms))
            origin = self._to_screen(cam, (spot[0], spot[1] - emit_height))
            draw_steam(frame, [puff], origin, k)

    def _draw_splash(
        self,
        frame: Any,
        effect: ResolvedEffect,
        runtime: Any,
        cam: CameraState,
        k: float,
        t_ms: float,
        scene_id: str,
    ) -> None:
        prop_id = str(effect.params.get("prop", ""))
        if prop_id not in runtime.props:
            return
        spot = self._prop_anchor(scene_id, prop_id, t_ms)
        origin = self._to_screen(cam, (spot[0], spot[1] - BOWL_H))
        age = (t_ms - effect.start_ms) / 1000
        draw = ImageDraw.Draw(frame)
        for i in range(9):
            angle = math.radians(-35 - 110 * ((i * 0.618) % 1))
            speed = 190 + 90 * ((i * 0.37) % 1)
            x = origin[0] + math.cos(angle) * speed * age * k * 0.6
            y = origin[1] + (math.sin(angle) * speed * age + 380 * age * age) * k * 0.6
            life = clamp(1 - age / 0.8)
            r = max(1.0, (4.5 - 2.5 * (age / 0.8)) * k * life)
            if life > 0:
                draw.ellipse(
                    [x - r, y - r, x + r, y + r],
                    fill=(236, 176, 74),
                    outline=(96, 58, 28),
                    width=max(1, round(k)),
                )

    def _draw_pours(
        self,
        frame: Any,
        runtime: Any,
        cam: CameraState,
        k: float,
        t_ms: float,
        characters: Sequence[tuple[CharacterFrame, CharacterRender]],
    ) -> None:
        for window in runtime.pours:
            if not window.t0 <= t_ms <= window.t1 + 120:
                continue
            ladle_state = self.choreo.prop_state(runtime.scene_id, window.ladle, t_ms)
            if ladle_state.holder is None:
                continue
            tip = self._ladle_lip(runtime.scene_id, window.ladle, ladle_state, t_ms)
            bowl = self._prop_anchor(runtime.scene_id, window.bowl, t_ms)
            start = self._to_screen(cam, tip)
            end = self._to_screen(cam, (bowl[0], bowl[1] - BOWL_H * 0.95))
            span = max(1.0, window.t1 - window.t0)
            progress = clamp((t_ms - window.t0) / (span * 0.3))
            tail = 1 - smoothstep(window.t1 - 80, window.t1 + 120, t_ms)
            if tail <= 0.02:
                continue
            pts = broth_arc(start, end, progress)
            draw = ImageDraw.Draw(frame)
            width = max(2, round(8 * k * tail))
            draw.line(
                pts,
                fill=(96, 58, 28),
                width=width + max(2, round(3 * k)),
                joint="curve",
            )
            draw.line(pts, fill=(238, 178, 76), width=width, joint="curve")
            draw.line(
                pts, fill=(255, 220, 140), width=max(1, width // 3), joint="curve"
            )

    def _ladle_lip(
        self, scene_id: str, ladle_id: str, state: PropState, t_ms: float
    ) -> Point:
        anchor = self._prop_anchor(scene_id, ladle_id, t_ms)
        frame_data = self.choreo.character_frame(scene_id, state.holder or "", t_ms)
        sf = 1.0 if frame_data.pose.yaw >= 0 else -1.0
        angle = math.radians(
            self.choreo.held_angle(scene_id, ladle_id, t_ms, state.holder or "")
        )
        vx, vy = -sf * 20.0, 104.0
        scale = frame_data.scale
        return (
            anchor[0] + (vx * math.cos(angle) - vy * math.sin(angle)) * scale,
            anchor[1] + (vx * math.sin(angle) + vy * math.cos(angle)) * scale,
        )

    # --- transitions, subtitles, fade ----------------------------------------

    def _blend(
        self, transition: ResolvedTransition, t_ms: float
    ) -> tuple[Any, _LayerMeta]:
        a, meta_a = self._layer(transition.from_scene, t_ms)
        b, meta_b = self._layer(transition.to_scene, t_ms)
        progress = transition_progress(t_ms, transition)
        frame = blend_frames(
            transition.kind, a, b, progress, direction=transition.direction
        )
        meta = meta_a if progress < 0.5 else meta_b
        return frame, meta

    def _draw_subtitle(self, frame: Any, t_ms: float) -> Box | None:
        cue = cue_at(self._cues, t_ms)
        if cue is None:
            return None
        layout = self._layouts.get(cue.index)
        if layout is None:
            layout = layout_cue(cue, self._measure)
            self._layouts[cue.index] = layout
        from ai_youtube_agent.animation.subtitles import active_word_index

        key = (cue.index, active_word_index(cue, t_ms))
        cached = self._cue_images.get(key)
        if cached is None:
            cached = render_cue(layout, cue, t_ms, self.assets.fonts.bold)
            self._cue_images[key] = cached
        image, position = cached
        if self.size != (STAGE_W, STAGE_H):
            scale = self.fs
            image = image.resize(
                (
                    max(1, round(image.width * scale)),
                    max(1, round(image.height * scale)),
                ),
                Image.Resampling.LANCZOS,
            )
            position = (round(position[0] * scale), round(position[1] * scale))
        frame.paste(image, position, image)
        box = layout.box
        return (
            round(box.left * self.fs),
            round(box.top * self.fs),
            round(box.right * self.fs),
            round(box.bottom * self.fs),
        )

    def _fade(self, t_ms: float) -> float:
        fade_ms = self.timeline.storyboard.format.fade_out_ms
        remaining = self.timeline.total_ms - t_ms
        if fade_ms <= 0 or remaining >= fade_ms:
            return 1.0
        return clamp(remaining / fade_ms)


def iter_frames(
    renderer: FrameRenderer,
    frames: range | None = None,
    on_meta: Callable[[FrameMeta], None] | None = None,
) -> Any:
    """Yield the RGB frames of ``renderer`` in order, reporting each frame's meta."""
    for index in frames if frames is not None else range(renderer.frame_count):
        image, meta = renderer.render(index)
        if on_meta is not None:
            on_meta(meta)
        yield image


__all__ = [
    "CREAM_HAZE",
    "GROUND_TOP",
    "FrameMeta",
    "FrameRenderer",
    "NamedBox",
    "RenderAssets",
    "iter_frames",
    "lerp",
]
