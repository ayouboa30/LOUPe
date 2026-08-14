"""Generate the pixel-art "Pixelbit" mascot sprite sheets shipped in web/assets.

Pixelbit is the MATH-flavoured member of the mascot family: a round sage-green
blob in a bottle-green crew-neck sweater with a chalk-cream shirt collar
peeking out at the neck, surrounded by small floating chalkboard props (a
chalk stick, a mini slate, a set-square, and twinkling chalk-yellow math
marks). The whole app switches to a chalkboard-green theme when this
character is active (MATH task kind) - see web/style.css and web/app.js for
the theme switch, driven by the ``kind-select`` control.

This generator is a sibling of ``generate_pixel_researcher.py``, not a
variant of it: the two are kept as separate, independently maintained files
(same reasoning as the CLI-agent prompt template in ``three_loop/latent.py`` -
a second file is easier to reason about than a shared abstraction over two
characters whose *appearance* is the entire point of the difference). The
generic engine - the deformation pipeline (_map_point/_deform), the geometry
primitives (_disc/_capsule/_boundary/_dilate/_translate/_shade_band), the
Canvas class, and the eye/face drawing - is copied verbatim from the
researcher generator, because it is character-agnostic. Only the rest-pose
silhouette, the palette, and the floating props are specific to Pixelbit.

The grid, the four clips (idle/hop/vanish/return, 34 frames total) and their
timing are identical to the researcher's, on purpose: web/style.css computes
its background-position percentages from a fixed 34-frame, 64px-cell layout,
and any consumer that treats one character interchangeably with another
(the web avatar's CSS sprite) depends on that layout staying the same.

Two sheets are emitted:

* ``pixel_pixelbit_strip.png`` has the eyes baked in on every frame - CSS can
  only slide a background image around, so the web avatar needs complete
  frames.
* ``pixel_pixelbit_base.png`` leaves the eyes out of the idle/hop frames, for
  parity with the researcher's asset set in case a future desktop-companion
  variant wants to composite its own eyes; vanish/return keep baked eyes
  since nothing is tracking a cursor mid-implosion.

Run: python tools/generate_pixel_pixelbit.py [--preview [--frame N]]
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
# Grid and pose constants: identical to the researcher's, so the character
# shares the same on-screen size, ground line and bounce/implosion physics.
# --------------------------------------------------------------------------

LOGICAL = 80

#: The grid every coordinate below is *written* in. Shapes here are analytic,
#: so rasterising the same authored geometry on a finer grid produces real
#: extra detail rather than an upscale of the old pixels. Keeping authoring
#: at 64 means none of the hand-tuned coordinates needed rescaling.
AUTHOR = 64.0

#: Raster pixels per authored unit. Anything that indexes the canvas directly
#: multiplies by this; masks built from _disc/_capsule/_ring do not, because
#: those already evaluate in authoring space.
SCALE = LOGICAL / AUTHOR
CENTRE_X = (AUTHOR - 1) / 2.0
GROUND_Y = 58.0
REST_HEIGHT = 47.0
MAX_BOB = 7
HOLE_Y = 8.0

#: Bigger than the 6x7 they were: oversized eyes are the strongest kawaii
#: cue, and the finer grid is what makes room for them. Spacing widened to
#: match so the extra width does not close the gap between them.
EYE_WIDTH = 7
EYE_HEIGHT = 8
EYE_SPACING = 8.0

#: Chalkboard-green palette: near-black board green for contours, a soft sage
#: for the body, a darker bottle green for the sweater, chalk-cream for the
#: collar/cuffs/chalk stick, and chalk-yellow for one accent badge. No violet,
#: no neon - this is the character's whole visual signature versus the
#: researcher and versus Cody.
PALETTE: dict[str, tuple[int, int, int, int]] = {
    "outline": (20, 34, 26, 255),
    "eye": (26, 22, 18, 255),
    "glint": (255, 255, 255, 255),
    "chalk": (238, 235, 224, 255),        # collar, cuffs, chalk stick, gloss
    "sage": (183, 214, 182, 255),         # body base
    "sage_shade": (137, 173, 140, 255),   # body shading, feet, mitts
    "sweater": (58, 107, 74, 255),        # sweater mid tone
    "sweater_dark": (35, 74, 50, 255),    # sweater shade
    "chalk_yellow": (235, 205, 110, 255), # badge / math-mark accent
    "blush": (246, 168, 204, 120),
    "mouth": (36, 30, 26, 255),
    "shadow": (30, 46, 34, 64),
    "glow": (183, 214, 182, 74),
    "spark": (232, 240, 222, 205),
    "void": (14, 22, 17, 255),
    "void_rim": (58, 107, 74, 255),
}


@dataclass(frozen=True)
class FrameSpec:
    """One frame's deformation and dressing state.

    No ``tilt`` field here: the researcher uses it to rock a held magnifying
    glass, and Pixelbit holds nothing - both arms rest at its sides.
    """

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
            )
        )
    return frames


_HOP_FRAMES = [
    FrameSpec(squash_x=1.10, squash_y=0.88, glow=0.10, smile=0),
    FrameSpec(squash_x=0.93, squash_y=1.14, bob=1.5, glow=0.30, smile=1),
    FrameSpec(squash_x=0.96, squash_y=1.08, bob=4.0, glow=0.45, smile=2),
    FrameSpec(squash_x=1.00, squash_y=1.01, bob=float(MAX_BOB), glow=0.55, smile=2),
    FrameSpec(squash_x=0.97, squash_y=1.05, bob=5.0, glow=0.40, smile=2),
    FrameSpec(squash_x=0.94, squash_y=1.10, bob=1.5, glow=0.22, smile=1),
    FrameSpec(squash_x=1.13, squash_y=0.85, glow=0.08, smile=0),
    FrameSpec(squash_x=1.04, squash_y=0.96, glow=0.04, smile=1),
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
# Generic geometry engine, copied verbatim from generate_pixel_researcher.py:
# it describes shapes and deformation, not any one character.
# --------------------------------------------------------------------------

#: Raster indices expressed in *authoring* units, so every ellipse below keeps
#: the coordinates it was tuned with while being evaluated at the finer
#: LOGICAL resolution. The half-pixel terms sample at pixel centres.
_RY, _RX = np.mgrid[0:LOGICAL, 0:LOGICAL]
_YY = (_RY + 0.5) / SCALE - 0.5
_XX = (_RX + 0.5) / SCALE - 0.5


def _px(value: float) -> int:
    """Authoring unit -> raster pixel index, rounded half up."""

    return int(math.floor(value * SCALE + 0.5))


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
# Pixelbit's own rest pose: a round sage body in a bottle-green crew-neck
# sweater, a chalk-cream collar peeking at the neck, small resting arms with
# cream cuffs and a chalk-yellow badge.
# --------------------------------------------------------------------------

_HEAD = (CENTRE_X, 25.0, 15.0, 14.5)
_BELLY = (CENTRE_X, 43.0, 12.0, 11.5)

#: Small relaxed arms, both resting at the sides (Pixelbit holds no prop, so
#: unlike the researcher's magnifier-holding right arm, both are symmetric).
_HAND_L = (16.5, 47.0)
_HAND_R = (46.5, 47.0)
_ARM_ROOT_L = (19.0, 40.0)
_ARM_ROOT_R = (44.0, 40.0)

_BODY_SHADE_DEPTH = 3
_SWEATER_SHADE_DEPTH = 2

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

    # Shallow, mostly-horizontal crew neck: only a small 2px dip at the
    # centre, versus the researcher's 5px V - this is what makes it read as
    # a sweater rather than a lab coat.
    neckline = 36.0 + np.clip(2.0 - np.abs(_XX - CENTRE_X) * 0.5, 0.0, None)
    sweater = (body & (_YY >= neckline)) | arm_l | arm_r
    seam = _pinch_fill(body | sweater)
    sweater = sweater | seam

    # Shirt collar: a small chalk-cream band sitting just above the sweater's
    # centre dip - "the collar peeking out from under the sweater".
    collar = body & (_YY >= neckline - 3) & (_YY < neckline) & (np.abs(_XX - CENTRE_X) <= 5)

    badge = (_XX >= 24) & (_XX <= 26) & (_YY >= 45) & (_YY <= 47) & sweater

    # Cuffs: a small cream band partway down each sleeve, between the
    # sweater and the hand/mitt.
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
        "coat": sweater,          # kept as "coat" key-name-free: see below
        "sweater": sweater,
        "seam": seam,
        "collar": collar,
        "badge": badge,
        "hands": hands & ~sweater,
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
    sweater = layers["sweater"]

    extras = {
        "body_shade": _shade_band(body, _BODY_SHADE_DEPTH),
        "sweater_shade": _shade_band(sweater, _SWEATER_SHADE_DEPTH),
        "gloss": body & _disc(24.0, 17.5, 3.4, 2.7),
        "gloss_core": body & _disc(23.5, 17.0, 1.7, 1.3),
    }
    _REST_EXTRAS_CACHE = extras
    return extras


_DEFORMED_CACHE: dict[FrameSpec, dict[str, np.ndarray]] = {}

_PAINT_ORDER: tuple[tuple[str, str], ...] = (
    ("legs", "sage"),
    ("feet", "sage_shade"),
    ("body", "sage"),
    ("body_shade", "sage_shade"),
    ("gloss", "chalk"),
    ("gloss_core", "glint"),
    ("sweater", "sweater"),
    ("seam", "sweater"),
    ("sweater_shade", "sweater_dark"),
    ("collar", "chalk"),
    ("badge", "chalk_yellow"),
    ("hands", "sage"),
    ("cuffs", "chalk"),
    ("mitts", "sage_shade"),
)

_SOLID_LAYERS = ("legs", "feet", "body", "sweater", "seam", "hands", "mitts")


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
        key = "sweater" if progress > 0.35 else "sweater_dark"
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
# Floating chalkboard props: a chalk stick, a mini slate, a set-square, and
# four twinkling math marks (+, =, a dot, a small burst) cycling through the
# same twinkle mechanism the researcher uses for its stars.
# --------------------------------------------------------------------------

_STAMP_LEGEND = {
    "#": "outline",
    "C": "chalk",
    "*": "glint",
    ".": "spark",
}

_SLATE = (
    "######",
    "#....#",
    "#.+=.#",
    "#....#",
    "######",
)

_CHALK_STICK = (
    ".CC..",
    "CC...",
    "C....",
)

_SET_SQUARE = (
    "C......",
    "CC.....",
    "C.C....",
    "C..C...",
    "C...C..",
    "CCCCCCC",
)

#: Four twinkle states for the floating math marks: a lone dot, a plus, an
#: equals sign, and a small burst - cycling the *appearance* rather than
#: fading a single glyph is how sparkle reads at this size (mirrors the
#: researcher's _STARS).
_MARKS = (
    (
        "     ",
        "     ",
        "  .  ",
        "     ",
        "     ",
    ),
    (
        "     ",
        "  .  ",
        " *.* ",
        "  .  ",
        "     ",
    ),
    (
        "     ",
        " ... ",
        "     ",
        " ... ",
        "     ",
    ),
    (
        "  .  ",
        " . . ",
        ".   .",
        " . . ",
        "  .  ",
    ),
)

_PROP_SLATE = (2, 47)
_PROP_CHALK = (56, 43)
_PROP_SQUARE = (2, 20)
_PROP_MARKS = ((44, 9), (57, 15), (10, 33), (12, 51))
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

    _stamp(canvas, _SET_SQUARE, _px(_PROP_SQUARE[0]), _px(_PROP_SQUARE[1] + lift_b))
    _stamp(canvas, _SLATE, _px(_PROP_SLATE[0]), _px(_PROP_SLATE[1] + lift_b))
    _stamp(canvas, _CHALK_STICK, _px(_PROP_CHALK[0]), _px(_PROP_CHALK[1] + lift_a))

    for index, (x0, y0) in enumerate(_PROP_MARKS):
        # chalk-yellow marks with the occasional glint accent: recolour the
        # generic '.' -> spark default by drawing marks with a dedicated
        # legend where '.' reads as the chalk-yellow accent instead.
        sprite = _MARKS[(spec.twinkle + index) % len(_MARKS)]
        _stamp_marks(canvas, sprite, x0, y0 + (lift_a if index % 2 else lift_b))

    for index, (x0, y0) in enumerate(_PROP_DUST):
        canvas.set(_px(x0), _px(y0 + (lift_b if index % 2 else lift_a)), "spark")


def _stamp_marks(canvas: Canvas, sprite: tuple[str, ...], x0: int, y0: int) -> None:
    """Like ``_stamp``, but for the math marks: '.' is chalk-yellow, '*' is glint."""

    for dy, row in enumerate(sprite):
        for dx, char in enumerate(row):
            if char == ".":
                canvas.set(x0 + dx, y0 + dy, "chalk_yellow")
            elif char == "*":
                canvas.set(x0 + dx, y0 + dy, "glint")


# --------------------------------------------------------------------------
# Face: identical geometry to the researcher's, only the palette keys differ
# in colour (not in name), so this code is a straight copy.
# --------------------------------------------------------------------------

_EYE_CENTRE_Y = 26.0
_MOUTH_Y = 32.0
_BLUSH_Y = 30.0
_BLUSH_X = 11.0

_SPARKLE_OFFSETS = ((-13, -11), (13, -11), (-16, 5), (16, 5), (0, -19), (0, 11))


def _eye_box(spec: FrameSpec) -> tuple[int, int]:
    # Returned in *raster* pixels: _draw_eyes and native_widget both step this
    # box one canvas pixel at a time, and the JSON publishes the same numbers.
    return (
        max(1, _px(EYE_WIDTH * spec.scale)),
        max(1, _px(EYE_HEIGHT * spec.scale)),
    )


def _eye_anchors(spec: FrameSpec) -> list[tuple[int, int]]:
    width, height = _eye_box(spec)
    centre_x, _centre_y = _map_point(spec, CENTRE_X, _EYE_CENTRE_Y)
    left_cx, left_cy = _map_point(spec, CENTRE_X - EYE_SPACING, _EYE_CENTRE_Y)
    # _map_point works in authoring units, the canvas is indexed in raster
    # pixels: both centres cross over here, before the rounding, so the
    # mirror below still lands on a whole pixel.
    left_x = _round(left_cx * SCALE - (width - 1) / 2.0)
    top_y = _round(left_cy * SCALE - (height - 1) / 2.0)
    right_x = _round(2.0 * centre_x * SCALE - left_x - (width - 1))
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
        # Corner radius grows with the eye: knocking off a single pixel of a
        # 9x10 eye leaves a rectangle, and rectangular eyes are the fastest
        # way to make a face look dead rather than cute.
        corner = max(1, round(width / 5))
        for dy in range(height):
            for dx in range(width):
                near_x = min(dx, width - 1 - dx)
                near_y = min(dy, height - 1 - dy)
                if near_x + near_y < corner:
                    continue  # corner left to the body: that is the rounding
                canvas.set(x0 + dx, y0 + dy, "eye")
        # The glint scales with the eye too. Held at two fixed pixels it
        # became a speck on the bigger eye and the face lost its spark.
        gw, gh = max(2, round(width / 2.6)), max(2, round(height / 3.2))
        for dy in range(gh):
            for dx in range(gw):
                canvas.set(x0 + corner + dx, y0 + corner + dy, "glint")


def _draw_face(canvas: Canvas, spec: FrameSpec, body: np.ndarray) -> None:
    centre_x, mouth_y = _map_point(spec, CENTRE_X, _MOUTH_Y)
    # _map_point answers in authoring units and the canvas is indexed in
    # raster pixels: without this crossing the mouth lands high and left of
    # the face, where the body clip silently discards it.
    mouth_x = _round(centre_x * SCALE)
    row = _round(mouth_y * SCALE)
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
                x, y = _round(cheek_x * SCALE), _round(cheek_y * SCALE)
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

    # Mouth and cheeks go down *before* the eyes so the eyes always win where
    # they overlap: the desktop companion draws its own eyes over the eyeless
    # sheet at runtime and can only ever put them on top, so baking the blush
    # over an eye here would make the two paths disagree by a pixel.
    if spec.face:
        _draw_face(canvas, spec, layers["body"])
    if eyes or spec.bake_eyes:
        _draw_eyes(canvas, spec)

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
        # Published in raster pixels: both consumers measure these against the
        # emitted image, not against the authoring grid.
        "ground_y": GROUND_Y * SCALE,
        "eye": {"width": _px(EYE_WIDTH), "height": _px(EYE_HEIGHT)},
        "clips": clips_meta,
        "frames": frames_meta,
        "palette": {key: list(value) for key, value in PALETTE.items()},
    }


_PREVIEW_CHARS = {
    "outline": "#",
    "eye": "O",
    "glint": "*",
    "chalk": "W",
    "sage": "-",
    "sage_shade": "=",
    "sweater": "+",
    "sweater_dark": "%",
    "chalk_yellow": "y",
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
    parser = argparse.ArgumentParser(description="Generate the Pixelbit sprite sheets.")
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
        "pixel_pixelbit_strip.png": build_strip(eyes=True),
        "pixel_pixelbit_base.png": build_strip(eyes=False),
    }
    for name, image in outputs.items():
        image.save(assets / name)
        print(f"wrote {name} {image.size[0]}x{image.size[1]}")

    (assets / "pixel_pixelbit.json").write_text(
        json.dumps(metadata(), indent=2), encoding="utf-8"
    )
    print("wrote pixel_pixelbit.json")


if __name__ == "__main__":
    main()
