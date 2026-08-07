"""Generate the pixel-art researcher mascot sprite sheets shipped in web/assets.

The art is produced by code rather than hand-drawn for the same reason the
slime mascot is (tools/generate_pixel_mascot.py): 34 frames x 4 sheets x 5
icon sizes is 140 hand-drawn images that would drift apart pixel by pixel.
Here one palette, one rest-pose description and one deformation pipeline drive
every frame, so a tweak to the lab coat or the magnifier lands identically on
the web avatar, on the desktop companion and on the executable icon.

The character: a very round lavender blob in a small white lab coat, big
glossy eyes, a violet badge on the chest, tiny arms and legs, holding a
magnifying glass, surrounded by small floating research props (stars, a vial,
a DNA helix, a book stack, an atom, dust particles). The "watch" variant adds
a thought bubble holding a bubbling violet vial, which is how the assistant
signals that it is off doing research.

Four clips live in every sheet (34 frames total, contiguous, in this order):

* ``idle``   - 12 frames: a ~1px breath, a slow violet aura that swells and
  fades across the whole loop, twinkling stars, floating books, a bubbling
  vial and a magnifier that rocks between three discrete orientations.
* ``hop``    - 8 frames: anticipation squash, launch stretch, apex float,
  landing squash, recovery. Squash and stretch are volume preserving (wider
  as it flattens, narrower as it stretches), which is what makes a blob read
  as springy rather than merely scaled.
* ``vanish`` - 7 frames: the "sucked into a black hole" exit. A hole opens
  overhead, the body is stretched thin, twisted and pulled up into a point,
  leaving sparkles behind.
* ``return`` - 7 frames: the reverse, with a landing overshoot so it pops
  back into place instead of fading in.

Why the magnifier does not really rotate: at a 64px logical grid a real
rotation of a 10px ring produces ragged, uneven pixels. Instead the ring and
handle geometry is rotated around the hand pivot by -8 / 0 / +8 degrees
*before* rasterising, and frames alternate between those three discrete
orientations. The eye reads it as a gentle rock; the pixels stay clean.

Four sheets are emitted because the consumers need different things:

* ``pixel_researcher_strip.png`` / ``pixel_researcher_watch_strip.png`` have
  the eyes baked in. CSS can only slide a background image around, so the web
  avatar needs complete frames (it even gets a baked blink).
* ``pixel_researcher_base.png`` / ``pixel_researcher_watch_base.png`` leave
  the eyes out of the ``idle``/``hop`` frames. The native widget composites
  those itself so the character can follow the cursor and blink without one
  sheet per look direction. ``vanish``/``return`` frames keep their eyes baked
  - eye size has to follow the shrinking body there, and nothing is tracking a
  cursor mid-implosion. ``pixel_researcher.json`` says which frames are which.

Run: python tools/generate_pixel_researcher.py [--preview [--frame N] [--watch]]
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
from PIL import Image

#: Logical pixel grid. Everything is authored at this size and scaled up with
#: nearest-neighbour at display time, which is what keeps the pixels crisp
#: instead of turning into a smudge.
LOGICAL = 64

#: Horizontal centre of the canvas. 31.5 on an even grid, so any shape built
#: symmetrically around it comes out perfectly mirrored.
CENTRE_X = (LOGICAL - 1) / 2.0

#: Ground line, in logical pixels: the row the feet rest on. Published in the
#: JSON as ``ground_y`` because the desktop companion uses it to keep the
#: character standing on the taskbar edge instead of sinking under it.
GROUND_Y = 58.0

#: Height of the character at rest, from the ground line to the top of the
#: head (row 11). Used to normalise vertical position for the lean/swirl.
REST_HEIGHT = 47.0

#: Highest hop, in logical pixels. Capped so the head still fits inside the
#: canvas at the apex instead of being clipped.
MAX_BOB = 7

#: Where the black hole opens, in logical pixels from the top of the canvas.
HOLE_Y = 8.0

#: Eye geometry at scale 1.0, in logical pixels. Mirrored into the JSON
#: metadata so the generator and the native widget cannot drift apart.
EYE_WIDTH = 6
EYE_HEIGHT = 7
EYE_SPACING = 7.5  # distance from the face centre to each eye centre

#: Deliberately small palette: pure white, off white, lavender, three violets,
#: a near-black violet for outlines, and one very discreet pink for the
#: cheeks. Everything else (aura, shadow, sparkles, the void) is drawn from
#: the same violets at reduced alpha, so no new hue ever sneaks in.
PALETTE: dict[str, tuple[int, int, int, int]] = {
    "outline": (38, 24, 68, 255),        # contour violet tres sombre
    "eye": (28, 16, 52, 255),            # eyes only - never reused elsewhere
    "glint": (255, 255, 255, 255),       # blanc pur: reflets
    "coat": (241, 238, 252, 255),        # blanc casse: la blouse
    "lavender": (215, 203, 255, 255),
    "violet_light": (172, 146, 251, 255),
    "violet_mid": (134, 92, 240, 255),
    "violet_dark": (96, 56, 190, 255),
    "blush": (246, 168, 204, 120),       # rose tres discret, pose en alpha
    "mouth": (52, 32, 92, 255),
    "shadow": (60, 34, 110, 64),
    "glow": (172, 146, 251, 74),         # aura, alpha modulee par frame
    "spark": (233, 226, 255, 205),
    "void": (16, 9, 32, 255),
    "void_rim": (134, 92, 240, 255),
}


@dataclass(frozen=True)
class FrameSpec:
    """One frame's deformation and dressing state."""

    squash_x: float = 1.0
    squash_y: float = 1.0
    bob: float = 0.0
    #: Phase of the jelly lean; also drives the breath in the idle clip.
    phase: float = 0.0
    #: Overall size. Only the vanish/return clips use anything but 1.0.
    scale: float = 1.0
    #: Extra horizontal shear toward the top, in logical pixels: the twist of
    #: being pulled into something.
    swirl: float = 0.0
    shadow: bool = True
    sparkles: int = 0
    #: Draw the eyes even on the "base" sheets the native widget uses.
    bake_eyes: bool = False
    #: Face and prop detail is dropped once the body is too small to carry it.
    face: bool = True
    #: Size of the black hole overhead, 0 = none, 1 = fully open.
    hole: float = 0.0
    #: Draw the taffy strand from the body up into the hole. This is what makes
    #: the implosion read as *being pulled* rather than merely scaling down: a
    #: shrinking blob on its own looks like a zoom-out.
    tail: bool = False
    #: Floating props + thought bubble. Off while the character is imploding,
    #: since a calm halo of books around a black hole reads as a bug.
    props: bool = True
    #: Violet aura strength, 0..1.
    glow: float = 0.0
    #: Magnifier orientation: -1 tilted left, 0 neutral, +1 tilted right.
    tilt: int = 0
    #: Star appearance index (0..3) and bubbling/float phases. Assigned from
    #: the global frame index in _all_frames so props keep animating across
    #: clip boundaries.
    twinkle: int = 0
    bubble: int = 0
    float_a: int = 0
    float_b: int = 0
    #: Smile width, 0 small .. 2 widest. The cheeks redden with it.
    smile: int = 1
    #: Baked blink, for the eyes-included sheets only. The widget blinks the
    #: base sheets on its own schedule.
    blink: bool = False


def _tilt_from(value: float) -> int:
    """Quantise a smooth -1..1 signal into the three magnifier orientations.

    The dead zone in the middle is what keeps the neutral pose on screen long
    enough to be seen instead of flickering straight past it.
    """

    if value > 0.5:
        return 1
    if value < -0.5:
        return -1
    return 0


def _idle_frames() -> list[FrameSpec]:
    """Twelve frames of breathing, with the aura swelling over the full loop."""

    frames: list[FrameSpec] = []
    for index in range(12):
        phase = 2.0 * math.pi * index / 12.0
        breath = math.sin(phase)
        # +-1px on a 47px body: squash_x and squash_y move in opposite
        # directions so the volume stays put and it reads as breathing.
        frames.append(
            FrameSpec(
                squash_x=1.0 - 0.022 * breath,
                squash_y=1.0 + 0.026 * breath,
                bob=1.0 if breath > 0.6 else 0.0,
                phase=phase,
                # One full swell per loop, starting and ending at zero so the
                # cycle is seamless when the clip repeats.
                glow=0.5 - 0.5 * math.cos(phase),
                tilt=_tilt_from(breath),
                smile=2 if breath > 0.35 else (1 if breath > -0.45 else 0),
                # Baked blink for the web sheet, on the two frames where the
                # smile is already relaxing.
                blink=index in (9, 10),
            )
        )
    return frames


#: The bounce, keyed by hand rather than by a formula: the timing of a hop is
#: the whole point (slow anticipation, fast launch, float at the apex, hard
#: landing), and an easing curve flattens exactly those accents.
_HOP_FRAMES = [
    FrameSpec(squash_x=1.10, squash_y=0.88, tilt=-1, glow=0.10, smile=0),          # crouch
    FrameSpec(squash_x=0.93, squash_y=1.14, bob=1.5, glow=0.30, smile=1),          # launch
    FrameSpec(squash_x=0.96, squash_y=1.08, bob=4.0, tilt=1, glow=0.45, smile=2),  # rising
    FrameSpec(squash_x=1.00, squash_y=1.01, bob=float(MAX_BOB), tilt=1,
              glow=0.55, smile=2),                                                # apex
    FrameSpec(squash_x=0.97, squash_y=1.05, bob=5.0, tilt=1, glow=0.40, smile=2),  # falling
    FrameSpec(squash_x=0.94, squash_y=1.10, bob=1.5, glow=0.22, smile=1),          # pre-impact
    FrameSpec(squash_x=1.13, squash_y=0.85, tilt=-1, glow=0.08, smile=0),          # landing
    FrameSpec(squash_x=1.04, squash_y=0.96, tilt=-1, glow=0.04, smile=1),          # recover
]

#: Being sucked in: a hole opens overhead, the character crouches, then gets
#: drawn up into it as a thinning strand until only sparkles remain. Eyes stay
#: baked here and shrink with the body.
#:
#: Swirl is kept small on purpose. The 32px generator learned this the hard
#: way: a twist of a few logical pixels tears the last frames apart, because
#: once the body is barely two pixels wide a shear that large displaces each
#: row further than the row is wide, so the silhouette stops being connected
#: and renders as scattered specks. _offset damps it by ``scale`` for the same
#: reason.
_VANISH_FRAMES = [
    FrameSpec(squash_x=1.12, squash_y=0.88, bake_eyes=True, props=False,
              smile=0, glow=0.15),                                             # crouch
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

#: Coming back: spat out of the closing hole, then an overshoot on landing so
#: it pops into place instead of fading in.
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
              smile=0, glow=0.2),                                              # impact
    FrameSpec(squash_x=0.94, squash_y=1.08, bob=1.5, bake_eyes=True,
              props=False, smile=2, glow=0.15),                                # rebound
    FrameSpec(squash_x=1.03, squash_y=0.97, bake_eyes=True, props=False,
              smile=2, glow=0.1),                                              # settle
]

CLIPS: dict[str, dict[str, object]] = {
    "idle": {"frames": _idle_frames(), "frame_ms": 150, "loop": True},
    "hop": {"frames": _HOP_FRAMES, "frame_ms": 70, "loop": False},
    "vanish": {"frames": _VANISH_FRAMES, "frame_ms": 65, "loop": False},
    "return": {"frames": _RETURN_FRAMES, "frame_ms": 65, "loop": False},
}


_ALL_FRAMES_CACHE: list[FrameSpec] | None = None


def _all_frames() -> list[FrameSpec]:
    """The 34 frames, in clip order, with the prop phases filled in.

    Twinkle/bubble/float phases are derived from the *global* frame index so
    the props keep animating at their own tempo instead of resetting whenever
    a clip starts. Everything is a pure function of the index, so two runs of
    this script produce byte-identical art.
    """

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
            bubble=index % 6,
            float_a=1 if (index // 3) % 2 == 0 else 0,
            float_b=1 if (index // 2) % 2 == 0 else 0,
        )
        for index, spec in enumerate(frames)
    ]
    return _ALL_FRAMES_CACHE


# --------------------------------------------------------------------------
# Rest pose: the character is described once, in "rest space", as a set of
# boolean masks. Every frame is that description pushed through _deform, which
# is the only place squash/stretch/scale/swirl live. Adding a prop therefore
# never means re-deriving its deformation maths.
# --------------------------------------------------------------------------

_YY, _XX = np.mgrid[0:LOGICAL, 0:LOGICAL]

#: Head and belly are two overlapping discs. Their union gives the soft
#: cloud/droplet waist asked for, with no straight edges anywhere - which is
#: exactly why it is built from discs rather than from a profile table.
_HEAD = (CENTRE_X, 25.0, 15.0, 14.5)   # cx, cy, rx, ry
_BELLY = (CENTRE_X, 43.0, 12.0, 11.5)

#: Hand that holds the magnifier, and the pivot the three tilt variants of the
#: magnifier are rotated around.
_HAND_R = (48.5, 41.0)
_HAND_L = (15.5, 45.5)

#: Magnifier at rest, before tilting: ring centre relative to the pivot.
_LENS_OFFSET = (4.5, -9.0)
_LENS_OUTER = 5.0
_LENS_INNER = 3.1
_TILT_DEGREES = 8.0


def _disc(cx: float, cy: float, rx: float, ry: float) -> np.ndarray:
    """Filled ellipse, tested at pixel centres."""

    return ((_XX - cx) / max(rx, 1e-6)) ** 2 + ((_YY - cy) / max(ry, 1e-6)) ** 2 <= 1.0


def _capsule(p0: tuple[float, float], p1: tuple[float, float], half: float) -> np.ndarray:
    """Rounded bar between two points: how arms, legs and the handle are made."""

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


def _ring(cx: float, cy: float, outer: float, inner: float) -> np.ndarray:
    return _disc(cx, cy, outer, outer) & ~_disc(cx, cy, inner, inner)


def _boundary(mask: np.ndarray) -> np.ndarray:
    """Outermost ring of ``mask``, used as the 1px outline.

    Taken from *inside* the shape rather than grown outside it: growing would
    inflate the silhouette on every frame and make the bounce look wobbly.
    """

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


def _lens_geometry(tilt: int) -> tuple[float, float, float, float]:
    """Magnifier ring centre and handle root for one of the three tilts.

    The geometry is rotated around the hand before rasterising - see the module
    docstring for why a real pixel rotation is not an option at this size.
    """

    angle = math.radians(_TILT_DEGREES * tilt)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    vx, vy = _LENS_OFFSET
    rx = vx * cos_a - vy * sin_a
    ry = vx * sin_a + vy * cos_a
    cx, cy = _HAND_R[0] + rx, _HAND_R[1] + ry
    length = math.hypot(rx, ry)
    # Handle stops at the ring's inner edge so it does not cross the glass.
    root_x = cx - rx / length * (_LENS_INNER + 0.4)
    root_y = cy - ry / length * (_LENS_INNER + 0.4)
    return cx, cy, root_x, root_y


_REST_CACHE: dict[int, dict[str, np.ndarray]] = {}


def _rest_layers(tilt: int) -> dict[str, np.ndarray]:
    """Every part of the character as a rest-space mask, keyed by paint order."""

    cached = _REST_CACHE.get(tilt)
    if cached is not None:
        return cached

    head = _disc(*_HEAD)
    belly = _disc(*_BELLY)
    body = head | belly

    # Legs: two short capsules poking below the belly, symmetric about 31.5.
    legs = _capsule((26.0, 52.0), (26.0, 56.0), 2.5) | _capsule(
        (37.0, 52.0), (37.0, 56.0), 2.5
    )
    feet = legs & (_YY >= 56)

    # Very short arms. The right one is angled up so it can hold the lens.
    arm_l = _capsule((20.0, 41.0), _HAND_L, 2.6)
    arm_r = _capsule((43.0, 40.0), _HAND_R, 2.6)
    hands = _disc(_HAND_L[0], _HAND_L[1], 2.4, 2.4) | _disc(
        _HAND_R[0], _HAND_R[1], 2.4, 2.4
    )

    # Lab coat: the belly half of the body, with a V neck cut out so the
    # lavender chest shows through and the collar reads as lapels.
    v_neck = 36.0 + np.clip(5.0 - np.abs(_XX - CENTRE_X) * 0.9, 0.0, None)
    coat = (body & (_YY >= v_neck)) | arm_l | arm_r
    coat_edge = _boundary(coat) & ~_dilate(~body, 1)  # inner rim, for shading

    badge = (_XX >= 24) & (_XX <= 26) & (_YY >= 45) & (_YY <= 47) & coat

    lens_cx, lens_cy, root_x, root_y = _lens_geometry(tilt)
    handle = _capsule(_HAND_R, (root_x, root_y), 1.5)
    rim = _ring(lens_cx, lens_cy, _LENS_OUTER, _LENS_INNER)
    glass = _disc(lens_cx, lens_cy, _LENS_INNER, _LENS_INNER)

    layers = {
        "legs": legs & ~body,
        "feet": feet & ~body,
        "body": body,
        "coat": coat,
        "coat_edge": coat_edge,
        "badge": badge,
        "hands": hands & ~coat,
        "handle": handle & ~glass,
        "rim": rim,
        "glass": glass,
    }
    _REST_CACHE[tilt] = layers
    return layers


def _offset(spec: FrameSpec, t: float) -> float:
    """Horizontal displacement of the row at height ``t`` (0 ground, 1 head top).

    Two contributions: a gentle jelly lean, strongest mid-body and zero at both
    ends, and the swirl, which grows toward the top so the body twists as it is
    pulled in.

    The swirl is damped by ``scale`` so it can never displace a row further
    than a shrunken body is wide, which would break the silhouette into
    disconnected specks.
    """

    lean = 0.8 * math.sin(spec.phase) * math.sin(t * math.pi)
    return lean + spec.swirl * t * spec.scale


def _map_point(spec: FrameSpec, x: float, y: float) -> tuple[float, float]:
    """Rest-space point -> frame-space point, for this frame's deformation."""

    scale_y = spec.squash_y * spec.scale
    scale_x = spec.squash_x * spec.scale
    above = GROUND_Y - y
    out_y = GROUND_Y - spec.bob - above * scale_y
    t = min(1.0, max(0.0, above / REST_HEIGHT))
    out_x = CENTRE_X + (x - CENTRE_X) * scale_x + _offset(spec, t)
    return out_x, out_y


def _deform(mask: np.ndarray, spec: FrameSpec) -> np.ndarray:
    """Push a rest-space mask through this frame's deformation.

    Forward scatter (source pixel -> the rectangle it covers in the output)
    rather than inverse sampling, because it behaves at both ends: stretching
    fills the gaps between mapped centres, and shrinking still lands every
    source pixel somewhere, so a body squeezed to two pixels stays a connected
    blob instead of dissolving into speckle.
    """

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
        if x1 < x0:  # footprint narrower than a pixel: keep the nearest one
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
    """A logical frame plus a parallel grid of palette keys.

    The key grid is what makes ``--preview`` trustworthy: the ASCII dump prints
    what was actually painted instead of guessing from RGB distances, which
    matters as soon as two palette entries are close or semi transparent.
    """

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
        """Alpha-composite a colour over what is already there."""

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


# --------------------------------------------------------------------------
# Floating props. These are hand-authored micro sprites rather than generated
# shapes: at 5-7px a formula has no room to be cute, and a literal picture in
# the source is easier to review than a circle equation.
# --------------------------------------------------------------------------

_STAMP_LEGEND = {
    "#": "outline",
    "W": "coat",
    "L": "lavender",
    "l": "violet_light",
    "V": "violet_mid",
    "D": "violet_dark",
    "*": "glint",
    ".": "spark",
    "-": "lavender",
}

_ATOM = (
    "  ...  ",
    " .   . ",
    "*. V .*",
    " .   . ",
    "  ...  ",
)

_DNA = (
    "l---l",
    " l l ",
    "  l  ",
    " l l ",
    "l---l",
    " l l ",
    "  l  ",
)

_BOOKS = (
    " VVVV ",
    " WWWW ",
    "LLLLLL",
    "WWWWWW",
    " VVVVV",
    " WWWWW",
)

#: The four twinkle states a star cycles through: a lone dot, a small cross, a
#: full star and a hollow one. Cycling the *appearance* rather than fading a
#: single shape is how sparkle reads at this size.
_STARS = (
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
        " .*. ",
        "  .  ",
        "     ",
    ),
    (
        "  .  ",
        "  *  ",
        ".***.",
        "  *  ",
        "  .  ",
    ),
    (
        "  .  ",
        " . . ",
        ".   .",
        " . . ",
        "  .  ",
    ),
)


def _stamp(canvas: Canvas, sprite: tuple[str, ...], x0: int, y0: int) -> None:
    for dy, row in enumerate(sprite):
        for dx, char in enumerate(row):
            key = _STAMP_LEGEND.get(char)
            if key is not None:
                canvas.set(x0 + dx, y0 + dy, key)


def _draw_vial(canvas: Canvas, x0: int, y0: int, phase: int) -> None:
    """A 5x8 test tube of bubbling violet liquid.

    The level rises and falls by one pixel and two bubbles walk up the tube, on
    a six frame cycle - enough to read as boiling without becoming noisy.
    """

    glass_rows = range(y0 + 3, y0 + 8)
    for dy in range(3):  # neck
        canvas.set(x0 + 1, y0 + dy, "outline")
        canvas.set(x0 + 3, y0 + dy, "outline")
        canvas.set(x0 + 2, y0 + dy, "lavender" if dy == 0 else "coat")
    for y in glass_rows:  # walls
        canvas.set(x0, y, "outline")
        canvas.set(x0 + 4, y, "outline")
        for dx in range(1, 4):
            canvas.set(x0 + dx, y, "coat")
    canvas.set(x0, y0 + 3, "outline")
    for dx in range(1, 4):  # rounded bottom
        canvas.set(x0 + dx, y0 + 8, "outline")

    level = y0 + 5 - (1 if phase % 3 == 0 else 0)
    for y in range(level, y0 + 8):
        for dx in range(1, 4):
            canvas.set(x0 + dx, y, "violet_mid")
    # Two bubbles climbing the liquid, positions fixed per phase so the art is
    # reproducible byte for byte.
    for index, (bx, offset) in enumerate(((1, 0), (3, 3))):
        bubble_y = y0 + 7 - ((phase + offset) % 3)
        if bubble_y >= level:
            canvas.set(x0 + bx + (index % 2), bubble_y, "violet_light")
    canvas.set(x0 + 1, y0 + 4, "glint")  # highlight on the glass


def _draw_thought_bubble(canvas: Canvas, spec: FrameSpec) -> None:
    """Cloud in the upper left holding a violet vial: the research marker.

    It is anchored to the canvas rather than to the body: the head sweeps up to
    row 3 at the hop apex, and anything parented to the head would either be
    clipped off the top or crash into the cloud.
    """

    cloud = (
        _disc(7.0, 7.0, 5.2, 5.2)
        | _disc(12.5, 10.0, 4.2, 4.2)
        | _disc(6.0, 12.5, 4.6, 4.6)
    )
    canvas.fill(cloud, "coat")
    canvas.fill(_boundary(cloud), "outline")
    _draw_vial(canvas, 5, 4, spec.bubble)
    # Trail: a round puff below the cloud, then a single pixel near the head.
    puff = _disc(12.5, 18.5, 1.9, 1.9)
    canvas.fill(puff, "coat")
    canvas.fill(_boundary(puff), "outline")
    canvas.set(15, 21, "coat")


# --------------------------------------------------------------------------
# Face geometry and lighting. Both are described in *rest space* and pushed
# through _map_point / _deform like everything else, so the face follows the
# squash, the lean and the implosion without a second set of maths.
# --------------------------------------------------------------------------

#: Rest-space centre of the eyes. Slightly below the head's own centre (25):
#: eyes low on a round head is the whole kawaii trick - centred eyes read as
#: an adult, high ones as a cartoon villain.
_EYE_CENTRE_Y = 26.0

#: Rest-space rows the mouth and the cheeks live on, and how far out from the
#: face centre the cheeks sit.
_MOUTH_Y = 32.0
_BLUSH_Y = 30.0
_BLUSH_X = 11.0

#: Light from the upper left, as a shade band hugging the lower-right edge of
#: whatever it is applied to: ``_shade_band(mask, n)`` keeps the pixels whose
#: neighbour ``n`` steps down-and-right is outside the shape.
#:
#: A single global terminator (``x + y > k``) was tried first and is wrong here:
#: the belly is simply *lower* on the canvas than the head, so one diagonal put
#: the entire lab coat on the dark side and the coat stopped reading as white at
#: all. A band measured per shape shades every part - head, coat, both sleeves -
#: relative to its own silhouette, with one rule and no per-part constants.
_BODY_SHADE_DEPTH = 3
_COAT_SHADE_DEPTH = 2

#: Twinkles left behind by the implosion, as offsets from the body centre.
#: Fixed rather than random so every regeneration produces identical art, and
#: mirrored in pairs so the burst reads as one event radiating outward instead
#: of as stray dots.
_SPARKLE_OFFSETS = ((-13, -11), (13, -11), (-16, 5), (16, 5), (0, -19), (0, 11))

#: Where the floating research props sit, as the top-left corner of each micro
#: sprite. Chosen to clear the character at every deformation *and* the thought
#: bubble of the watch variant: the props are drawn under the character, so an
#: overlap would not corrupt the silhouette, but a book poking out of the belly
#: still reads as a bug.
_PROP_ATOM = (51, 3)
_PROP_DNA = (2, 20)
_PROP_BOOKS = (2, 47)
_PROP_VIAL = (56, 43)
_PROP_STARS = ((44, 9), (57, 15), (10, 33), (12, 51))
_PROP_DUST = ((49, 20), (8, 42), (55, 55), (20, 8))


def _round(value: float) -> int:
    """Round half up, on both signs of the axis.

    ``round()`` is banker's rounding: ``round(21.5) == 22`` but
    ``round(36.5) == 36``, which silently breaks the mirror symmetry of a face
    built around 31.5 by one pixel.
    """

    return int(math.floor(value + 0.5))


def _eye_box(spec: FrameSpec) -> tuple[int, int]:
    """Eye size for this frame: it has to shrink with an imploding body.

    Only ``scale`` is honoured, not the squash: distorting the eyes with every
    breath makes a blob look deflated. Their *spacing* does follow the squash,
    because that comes from _map_point.
    """

    return (
        max(1, _round(EYE_WIDTH * spec.scale)),
        max(1, _round(EYE_HEIGHT * spec.scale)),
    )


def _eye_anchors(spec: FrameSpec) -> list[tuple[int, int]]:
    """Top-left corner of each eye, in logical pixels, for one frame.

    The right eye is *mirrored* from the left one around the deformed face
    centre rather than computed from its own rest point: rounding two
    independent floats to the pixel grid drops one eye a pixel off-centre on
    about half the frames, and asymmetric eyes are the single most visible
    defect on a symmetric face.
    """

    width, height = _eye_box(spec)
    centre_x, _centre_y = _map_point(spec, CENTRE_X, _EYE_CENTRE_Y)
    left_cx, left_cy = _map_point(spec, CENTRE_X - EYE_SPACING, _EYE_CENTRE_Y)
    left_x = _round(left_cx - (width - 1) / 2.0)
    top_y = _round(left_cy - (height - 1) / 2.0)
    right_x = _round(2.0 * centre_x - left_x - (width - 1))
    return [(left_x, top_y), (right_x, top_y)]


def _translate(mask: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """``mask`` resampled ``dy`` rows down and ``dx`` columns right.

    Off-grid samples come back empty, which is what makes the shade band close
    along the canvas edges instead of wrapping around like ``np.roll`` would.
    """

    out = np.zeros_like(mask)
    src_y0, src_y1 = max(0, dy), min(LOGICAL, LOGICAL + dy)
    src_x0, src_x1 = max(0, dx), min(LOGICAL, LOGICAL + dx)
    out[src_y0 - dy : src_y1 - dy, src_x0 - dx : src_x1 - dx] = mask[
        src_y0:src_y1, src_x0:src_x1
    ]
    return out


def _pinch_fill(mask: np.ndarray) -> np.ndarray:
    """The empty pixels ``mask`` pinches shut on two opposite sides.

    Two of them exist on this character, one under each sleeve, where the arm
    capsule and the belly disc pass within a pixel of each other without
    overlapping. They matter out of proportion to their size: ``_boundary``
    outlines whatever it finds, so a single missing pixel becomes a dark nick
    chewed out of the coat's edge, and it moves from frame to frame.
    """

    left = _translate(mask, -1, 0) & _translate(mask, 1, 0)
    above = _translate(mask, 0, -1) & _translate(mask, 0, 1)
    return ~mask & (left | above)


def _shade_band(mask: np.ndarray, depth: int) -> np.ndarray:
    """The ``depth``-pixel band along the lower-right edge of ``mask``.

    A pixel is in shade when the material ``depth`` steps toward the light's
    opposite corner is missing, i.e. when it sits on the side facing away from
    the upper-left light.
    """

    return mask & ~_translate(mask, depth, depth)


def _wash(canvas: Canvas, mask: np.ndarray, key: str, strength: float = 1.0) -> None:
    """Lay a translucent colour on the *empty* pixels of ``mask`` only.

    Used for the ground shadow and the aura. Both live behind everything else,
    so they must never touch a pixel that is already painted - and unlike
    ``Canvas.blend`` they are laid on transparency, where blending toward a
    zeroed background would darken the colour instead of just thinning it.
    """

    colour = PALETTE[key]
    alpha = int(round(colour[3] * max(0.0, min(1.0, strength))))
    if alpha <= 0:
        return
    empty = mask & (canvas.rgba[:, :, 3] == 0)
    canvas.rgba[empty] = (colour[0], colour[1], colour[2], alpha)
    for y, x in zip(*np.nonzero(empty)):
        canvas.keys[int(y)][int(x)] = key


_EXTRAS_CACHE: dict[int, dict[str, np.ndarray]] = {}


def _rest_extras(tilt: int) -> dict[str, np.ndarray]:
    """Shading masks, derived from the rest layers rather than re-derived.

    Every mask here is a *subset* of a layer in _rest_layers, which matters:
    _deform scatters each source pixel forward onto the same output rectangle,
    so a subset in rest space is still a subset after deformation. Shading can
    therefore never leak outside the body it shades, on any frame.
    """

    cached = _EXTRAS_CACHE.get(tilt)
    if cached is not None:
        return cached

    layers = _rest_layers(tilt)
    body = layers["body"]
    coat = layers["coat"]
    lens_cx, lens_cy, _root_x, _root_y = _lens_geometry(tilt)
    # Sleeve/torso seams, welded shut before anything is shaded so the shade
    # band and the outline both see one continuous coat.
    seam = _pinch_fill(body | coat)
    coat = coat | seam

    extras = {
        "seam": seam,
        "body_shade": _shade_band(body, _BODY_SHADE_DEPTH),
        "coat_shade": _shade_band(coat, _COAT_SHADE_DEPTH),
        # Specular highlight on the forehead, two tones so it reads as wet
        # gloss and not as a sticker. Kept above the eye rows on purpose.
        "gloss": body & _disc(24.0, 17.5, 3.4, 2.7),
        "gloss_core": body & _disc(23.5, 17.0, 1.7, 1.3),
        # Mitten hands, drawn *over* the handle so the character reads as
        # gripping it. The sleeve capsule of _rest_layers already swallows the
        # full hand disc, which is why layers["hands"] is empty at rest and a
        # slightly inset mitt is what actually shows.
        "mitts": _disc(_HAND_L[0], _HAND_L[1], 1.8, 1.8)
        | _disc(_HAND_R[0], _HAND_R[1], 1.8, 1.8),
        "glass_lit": layers["glass"] & ((_XX - lens_cx) + (_YY - lens_cy) <= -1.5),
        "glass_glint": layers["glass"] & _disc(lens_cx - 1.7, lens_cy - 1.7, 1.0, 1.0),
    }
    _EXTRAS_CACHE[tilt] = extras
    return extras


#: Deformed layers are cached per frame spec: the four sheets differ only by
#: their eyes and their thought bubble, so without this every silhouette would
#: be scattered four times over. FrameSpec is frozen, hence hashable, and the
#: deformation is a pure function of it - the cache cannot change the output.
_DEFORMED_CACHE: dict[FrameSpec, dict[str, np.ndarray]] = {}

#: Painted in this order. Names come from _rest_layers and _rest_extras.
_PAINT_ORDER: tuple[tuple[str, str], ...] = (
    ("legs", "violet_light"),
    ("feet", "violet_mid"),          # little shoes: a darker foot reads as one
    ("body", "lavender"),
    ("body_shade", "violet_light"),
    ("gloss", "coat"),
    ("gloss_core", "glint"),
    ("coat", "coat"),
    ("seam", "coat"),
    ("coat_shade", "lavender"),
    ("coat_edge", "violet_light"),   # collar / lapel line, inside the body
    ("badge", "violet_mid"),
    ("handle", "violet_dark"),
    ("rim", "violet_mid"),
    ("glass", "lavender"),
    ("glass_lit", "coat"),
    ("glass_glint", "glint"),
    ("hands", "lavender"),
    # One step darker than the body, not the same lavender: the coat's own
    # shade band is lavender too, so a lavender mitt at the end of a lavender
    # sleeve shadow merged into one 6px blob and the hand stopped reading as a
    # hand. Darker also matches where the hands are - in the sleeve's shadow.
    ("mitts", "violet_light"),
)

#: The layers that make up the silhouette the outline wraps. Shading layers are
#: subsets of these, so listing them would change nothing.
_SOLID_LAYERS = (
    "legs",
    "feet",
    "body",
    "coat",
    "seam",
    "badge",
    "handle",
    "rim",
    "glass",
    "hands",
    "mitts",
)


def _frame_layers(spec: FrameSpec) -> dict[str, np.ndarray]:
    """Every layer of the character, deformed into this frame's pose."""

    cached = _DEFORMED_CACHE.get(spec)
    if cached is not None:
        return cached

    rest: dict[str, np.ndarray] = dict(_rest_layers(spec.tilt))
    rest.update(_rest_extras(spec.tilt))
    deformed = {name: _deform(mask, spec) for name, mask in rest.items()}
    _DEFORMED_CACHE[spec] = deformed
    return deformed


def _draw_ground_shadow(canvas: Canvas, spec: FrameSpec) -> None:
    """Soft ellipse on the ground line, tightening as the character rises."""

    lift = min(1.0, spec.bob / float(MAX_BOB))
    half_x = (13.0 * spec.squash_x * spec.scale) * (1.0 - 0.5 * lift)
    half_y = 1.8 * spec.scale * (1.0 - 0.35 * lift)
    if half_x <= 0.2 or half_y <= 0.2:
        return
    # Centred just under the feet (GROUND_Y is the row they rest on), so the
    # contact patch shows on both sides of the legs instead of behind them.
    ellipse = _disc(CENTRE_X, GROUND_Y + 0.5, half_x, half_y)
    _wash(canvas, ellipse, "shadow", 1.0 - 0.35 * lift)


def _draw_hole(canvas: Canvas, spec: FrameSpec) -> None:
    """The void overhead: dark core plus a violet event horizon."""

    if spec.hole <= 0.0:
        return
    radius_x = 8.8 * spec.hole
    radius_y = 5.0 * spec.hole
    core = _disc(CENTRE_X, HOLE_Y, radius_x, radius_y)
    rim = _disc(CENTRE_X, HOLE_Y, radius_x * 1.25, radius_y * 1.25) & ~core
    canvas.fill(rim, "void_rim")
    canvas.fill(core, "void")


def _draw_tail(canvas: Canvas, spec: FrameSpec, apex_y: float) -> None:
    """Taffy strand from the top of the head up into the hole.

    Without it the implosion reads as a zoom-out: something has to visibly
    connect the body to what is pulling on it.
    """

    if not spec.tail or apex_y <= HOLE_Y:
        return
    span = apex_y - HOLE_Y
    body_x = CENTRE_X + _offset(spec, 1.0)
    for y in range(int(round(HOLE_Y)), int(round(apex_y)) + 1):
        if not (0 <= y < LOGICAL):
            continue
        progress = (y - HOLE_Y) / span if span > 0 else 1.0
        strand_x = CENTRE_X + (body_x - CENTRE_X) * progress
        half = 0.6 + 1.6 * progress  # thin at the hole, thicker at the body
        key = "violet_mid" if progress > 0.35 else "violet_dark"
        for x in range(LOGICAL):
            if abs(x - strand_x) <= half:
                canvas.set(x, y, key)


def _draw_props(canvas: Canvas, spec: FrameSpec) -> None:
    """The floating lab: stars, a vial, a DNA helix, books, an atom, dust.

    Anchored to the canvas rather than to the body, and drawn *under* the
    character. They bob by one pixel on two alternating cadences (float_a /
    float_b) so they do not all rise and fall in lockstep, which would read as
    the whole frame shaking rather than as props drifting.
    """

    if not spec.props:
        return

    lift_a = -spec.float_a
    lift_b = -spec.float_b

    _stamp(canvas, _ATOM, _PROP_ATOM[0], _PROP_ATOM[1] + lift_a)
    _stamp(canvas, _DNA, _PROP_DNA[0], _PROP_DNA[1] + lift_b)
    _stamp(canvas, _BOOKS, _PROP_BOOKS[0], _PROP_BOOKS[1] + lift_b)
    _draw_vial(canvas, _PROP_VIAL[0], _PROP_VIAL[1] + lift_a, spec.bubble)

    # Each star is one step further along the twinkle cycle than the previous
    # one: a single shared appearance would make them blink as one lamp.
    for index, (x0, y0) in enumerate(_PROP_STARS):
        sprite = _STARS[(spec.twinkle + index) % len(_STARS)]
        _stamp(canvas, sprite, x0, y0 + (lift_a if index % 2 else lift_b))

    for index, (x0, y0) in enumerate(_PROP_DUST):
        canvas.set(x0, y0 + (lift_b if index % 2 else lift_a), "spark")


def _draw_aura(canvas: Canvas, silhouette: np.ndarray, spec: FrameSpec) -> None:
    """Two-step violet halo hugging the silhouette.

    Built from the silhouette itself rather than from a circle, so it tracks
    every squash and the magnifier without a second description of the shape.
    """

    if spec.glow <= 0.0:
        return
    near = _dilate(silhouette, 1) & ~silhouette
    far = _dilate(silhouette, 2) & ~_dilate(silhouette, 1)
    _wash(canvas, near, "glow", spec.glow)
    _wash(canvas, far, "glow", spec.glow * 0.5)


def _draw_eyes(canvas: Canvas, spec: FrameSpec) -> None:
    """Paint both eyes, corners knocked off, with a two-pixel glint.

    Pixel for pixel what ``native_widget._paint_eyes`` does at runtime: the
    widget draws these itself on the ``*_base`` sheets, and a baked eye that
    did not match would make the character's face pop between the web avatar
    and the desktop companion.
    """

    if spec.scale < 0.12:
        # Nothing left of the body to hold them: a pair of floating dark dots
        # in an empty frame reads as dirt on the screen, not as eyes.
        return

    width, height = _eye_box(spec)
    for x0, y0 in _eye_anchors(spec):
        if spec.blink:
            # A single row: the closed lid. Anything thicker reads as a squint.
            bar = y0 + height // 2
            for dx in range(width):
                canvas.set(x0 + dx, bar, "eye")
            continue
        rounded = width >= 3 and height >= 3
        for dy in range(height):
            for dx in range(width):
                if rounded and dx in (0, width - 1) and dy in (0, height - 1):
                    continue  # corner left to the body: that is the rounding
                canvas.set(x0 + dx, y0 + dy, "eye")
        if width >= 3 and height >= 4:
            canvas.set(x0 + 1, y0 + 1, "glint")
            canvas.set(x0 + 2, y0 + 1, "glint")


def _draw_face(canvas: Canvas, spec: FrameSpec, body: np.ndarray) -> None:
    """Mouth and cheeks, clipped to the body so nothing floats off the face."""

    centre_x, mouth_y = _map_point(spec, CENTRE_X, _MOUTH_Y)
    mouth_x = _round(centre_x)
    row = _round(mouth_y)
    half = 1 + spec.smile  # 3, 5 or 7 pixels wide
    for dx in range(-half, half + 1):
        # Ends up, middle down: the minimum a smile needs at this size.
        depth = 1 if abs(dx) <= half - 1 else 0
        x, y = mouth_x + dx, row + depth
        if 0 <= x < LOGICAL and 0 <= y < LOGICAL and body[y, x]:
            canvas.set(x, y, "mouth")

    if spec.scale <= 0.8:
        return
    # Cheeks redden with the smile. Laid with alpha so the shading underneath
    # still shows: an opaque pink patch reads as a sticker.
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
    """Twinkles of the implosion, converging with the body as it shrinks."""

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


def compose(spec: FrameSpec, *, watch: bool, eyes: bool) -> Canvas:
    """Assemble one frame, back to front, and return the painted canvas.

    The canvas rather than an image, because it also carries the palette key of
    every pixel, which is what ``--preview`` prints.

    ``eyes`` only decides whether the *idle/hop* eyes are baked in;
    ``spec.bake_eyes`` overrides it for the vanish/return frames, whose eyes
    shrink with the body and therefore cannot be composited at runtime.
    """

    canvas = Canvas()
    layers = _frame_layers(spec)
    silhouette = np.zeros((LOGICAL, LOGICAL), dtype=bool)
    for name in _SOLID_LAYERS:
        silhouette |= layers[name]

    # 1. Ground contact, then the void and the strand it pulls the body up
    #    with. All behind the character: the body stays in front of whatever is
    #    swallowing it, which is what keeps the face readable to the last frame.
    if spec.shadow:
        _draw_ground_shadow(canvas, spec)
    _draw_hole(canvas, spec)
    _, apex_y = _map_point(spec, CENTRE_X, _HEAD[1] - _HEAD[3])
    _draw_tail(canvas, spec, apex_y)

    # 2. The floating lab, then the aura on top of the still-empty pixels
    #    around the silhouette.
    _draw_props(canvas, spec)
    _draw_aura(canvas, silhouette, spec)

    # 3. The character itself, in paint order.
    for name, key in _PAINT_ORDER:
        canvas.fill(layers[name], key)

    # 4. One outline for body + coat + arms + magnifier, taken from *inside*
    #    the silhouette so the character never grows by a pixel between frames.
    canvas.fill(_boundary(silhouette), "outline")

    # 5. Face last: it has to sit on top of the shading and the outline.
    if eyes or spec.bake_eyes:
        _draw_eyes(canvas, spec)
    if spec.face:
        _draw_face(canvas, spec, layers["body"])

    _draw_sparkles(canvas, spec)

    # 6. The thought bubble is drawn over the character, not under it: it is a
    #    status marker, and at the hop apex the head climbs high enough to
    #    graze the trail puff.
    if watch and spec.props:
        _draw_thought_bubble(canvas, spec)

    return canvas


def build_frame(spec: FrameSpec, *, watch: bool, eyes: bool) -> Image.Image:
    """One 64x64 RGBA frame of the researcher."""

    return compose(spec, watch=watch, eyes=eyes).image()


def build_strip(*, watch: bool, eyes: bool) -> Image.Image:
    """The 34 frames side by side: 2176x64, the layout every consumer expects."""

    frames = _all_frames()
    strip = Image.new("RGBA", (LOGICAL * len(frames), LOGICAL), (0, 0, 0, 0))
    for index, spec in enumerate(frames):
        strip.paste(build_frame(spec, watch=watch, eyes=eyes), (index * LOGICAL, 0))
    return strip


def build_icon_frame() -> Image.Image:
    """A neutral, shadowless portrait, cropped tight and centred for the icon.

    Shadow, aura and floating props are all off: at 16px a ground shadow is a
    grey smear and a book six pixels from the character is indistinguishable
    from dirt. What is left is cropped to its bounding box and re-centred, so
    the character fills as much of the square as it can.

    The square stays ``LOGICAL`` (64) wide even though the crop is smaller,
    because every icon size has to be an integer ratio of it: 64 gives 16 (1/4),
    32 (1/2), 64 (1:1), 128 (x2) and 256 (x4). Padding to the crop's own size
    instead - 44x48 here - would put 32px on a 1.5 ratio, and half the pixels
    would come out a device pixel wider than their neighbours.
    """

    spec = FrameSpec(bake_eyes=True, shadow=False, props=False, glow=0.0, smile=2)
    frame = build_frame(spec, watch=False, eyes=True)
    box = frame.getbbox() or (0, 0, LOGICAL, LOGICAL)
    cropped = frame.crop(box)
    square = Image.new("RGBA", (LOGICAL, LOGICAL), (0, 0, 0, 0))
    square.paste(
        cropped,
        ((LOGICAL - cropped.width) // 2, (LOGICAL - cropped.height) // 2),
    )
    return square


#: Sizes the ICO must carry. 256 feeds Explorer's big thumbnails, 16/32 the
#: taskbar and list views; a single-size ICO lets Windows resample on its own
#: and the icon comes out blurry or empty depending on the context. Mirrors
#: $iconSizes in build_exe.ps1.
ICON_SIZES = (16, 32, 64, 128, 256)


def build_icon_sizes() -> list[Image.Image]:
    """Icon bitmaps at integer ratios of the art, so pixels stay square.

    Every size here is 64 divided or multiplied by a power of two, resampled
    with ``Image.NEAREST``: a fractional factor makes neighbouring source
    pixels different sizes on screen, and any smooth filter turns the 1px
    outline into a halo. Both are exactly what makes rescaled pixel art look
    broken.
    """

    source = build_icon_frame()
    for size in ICON_SIZES:
        larger, smaller = max(size, LOGICAL), min(size, LOGICAL)
        if larger % smaller:
            raise ValueError(f"icon size {size} is not an integer ratio of {LOGICAL}")
    return [source.resize((size, size), Image.NEAREST) for size in ICON_SIZES]


def save_icon(path: Path, icons: list[Image.Image]) -> None:
    """Write the multi-size ICO from bitmaps we scaled ourselves.

    ``append_images`` is what makes this correct: handed a single image and a
    list of sizes, Pillow resamples the missing ones with LANCZOS, which would
    silently blur the small entries - the ones that need the crispness most.
    Passing the largest bitmap as the base matters too: Pillow skips any
    requested size larger than the base image.
    """

    largest = icons[-1]
    largest.save(
        path,
        format="ICO",
        sizes=[image.size for image in icons],
        append_images=icons[:-1],
    )


def metadata() -> dict[str, object]:
    """The contract the web page and the desktop companion both read.

    Written from the same constants the art is drawn with, so the anchors here
    cannot drift away from the pixels.
    """

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
                # ``null`` marks the frames whose eyes are already in the sheet
                # (vanish/return): drawing them again would double them up.
                "eyes": None if spec.bake_eyes else [list(a) for a in _eye_anchors(spec)],
                "eye_size": [width, height],
                "bob": spec.bob,
                "scale": spec.scale,
            }
        )

    return {
        "logical_size": LOGICAL,
        "frame_count": len(frames_meta),
        # The row the feet rest on. The desktop companion subtracts it from the
        # canvas height to stand the character on the taskbar instead of
        # letting it sink behind it.
        "ground_y": GROUND_Y,
        "eye": {"width": EYE_WIDTH, "height": EYE_HEIGHT},
        "clips": clips_meta,
        "frames": frames_meta,
        "palette": {key: list(value) for key, value in PALETTE.items()},
    }


#: One character per palette entry, dark ink for dark paint, so the ASCII dump
#: reads as a picture. Printed from the canvas' key grid, never guessed from
#: RGB distances: two violets one step apart would otherwise be reported as
#: whichever the metric preferred.
_PREVIEW_CHARS = {
    "outline": "#",
    "eye": "O",
    "glint": "*",
    "coat": "W",
    "lavender": "-",
    "violet_light": "+",
    "violet_mid": "=",
    "violet_dark": "%",
    "blush": "b",
    "mouth": "w",
    "shadow": ".",
    "glow": "'",
    "spark": ",",
    "void": "@",
    "void_rim": "o",
}


def preview(index: int, *, watch: bool) -> str:
    """Render one frame as text, so the silhouette can be checked without a
    viewer (the whole point of generated art is that it stays inspectable)."""

    canvas = compose(_all_frames()[index], watch=watch, eyes=True)
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
    parser = argparse.ArgumentParser(
        description="Generate the pixel researcher sprite sheets."
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="print frames as text instead of writing files",
    )
    parser.add_argument("--frame", type=int, default=None, help="frame index to preview")
    parser.add_argument(
        "--watch", action="store_true", help="preview the thought-bubble variant"
    )
    args = parser.parse_args()

    if args.preview:
        # Defaults: the resting pose, the middle of the breath, the hop apex
        # and a late vanish frame - the four places a broken silhouette shows.
        indices = [args.frame] if args.frame is not None else [0, 6, 15, 23]
        for index in indices:
            label = f"{_clip_of(index)}{' (watch)' if args.watch else ''}"
            print(f"--- frame {index} = {label} ---")
            print(preview(index, watch=args.watch))
        return

    assets = Path(__file__).resolve().parent.parent / "web" / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    outputs = {
        # Eyes baked in: CSS can only slide a background image around, so the
        # web avatar needs complete frames.
        "pixel_researcher_strip.png": build_strip(watch=False, eyes=True),
        "pixel_researcher_watch_strip.png": build_strip(watch=True, eyes=True),
        # Eyes left out of idle/hop: the native widget draws those itself so
        # the character can follow the cursor and blink.
        "pixel_researcher_base.png": build_strip(watch=False, eyes=False),
        "pixel_researcher_watch_base.png": build_strip(watch=True, eyes=False),
    }
    for name, image in outputs.items():
        image.save(assets / name)
        print(f"wrote {name} {image.size[0]}x{image.size[1]}")

    icons = build_icon_sizes()
    save_icon(assets / "pixel_researcher.ico", icons)
    print(f"wrote pixel_researcher.ico {[image.size[0] for image in icons]}")

    favicon = next(image for image in icons if image.size[0] == 128)
    favicon.save(assets / "pixel_researcher_icon.png")
    print(f"wrote pixel_researcher_icon.png {favicon.size[0]}x{favicon.size[1]}")

    (assets / "pixel_researcher.json").write_text(
        json.dumps(metadata(), indent=2), encoding="utf-8"
    )
    print("wrote pixel_researcher.json")


if __name__ == "__main__":
    main()
