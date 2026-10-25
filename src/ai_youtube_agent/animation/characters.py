"""Characters drawn as vectors: Mai and Chu Ba share one outline and shade rule.

``draw_character`` turns a ``CharacterFrame`` into a clipped, supersampled RGBA
sprite at the exact scale of the frame (so close-ups are crisp). The cut-out
puppet has a 3/4 view: the torso narrows with the body yaw, the face features
shift and the far eye is squashed with the head yaw, brows and mouth carry the
expression and the hair and the hem follow the body a moment late.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ai_youtube_agent.animation.actions import HOLDS, CharacterFrame
from ai_youtube_agent.animation.draw import (
    Pen,
    composite_at,
    finish_canvas,
    new_canvas,
    plan_canvas,
)
from ai_youtube_agent.animation.easing import clamp, lerp
from ai_youtube_agent.animation.props import PropLook, blit_prop
from ai_youtube_agent.animation.rig import (
    CharacterStyle,
    Rgb,
    Skeleton,
    shade_of,
    solve_skeleton,
)

Point = tuple[float, float]
MOUTH_IN: Rgb = (150, 52, 58)
TONGUE: Rgb = (238, 122, 132)
TEETH: Rgb = (255, 252, 244)
GLASSES: Rgb = (86, 58, 44)
BROW_GREY: Rgb = (150, 148, 154)
MOUSTACHE: Rgb = (214, 212, 214)


@dataclass(frozen=True)
class HeldItem:
    """A prop or tool in a hand; ``angle`` turns it about the hand."""

    kind: str
    hand: str
    angle: float = 0.0
    look: PropLook = field(default_factory=PropLook)
    offset: Point = (0.0, 0.0)  # prop anchor relative to the hand (local units)


@dataclass
class CharacterRender:
    image: Any
    left: int
    top: int
    hands: dict[str, Point]
    feet: tuple[Point, Point]
    head: Point
    face_box: tuple[float, float, float, float]
    body_box: tuple[float, float, float, float]
    skeleton: Skeleton


def rrect_points(
    x0: float, y0: float, x1: float, y1: float, r: float, steps: int = 5
) -> list[Point]:
    """A rounded rectangle as polygon points (so it can be rotated)."""
    pts: list[Point] = []
    corners = (
        (x1 - r, y0 + r, -90),
        (x1 - r, y1 - r, 0),
        (x0 + r, y1 - r, 90),
        (x0 + r, y0 + r, 180),
    )
    for cx, cy, start in corners:
        for i in range(steps + 1):
            a = math.radians(start + 90 * i / steps)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _extent(
    skel: Skeleton, held: Sequence[HeldItem], pose: Any
) -> tuple[float, float, float, float]:
    points = [
        skel.pelvis,
        skel.chest,
        skel.hand_l,
        skel.hand_r,
        skel.elbow_l,
        skel.elbow_r,
        skel.knee_l,
        skel.knee_r,
        skel.ankle_l,
        skel.ankle_r,
    ]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs) - 90, max(xs) + 90
    y0, y1 = min(ys) - 60, max(ys) + 40
    hx, hy = skel.head
    x0, x1 = min(x0, hx - 130), max(x1, hx + 130)
    y0 = min(y0, hy - 270)
    if held:
        for item in held:
            hand = skel.hand(item.hand)
            x0, x1 = min(x0, hand[0] - 210), max(x1, hand[0] + 210)
            y0, y1 = min(y0, hand[1] - 220), max(y1, hand[1] + 260)
    return (x0, y0, x1, y1)


class _Painter:
    """Draws one character into a canvas (everything in local units)."""

    def __init__(
        self, pen: Pen, style: CharacterStyle, skel: Skeleton, frame: CharacterFrame
    ) -> None:
        self.pen = pen
        self.style = style
        self.skel = skel
        self.pose = frame.pose
        self.frame = frame
        self.sf = skel.facing
        self.ca = skel.body_cos
        self.sa = skel.body_sin
        self.skin = style.skin
        self.has = set(style.accessories)

    # --- torso helpers ----------------------------------------

    def torso_point(self, u: float, v: float, front: float = 1.0) -> Point:
        """A point on the torso: ``u`` across (own left +), ``v`` up from the pelvis."""
        skel = self.skel
        ax = (skel.chest[0] - skel.pelvis[0], skel.chest[1] - skel.pelvis[1])
        length = math.hypot(*ax) or 1.0
        axis = (ax[0] / length, ax[1] / length)
        normal = (-axis[1], axis[0])
        lateral = u * self.ca + front * self.sa * 30.0
        return (
            skel.pelvis[0] + axis[0] * v + normal[0] * lateral,
            skel.pelvis[1] + axis[1] * v + normal[1] * lateral,
        )

    # --- body ----------------------------------------

    def legs(self) -> None:
        skel, style, pen = self.skel, self.style, self.pen
        far = "left" if skel.near == "right" else "right"
        for side in (far, skel.near):
            hip = skel.hip_l if side == "left" else skel.hip_r
            knee = skel.knee_l if side == "left" else skel.knee_r
            ankle = skel.ankle_l if side == "left" else skel.ankle_r
            pen.limb([hip, knee, ankle], 41, style.pants)
            sf = skel.facing
            pen.ellipse(
                ankle[0] + sf * 15, ankle[1] + 4, 34, 13.5, style.shoes, shade=True
            )
            pen.ellipse(
                ankle[0] + sf * 26,
                ankle[1] + 1,
                14,
                6,
                shade_of(style.shoes, -0.3),
                outline=False,
            )

    def torso(self) -> None:
        skel, style, pen, ca = self.skel, self.style, self.pen, self.ca
        r_hip = 60 * (0.72 + 0.28 * ca)
        r_chest = 72 * (0.66 + 0.34 * ca)
        pelvis = (skel.pelvis[0], skel.pelvis[1] + 8)
        chest = (skel.chest[0], skel.chest[1] + 26)
        pen.taper(pelvis, r_hip, chest, r_chest, style.top)
        cloth = self.pose.cloth_x
        if "apron" in self.has:
            self._apron(cloth)
        elif "cardigan" in self.has:
            self._cardigan(cloth)

    def _apron(self, cloth: float) -> None:
        pen, style = self.pen, self.style
        tp = self.torso_point
        hem = cloth * 1.4
        pts = [tp(-38, 128), tp(38, 128), tp(58, 46), tp(66, -40)]
        bottom = [(p[0] + hem, p[1]) for p in (tp(40, -70), tp(0, -76), tp(-40, -70))]
        left = [tp(-66, -40), tp(-58, 46)]
        outline_pts = pts + bottom + left
        pen.polygon(outline_pts, style.accent, shade=7)
        pocket = [tp(-30, 34), tp(30, 34), tp(28, -10), tp(-28, -10)]
        pen.polygon(pocket, (250, 238, 214))
        for sign in (-1, 1):
            a, b = tp(sign * 34, 124), tp(sign * 60, 166)
            pen.limb([a, b], 13, style.accent, shade=False)

    def _cardigan(self, cloth: float) -> None:
        pen, style = self.pen, self.style
        tp = self.torso_point
        shirt = [
            tp(-20, 146),
            tp(20, 146),
            tp(15 + cloth * 0.2, 30),
            tp(-15 + cloth * 0.2, 30),
        ]
        pen.polygon(shirt, style.accent, shade=4)
        for v in (112, 80, 48):
            x, y = tp(0, v)
            pen.ellipse(x, y, 4.6, 4.6, (56, 86, 62), outline=False)
        for sign in (-1, 1):
            pocket = [
                tp(sign * 24, 12),
                tp(sign * 54, 14),
                tp(sign * 52, -26),
                tp(sign * 24, -24),
            ]
            pen.stroke([*pocket, pocket[0]], 3.2, shade_of(style.top, 0.55))
        collar = [tp(-26, 150), tp(0, 118), tp(-4, 152)]
        collar2 = [tp(26, 150), tp(0, 118), tp(4, 152)]
        pen.polygon(collar, style.accent)
        pen.polygon(collar2, style.accent)

    def neck(self) -> None:
        skel, pen = self.skel, self.pen
        top = (skel.head[0], skel.head[1] + 62)
        base = (skel.chest[0], skel.chest[1] + 6)
        pen.limb([base, top], 40, self.skin)

    def arm(self, side: str) -> None:
        skel, style, pen = self.skel, self.style, self.pen
        sh = skel.shoulder_l if side == "left" else skel.shoulder_r
        el = skel.elbow_l if side == "left" else skel.elbow_r
        hd = skel.hand_l if side == "left" else skel.hand_r
        sleeve = style.top
        pen.limb([sh, el], 40, sleeve)
        long_sleeves = "cardigan" in self.has
        pen.limb([el, hd], 32, sleeve if long_sleeves else self.skin)
        if long_sleeves:
            # a cuff and a little shirt
            dx, dy = hd[0] - el[0], hd[1] - el[1]
            norm = math.hypot(dx, dy) or 1.0
            cuff = (hd[0] - dx / norm * 19, hd[1] - dy / norm * 19)
            pen.ellipse(cuff[0], cuff[1], 17, 17, style.accent, outline=True)
            pen.limb(
                [el, (cuff[0] - dx / norm * 4, cuff[1] - dy / norm * 4)],
                33,
                sleeve,
                shade=False,
            )
        thumb_up = (self.pose.thumb_l if side == "left" else self.pose.thumb_r) > 0.5
        outward = self.sf
        pen.ellipse(hd[0] + outward * 14, hd[1] - 2, 9.5, 13, self.skin)
        pen.ellipse(hd[0], hd[1], 21, 21, self.skin, shade=True)
        if thumb_up:
            pen.limb(
                [(hd[0] + outward * 3, hd[1] - 12), (hd[0] + outward * 5, hd[1] - 38)],
                17,
                self.skin,
                shade=False,
            )
            pen.ellipse(
                hd[0] + outward * 5, hd[1] - 40, 10, 12, self.skin, outline=True
            )

    # --- head ----------------------------------------

    def head(self) -> None:
        skel, pen = self.skel, self.pen
        pose = self.pose
        hx, hy = skel.head
        roll = math.radians(skel.head_roll)
        cr, sr = math.cos(roll), math.sin(roll)

        def H(x: float, y: float) -> Point:
            return (hx + x * cr - y * sr, hy + x * sr + y * cr)

        psi = skel.face_yaw
        nod = skel.head_nod
        fx = psi * 28.0
        fy = nod * 12.0
        self._head_back(H, psi, pose)
        for sign in (-1, 1):
            ex, ey = H(
                sign
                * (
                    80
                    - 8 * abs(psi) * (1 if sign == -math.copysign(1, psi or 1) else -1)
                ),
                10,
            )
            pen.ellipse(ex, ey, 17, 21, self.skin, shade=False)
        face = H(0, 0)
        pen.ellipse(face[0], face[1], 82, 87, self.skin, shade=True)
        self._cheeks(H, fx, fy, pose)
        self._eyes(H, fx, fy, psi, pose)
        self._brows(H, fx, fy, psi, pose)
        self._nose(H, fx, fy, psi)
        if "moustache" in self.has:
            for sign in (-1, 1):
                mx, my = H(fx * 0.95 + sign * 13, 41 + fy)
                pen.ellipse(mx, my, 17, 8.5, MOUSTACHE, shade=False)
        self._mouth(H, fx * 0.95, 53 + fy, pose)
        if "wrinkles" in self.has:
            self._wrinkles(H, fx, fy)
        if "glasses" in self.has:
            self._glasses(H, fx, fy, psi)
        self._head_front(H, psi, pose)

    def _head_back(self, H: Any, psi: float, pose: Any) -> None:
        pen, style = self.pen, self.style
        lag_x, lag_y = pose.hair_x, pose.hair_y
        if style.hair_style == "bob":
            cx, cy = H(0, 4)
            pen.rounded(cx - 98, cy - 80, cx + 98, cy + 66, 56, style.hair, shade=True)
            for sign in (-1, 1):
                tx, ty = H(sign * 88, 62)
                pen.ellipse(
                    tx + lag_x * 1.5, ty + lag_y, 27, 27, style.hair, shade=True
                )
        else:
            for sign in (-1, 1):
                tx, ty = H(sign * 76, -8)
                pen.ellipse(tx + lag_x * 0.5, ty, 30, 44, style.hair, shade=True)

    def _head_front(self, H: Any, psi: float, pose: Any) -> None:
        pen, style = self.pen, self.style
        lag_x, lag_y = pose.hair_x, pose.hair_y
        if style.hair_style == "bob":
            outline: list[Point] = []
            for i in range(0, 19):
                a = math.radians(180 + 180 * i / 18)
                outline.append(H(93 * math.cos(a), -6 + 92 * math.sin(a)))
            scallops = [
                (86, -22),
                (66, -40),
                (40, -30),
                (14, -44),
                (-14, -34),
                (-40, -46),
                (-66, -32),
                (-86, -18),
            ]
            lower = [H(x, y) for x, y in scallops]
            pen.polygon(outline + lower, style.hair, shade=8)
            for sign in (-1, 1):
                far = abs(psi) > 0.04 and sign == (1 if psi > 0 else -1)
                width = 15 * (1 - 0.45 * abs(psi)) if far else 15 * (1 + 0.1 * abs(psi))
                sx, sy = H(sign * (80 + (6 * abs(psi) if far else 0)), 14)
                pen.ellipse(
                    sx + lag_x * 0.6,
                    sy + lag_y * 0.4,
                    width,
                    52,
                    style.hair,
                    shade=True,
                )
        else:
            top = [
                H(-70, -52),
                H(-34, -86),
                H(10, -90),
                H(52, -78),
                H(76, -48),
                H(46, -64),
                H(8, -70),
                H(-30, -62),
            ]
            pen.polygon(top, style.hair, shade=5)
            for x, y in ((-40, -86), (12, -92), (50, -82)):
                px, py = H(x, y)
                pen.stroke(
                    [(px, py), (px + 7 + lag_x * 0.3, py - 12)],
                    3.4,
                    shade_of(style.hair, 0.5),
                )
        if "chef_hat" in self.has:
            hat = (255, 252, 244)
            circles = []
            for x, y, r in ((-42, -128, 37), (0, -146, 47), (42, -128, 37)):
                cx, cy = H(x, y)
                circles.append((cx, cy - lag_y * 0.5, r))
            pen.blob(circles, hat)
            band = [H(x, y) for x, y in rrect_points(-72, -108, 72, -64, 15)]
            pen.polygon(band, hat, shade=8)

    def _cheeks(self, H: Any, fx: float, fy: float, pose: Any) -> None:
        pen = self.pen
        strength = 0.4 + 0.6 * pose.blush
        color = (
            round(lerp(self.skin[0], self.style.cheek[0], strength)),
            round(lerp(self.skin[1], self.style.cheek[1], strength)),
            round(lerp(self.skin[2], self.style.cheek[2], strength)),
        )
        widen = 1.0 + 0.3 * pose.blush
        for sign in (-1, 1):
            x, y = H(fx * 0.9 + sign * 52, 34 + fy)
            pen.ellipse(x, y, 19 * widen, 11 * widen, color, outline=False)

    def _eyes(self, H: Any, fx: float, fy: float, psi: float, pose: Any) -> None:
        pen, style = self.pen, self.style
        scale = 0.82 if "glasses" in self.has else 1.0
        near_sign = -1 if psi > 0 else 1
        for sign in (-1, 1):
            far = sign != near_sign and abs(psi) > 0.04
            width = (1 - 0.4 * abs(psi)) if far else (1 + 0.1 * abs(psi))
            separation = 34 * (1 - 0.22 * abs(psi))
            ex, ey = H(fx + sign * separation, 8 + fy)
            rx, ry = 14.5 * scale * width, 19.5 * scale
            gaze_x = clamp(pose.gaze_x + 0.5 * pose.head_yaw, -1, 1) * 4.2 * scale
            gaze_y = clamp(pose.gaze_y, -1, 1) * 4.0 * scale
            openness = clamp(pose.eye_open)
            if pose.squint > 0.5:
                pts = pen.arc_points(ex, ey + 7, rx * 1.25, ry * 0.9, 200, 340, 12)
                pen.stroke(pts, 5.2, style.outline)
                continue
            if openness < 0.14:
                pts = pen.arc_points(ex, ey - 4, rx * 1.2, ry * 0.5, 25, 155, 10)
                pen.stroke(pts, 5.0, style.outline)
                continue
            ry_now = (
                ry * (0.35 + 0.65 * min(1.0, openness / 0.9)) * (1 - 0.35 * pose.squint)
            )
            pen.ellipse(ex + gaze_x, ey + gaze_y, rx, ry_now, style.eye, outline=False)
            if ry_now > 8:
                pen.ellipse(
                    ex + gaze_x + 0.32 * rx,
                    ey + gaze_y - 0.38 * ry_now,
                    4.4 * scale,
                    4.8 * scale,
                    (255, 255, 255),
                    outline=False,
                )
                pen.ellipse(
                    ex + gaze_x - 0.3 * rx,
                    ey + gaze_y + 0.4 * ry_now,
                    2.2 * scale,
                    2.3 * scale,
                    (255, 255, 255),
                    outline=False,
                )

    def _brows(self, H: Any, fx: float, fy: float, psi: float, pose: Any) -> None:
        pen, style = self.pen, self.style
        grey = "glasses" in self.has
        width = 10.5 if grey else 5.4
        color = BROW_GREY if grey else shade_of(style.hair, -0.1)
        for sign, raise_ in ((-1, pose.brow_r), (1, pose.brow_l)):
            near = (-1 if psi > 0 else 1) == sign
            span = 24 * (1 if near else 1 - 0.3 * abs(psi))
            cx = fx + sign * 34 * (1 - 0.22 * abs(psi))
            base = -30 + fy - raise_ * 14
            inner = base - pose.brow_tilt * 9
            outer = base + pose.brow_tilt * 6
            pts = [
                H(cx - sign * span, inner),
                H(cx, min(inner, outer) - 5),
                H(cx + sign * span, outer),
            ]
            pen.stroke(pts, width, color)

    def _nose(self, H: Any, fx: float, fy: float, psi: float) -> None:
        pen = self.pen
        x, y = H(fx * 1.1 + psi * 12, 29 + fy)
        pen.stroke(
            pen.arc_points(x, y - 3, 7.5 + 3 * abs(psi), 5.5, 15, 165, 8),
            3.4,
            self.style.outline,
        )
        if abs(psi) > 0.3:
            bx, by = H(fx * 1.1 + psi * 22, 26 + fy)
            pen.stroke(
                [(bx, by - 6), (bx + psi * 2, by + 1)], 3.0, shade_of(self.skin, 0.5)
            )

    def _mouth(self, H: Any, cx: float, cy: float, pose: Any) -> None:
        pen, style = self.pen, self.style
        smile = clamp(pose.smile, -1, 1)
        openness = clamp(pose.mouth_open)
        wide = clamp(pose.mouth_wide, -1, 1)
        if openness < 0.05:
            half = 21 + 11 * max(0.0, smile) + 2 * wide
            pts = []
            for i in range(13):
                x = -half + 2 * half * i / 12
                bow = 1 - (x / half) ** 2
                y = (1 - bow) * -smile * 9 + bow * smile * 3
                pts.append(H(cx + x, cy + y))
            pen.stroke(pts, 4.6, style.outline)
            if smile > 0.35:
                for sign in (-1, 1):
                    dx, dy = H(cx + sign * (half + 2), cy - smile * 8)
                    pen.ellipse(dx, dy, 2.6, 2.6, style.outline, outline=False)
            return
        width = (18 + 10 * max(0.0, smile)) * (
            1 + 0.42 * wide if wide >= 0 else 1 + 0.42 * wide
        )
        height = 4.0 + 29.0 * openness
        lift = -smile * 6
        mx, my = H(cx, cy + lift + height * 0.45)
        if smile > 0.5 and openness > 0.08:
            top_y = cy + lift - 2
            pts: list[Point] = [H(cx - width, top_y), H(cx + width, top_y)]
            for i in range(1, 12):
                a = math.pi * i / 12
                pts.append(
                    H(cx + width * math.cos(a), top_y + (height + 6) * math.sin(a))
                )
            pen.polygon(pts, MOUTH_IN)
            pen.polygon(
                [
                    H(cx - width + 3, top_y + 1),
                    H(cx + width - 3, top_y + 1),
                    H(cx + width - 6, top_y + 8),
                    H(cx - width + 6, top_y + 8),
                ],
                TEETH,
                outline=False,
            )
            if height > 12:
                tx, ty = H(cx, top_y + height * 0.95)
                pen.ellipse(tx, ty, width * 0.5, height * 0.32, TONGUE, outline=False)
            return
        pen.ellipse(mx, my, width, height, MOUTH_IN)
        if height > 11:
            tx, ty = H(cx, cy + lift + height * 0.85)
            pen.ellipse(tx, ty, width * 0.55, height * 0.3, TONGUE, outline=False)

    def _wrinkles(self, H: Any, fx: float, fy: float) -> None:
        pen = self.pen
        color = shade_of(self.skin, 0.55)
        for sign in (-1, 1):
            base_x = fx + sign * 66
            for dy in (-2, 7):
                pen.stroke(
                    [H(base_x, 4 + dy + fy), H(base_x + sign * 10, 7 + dy + fy)],
                    2.4,
                    color,
                )
        for dy in (-60, -50):
            pen.stroke(
                pen.arc_points(*H(fx * 0.8, dy + fy), 30, 6, 200, 340, 8), 2.2, color
            )

    def _glasses(self, H: Any, fx: float, fy: float, psi: float) -> None:
        pen = self.pen
        centres = []
        for sign in (-1, 1):
            separation = 34 * (1 - 0.22 * abs(psi))
            centres.append(H(fx + sign * separation, 8 + fy))
        for sign, (cx, cy) in zip((-1, 1), centres, strict=True):
            far = abs(psi) > 0.04 and sign == (1 if psi > 0 else -1)
            rx = 31 * ((1 - 0.35 * abs(psi)) if far else 1.0)
            ring = [
                (
                    cx + rx * math.cos(a * math.tau / 24),
                    cy + 31 * math.sin(a * math.tau / 24),
                )
                for a in range(25)
            ]
            pen.stroke(ring, 4.6, GLASSES)
            pen.stroke(
                [(cx - rx * 0.55, cy - 16), (cx - rx * 0.1, cy - 24)],
                3.2,
                (238, 246, 252),
            )
        pen.stroke(
            [
                (centres[0][0] + 22, centres[0][1] - 4),
                (centres[1][0] - 22, centres[1][1] - 4),
            ],
            4.2,
            GLASSES,
        )
        for sign, (cx, cy) in zip((-1, 1), centres, strict=True):
            pen.stroke(
                [(cx + sign * 30, cy - 4), (cx + sign * 52, cy + 2)], 3.6, GLASSES
            )


def _held_hand_offset(item: HeldItem) -> Point:
    return item.offset


def draw_character(
    frame: CharacterFrame,
    style: CharacterStyle,
    anchor: Point,
    k: float,
    viewport: tuple[int, int],
    *,
    held: Sequence[HeldItem] = (),
    preferred_ss: float = 2.0,
) -> CharacterRender | None:
    """Draw ``frame`` with its feet at ``anchor`` (screen px).

    ``k`` is screen pixels per stage pixel (zoom x frame scale); the character's
    own scale is applied here. Returns ``None`` when nothing of it is in view.
    """
    skel = solve_skeleton(frame.pose)
    size_k = k * style.scale
    plan = plan_canvas(
        _extent(skel, held, frame.pose),
        anchor,
        size_k,
        viewport,
        preferred_ss=preferred_ss,
    )
    if plan is None:
        return None
    image = new_canvas(plan)
    pen = Pen(
        image,
        plan.origin,
        plan.k,
        outline=style.outline,
        outline_w=style.outline_w / style.scale,
    )
    painter = _Painter(pen, style, skel, frame)
    near = skel.near
    far = "left" if near == "right" else "right"
    head_y = skel.head[1]

    def raised(side: str) -> bool:
        return skel.hand(side)[1] < head_y + 96

    painter.legs()
    if not raised(far):
        painter.arm(far)
    painter.torso()
    painter.neck()
    painter.head()
    if raised(far):
        painter.arm(far)
    prop_k = plan.k / style.scale  # canvas px per stage px; blit_prop adds PROP_SCALE
    for item in held:
        hand = skel.hand(item.hand)
        spec = HOLDS.get(item.kind)
        offset = item.offset
        if spec is not None and item.offset == (0.0, 0.0):
            offset = (-skel.facing * spec.grip[0], -spec.grip[1])
        anchor_px = pen.xy(hand[0] + offset[0], hand[1] + offset[1])
        blit_prop(image, item.kind, anchor_px, prop_k, item.look, angle=item.angle)
    painter.arm(near)
    sprite = finish_canvas(image, plan)
    to_screen = lambda p: (anchor[0] + p[0] * size_k, anchor[1] + p[1] * size_k)  # noqa: E731
    head = to_screen(skel.head)
    face_box = (
        head[0] - 84 * size_k,
        head[1] - 88 * size_k,
        head[0] + 84 * size_k,
        head[1] + 88 * size_k,
    )
    pts = [
        skel.pelvis,
        skel.chest,
        skel.hand_l,
        skel.hand_r,
        skel.ankle_l,
        skel.ankle_r,
        skel.head,
    ]
    xs = [to_screen(p)[0] for p in pts]
    ys = [to_screen(p)[1] for p in pts]
    body_box = (
        min(xs) - 40 * size_k,
        head[1] - 260 * size_k * (1 if "chef_hat" in style.accessories else 0.5),
        max(xs) + 40 * size_k,
        max(ys) + 20 * size_k,
    )
    return CharacterRender(
        image=sprite,
        left=plan.left,
        top=plan.top,
        hands={"left": to_screen(skel.hand_l), "right": to_screen(skel.hand_r)},
        feet=(to_screen(skel.ankle_l), to_screen(skel.ankle_r)),
        head=head,
        face_box=face_box,
        body_box=body_box,
        skeleton=skel,
    )


def paste_sprite(frame_image: Any, render: CharacterRender) -> None:
    """Composite a character sprite onto an RGB frame."""
    frame_image.paste(render.image, (render.left, render.top), render.image)


__all__ = [
    "CharacterRender",
    "HeldItem",
    "composite_at",
    "draw_character",
    "paste_sprite",
]
