"""Puppet rig: proportions, character styles, pose, IK, walk cycle, visemes (pure).

A character is a cut-out puppet with a 3/4 view: the body turns by ``yaw`` and
every joint is placed in local pixels (x right, y down, the origin on the ground
between the feet, scale 1 = ``RIG`` proportions). ``solve_skeleton`` turns a
``Pose`` into joint positions with 2-bone IK for the arms and legs;
``characters.draw_character`` draws them. Nothing here needs Pillow.

Conventions: ``yaw`` > 0 faces the screen right; the character's own left is
``u`` > 0 and at yaw 0 it shows on the screen right. Hand targets are absolute,
relative to the pelvis, in screen axes.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from typing import Any, Final

from ai_youtube_agent.animation.easing import clamp, lerp

Vec = tuple[float, float]
Rgb = tuple[int, int, int]

OUTLINE_RGB: Final[Rgb] = (59, 36, 24)
OUTLINE_WIDTH: Final[float] = 4.5
SHADE_TONE: Final[Rgb] = (126, 78, 92)
SHADE_AMOUNT: Final[float] = 0.22
MAX_YAW_DEG: Final[float] = 55.0
SIDES: Final[tuple[str, str]] = ("left", "right")
# Everything drawn (characters and props) is this much bigger than the rig units,
# so the people are the hero even in the wide shot.
WORLD_SCALE: Final[float] = 1.2


def shade_of(rgb: Rgb, amount: float = SHADE_AMOUNT) -> Rgb:
    """The one soft shade tone of a flat colour (shared by every character)."""
    return (
        round(lerp(rgb[0], SHADE_TONE[0], amount)),
        round(lerp(rgb[1], SHADE_TONE[1], amount)),
        round(lerp(rgb[2], SHADE_TONE[2], amount)),
    )


@dataclass(frozen=True)
class RigProportions:
    """Local-pixel proportions at scale 1: the head is 1/3.2 of the height."""

    hip_y: float = -216.0  # pelvis height above the ground (y is negative up)
    torso_h: float = 164.0  # pelvis to shoulder line
    head_w: float = 168.0
    head_h: float = 176.0
    neck: float = 12.0  # chin above the shoulder line
    thigh: float = 114.0
    shin: float = 110.0
    upper_arm: float = 100.0
    forearm: float = 92.0
    shoulder_w: float = 68.0  # half width between the shoulders
    hip_w: float = 34.0
    foot_sep: float = 40.0  # half distance between the feet
    ankle_h: float = 15.0
    hand_r: float = 21.0
    sit_drop: float = 78.0  # pelvis drop when seated on a low stool
    sit_forward: float = 62.0  # feet forward of the pelvis when seated

    @property
    def height(self) -> float:
        return -self.hip_y + self.torso_h + self.neck + self.head_h - 6

    @property
    def head_center_y(self) -> float:
        """Head centre above the pelvis of the standing figure (pelvis-relative)."""
        return -(self.torso_h + self.neck + self.head_h / 2)


RIG: Final[RigProportions] = RigProportions()


@dataclass(frozen=True)
class CharacterStyle:
    """Look of one character; every character shares the outline and shade rules."""

    id: str
    scale: float
    skin: Rgb
    hair: Rgb
    top: Rgb  # shirt or cardigan
    pants: Rgb
    shoes: Rgb
    accent: Rgb  # apron, collar, hat
    eye: Rgb = (44, 28, 22)
    cheek: Rgb = (250, 150, 150)
    lips: Rgb = (176, 74, 74)
    outline: Rgb = OUTLINE_RGB
    outline_w: float = OUTLINE_WIDTH
    accessories: tuple[str, ...] = ()
    hair_style: str = "bob"
    stoop: float = 0.0  # permanent forward lean (degrees)
    default_smile: float = 0.35
    default_brow_tilt: float = 0.0


MAI: Final[CharacterStyle] = CharacterStyle(
    id="mai",
    scale=WORLD_SCALE,
    skin=(252, 218, 186),
    hair=(52, 34, 32),
    top=(238, 126, 70),
    pants=(104, 78, 72),
    shoes=(70, 46, 40),
    accent=(255, 248, 232),
    accessories=("chef_hat", "apron"),
    hair_style="bob",
    default_smile=0.45,
)
CHU_BA: Final[CharacterStyle] = CharacterStyle(
    id="chu_ba",
    scale=WORLD_SCALE * 0.92,
    skin=(240, 200, 166),
    hair=(176, 176, 182),
    top=(92, 140, 96),
    pants=(118, 92, 76),
    shoes=(86, 58, 46),
    accent=(246, 240, 226),
    accessories=("glasses", "cardigan", "moustache", "wrinkles"),
    hair_style="short_grey",
    stoop=8.0,
    default_smile=0.2,
    default_brow_tilt=0.25,
)
STYLES: Final[dict[str, CharacterStyle]] = {"mai": MAI, "chu_ba": CHU_BA}


def get_style(rig_id: str) -> CharacterStyle:
    try:
        return STYLES[rig_id]
    except KeyError:
        raise KeyError(f"unknown rig '{rig_id}'") from None


def rest_hand(side: str, yaw: float, rig: RigProportions = RIG) -> Vec:
    """The hanging-arm hand position (pelvis-relative) of ``side`` at ``yaw``."""
    sign = 1.0 if side == "left" else -1.0
    ca = math.cos(math.radians(clamp(yaw, -1, 1) * MAX_YAW_DEG))
    return (
        sign * (rig.shoulder_w * ca + 12.0),
        -rig.torso_h + rig.upper_arm + rig.forearm - 6.0,
    )


@dataclass(frozen=True)
class Pose:
    """Every animatable channel of one character at one instant.

    All channels are numbers (or number pairs) so poses blend by interpolation;
    ``tool``/``tool_hand`` are the exceptions and switch at weight 0.5.
    """

    # body
    dx: float = 0.0
    dy: float = 0.0
    lean: float = 0.0  # degrees, forward (toward the facing side) positive
    sway: float = 0.0  # degrees, shoulder-line roll
    yaw: float = 0.0
    sit: float = 0.0
    shrug: float = 0.0
    # legs: offset of each ankle from its rest place (screen x, screen y)
    foot_l: Vec = (0.0, 0.0)
    foot_r: Vec = (0.0, 0.0)
    # arms: hand targets relative to the pelvis (screen axes)
    hand_l: Vec = (80.0, 36.0)
    hand_r: Vec = (-80.0, 36.0)
    thumb_l: float = 0.0
    thumb_r: float = 0.0
    # 0..1: the elbows point down and forward (a hand at the face), not up and out
    elbow_down: float = 0.0
    # head
    head_yaw: float = 0.0
    head_roll: float = 0.0
    head_nod: float = 0.0  # positive: chin down
    # face
    eye_open: float = 1.0
    gaze_x: float = 0.0
    gaze_y: float = 0.0
    brow_l: float = 0.0
    brow_r: float = 0.0
    brow_tilt: float = 0.0  # positive: worried, negative: stern
    squint: float = 0.0
    smile: float = 0.35
    mouth_open: float = 0.0
    mouth_wide: float = 0.0  # -1 round ... +1 spread
    blush: float = 0.0
    # secondary motion (delayed follow-through of hair and cloth)
    hair_x: float = 0.0
    hair_y: float = 0.0
    cloth_x: float = 0.0
    cloth_y: float = 0.0
    # tool held in a hand
    tool: str = ""
    tool_hand: str = "right"
    tool_angle: float = 0.0

    def blended(self, values: Mapping[str, Any], weight: float) -> Pose:
        """Move the channels named in ``values`` toward their targets by ``weight``."""
        weight = clamp(weight)
        if weight <= 0.0 or not values:
            return self
        changes: dict[str, Any] = {}
        for raw_name, target in values.items():
            name = CHANNEL_ALIASES.get(raw_name, raw_name)
            if name not in POSE_CHANNELS:
                raise KeyError(f"unknown pose channel '{name}'")
            current = getattr(self, name)
            if isinstance(current, str):
                if weight >= 0.5:
                    changes[name] = target
            elif isinstance(current, tuple):
                changes[name] = (
                    lerp(current[0], target[0], weight),
                    lerp(current[1], target[1], weight),
                )
            else:
                changes[name] = lerp(current, float(target), weight)
        return replace(self, **changes)


POSE_CHANNELS: Final[frozenset[str]] = frozenset(f.name for f in fields(Pose))
CHANNEL_ALIASES: Final[dict[str, str]] = {
    "hand_left": "hand_l",
    "hand_right": "hand_r",
    "thumb_left": "thumb_l",
    "thumb_right": "thumb_r",
    "foot_left": "foot_l",
    "foot_right": "foot_r",
}


def facing_sign(yaw: float) -> float:
    return 1.0 if yaw >= 0 else -1.0


def near_side(yaw: float) -> str:
    """The character's own hand nearer to the camera (the one gestures use)."""
    return "right" if yaw > 0.01 else "left"


# ---------------------------------------------------------------------------
# Two-bone IK
# ---------------------------------------------------------------------------


def solve_two_bone_ik(
    origin: Vec, target: Vec, l1: float, l2: float, bend: int = 1
) -> tuple[Vec, Vec]:
    """Joint and end of a two-bone chain reaching for ``target``.

    The end is clamped to the reachable distance (never exactly straight or
    folded). ``bend`` +1 puts the joint on the side where cross(direction,
    joint - origin) > 0 (screen axes), -1 on the other side.
    """
    dx, dy = target[0] - origin[0], target[1] - origin[1]
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        ux, uy = 0.0, 1.0
    else:
        ux, uy = dx / dist, dy / dist
    reach = clamp(dist, abs(l1 - l2) + 1e-3, (l1 + l2) * 0.9995)
    end = (origin[0] + ux * reach, origin[1] + uy * reach)
    cos_a = (l1 * l1 + reach * reach - l2 * l2) / (2 * l1 * reach)
    angle = math.acos(clamp(cos_a, -1.0, 1.0))
    heading = math.atan2(uy, ux) + (angle if bend >= 0 else -angle)
    joint = (origin[0] + l1 * math.cos(heading), origin[1] + l1 * math.sin(heading))
    return joint, end


def bend_toward(origin: Vec, target: Vec, pole: Vec) -> int:
    """The ``bend`` that puts the joint on the ``pole`` side of origin -> target."""
    cross = (target[0] - origin[0]) * pole[1] - (target[1] - origin[1]) * pole[0]
    return 1 if cross >= 0 else -1


# ---------------------------------------------------------------------------
# Walk cycle
# ---------------------------------------------------------------------------

DEFAULT_STRIDE: Final[float] = 330.0  # distance of two steps at a normal pace
MIN_STRIDE: Final[float] = 150.0
MAX_STRIDE: Final[float] = 360.0
START_PHASE: Final[float] = 0.25  # the passing pose: both feet under the pelvis
LIFT_PX: Final[float] = 30.0
BOB_PX: Final[float] = 6.0


@dataclass(frozen=True)
class WalkFrame:
    """The legs and body of a walk at one phase.

    ``foot_a``/``foot_b`` are (forward offset from the pelvis along the travel
    direction, lift above the ground); a planted foot keeps one world position.
    ``swing_a``/``swing_b`` (-1..1) are the arm swings, opposite to the legs.
    """

    foot_a: Vec
    foot_b: Vec
    planted_a: bool
    planted_b: bool
    bob: float
    swing_a: float
    swing_b: float


def _foot_cycle(fraction: float, stride: float, lift: float) -> tuple[Vec, bool]:
    f = fraction % 1.0
    if f < 0.5:
        return (stride / 4 - stride * f, 0.0), True
    u = (f - 0.5) / 0.5
    smooth = u * u * (3 - 2 * u)
    return (lerp(-stride / 4, stride / 4, smooth), lift * math.sin(math.pi * u)), False


def walk_cycle(
    phase: float, stride: float, lift: float = LIFT_PX, bob: float = BOB_PX
) -> WalkFrame:
    """Legs, bob and arm swing at ``phase`` (cycles; one cycle = ``stride``)."""
    foot_a, planted_a = _foot_cycle(phase, stride, lift)
    foot_b, planted_b = _foot_cycle(phase + 0.5, stride, lift)
    quarter = stride / 4
    return WalkFrame(
        foot_a=foot_a,
        foot_b=foot_b,
        planted_a=planted_a,
        planted_b=planted_b,
        bob=-bob * (1 - math.cos(4 * math.pi * phase)) / 2,
        swing_a=clamp(-foot_a[0] / quarter, -1.0, 1.0),
        swing_b=clamp(-foot_b[0] / quarter, -1.0, 1.0),
    )


def plan_walk(distance: float, stride: float = DEFAULT_STRIDE) -> tuple[float, int]:
    """Effective stride and number of steps so the walk ends in a passing pose.

    Starting at ``START_PHASE`` and moving ``distance`` takes a whole number of
    steps with this stride, so the character never ends mid-stride.
    """
    distance = abs(distance)
    steps = max(1, round(distance / (stride / 2)))
    return 2 * distance / steps, steps


def stride_for_speed(speed_px_s: float) -> float:
    """Stride length for an average ``speed_px_s``: slow walkers take short steps."""
    return clamp(speed_px_s * 0.8, MIN_STRIDE, MAX_STRIDE)


def phase_at_distance(distance: float, stride: float) -> float:
    return START_PHASE + distance / stride


def foot_contacts(distance: float, stride: float) -> list[float]:
    """Distances (from the start) at which a foot lands during a walk.

    Every half cycle, starting at the first heel strike after the passing pose.
    The last foot always lands at ``distance`` when the walk stops (the foot in
    the air in the final passing pose is put down).
    """
    distance = abs(distance)
    half = stride / 2
    contacts: list[float] = []
    k = 0
    while True:
        d = stride * START_PHASE + k * half
        if d > distance + 1e-6:
            break
        contacts.append(d)
        k += 1
    if not contacts or distance - contacts[-1] > 1e-6:
        contacts.append(distance)
    return contacts


# ---------------------------------------------------------------------------
# Visemes
# ---------------------------------------------------------------------------

VISEME_SHAPES: Final[dict[str, tuple[float, float]]] = {
    # name: (mouth_open, mouth_wide)
    "closed": (0.0, 0.0),
    "wide": (0.95, 0.1),
    "round": (0.6, -1.0),
    "spread": (0.38, 1.0),
}
_VOWEL_CLASS: Final[dict[str, str]] = {
    "a": "wide",
    "o": "round",
    "u": "round",
    "i": "spread",
    "y": "spread",
    "e": "spread",
}


def viseme_for_syllable(text: str) -> str:
    """Mouth shape of a Vietnamese syllable from its first vowel.

    ``wide`` (a, ă, â), ``round`` (o, ô, ơ, u, ư), ``spread`` (i, y, e, ê) or
    ``closed`` when there is no vowel.
    """
    for char in unicodedata.normalize("NFD", text.lower()):
        if char in _VOWEL_CLASS:
            return _VOWEL_CLASS[char]
    return "closed"


# ---------------------------------------------------------------------------
# Skeleton
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Skeleton:
    """Joint positions in local pixels (origin on the ground, y down)."""

    pelvis: Vec
    hip_l: Vec
    hip_r: Vec
    knee_l: Vec
    knee_r: Vec
    ankle_l: Vec
    ankle_r: Vec
    chest: Vec
    shoulder_l: Vec
    shoulder_r: Vec
    elbow_l: Vec
    elbow_r: Vec
    hand_l: Vec
    hand_r: Vec
    head: Vec
    head_roll: float
    head_nod: float
    face_yaw: float
    body_cos: float
    body_sin: float
    facing: float
    near: str  # side whose arm is nearer to the camera ("left"/"right")
    torso_angle: float = 0.0
    extra: Mapping[str, Vec] = field(default_factory=dict)

    def hand(self, side: str) -> Vec:
        return self.hand_l if side == "left" else self.hand_r

    def mouth(self, rig: RigProportions = RIG) -> Vec:
        """Where the mouth is (for a hand or a spoon going to it)."""
        roll = math.radians(self.head_roll)
        off = (self.face_yaw * 14.0, 52.0 + self.head_nod * 12.0)
        return (
            self.head[0] + off[0] * math.cos(roll) - off[1] * math.sin(roll),
            self.head[1] + off[0] * math.sin(roll) + off[1] * math.cos(roll),
        )


def solve_skeleton(pose: Pose, rig: RigProportions = RIG) -> Skeleton:
    """Joint positions of ``pose`` (arms and legs by 2-bone IK)."""
    yaw = clamp(pose.yaw, -1.0, 1.0)
    angle = math.radians(yaw * MAX_YAW_DEG)
    ca, sa = math.cos(angle), math.sin(angle)
    sf = facing_sign(yaw)
    pelvis = (pose.dx, rig.hip_y + pose.dy + rig.sit_drop * pose.sit)
    lean = math.radians(pose.lean)
    roll = math.radians(pose.sway)
    chest = (
        pelvis[0] + sf * math.sin(lean) * rig.torso_h,
        pelvis[1] - math.cos(lean) * rig.torso_h - 12.0 * pose.shrug,
    )

    def shoulder(u: float) -> Vec:
        return (
            chest[0] + u * ca * math.cos(roll),
            chest[1] + u * ca * math.sin(roll) - 6.0 * pose.shrug,
        )

    def hip(u: float) -> Vec:
        return (pelvis[0] + u * ca, pelvis[1])

    shoulder_l, shoulder_r = shoulder(rig.shoulder_w), shoulder(-rig.shoulder_w)
    hip_l, hip_r = hip(rig.hip_w), hip(-rig.hip_w)

    def ankle(u: float, offset: Vec) -> Vec:
        forward = sf * (rig.sit_forward + 34.0 * abs(sa)) * pose.sit
        return (u * 0.75 + forward + offset[0], -rig.ankle_h + offset[1])

    ankle_l = ankle(rig.foot_sep, pose.foot_l)
    ankle_r = ankle(-rig.foot_sep, pose.foot_r)
    knee_pole = (sf, -0.15 - 0.9 * pose.sit)
    knee_l, ankle_l = solve_two_bone_ik(
        hip_l, ankle_l, rig.thigh, rig.shin, bend_toward(hip_l, ankle_l, knee_pole)
    )
    knee_r, ankle_r = solve_two_bone_ik(
        hip_r, ankle_r, rig.thigh, rig.shin, bend_toward(hip_r, ankle_r, knee_pole)
    )

    def arm(side: str, sh: Vec, target: Vec) -> tuple[Vec, Vec]:
        outward = 1.0 if side == "left" else -1.0
        down = clamp(pose.elbow_down)
        pole = (outward * 0.9 * (1 - 0.6 * down) - sf * 0.25, 0.6 + 2.0 * down)
        goal = (pelvis[0] + target[0], pelvis[1] + target[1])
        return solve_two_bone_ik(
            sh, goal, rig.upper_arm, rig.forearm, bend_toward(sh, goal, pole)
        )

    elbow_l, hand_l = arm("left", shoulder_l, pose.hand_l)
    elbow_r, hand_r = arm("right", shoulder_r, pose.hand_r)

    head_roll = pose.head_roll + sf * pose.lean * 0.6 + pose.sway * 0.4
    rr = math.radians(head_roll)
    neck = (0.0, -(rig.head_h / 2 + rig.neck - 6.0 + pose.head_nod * -4.0))
    head = (
        chest[0]
        + neck[0] * math.cos(rr)
        - neck[1] * math.sin(rr)
        + sf * pose.head_nod * 6,
        chest[1] + neck[0] * math.sin(rr) + neck[1] * math.cos(rr) - 6.0 * pose.shrug,
    )
    face_yaw = clamp(yaw * 0.75 + pose.head_yaw, -1.0, 1.0)
    return Skeleton(
        pelvis=pelvis,
        hip_l=hip_l,
        hip_r=hip_r,
        knee_l=knee_l,
        knee_r=knee_r,
        ankle_l=ankle_l,
        ankle_r=ankle_r,
        chest=chest,
        shoulder_l=shoulder_l,
        shoulder_r=shoulder_r,
        elbow_l=elbow_l,
        elbow_r=elbow_r,
        hand_l=hand_l,
        hand_r=hand_r,
        head=head,
        head_roll=head_roll,
        head_nod=pose.head_nod,
        face_yaw=face_yaw,
        body_cos=ca,
        body_sin=sa,
        facing=sf,
        near=near_side(yaw),
        torso_angle=pose.lean * sf,
    )
