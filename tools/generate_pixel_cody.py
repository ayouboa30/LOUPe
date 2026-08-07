"""Generate the pixel-art "Cody" mascot sprite sheets shipped in web/assets.

Cody is the CODE-flavoured member of the mascot family: a round near-black
blob wearing a neon-green hoodie, with the hood visible around the neck and
two drawstrings hanging down the chest, surrounded by small floating
terminal props (angle brackets, curly braces, a blinking cursor, and stray
binary digits). The whole app switches to a terminal theme when this
character is active (CODE task kind) - see web/style.css and web/app.js for
the theme switch, driven by the ``kind-select`` control.

This generator is a sibling of ``generate_pixel_researcher.py`` and
``generate_pixel_pixelbit.py``, not a variant of either: same reasoning as
the CLI-agent prompt template in ``three_loop/latent.py`` - a second (third)
file is easier to reason about than a shared abstraction over characters
whose *appearance* is the entire point of the difference. The generic engine
(deformation pipeline, geometry primitives, Canvas, eye/face drawing) is
copied verbatim; only the rest-pose silhouette, the palette and the props are
Cody-specific.

The grid, the four clips (idle/hop/vanish/return, 34 frames total) and their
timing match the researcher's exactly, since web/style.css computes its
background-position percentages from a fixed 34-frame, 64px-cell layout.

Two sheets are emitted, same rationale as the other two generators:

* ``pixel_cody_strip.png`` - eyes baked in on every frame, for the web
  avatar's CSS sprite.
* ``pixel_cody_base.png`` - eyes left out of idle/hop, for parity with the
  other characters' asset sets.

Run: python tools/generate_pixel_cody.py [--preview [--frame N]]
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
from PIL import Image

# --------------------------------------------------------------------------
# Grid and pose constants: identical to the researcher's and Pixelbit's.
# --------------------------------------------------------------------------

LOGICAL = 64
CENTRE_X = (LOGICAL - 1) / 2.0
GROUND_Y = 58.0
REST_HEIGHT = 47.0
MAX_BOB = 7
HOLE_Y = 8.0

EYE_WIDTH = 6
EYE_HEIGHT = 7
EYE_SPACING = 7.5

#: Terminal palette: near-black contours and body base, neon-green hoodie as
#: the dominant colour, a brighter neon for highlights/glow, cyan for the
#: cursor and binary digits so the two accent colours read as "screen light"
#: rather than as one flat green. No violet anywhere - the whole point is to
#: read as unmistakably different from the researcher and from Pixelbit.
PALETTE: dict[str, tuple[int, int, int, int]] = {
    "outline": (10, 14, 12, 255),
    "eye": (18, 26, 22, 255),
    "glint": (255, 255, 255, 255),
    "skin": (58, 74, 66, 255),          # body base: dark slate, screen-lit
    "skin_shade": (34, 46, 40, 255),
    "hoodie": (57, 224, 130, 255),      # neon green
    "hoodie_dark": (27, 138, 78, 255),  # hoodie shade
    "hoodie_light": (146, 255, 189, 255), # hoodie highlight / drawstring tips
    "cursor": (86, 234, 255, 255),      # cyan terminal cursor / accents
    "blush": (150, 255, 210, 90),
    "mouth": (12, 20, 16, 255),
    "shadow": (10, 24, 18, 64),
    "glow": (57, 224, 130, 84),
    "spark": (200, 255, 225, 205),
    "void": (6, 10, 9, 255),
    "void_rim": (57, 224, 130, 255),
}


@dataclass(frozen=True)
class FrameSpec:
    """One frame's deformation and dressing state. No held prop, no tilt."""

    squash_x: float = 1.0
    squash_y: float = 1.0
    bob: float = 0.0
    phase: float = 0.0
    scale: float = 1.0
    swirl: float = 0.0
    shadow: bool = True
    sparkles: int = 0
    bake_eyes: bool = False
    face: bool = True
    hole: float = 0.0
    tail: bool = False
    props: bool = True
    glow: float = 0.0
    twinkle: int = 0
    cursor_on: bool = True
    float_a: int = 0
    float_b: int = 0
    smile: int = 1
    blink: bool = False


def _idle_frames() -> list[FrameSpec]:
    frames: list[FrameSpec] = []
    for index in range(12):
        phase = 2.0 * math.pi * index / 12.0
        breath = math.sin(phase)
        frames.append(
            FrameSpec(
                squash_x=1.0 - 0.022 * breath,
                squash_y=1.0 + 0.026 * breath,
                bob=1.0 if breath > 0.6 else 0.0,
                phase=phase,
                glow=0.5 - 0.5 * math.cos(phase),
                smile=2 if breath > 0.35 else (1 if breath > -0.45 else 0),
                blink=index in (9, 10),
                # Blinking terminal cursor: on for roughly half the loop,
                # independent of the eye blink so the two reads stay distinct.
                cursor_on=index % 4 < 2,
            )
        )
    return frames


_HOP_FRAMES = [
    FrameSpec(squash_x=1.10, squash_y=0.88, glow=0.10, smile=0, cursor_on=True),
    FrameSpec(squash_x=0.93, squash_y=1.14, bob=1.5, glow=0.30, smile=1, cursor_on=False),
    FrameSpec(squash_x=0.96, squash_y=1.08, bob=4.0, glow=0.45, smile=2, cursor_on=True),
    FrameSpec(squash_x=1.00, squash_y=1.01, bob=float(MAX_BOB), glow=0.55, smile=2, cursor_on=False),
    FrameSpec(squash_x=0.97, squash_y=1.05, bob=5.0, glow=0.40, smile=2, cursor_on=True),
    FrameSpec(squash_x=0.94, squash_y=1.10, bob=1.5, glow=0.22, smile=1, cursor_on=False),
    FrameSpec(squash_x=1.13, squash_y=0.85, glow=0.08, smile=0, cursor_on=True),
    FrameSpec(squash_x=1.04, squash_y=0.96, glow=0.04, smile=1, cursor_on=True),
]

_VANISH_FRAMES = [
    FrameSpec(squash_x=1.12, squash_y=0.88, bake_eyes=True, props=False, smile=0, glow=0.15),
    FrameSpec(squash_x=0.90, squash_y=1.18, bob=3.0, scale=0.90, swirl=1.2,
              hole=0.55, tail=True, bake_eyes=True, props=False, smile=0, glow=0.35),
    FrameSpec(squash_x=0.80, squash_y=1.26, bob=8.0, scale=0.70, swirl=-1.5,
              hole=0.90, tail=True, bake_eyes=True, props=False, smile=0, glow=0.5),
    FrameSpec(squash_x=0.70, squash_y=1.30, bob=14.0, scale=0.50, swirl=1.6,
              hole=1.00, tail=True, shadow=False, bake_eyes=True, props=False,
              smile=0, glow=0.4),
    FrameSpec(squash_x=0.60, squash_y=1.30, bob=20.0, scale=0.32, swirl=-1.4,
              hole=1.00, tail=True, shadow=False, sparkles=3, bake_eyes=True,
              props=False, face=False, glow=0.3),
    FrameSpec(squash_x=0.52, squash_y=1.20, bob=25.0, scale=0.16, swirl=1.0,
              hole=0.80, tail=True, shadow=False, sparkles=4, bake_eyes=True,
              props=False, face=False, glow=0.2),
    FrameSpec(squash_x=0.46, squash_y=1.00, bob=28.0, scale=0.06,
              hole=0.45, shadow=False, sparkles=5, bake_eyes=True, props=False,
              face=False, glow=0.1),
]

_RETURN_FRAMES = [
    FrameSpec(squash_x=0.46, squash_y=1.00, bob=28.0, scale=0.08,
              hole=0.50, shadow=False, sparkles=4, bake_eyes=True, props=False,
              face=False, glow=0.1),
    FrameSpec(squash_x=0.54, squash_y=1.22, bob=23.0, scale=0.22, swirl=-1.0,
              hole=0.85, tail=True, shadow=False, sparkles=3, bake_eyes=True,
              props=False, face=False, glow=0.25),
    FrameSpec(squash_x=0.68, squash_y=1.30, bob=15.0, scale=0.50, swirl=1.2,
              hole=0.70, tail=True, shadow=False, bake_eyes=True, props=False,
              smile=0, glow=0.4),
    FrameSpec(squash_x=0.84, squash_y=1.20, bob=6.0, scale=0.80, hole=0.35,
              bake_eyes=True, props=False, smile=1, glow=0.3),
    FrameSpec(squash_x=1.18, squash_y=0.82, bake_eyes=True, props=False,
              smile=0, glow=0.2),
    FrameSpec(squash_x=0.94, squash_y=1.08, bob=1.5, bake_eyes=True,
              props=False, smile=2, glow=0.15),
    FrameSpec(squash_x=1.03, squash_y=0.97, bake_eyes=True, props=False,
              smile=2, glow=0.1),
]

CLIPS: dict[str, dict[str, object]] = {
    "idle": {"frames": _idle_frames(), "frame_ms": 150, "loop": True},
    "hop": {"frames": _HOP_FRAMES, "frame_ms": 70, "loop": False},
    "vanish": {"frames": _VANISH_FRAMES, "frame_ms": 65, "loop": False},
    "return": {"frames": _RETURN_FRAMES, "frame_ms": 65, "loop": False},
}

_ALL_FRAMES_CACHE: list[FrameSpec] | None = None


def _all_frames() -> list[FrameSpec]:
    global _ALL_FRAMES_CACHE
    if _ALL_FRAMES_CACHE is not None:
        return _ALL_FRAMES_CACHE
    frames: list[FrameSpec] = []
    for clip in CLIPS.values():
        frames.extend(clip["frames"])  # type: ignore[arg-type]
    _ALL_FRAMES_CACHE = [
        replace(
            spec,
            twinkle=index % 4,
            float_a=1 if (index // 3) % 2 == 0 else 0,
            float_b=1 if (index // 2) % 2 == 0 else 0,
        )
        for index, spec in enumerate(frames)
    ]
    return _ALL_FRAMES_CACHE


# --------------------------------------------------------------------------
# Generic geometry engine, copied verbatim from generate_pixel_researcher.py.
# --------------------------------------------------------------------------

_YY, _XX = np.mgrid[0:LOGICAL, 0:LOGICAL]


def _disc(cx: float, cy: float, rx: float, ry: float) -> np.ndarray:
    return ((_XX - cx) / max(rx, 1e-6)) ** 2 + ((_YY - cy) / max(ry, 1e-6)) ** 2 <= 1.0


def _capsule(p0: tuple[float, float], p1: tuple[float, float], half: float) -> np.ndarray:
    x0, y0 = p0
    x1, y1 = p1
    dx, dy = x1 - x0, y1 - y0
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-9:
        return _disc(x0, y0, half, half)
    t = ((_XX - x0) * dx + (_YY - y0) * dy) / length_sq
    t = np.clip(t, 0.0, 1.0)
    px = x0 + t * dx
    py = y0 + t * dy
    return (_XX - px) ** 2 + (_YY - py) ** 2 <= half * half


def _boundary(mask: np.ndarray) -> np.ndarray:
    padded = np.zeros((LOGICAL + 2, LOGICAL + 2), dtype=bool)
    padded[1:-1, 1:-1] = mask
    interior = (
        padded[1:-1, 1:-1]
        & padded[:-2, 1:-1]
        & padded[2:, 1:-1]
        & padded[1:-1, :-2]
        & padded[1:-1, 2:]
    )
    return mask & ~interior


def _dilate(mask: np.ndarray, steps: int = 1) -> np.ndarray:
    grown = mask
    for _ in range(steps):
        padded = np.zeros((LOGICAL + 2, LOGICAL + 2), dtype=bool)
        padded[1:-1, 1:-1] = grown
        grown = (
            padded[1:-1, 1:-1]
            | padded[:-2, 1:-1]
            | padded[2:, 1:-1]
            | padded[1:-1, :-2]
            | padded[1:-1, 2:]
        )
    return grown


def _translate(mask: np.ndarray, dx: int, dy: int) -> np.ndarray:
    out = np.zeros_like(mask)
    src_y0, src_y1 = max(0, dy), min(LOGICAL, LOGICAL + dy)
    src_x0, src_x1 = max(0, dx), min(LOGICAL, LOGICAL + dx)
    out[src_y0 - dy : src_y1 - dy, src_x0 - dx : src_x1 - dx] = mask[
        src_y0:src_y1, src_x0:src_x1
    ]
    return out


def _pinch_fill(mask: np.ndarray) -> np.ndarray:
    left = _translate(mask, -1, 0) & _translate(mask, 1, 0)
    above = _translate(mask, 0, -1) & _translate(mask, 0, 1)
    return ~mask & (left | above)


def _shade_band(mask: np.ndarray, depth: int) -> np.ndarray:
    return mask & ~_translate(mask, depth, depth)


def _offset(spec: FrameSpec, t: float) -> float:
    lean = 0.8 * math.sin(spec.phase) * math.sin(t * math.pi)
    return lean + spec.swirl * t * spec.scale


def _map_point(spec: FrameSpec, x: float, y: float) -> tuple[float, float]:
    scale_y = spec.squash_y * spec.scale
    scale_x = spec.squash_x * spec.scale
    above = GROUND_Y - y
    out_y = GROUND_Y - spec.bob - above * scale_y
    t = min(1.0, max(0.0, above / REST_HEIGHT))
    out_x = CENTRE_X + (x - CENTRE_X) * scale_x + _offset(spec, t)
    return out_x, out_y


def _deform(mask: np.ndarray, spec: FrameSpec) -> np.ndarray:
    out = np.zeros_like(mask)
    scale_x = spec.squash_x * spec.scale
    scale_y = spec.squash_y * spec.scale
    half_x = 0.5 * scale_x
    half_y = 0.5 * scale_y
    ys, xs = np.nonzero(mask)
    for y, x in zip(ys.tolist(), xs.tolist()):
        cx, cy = _map_point(spec, float(x), float(y))
        x0 = int(math.ceil(cx - half_x))
        x1 = int(math.floor(cx + half_x))
        if x1 < x0:
            x0 = x1 = int(round(cx))
        y0 = int(math.ceil(cy - half_y))
        y1 = int(math.floor(cy + half_y))
        if y1 < y0:
            y0 = y1 = int(round(cy))
        x0, x1 = max(0, x0), min(LOGICAL - 1, x1)
        y0, y1 = max(0, y0), min(LOGICAL - 1, y1)
        if x1 < x0 or y1 < y0:
            continue
        out[y0 : y1 + 1, x0 : x1 + 1] = True
    return out


class Canvas:
    def __init__(self) -> None:
        self.rgba = np.zeros((LOGICAL, LOGICAL, 4), dtype=np.int16)
        self.keys: list[list[str | None]] = [[None] * LOGICAL for _ in range(LOGICAL)]

    def set(self, x: int, y: int, key: str) -> None:
        if not (0 <= x < LOGICAL and 0 <= y < LOGICAL):
            return
        self.rgba[y, x] = PALETTE[key]
        self.keys[y][x] = key

    def fill(self, mask: np.ndarray, key: str) -> None:
        self.rgba[mask] = PALETTE[key]
        for y, x in zip(*np.nonzero(mask)):
            self.keys[int(y)][int(x)] = key

    def blend(self, x: int, y: int, key: str, strength: float = 1.0) -> None:
        if not (0 <= x < LOGICAL and 0 <= y < LOGICAL):
            return
        colour = PALETTE[key]
        alpha = colour[3] / 255.0 * max(0.0, min(1.0, strength))
        if alpha <= 0.0:
            return
        base = self.rgba[y, x]
        for channel in range(3):
            self.rgba[y, x, channel] = int(
                round(base[channel] * (1.0 - alpha) + colour[channel] * alpha)
            )
        self.rgba[y, x, 3] = max(int(base[3]), int(round(colour[3] * max(0.0, min(1.0, strength)))))
        self.keys[y][x] = key

    def occupied(self, x: int, y: int) -> bool:
        return 0 <= x < LOGICAL and 0 <= y < LOGICAL and bool(self.rgba[y, x, 3] > 0)

    def image(self) -> Image.Image:
        return Image.fromarray(self.rgba.clip(0, 255).astype(np.uint8), "RGBA")


def _round(value: float) -> int:
    return int(math.floor(value + 0.5))


# --------------------------------------------------------------------------
# Cody's own rest pose: a round dark-slate body in a neon-green hoodie, hood
# visible around the neck/shoulders, two drawstrings hanging down the chest.
# --------------------------------------------------------------------------

_HEAD = (CENTRE_X, 25.0, 15.0, 14.5)
_BELLY = (CENTRE_X, 43.0, 12.0, 11.5)

_HAND_L = (16.5, 47.0)
_HAND_R = (46.5, 47.0)
_ARM_ROOT_L = (19.0, 40.0)
_ARM_ROOT_R = (44.0, 40.0)

_BODY_SHADE_DEPTH = 3
_HOODIE_SHADE_DEPTH = 2

_REST_LAYERS_CACHE: dict[str, np.ndarray] | None = None


def _rest_layers() -> dict[str, np.ndarray]:
    global _REST_LAYERS_CACHE
    if _REST_LAYERS_CACHE is not None:
        return _REST_LAYERS_CACHE

    head = _disc(*_HEAD)
    belly = _disc(*_BELLY)
    body = head | belly

    legs = _capsule((26.0, 52.0), (26.0, 56.0), 2.5) | _capsule(
        (37.0, 52.0), (37.0, 56.0), 2.5
    )
    feet = legs & (_YY >= 56)

    arm_l = _capsule(_ARM_ROOT_L, _HAND_L, 2.6)
    arm_r = _capsule(_ARM_ROOT_R, _HAND_R, 2.6)
    hands = _disc(*_HAND_L, 2.4, 2.4) | _disc(*_HAND_R, 2.4, 2.4)

    # Hoodie: covers the belly like the sweater does, but the neckline sits
    # a little higher and is flanked by a raised hood collar (see below),
    # which is what makes the silhouette read as a hoodie rather than a
    # crew-neck.
    neckline = 35.0 + np.clip(2.0 - np.abs(_XX - CENTRE_X) * 0.5, 0.0, None)
    hoodie = (body & (_YY >= neckline)) | arm_l | arm_r
    seam = _pinch_fill(body | hoodie)
    hoodie = hoodie | seam

    # Hood collar: a raised band of hoodie material bunched at the neck,
    # slightly above and wider than the neckline itself - the two "horns" of
    # a bunched-up hood as seen from the front.
    hood_collar = (
        body
        & (_YY >= neckline - 4)
        & (_YY < neckline)
        & (np.abs(_XX - CENTRE_X) >= 3)
        & (np.abs(_XX - CENTRE_X) <= 9)
    )

    # Drawstrings: two thin vertical cords hanging from the collar down onto
    # the chest, each ending in a small round aglet tip.
    string_l = _capsule((CENTRE_X - 3.0, neckline.min() - 1.0), (CENTRE_X - 3.0, 44.0), 0.7)
    string_r = _capsule((CENTRE_X + 3.0, neckline.min() - 1.0), (CENTRE_X + 3.0, 44.0), 0.7)
    aglets = _disc(CENTRE_X - 3.0, 44.5, 1.1, 1.1) | _disc(CENTRE_X + 3.0, 44.5, 1.1, 1.1)
    drawstrings = (string_l | string_r) & hoodie
    drawstrings = drawstrings | (aglets & (hoodie | _dilate(hoodie, 1)))

    badge = (_XX >= 24) & (_XX <= 26) & (_YY >= 48) & (_YY <= 50) & hoodie

    cuff_l_pt = (
        _ARM_ROOT_L[0] + 0.72 * (_HAND_L[0] - _ARM_ROOT_L[0]),
        _ARM_ROOT_L[1] + 0.72 * (_HAND_L[1] - _ARM_ROOT_L[1]),
    )
    cuff_r_pt = (
        _ARM_ROOT_R[0] + 0.72 * (_HAND_R[0] - _ARM_ROOT_R[0]),
        _ARM_ROOT_R[1] + 0.72 * (_HAND_R[1] - _ARM_ROOT_R[1]),
    )
    cuffs = (_disc(*cuff_l_pt, 1.8, 1.8) & arm_l) | (_disc(*cuff_r_pt, 1.8, 1.8) & arm_r)
    mitts = _disc(*_HAND_L, 1.8, 1.8) | _disc(*_HAND_R, 1.8, 1.8)

    layers = {
        "legs": legs & ~body,
        "feet": feet & ~body,
        "body": body,
        "hoodie": hoodie,
        "seam": seam,
        "hood_collar": hood_collar & ~hoodie,
        "drawstrings": drawstrings & ~hood_collar,
        "badge": badge,
        "hands": hands & ~hoodie,
        "cuffs": cuffs,
        "mitts": mitts,
    }
    _REST_LAYERS_CACHE = layers
    return layers


_REST_EXTRAS_CACHE: dict[str, np.ndarray] | None = None


def _rest_extras() -> dict[str, np.ndarray]:
    global _REST_EXTRAS_CACHE
    if _REST_EXTRAS_CACHE is not None:
        return _REST_EXTRAS_CACHE

    layers = _rest_layers()
    body = layers["body"]
    hoodie = layers["hoodie"]

    extras = {
        "body_shade": _shade_band(body, _BODY_SHADE_DEPTH),
        "hoodie_shade": _shade_band(hoodie, _HOODIE_SHADE_DEPTH),
        "gloss": body & _disc(24.0, 17.5, 3.4, 2.7),
        "gloss_core": body & _disc(23.5, 17.0, 1.7, 1.3),
    }
    _REST_EXTRAS_CACHE = extras
    return extras


_DEFORMED_CACHE: dict[FrameSpec, dict[str, np.ndarray]] = {}

_PAINT_ORDER: tuple[tuple[str, str], ...] = (
    ("legs", "skin"),
    ("feet", "skin_shade"),
    ("body", "skin"),
    ("body_shade", "skin_shade"),
    ("gloss", "hoodie_light"),
    ("gloss_core", "glint"),
    ("hoodie", "hoodie"),
    ("seam", "hoodie"),
    ("hoodie_shade", "hoodie_dark"),
    ("hood_collar", "hoodie_dark"),
    ("drawstrings", "hoodie_light"),
    ("badge", "cursor"),
    ("hands", "skin"),
    ("cuffs", "hoodie_light"),
    ("mitts", "skin_shade"),
)

_SOLID_LAYERS = ("legs", "feet", "body", "hoodie", "seam", "hood_collar", "hands", "mitts")


def _frame_layers(spec: FrameSpec) -> dict[str, np.ndarray]:
    cached = _DEFORMED_CACHE.get(spec)
    if cached is not None:
        return cached
    rest: dict[str, np.ndarray] = dict(_rest_layers())
    rest.update(_rest_extras())
    deformed = {name: _deform(mask, spec) for name, mask in rest.items()}
    _DEFORMED_CACHE[spec] = deformed
    return deformed


def _draw_ground_shadow(canvas: Canvas, spec: FrameSpec) -> None:
    lift = min(1.0, spec.bob / float(MAX_BOB))
    half_x = (13.0 * spec.squash_x * spec.scale) * (1.0 - 0.5 * lift)
    half_y = 1.8 * spec.scale * (1.0 - 0.35 * lift)
    if half_x <= 0.2 or half_y <= 0.2:
        return
    ellipse = _disc(CENTRE_X, GROUND_Y + 0.5, half_x, half_y)
    _wash(canvas, ellipse, "shadow", 1.0 - 0.35 * lift)


def _wash(canvas: Canvas, mask: np.ndarray, key: str, strength: float = 1.0) -> None:
    colour = PALETTE[key]
    alpha = int(round(colour[3] * max(0.0, min(1.0, strength))))
    if alpha <= 0:
        return
    empty = mask & (canvas.rgba[:, :, 3] == 0)
    canvas.rgba[empty] = (colour[0], colour[1], colour[2], alpha)
    for y, x in zip(*np.nonzero(empty)):
        canvas.keys[int(y)][int(x)] = key


def _draw_hole(canvas: Canvas, spec: FrameSpec) -> None:
    if spec.hole <= 0.0:
        return
    radius_x = 8.8 * spec.hole
    radius_y = 5.0 * spec.hole
    core = _disc(CENTRE_X, HOLE_Y, radius_x, radius_y)
    rim = _disc(CENTRE_X, HOLE_Y, radius_x * 1.25, radius_y * 1.25) & ~core
    canvas.fill(rim, "void_rim")
    canvas.fill(core, "void")


def _draw_tail(canvas: Canvas, spec: FrameSpec, apex_y: float) -> None:
    if not spec.tail or apex_y <= HOLE_Y:
        return
    span = apex_y - HOLE_Y
    body_x = CENTRE_X + _offset(spec, 1.0)
    for y in range(int(round(HOLE_Y)), int(round(apex_y)) + 1):
        if not (0 <= y < LOGICAL):
            continue
        progress = (y - HOLE_Y) / span if span > 0 else 1.0
        strand_x = CENTRE_X + (body_x - CENTRE_X) * progress
        half = 0.6 + 1.6 * progress
        key = "hoodie" if progress > 0.35 else "hoodie_dark"
        for x in range(LOGICAL):
            if abs(x - strand_x) <= half:
                canvas.set(x, y, key)


def _draw_aura(canvas: Canvas, silhouette: np.ndarray, spec: FrameSpec) -> None:
    if spec.glow <= 0.0:
        return
    near = _dilate(silhouette, 1) & ~silhouette
    far = _dilate(silhouette, 2) & ~_dilate(silhouette, 1)
    _wash(canvas, near, "glow", spec.glow)
    _wash(canvas, far, "glow", spec.glow * 0.5)


# --------------------------------------------------------------------------
# Floating terminal props: angle brackets, curly braces, a blinking cursor,
# and stray binary digits.
# --------------------------------------------------------------------------

_STAMP_LEGEND = {
    "#": "outline",
    "H": "hoodie",
    "L": "hoodie_light",
    "C": "cursor",
    "*": "glint",
    ".": "spark",
}

_ANGLE_BRACKETS = (
    "H....H",
    ".H..H.",
    "..HH..",
    "..HH..",
    ".H..H.",
    "H....H",
)

_CURLY_BRACES = (
    ".HH",
    "H..",
    "H..",
    ".HH",
    "H..",
    "H..",
    ".HH",
)

_BINARY = (
    "L.L",
    "LLL",
    "L.L",
)

_CURSOR_BLOCK = (
    "CCC",
    "CCC",
    "CCC",
)

_PROP_BRACKETS = (2, 20)
_PROP_BRACES = (56, 43)
_PROP_BINARY = ((44, 9), (10, 33), (57, 15), (12, 51))
_PROP_CURSOR = (2, 47)
_PROP_DUST = ((49, 20), (8, 42), (55, 55), (20, 8))


def _stamp(canvas: Canvas, sprite: tuple[str, ...], x0: int, y0: int) -> None:
    for dy, row in enumerate(sprite):
        for dx, char in enumerate(row):
            key = _STAMP_LEGEND.get(char)
            if key is not None:
                canvas.set(x0 + dx, y0 + dy, key)


def _draw_props(canvas: Canvas, spec: FrameSpec) -> None:
    if not spec.props:
        return

    lift_a = -spec.float_a
    lift_b = -spec.float_b

    _stamp(canvas, _ANGLE_BRACKETS, _PROP_BRACKETS[0], _PROP_BRACKETS[1] + lift_b)
    _stamp(canvas, _CURLY_BRACES, _PROP_BRACES[0], _PROP_BRACES[1] + lift_a)

    for index, (x0, y0) in enumerate(_PROP_BINARY):
        _stamp(canvas, _BINARY, x0, y0 + (lift_a if index % 2 else lift_b))

    # Blinking terminal cursor: a small solid cyan block that only appears
    # while ``cursor_on`` is true, i.e. it winks on/off across the idle loop
    # and the hop.
    if spec.cursor_on:
        _stamp(canvas, _CURSOR_BLOCK, _PROP_CURSOR[0], _PROP_CURSOR[1] + lift_a)

    for index, (x0, y0) in enumerate(_PROP_DUST):
        canvas.set(x0, y0 + (lift_b if index % 2 else lift_a), "spark")


# --------------------------------------------------------------------------
# Face: identical geometry to the researcher's/Pixelbit's.
# --------------------------------------------------------------------------

_EYE_CENTRE_Y = 26.0
_MOUTH_Y = 32.0
_BLUSH_Y = 30.0
_BLUSH_X = 11.0

_SPARKLE_OFFSETS = ((-13, -11), (13, -11), (-16, 5), (16, 5), (0, -19), (0, 11))


def _eye_box(spec: FrameSpec) -> tuple[int, int]:
    return (
        max(1, _round(EYE_WIDTH * spec.scale)),
        max(1, _round(EYE_HEIGHT * spec.scale)),
    )


def _eye_anchors(spec: FrameSpec) -> list[tuple[int, int]]:
    width, height = _eye_box(spec)
    centre_x, _centre_y = _map_point(spec, CENTRE_X, _EYE_CENTRE_Y)
    left_cx, left_cy = _map_point(spec, CENTRE_X - EYE_SPACING, _EYE_CENTRE_Y)
    left_x = _round(left_cx - (width - 1) / 2.0)
    top_y = _round(left_cy - (height - 1) / 2.0)
    right_x = _round(2.0 * centre_x - left_x - (width - 1))
    return [(left_x, top_y), (right_x, top_y)]


def _draw_eyes(canvas: Canvas, spec: FrameSpec) -> None:
    if spec.scale < 0.12:
        return
    width, height = _eye_box(spec)
    for x0, y0 in _eye_anchors(spec):
        if spec.blink:
            bar = y0 + height // 2
            for dx in range(width):
                canvas.set(x0 + dx, bar, "eye")
            continue
        rounded = width >= 3 and height >= 3
        for dy in range(height):
            for dx in range(width):
                if rounded and dx in (0, width - 1) and dy in (0, height - 1):
                    continue
                canvas.set(x0 + dx, y0 + dy, "eye")
        if width >= 3 and height >= 4:
            canvas.set(x0 + 1, y0 + 1, "glint")
            canvas.set(x0 + 2, y0 + 1, "glint")


def _draw_face(canvas: Canvas, spec: FrameSpec, body: np.ndarray) -> None:
    centre_x, mouth_y = _map_point(spec, CENTRE_X, _MOUTH_Y)
    mouth_x = _round(centre_x)
    row = _round(mouth_y)
    half = 1 + spec.smile
    for dx in range(-half, half + 1):
        depth = 1 if abs(dx) <= half - 1 else 0
        x, y = mouth_x + dx, row + depth
        if 0 <= x < LOGICAL and 0 <= y < LOGICAL and body[y, x]:
            canvas.set(x, y, "mouth")

    if spec.scale <= 0.8:
        return
    strength = 0.55 + 0.22 * spec.smile
    for side in (-1, 1):
        for dx in range(3):
            for dy in range(2):
                cheek_x, cheek_y = _map_point(
                    spec,
                    CENTRE_X + side * _BLUSH_X + (dx - 1) * side,
                    _BLUSH_Y + dy,
                )
                x, y = _round(cheek_x), _round(cheek_y)
                if 0 <= x < LOGICAL and 0 <= y < LOGICAL and body[y, x]:
                    canvas.blend(x, y, "blush", strength)


def _draw_sparkles(canvas: Canvas, spec: FrameSpec) -> None:
    if not spec.sparkles:
        return
    _centre_x, centre_y = _map_point(spec, CENTRE_X, 34.0)
    converge = 0.45 + 0.55 * spec.scale
    for dx, dy in _SPARKLE_OFFSETS[: spec.sparkles]:
        x = _round(CENTRE_X + dx * converge)
        y = _round(centre_y + dy * converge)
        if not (0 <= x < LOGICAL and 0 <= y < LOGICAL):
            continue
        canvas.set(x, y, "glint")
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if not canvas.occupied(nx, ny):
                canvas.set(nx, ny, "spark")


def compose(spec: FrameSpec, *, eyes: bool) -> Canvas:
    canvas = Canvas()
    layers = _frame_layers(spec)
    silhouette = np.zeros((LOGICAL, LOGICAL), dtype=bool)
    for name in _SOLID_LAYERS:
        silhouette |= layers[name]

    if spec.shadow:
        _draw_ground_shadow(canvas, spec)
    _draw_hole(canvas, spec)
    _, apex_y = _map_point(spec, CENTRE_X, _HEAD[1] - _HEAD[3])
    _draw_tail(canvas, spec, apex_y)

    _draw_props(canvas, spec)
    _draw_aura(canvas, silhouette, spec)

    for name, key in _PAINT_ORDER:
        canvas.fill(layers[name], key)

    canvas.fill(_boundary(silhouette), "outline")

    if eyes or spec.bake_eyes:
        _draw_eyes(canvas, spec)
    if spec.face:
        _draw_face(canvas, spec, layers["body"])

    _draw_sparkles(canvas, spec)
    return canvas


def build_frame(spec: FrameSpec, *, eyes: bool) -> Image.Image:
    return compose(spec, eyes=eyes).image()


def build_strip(*, eyes: bool) -> Image.Image:
    frames = _all_frames()
    strip = Image.new("RGBA", (LOGICAL * len(frames), LOGICAL), (0, 0, 0, 0))
    for index, spec in enumerate(frames):
        strip.paste(build_frame(spec, eyes=eyes), (index * LOGICAL, 0))
    return strip


def metadata() -> dict[str, object]:
    clips_meta: dict[str, dict[str, object]] = {}
    cursor = 0
    for name, clip in CLIPS.items():
        clip_frames: list[FrameSpec] = clip["frames"]  # type: ignore[assignment]
        clips_meta[name] = {
            "start": cursor,
            "count": len(clip_frames),
            "frame_ms": int(clip["frame_ms"]),  # type: ignore[arg-type]
            "loop": bool(clip["loop"]),
        }
        cursor += len(clip_frames)

    frames_meta = []
    for spec in _all_frames():
        width, height = _eye_box(spec)
        frames_meta.append(
            {
                "eyes": None if spec.bake_eyes else [list(a) for a in _eye_anchors(spec)],
                "eye_size": [width, height],
                "bob": spec.bob,
                "scale": spec.scale,
            }
        )

    return {
        "logical_size": LOGICAL,
        "frame_count": len(frames_meta),
        "ground_y": GROUND_Y,
        "eye": {"width": EYE_WIDTH, "height": EYE_HEIGHT},
        "clips": clips_meta,
        "frames": frames_meta,
        "palette": {key: list(value) for key, value in PALETTE.items()},
    }


_PREVIEW_CHARS = {
    "outline": "#",
    "eye": "O",
    "glint": "*",
    "skin": "-",
    "skin_shade": "=",
    "hoodie": "+",
    "hoodie_dark": "%",
    "hoodie_light": "l",
    "cursor": "c",
    "blush": "b",
    "mouth": "w",
    "shadow": ".",
    "glow": "'",
    "spark": ",",
    "void": "@",
    "void_rim": "o",
}


def preview(index: int) -> str:
    canvas = compose(_all_frames()[index], eyes=True)
    return "\n".join(
        "".join(" " if key is None else _PREVIEW_CHARS.get(key, "?") for key in row)
        for row in canvas.keys
    )


def _clip_of(index: int) -> str:
    cursor = 0
    for name, clip in CLIPS.items():
        count = len(clip["frames"])  # type: ignore[arg-type]
        if index < cursor + count:
            return f"{name}[{index - cursor}]"
        cursor += count
    return "?"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the Cody sprite sheets.")
    parser.add_argument("--preview", action="store_true", help="print frames as text instead of writing files")
    parser.add_argument("--frame", type=int, default=None, help="frame index to preview")
    args = parser.parse_args()

    if args.preview:
        indices = [args.frame] if args.frame is not None else [0, 6, 15, 23]
        for index in indices:
            print(f"--- frame {index} = {_clip_of(index)} ---")
            print(preview(index))
        return

    assets = Path(__file__).resolve().parent.parent / "web" / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    outputs = {
        "pixel_cody_strip.png": build_strip(eyes=True),
        "pixel_cody_base.png": build_strip(eyes=False),
    }
    for name, image in outputs.items():
        image.save(assets / name)
        print(f"wrote {name} {image.size[0]}x{image.size[1]}")

    (assets / "pixel_cody.json").write_text(
        json.dumps(metadata(), indent=2), encoding="utf-8"
    )
    print("wrote pixel_cody.json")


if __name__ == "__main__":
    main()
