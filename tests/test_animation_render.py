"""Frame renderer: composition, determinism, scale, transitions (needs Pillow)."""

from __future__ import annotations

import hashlib

import pytest

pytest.importorskip("PIL.Image")

from PIL import Image  # noqa: E402

from ai_youtube_agent.animation.actions import Choreography  # noqa: E402
from ai_youtube_agent.animation.characters import HeldItem, draw_character  # noqa: E402
from ai_youtube_agent.animation.draw import (  # noqa: E402
    choose_supersample,
    plan_canvas,
)
from ai_youtube_agent.animation.fonts import resolve_font_files  # noqa: E402
from ai_youtube_agent.animation.pipeline_steps import StepBlocked  # noqa: E402
from ai_youtube_agent.animation.props import PropLook, render_prop  # noqa: E402
from ai_youtube_agent.animation.renderer import (  # noqa: E402
    FrameRenderer,
    RenderAssets,
)
from ai_youtube_agent.animation.rig import CHU_BA, MAI, Pose  # noqa: E402
from ai_youtube_agent.animation.scenery import (  # noqa: E402
    broth_arc,
    dust_motes,
    passersby,
    steam_puffs,
)
from ai_youtube_agent.animation.subtitles import CAPTION_RECT  # noqa: E402
from ai_youtube_agent.animation.timeline import resolve_timeline  # noqa: E402
from ai_youtube_agent.animation.transitions import (  # noqa: E402
    blend_frames,
    transition_progress,
    whip_offset,
)
from animation_fixtures import fake_spoken, shipped_storyboard  # noqa: E402

SIZE = (270, 480)


def make_plate() -> Image.Image:
    plate = Image.new("RGB", (1080, 1920), (150, 176, 170))
    for y in range(0, 1920, 8):
        plate.paste((140 + y // 40, 170, 160 - y // 60), (0, y, 1080, y + 8))
    return plate


@pytest.fixture(scope="module")
def storyboard():
    return shipped_storyboard()


@pytest.fixture(scope="module")
def fonts():
    try:
        return resolve_font_files(None)
    except StepBlocked:
        pytest.skip("no Vietnamese-capable TrueType font on this machine")


@pytest.fixture(scope="module")
def timeline(storyboard):
    return resolve_timeline(storyboard, fake_spoken(storyboard))


@pytest.fixture(scope="module")
def renderer(timeline, fonts):
    plate = make_plate()
    return FrameRenderer(
        timeline, RenderAssets({"front": plate, "kitchen": plate}, fonts), size=SIZE
    )


def mean_luma(image: Image.Image) -> float:
    data = image.convert("L").tobytes()
    return sum(data) / len(data)


def digest(image: Image.Image) -> str:
    return hashlib.sha256(image.tobytes()).hexdigest()


def test_a_frame_is_an_rgb_image_of_the_requested_size(renderer) -> None:
    image, meta = renderer.render(45)

    assert image.size == SIZE and image.mode == "RGB"
    assert meta.frame == 45 and meta.scene_id == "s1_hook" and meta.shot_id == "s1a"


def test_a_frame_depends_only_on_its_index(timeline, fonts, renderer) -> None:
    plate = make_plate()
    other = FrameRenderer(
        timeline, RenderAssets({"front": plate, "kitchen": plate}, fonts), size=SIZE
    )
    indexes = [30, 330, 500, 700]

    first = [digest(renderer.render(i)[0]) for i in indexes]
    # a different order on a fresh renderer gives the same pixels
    second = [digest(other.render(i)[0]) for i in reversed(indexes)][::-1]

    assert first == second
    assert digest(renderer.render(330)[0]) == first[1]


def test_faces_stay_above_the_caption_in_every_sampled_frame(
    renderer, timeline
) -> None:
    scale = SIZE[0] / 1080
    limit = CAPTION_RECT.top * scale
    checked = 0
    for index in range(0, timeline.frame_count, 12):
        _, meta = renderer.render(index)
        if meta.transition:
            continue
        for face in meta.faces:
            checked += 1
            assert face.y1 <= limit + 1, (index, face)
    assert checked > 40


def test_the_only_in_world_text_is_the_sign_and_it_is_small(renderer, timeline) -> None:
    sign_frames = 0
    for index in range(0, timeline.frame_count, 15):
        _, meta = renderer.render(index)
        area = sum(box.area for box in meta.text_boxes)
        assert all(box.name == "sign" for box in meta.text_boxes)
        assert area <= SIZE[0] * SIZE[1] * 0.04
        if meta.text_boxes:
            sign_frames += 1
            assert (
                area * 5 <= sum(box.area for box in meta.characters)
                or not meta.characters
            )
    assert sign_frames > 0


def test_the_caption_box_is_inside_the_safe_caption_area(renderer) -> None:
    _, meta = renderer.render(60)

    assert meta.caption is not None
    scale = SIZE[0] / 1080
    assert meta.caption[1] >= CAPTION_RECT.top * scale - 1
    assert meta.caption[3] <= CAPTION_RECT.bottom * scale + 1


def test_the_last_frames_fade_to_dark(renderer, timeline) -> None:
    _, middle = renderer.render(timeline.frame_count // 2)
    last, meta = renderer.render(timeline.frame_count - 1)

    assert middle.fade == 1.0 and meta.fade < 0.2
    assert mean_luma(last) < 40


def test_transition_frames_blend_two_scenes(renderer, timeline) -> None:
    transition = timeline.transitions[0]  # steam_dissolve
    middle = (transition.start_ms + transition.end_ms) / 2
    image, meta = renderer.render_time(middle)

    assert meta.transition == "steam_dissolve" and image.size == SIZE
    brightness = mean_luma(image)
    plain = renderer.render_time(transition.start_ms - 400)[0]
    assert brightness > mean_luma(plain) - 2


@pytest.mark.parametrize(
    "kind", ["cut", "crossfade", "whip_pan", "steam_dissolve", "dip"]
)
def test_every_transition_kind_blends(kind: str) -> None:
    a = Image.new("RGB", (60, 100), (200, 40, 40))
    b = Image.new("RGB", (60, 100), (40, 40, 200))

    start = blend_frames(kind, a, b, 0.0, direction="right")
    end = blend_frames(kind, a, b, 1.0, direction="right")
    middle = blend_frames(kind, a, b, 0.5, direction="right")

    assert start.size == end.size == middle.size == a.size
    assert start.getpixel((30, 50))[0] > 150 or kind == "dip"
    assert end.getpixel((30, 50))[2] > 150 or kind == "dip"
    with pytest.raises(ValueError):
        blend_frames("wipe", a, b, 0.5)


def test_whip_pan_slides_in_the_given_direction_and_progress_is_clamped(
    timeline,
) -> None:
    assert whip_offset(0.0, 1080, "right") == 0
    assert whip_offset(1.0, 1080, "right") == 1080
    assert whip_offset(1.0, 1080, "left") == -1080
    transition = timeline.transitions[1]
    assert transition_progress(transition.start_ms - 50, transition) == 0.0
    assert transition_progress(transition.end_ms + 50, transition) == 1.0


def test_stills_and_the_contact_sheet(renderer) -> None:
    stills = renderer.render_stills([300, 5000, 12000])
    sheet = renderer.contact_sheet([300, 5000, 12000, 20000], columns=2, thumb_width=60)

    assert [round(t) for t, _, _ in stills] == [300, 5000, 12000]
    assert sheet.size[0] > 120 and sheet.size[1] > 200


def test_close_ups_are_drawn_as_vectors_at_the_frame_scale() -> None:
    pose = Pose(yaw=0.5)
    from ai_youtube_agent.animation.actions import CharacterFrame

    frame = CharacterFrame(
        "mai", "mai", 0, 0, MAI.scale, pose, (True, True), False, 0.0
    )
    wide = draw_character(frame, MAI, (135, 400), 0.25, SIZE)
    close = draw_character(frame, MAI, (135, 700), 0.25 * 3.6, SIZE)

    assert wide is not None and close is not None
    # the close-up sprite is clipped to the viewport, never bigger than the frame
    assert close.image.size[0] <= SIZE[0] and close.image.size[1] <= SIZE[1]
    assert close.image.size[1] > wide.image.size[1] * 1.5
    # and it is not blocky: it has many distinct edge shades
    soft_edges = sum(1 for a in close.image.getchannel("A").tobytes() if 0 < a < 255)
    assert soft_edges > 300


def test_offscreen_things_are_not_drawn() -> None:
    from ai_youtube_agent.animation.actions import CharacterFrame

    frame = CharacterFrame(
        "chu_ba", "chu_ba", 0, 0, CHU_BA.scale, Pose(), (True, True), False, 0.0
    )

    assert draw_character(frame, CHU_BA, (-3000, 400), 1.0, SIZE) is None
    assert render_prop("table", (5000, 400), 1.0, SIZE) is None


def test_held_props_are_drawn_with_the_character() -> None:
    from ai_youtube_agent.animation.actions import CharacterFrame

    pose = Pose(yaw=0.7, hand_r=(70.0, -120.0))
    frame = CharacterFrame(
        "mai", "mai", 0, 0, MAI.scale, pose, (True, True), False, 0.0
    )
    plain = draw_character(frame, MAI, (135, 400), 0.5, SIZE)
    held = draw_character(
        frame,
        MAI,
        (135, 400),
        0.5,
        SIZE,
        held=[HeldItem("bowl_pho", "right", 0.0, PropLook(broth=1.0))],
    )

    assert plain is not None and held is not None
    assert held.image.tobytes() != plain.image.tobytes()


def test_supersampling_is_capped_so_close_ups_stay_fast() -> None:
    assert choose_supersample(400, 600) == 2.0
    assert 1.0 <= choose_supersample(1080, 1920) < 2.0
    assert 1080 * 1920 * choose_supersample(1080, 1920) ** 2 <= 6_000_000
    plan = plan_canvas((-300, -700, 300, 40), (540, 1500), 3.0, (1080, 1920))
    assert plan is not None and plan.width <= 1080 and plan.height <= 1920


def test_particles_are_deterministic_and_fade_out() -> None:
    a = steam_puffs(1500, 0, 3000, burst=False, seed=3)
    b = steam_puffs(1500, 0, 3000, burst=False, seed=3)

    assert a == b and len(a) > 15
    assert all(p.dy < 0 for p in a if p.born_ms < 1400)  # rises
    assert steam_puffs(9000, 0, 3000, burst=False, seed=3) == []
    assert dust_motes(1000, 1) == dust_motes(1000, 1)
    walkers = passersby(5000, 4000, 7000, 2, 9)
    assert len(walkers) == 2 and walkers[0].direction != walkers[1].direction
    arc = broth_arc((100, 100), (140, 300), 1.0)
    assert arc[0] == (100, 100) and arc[-1][1] > arc[0][1]


def test_the_choreography_is_shared_with_the_renderer(renderer) -> None:
    assert isinstance(renderer.choreo, Choreography)
    assert renderer.frame_count == 881 or renderer.frame_count > 800


def test_wide_shots_frame_the_heads_in_the_upper_half_not_the_empty_facade(
    renderer, timeline
) -> None:
    shot = next(s for s in timeline.shots if s.id == "s3a")
    t = (shot.start_ms + shot.end_ms) / 2

    _, meta = renderer.render_time(t)

    faces = [f for f in meta.faces if f.name == "mai"]
    assert faces
    for face in faces:
        centre = (face.y0 + face.y1) / 2 / SIZE[1]
        assert 0.30 <= centre <= 0.50, centre
        assert face.y1 < CAPTION_RECT.top * SIZE[1] / 1920


def test_steam_is_a_visible_soft_curling_plume() -> None:
    burst = steam_puffs(300, 0, 700, burst=True, seed=3)
    rising = steam_puffs(1500, 0, 3000, burst=False, seed=3)

    assert max(p.alpha for p in burst) >= 0.4
    assert max(p.alpha for p in rising) >= 0.3
    assert all(p.alpha <= 0.7 for p in burst + rising)  # tasteful, never opaque
    sway = [p.dx for p in rising]
    assert max(sway) - min(sway) > 30  # it curls sideways as it climbs
    assert len(rising) > 40  # dense enough to read as one plume
    older = [p for p in rising if p.dy < -80]
    assert older and all(p.radius > rising[-1].radius for p in older)  # it widens
