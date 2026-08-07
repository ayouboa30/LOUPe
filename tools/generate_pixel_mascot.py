"""Generate the pixel-art slime mascot sprite sheets shipped in web/assets.

The art is produced by code rather than hand-drawn so the whole set stays
consistent: one palette, one silhouette function, and per-frame
squash/stretch parameters drive every frame. Three consumers read the same
output - the web avatar (CSS sprite, see .mascot in web/style.css), the
floating desktop companion (three_loop/native_widget.py), and the packaged
executable's icon (pixel_slime.ico, wired in build_exe.ps1).

Four animation clips live in each sheet:

* ``idle`` - a slow breath with a jelly lean that alternates direction.
* ``hop`` - a full bounce: anticipation squash, launch stretch, airborne
  apex, then a landing squash that recovers. Squash and stretch are
  volume-preserving (wider as it flattens, narrower as it stretches), which
  is what makes a blob read as springy instead of merely scaling.
* ``vanish`` - the "sucked into a black hole" exit played when the companion
  is sent off to research: it crouches, then gets stretched thin, twisted
  and pulled upward into a point, leaving a few sparkles behind.
* ``return`` - the reverse, landing with an overshoot squash so it pops back
  instead of just fading in.

A ground shadow is drawn on every grounded frame and shrinks as the body
rises, so hops and the vanish read as leaving the ground.

Four sheets are emitted because the consumers need different things:

* ``pixel_slime_strip.png`` / ``pixel_slime_hat_strip.png`` have the eyes
  baked in. CSS can only slide a background image around, so the web avatar
  needs complete frames.
* ``pixel_slime_base.png`` / ``pixel_slime_base_hat.png`` leave the eyes out
  of the ``idle``/``hop`` frames. The native widget composites those itself
  so the slime can track the cursor and blink without one sheet per look
  direction. ``vanish``/``return`` frames keep their eyes baked - eye size
  has to follow the shrinking body there, and nothing is tracking a cursor
  mid-implosion. ``pixel_slime.json`` says which frames are which.

Run: python tools/generate_pixel_mascot.py [--preview [--frame N] [--hat]]
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

#: Logical pixel grid. Everything is authored at this size and scaled up with
#: nearest-neighbour at display time, which is what keeps the pixels crisp
#: instead of turning into a smudge (see _SCALE in native_widget.py).
LOGICAL = 32

#: Ground line: the shadow sits here and the body rests just above it.
GROUND_Y = 29.0

#: Highest hop, in logical pixels. Capped so the hat's crown still fits
#: inside the canvas at the apex instead of being clipped.
MAX_BOB = 5

#: Where the black hole opens, in logical pixels from the top of the canvas.
HOLE_Y = 4.0

PALETTE: dict[str, tuple[int, int, int, int]] = {
    "outline": (34, 22, 62, 255),
    "dark": (96, 56, 190, 255),
    "mid": (131, 87, 241, 255),
    "light": (167, 139, 250, 255),
    "pale": (206, 188, 255, 255),
    "gloss": (247, 243, 255, 255),
    "eye": (28, 17, 52, 255),
    "glint": (255, 255, 255, 255),
    "blush": (244, 114, 182, 130),
    "mouth": (62, 34, 100, 255),
    "hat_dark": (40, 33, 64, 255),
    "hat_mid": (61, 51, 92, 255),
    "hat_band": (196, 181, 253, 255),
    "shadow": (30, 18, 56, 70),
    "spark": (233, 226, 255, 205),
    "void": (16, 9, 32, 255),
    "void_rim": (139, 92, 246, 255),
}

#: Eye geometry at scale 1.0, in logical pixels. Mirrored into the JSON
#: metadata so the generator and the native widget cannot drift apart.
EYE_WIDTH = 4
EYE_HEIGHT = 5
EYE_SPACING = 4.6  # distance from body centre to each eye centre

#: Crown rows, from the band upward: how much narrower than the crown's half
#: width each row is. A gentle inset domes the top; a sharp one pinches it.
_CROWN_TAPER = (0.0, 0.0, 0.25, 0.6, 1.1)

#: Twinkles left behind by the implosion, as offsets from the body centre.
#: Fixed rather than random so every regeneration produces identical art, and
#: mirrored in pairs so the burst reads as one event radiating outward instead
#: of as stray dots.
_SPARKLE_OFFSETS = ((-7, -6), (7, -6), (-8, 3), (8, 3), (0, -10), (0, 6))


@dataclass(frozen=True)
class FrameSpec:
    """One frame's deformation state."""

    squash_x: float = 1.0
    squash_y: float = 1.0
    bob: float = 0.0
    #: Phase of the jelly lean; also shifts the gloss and the face with it.
    phase: float = 0.0
    #: Overall size. Only the vanish/return clips use anything but 1.0.
    scale: float = 1.0
    #: Extra horizontal shear at the apex, in logical pixels: the twist of
    #: being pulled into something.
    swirl: float = 0.0
    shadow: bool = True
    sparkles: int = 0
    #: Draw the eyes even on the "base" sheets the native widget uses.
    bake_eyes: bool = False
    #: Face detail is dropped once the body is too small to carry it.
    face: bool = True
    #: Size of the black hole above the slime, 0 = none, 1 = fully open.
    hole: float = 0.0
    #: Draw the taffy strand running from the body up into the hole. This is
    #: what makes the implosion read as *being pulled* rather than merely
    #: scaling down: a shrinking blob alone looks like a zoom-out.
    tail: bool = False


def _idle_frames() -> list[FrameSpec]:
    frames = []
    for index in range(6):
        phase = 2.0 * math.pi * index / 6.0
        breath = math.sin(phase)
        frames.append(
            FrameSpec(
                squash_x=1.0 - 0.04 * breath,
                squash_y=1.0 + 0.05 * breath,
                bob=1.0 if breath > 0.55 else 0.0,
                phase=phase,
            )
        )
    return frames


#: The bounce, keyed by hand rather than by a formula: the timing of a hop is
#: the whole point (slow anticipation, fast launch, float at the apex, hard
#: landing), and an easing curve flattens exactly those accents.
_HOP_FRAMES = [
    FrameSpec(squash_x=1.11, squash_y=0.87),                    # crouch
    FrameSpec(squash_x=0.92, squash_y=1.15, bob=1.0),           # launch
    FrameSpec(squash_x=0.95, squash_y=1.09, bob=3.0),           # rising
    FrameSpec(squash_x=1.00, squash_y=1.01, bob=float(MAX_BOB)),  # apex
    FrameSpec(squash_x=0.97, squash_y=1.05, bob=3.5),           # falling
    FrameSpec(squash_x=0.94, squash_y=1.11, bob=1.0),           # pre-impact
    FrameSpec(squash_x=1.14, squash_y=0.84),                    # landing squash
    FrameSpec(squash_x=1.05, squash_y=0.95),                    # recover
]

#: Being sucked in: a hole opens overhead, the slime crouches, then gets
#: drawn up into it as a thinning strand until only sparkles remain. Eyes stay
#: baked here and shrink with the body.
#:
#: Swirl is kept small on purpose. An earlier pass used ±3 logical pixels of
#: twist, which tore the last frames apart: once the body is barely two pixels
#: wide, a shear that large displaces each row further than the row is wide,
#: so the silhouette stops being connected and renders as scattered specks.
_VANISH_FRAMES = [
    FrameSpec(squash_x=1.12, squash_y=0.88, bake_eyes=True),                       # crouch
    FrameSpec(squash_x=0.90, squash_y=1.18, bob=1.0, scale=0.90, swirl=0.6,
              hole=0.55, tail=True, bake_eyes=True),
    FrameSpec(squash_x=0.80, squash_y=1.26, bob=3.0, scale=0.70, swirl=-0.8,
              hole=0.90, tail=True, bake_eyes=True),
    FrameSpec(squash_x=0.70, squash_y=1.30, bob=5.5, scale=0.50, swirl=0.8,
              hole=1.00, tail=True, shadow=False, bake_eyes=True),
    FrameSpec(squash_x=0.60, squash_y=1.30, bob=8.0, scale=0.32, swirl=-0.7,
              hole=1.00, tail=True, shadow=False, sparkles=3, bake_eyes=True, face=False),
    FrameSpec(squash_x=0.52, squash_y=1.20, bob=10.5, scale=0.16, swirl=0.5,
              hole=0.80, tail=True, shadow=False, sparkles=4, bake_eyes=True, face=False),
    FrameSpec(squash_x=0.46, squash_y=1.00, bob=12.0, scale=0.06,
              hole=0.45, shadow=False, sparkles=5, bake_eyes=True, face=False),
]

#: Coming back: spat out of the closing hole, then an overshoot on landing so
#: it pops into place instead of fading in.
_RETURN_FRAMES = [
    FrameSpec(squash_x=0.46, squash_y=1.00, bob=12.0, scale=0.08,
              hole=0.50, shadow=False, sparkles=4, bake_eyes=True, face=False),
    FrameSpec(squash_x=0.54, squash_y=1.22, bob=9.5, scale=0.22, swirl=-0.5,
              hole=0.85, tail=True, shadow=False, sparkles=3, bake_eyes=True, face=False),
    FrameSpec(squash_x=0.68, squash_y=1.30, bob=6.0, scale=0.50, swirl=0.6,
              hole=0.70, tail=True, shadow=False, bake_eyes=True),
    FrameSpec(squash_x=0.84, squash_y=1.20, bob=2.5, scale=0.80, hole=0.35,
              bake_eyes=True),
    FrameSpec(squash_x=1.18, squash_y=0.82, bake_eyes=True),            # impact
    FrameSpec(squash_x=0.94, squash_y=1.08, bob=1.0, bake_eyes=True),   # rebound
    FrameSpec(squash_x=1.03, squash_y=0.97, bake_eyes=True),            # settle
]

CLIPS: dict[str, dict[str, object]] = {
    "idle": {"frames": _idle_frames(), "frame_ms": 150, "loop": True},
    "hop": {"frames": _HOP_FRAMES, "frame_ms": 70, "loop": False},
    "vanish": {"frames": _VANISH_FRAMES, "frame_ms": 65, "loop": False},
    "return": {"frames": _RETURN_FRAMES, "frame_ms": 65, "loop": False},
}


def _all_frames() -> list[FrameSpec]:
    frames: list[FrameSpec] = []
    for clip in CLIPS.values():
        frames.extend(clip["frames"])  # type: ignore[arg-type]
    return frames


def _body_geometry(spec: FrameSpec) -> tuple[float, float, float, float]:
    centre_x = (LOGICAL - 1) / 2.0
    base_y = GROUND_Y - 1.0 - spec.bob
    height = 19.0 * spec.squash_y * spec.scale
    half_width = 12.0 * spec.squash_x * spec.scale
    return centre_x, base_y, height, half_width


def _offset(spec: FrameSpec, t: float) -> float:
    """Horizontal displacement of the row at height ``t`` (0 base, 1 apex).

    Two contributions: the jelly lean, strongest mid-body and zero at both
    ends, and the swirl, which grows toward the apex so the body twists.

    The swirl is damped by ``scale`` so it can never displace a row further
    than a shrunken body is wide, which would break the silhouette into
    disconnected specks.
    """

    lean = 0.75 * math.sin(spec.phase) * math.sin(t * math.pi)
    return lean + spec.swirl * t * spec.scale


def _body_mask(spec: FrameSpec) -> list[list[bool]]:
    """Rasterise the slime silhouette: a wide-bottomed dome with a jelly lean."""

    centre_x, base_y, height, half_width = _body_geometry(spec)
    mask = [[False] * LOGICAL for _ in range(LOGICAL)]
    if height <= 0.0 or half_width <= 0.0:
        return mask
    for y in range(LOGICAL):
        t = (base_y - y) / height
        if t < 0.0 or t > 1.0:
            continue
        # Exponent 2.4 (rather than a plain circle's 2.0) keeps the flanks
        # full most of the way up, which is what separates a slime from a
        # half-circle.
        row_half = half_width * math.sqrt(max(0.0, 1.0 - t**2.4))
        if t < 0.12 and spec.bob <= 0.0:  # puddle lip, only while touching down
            row_half += 0.6
        shift = _offset(spec, t)
        for x in range(LOGICAL):
            if abs(x - centre_x - shift) <= row_half:
                mask[y][x] = True
    return mask


def _hat_masks(spec: FrameSpec) -> dict[str, list[list[bool]]]:
    """Rasterise the research-assistant hat as brim / crown / band layers."""

    centre_x, base_y, height, _ = _body_geometry(spec)
    apex_y = base_y - height
    shift = _offset(spec, 0.85)  # ride the top of the body

    brim = [[False] * LOGICAL for _ in range(LOGICAL)]
    crown = [[False] * LOGICAL for _ in range(LOGICAL)]
    band = [[False] * LOGICAL for _ in range(LOGICAL)]

    brim_y = int(round(apex_y + 1 * spec.scale))
    brim_half = 6.6 * spec.squash_x * spec.scale
    crown_half = 4.2 * spec.squash_x * spec.scale
    # Worn slightly off-centre: a perfectly centred hat reads as a helmet.
    hat_x = centre_x + shift + 0.5

    for x in range(LOGICAL):
        if abs(x - hat_x) <= brim_half:
            for y in (brim_y, brim_y + 1):
                if 0 <= y < LOGICAL:
                    brim[y][x] = True

    band_y = brim_y - 1
    for row, inset in enumerate(_CROWN_TAPER):
        y = brim_y - 1 - int(round(row * spec.scale))
        if not (0 <= y < LOGICAL):
            continue
        taper = crown_half - inset * spec.scale
        if taper <= 0:
            continue
        for x in range(LOGICAL):
            if abs(x - hat_x) <= taper:
                if y == band_y:
                    band[y][x] = True
                elif not band[y][x]:
                    crown[y][x] = True
    return {"brim": brim, "crown": crown, "band": band}


def _boundary(mask: list[list[bool]]) -> list[list[bool]]:
    """Outermost ring of ``mask``, used as the 1px outline.

    Taken from inside the shape rather than grown outside it: growing would
    inflate the silhouette on every frame and make the bounce look wobbly.
    """

    edge = [[False] * LOGICAL for _ in range(LOGICAL)]
    for y in range(LOGICAL):
        for x in range(LOGICAL):
            if not mask[y][x]:
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if not (0 <= nx < LOGICAL and 0 <= ny < LOGICAL) or not mask[ny][nx]:
                    edge[y][x] = True
                    break
    return edge


def _eye_box(spec: FrameSpec) -> tuple[int, int]:
    """Eye size for this frame: it has to shrink with an imploding body."""

    return (
        max(1, int(round(EYE_WIDTH * spec.scale))),
        max(1, int(round(EYE_HEIGHT * spec.scale))),
    )


def _eye_anchors(spec: FrameSpec) -> list[tuple[int, int]]:
    """Top-left corner of each eye, in logical pixels, for one frame."""

    centre_x, base_y, height, _ = _body_geometry(spec)
    eye_centre_y = base_y - height * 0.44
    shift = _offset(spec, 0.56)
    width, height_px = _eye_box(spec)
    anchors = []
    for side in (-1, 1):
        cx = centre_x + shift + side * EYE_SPACING * spec.squash_x * spec.scale
        anchors.append(
            (
                int(round(cx - (width - 1) / 2.0)),
                int(round(eye_centre_y - (height_px - 1) / 2.0)),
            )
        )
    return anchors


def _blend(base: tuple[int, int, int, int], over: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    alpha = over[3] / 255.0
    return (
        int(round(base[0] * (1 - alpha) + over[0] * alpha)),
        int(round(base[1] * (1 - alpha) + over[1] * alpha)),
        int(round(base[2] * (1 - alpha) + over[2] * alpha)),
        max(base[3], over[3]),
    )


def _draw_eyes(pixels, spec: FrameSpec) -> None:
    """Paint both eyes, with the corners knocked off and a glint, if they fit."""

    if spec.scale < 0.12:
        # Nothing left of the body to hold them: a pair of floating dark dots
        # in an empty frame reads as dirt on the screen, not as eyes.
        return
    width, height_px = _eye_box(spec)
    for x0, y0 in _eye_anchors(spec):
        for dy in range(height_px):
            for dx in range(width):
                rounded = width >= 3 and height_px >= 3
                if rounded and dx in (0, width - 1) and dy in (0, height_px - 1):
                    continue
                px, py = x0 + dx, y0 + dy
                if 0 <= px < LOGICAL and 0 <= py < LOGICAL:
                    pixels[px, py] = PALETTE["eye"]
        if width >= 3 and height_px >= 4:
            for gx, gy in ((x0 + 1, y0 + 1), (x0 + 2, y0 + 1)):
                if 0 <= gx < LOGICAL and 0 <= gy < LOGICAL:
                    pixels[gx, gy] = PALETTE["glint"]


def build_frame(spec: FrameSpec, *, hat: bool, eyes: bool) -> Image.Image:
    """Draw one 32x32 frame of the slime."""

    centre_x, base_y, height, _ = _body_geometry(spec)
    body = _body_mask(spec)
    hat_layers = _hat_masks(spec) if hat else None

    image = Image.new("RGBA", (LOGICAL, LOGICAL), (0, 0, 0, 0))
    pixels = image.load()

    # 1. Ground shadow, tightening as the slime rises. Drawn first so the body
    #    always paints over it on the frames where they touch.
    if spec.shadow:
        lift = min(1.0, spec.bob / float(MAX_BOB))
        shadow_half = (10.5 * spec.squash_x * spec.scale) * (1.0 - 0.55 * lift)
        for row, thinning in ((int(GROUND_Y), 1.0), (int(GROUND_Y) + 1, 0.55)):
            if not (0 <= row < LOGICAL):
                continue
            half = shadow_half * thinning
            for x in range(LOGICAL):
                if abs(x - centre_x) <= half:
                    pixels[x, row] = PALETTE["shadow"]

    # 2. The hole overhead, then the strand running down from it. Both go
    #    under the body so the slime stays in front of what is swallowing it.
    apex_y = base_y - height
    if spec.hole > 0.0:
        radius_x = 4.4 * spec.hole
        radius_y = 2.5 * spec.hole
        for y in range(LOGICAL):
            for x in range(LOGICAL):
                dx = (x - centre_x) / max(0.35, radius_x)
                dy = (y - HOLE_Y) / max(0.35, radius_y)
                distance = dx * dx + dy * dy
                if distance <= 1.0:
                    pixels[x, y] = PALETTE["void"]
                elif distance <= 1.55:
                    pixels[x, y] = PALETTE["void_rim"]

    if spec.tail and apex_y > HOLE_Y:
        hole_x = centre_x
        body_x = centre_x + _offset(spec, 1.0)
        span = apex_y - HOLE_Y
        for y in range(int(round(HOLE_Y)), int(round(apex_y)) + 1):
            if not (0 <= y < LOGICAL):
                continue
            progress = (y - HOLE_Y) / span if span > 0 else 1.0
            strand_x = hole_x + (body_x - hole_x) * progress
            half = 0.45 + 0.75 * progress  # thin at the hole, thicker at the body
            for x in range(LOGICAL):
                if abs(x - strand_x) <= half:
                    pixels[x, y] = PALETTE["mid"] if progress > 0.35 else PALETTE["dark"]

    # 3. Body fill, shaded top-light to bottom-dark.
    for y in range(LOGICAL):
        for x in range(LOGICAL):
            if not body[y][x]:
                continue
            t = (base_y - y) / height if height > 0 else 0.0
            if t >= 0.58:
                colour = PALETTE["light"]
            elif t >= 0.24:
                colour = PALETTE["mid"]
            else:
                colour = PALETTE["dark"]
            pixels[x, y] = colour

    # 4. Gloss highlight on the upper-left shoulder, where a wet blob catches
    #    the light. Two tones so it reads as a specular dot, not a sticker.
    if spec.face and spec.scale > 0.5:
        gloss_cx = centre_x - 4.4 * spec.scale + _offset(spec, 0.74)
        gloss_cy = base_y - height * 0.74
        for y in range(LOGICAL):
            for x in range(LOGICAL):
                if not body[y][x]:
                    continue
                dx = (x - gloss_cx) / (2.4 * spec.scale)
                dy = (y - gloss_cy) / (1.7 * spec.scale)
                distance = dx * dx + dy * dy
                if distance <= 1.0:
                    pixels[x, y] = PALETTE["gloss"]
                elif distance <= 2.1:
                    pixels[x, y] = PALETTE["pale"]

    # 5. Hat sits on top of the dome, before the outline so the outline can
    #    wrap body and hat as one silhouette.
    if hat_layers is not None:
        for name, colour_key in (("brim", "hat_dark"), ("crown", "hat_mid"), ("band", "hat_band")):
            for y in range(LOGICAL):
                for x in range(LOGICAL):
                    if hat_layers[name][y][x]:
                        pixels[x, y] = PALETTE[colour_key]

    # 6. Unified outline around body (+ hat), plus a separating line under the
    #    brim so the hat does not melt into the slime.
    silhouette = [
        [
            body[y][x]
            or (
                hat_layers is not None
                and (hat_layers["brim"][y][x] or hat_layers["crown"][y][x] or hat_layers["band"][y][x])
            )
            for x in range(LOGICAL)
        ]
        for y in range(LOGICAL)
    ]
    for y, row in enumerate(_boundary(silhouette)):
        for x, is_edge in enumerate(row):
            if is_edge:
                pixels[x, y] = PALETTE["outline"]
    if hat_layers is not None:
        for y in range(LOGICAL - 1):
            for x in range(LOGICAL):
                if hat_layers["brim"][y][x] and not hat_layers["brim"][y + 1][x] and body[y + 1][x]:
                    pixels[x, y + 1] = PALETTE["outline"]

    # 7. Face. On the sheets the native widget uses, idle/hop eyes are left
    #    out so it can draw them looking at the cursor.
    if eyes or spec.bake_eyes:
        _draw_eyes(pixels, spec)

    if spec.face:
        anchors = _eye_anchors(spec)
        _, eye_height = _eye_box(spec)
        mouth_y = anchors[0][1] + eye_height + 1
        centre_pixel = int(round(centre_x + _offset(spec, 0.4)))
        for mx, my in (
            (centre_pixel - 1, mouth_y),
            (centre_pixel, mouth_y + 1),
            (centre_pixel + 1, mouth_y + 1),
            (centre_pixel + 2, mouth_y),
        ):
            if 0 <= mx < LOGICAL and 0 <= my < LOGICAL and body[my][mx]:
                pixels[mx, my] = PALETTE["mouth"]

        if spec.scale > 0.8:
            eye_width, _ = _eye_box(spec)
            for side, anchor in zip((-1, 1), anchors):
                blush_y = anchor[1] + eye_height
                blush_x = anchor[0] + (-2 if side < 0 else eye_width)
                for dx in range(2):
                    bx = blush_x + dx
                    if 0 <= bx < LOGICAL and 0 <= blush_y < LOGICAL and body[blush_y][bx]:
                        pixels[bx, blush_y] = _blend(pixels[bx, blush_y], PALETTE["blush"])

    # 8. Twinkles from the implosion, converging with the body as it shrinks.
    if spec.sparkles:
        spark_cy = base_y - height * 0.5
        for dx, dy in _SPARKLE_OFFSETS[: spec.sparkles]:
            converge = 0.45 + 0.55 * spec.scale
            sx = int(round(centre_x + dx * converge))
            sy = int(round(spark_cy + dy * converge))
            if not (0 <= sx < LOGICAL and 0 <= sy < LOGICAL):
                continue
            pixels[sx, sy] = PALETTE["glint"]
            for nx, ny in ((sx - 1, sy), (sx + 1, sy), (sx, sy - 1), (sx, sy + 1)):
                if 0 <= nx < LOGICAL and 0 <= ny < LOGICAL and pixels[nx, ny][3] == 0:
                    pixels[nx, ny] = PALETTE["spark"]

    return image


def build_strip(*, hat: bool, eyes: bool) -> Image.Image:
    frames = _all_frames()
    strip = Image.new("RGBA", (LOGICAL * len(frames), LOGICAL), (0, 0, 0, 0))
    for index, spec in enumerate(frames):
        strip.paste(build_frame(spec, hat=hat, eyes=eyes), (index * LOGICAL, 0))
    return strip


def build_icon_frame() -> Image.Image:
    """A neutral, shadowless portrait cropped tight for use as an app icon."""

    spec = FrameSpec(bake_eyes=True, shadow=False)
    frame = build_frame(spec, hat=False, eyes=True)
    box = frame.getbbox() or (0, 0, LOGICAL, LOGICAL)
    cropped = frame.crop(box)
    side = max(cropped.size) + 2  # 1px of breathing room on the tight axis
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(
        cropped,
        ((side - cropped.width) // 2, (side - cropped.height) // 2),
    )
    return square


def build_icon_sizes() -> list[Image.Image]:
    """Icon bitmaps at power-of-two multiples of the art, so pixels stay square.

    Only integer scale factors are emitted (and one clean 2:1 reduction for
    16px): a 1.5x nearest-neighbour resize would make some pixels twice the
    width of their neighbours, which is exactly what makes upscaled pixel art
    look broken.
    """

    source = build_icon_frame()
    images = []
    for size in (16, 32, 64, 128, 256):
        images.append(source.resize((size, size), Image.NEAREST))
    return images


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
        width, height_px = _eye_box(spec)
        frames_meta.append(
            {
                # ``null`` marks frames whose eyes are already in the sheet.
                "eyes": None if spec.bake_eyes else [list(a) for a in _eye_anchors(spec)],
                "eye_size": [width, height_px],
                "bob": spec.bob,
                "scale": spec.scale,
            }
        )
    return {
        "logical_size": LOGICAL,
        "frame_count": len(frames_meta),
        "eye": {"width": EYE_WIDTH, "height": EYE_HEIGHT},
        "clips": clips_meta,
        "frames": frames_meta,
        "palette": {key: list(value) for key, value in PALETTE.items()},
    }


_PREVIEW_CHARS = {
    "outline": "#",
    "dark": "=",
    "mid": "+",
    "light": "-",
    "pale": ":",
    "gloss": "@",
    "eye": "O",
    "glint": "*",
    "mouth": "w",
    "hat_dark": "H",
    "hat_mid": "h",
    "hat_band": "B",
    "shadow": ".",
    "spark": ",",
    "void": "%",
    "void_rim": "o",
}


def preview(index: int, *, hat: bool) -> str:
    """Render one frame as text, so the silhouette can be checked without a
    viewer (the whole point of generated art is that it stays inspectable)."""

    image = build_frame(_all_frames()[index], hat=hat, eyes=True)
    pixels = image.load()
    lines = []
    for y in range(LOGICAL):
        row = []
        for x in range(LOGICAL):
            colour = pixels[x, y]
            if colour[3] == 0:
                row.append(" ")
                continue
            best_key, best_distance = "?", None
            for key, reference in PALETTE.items():
                distance = sum((a - b) ** 2 for a, b in zip(colour, reference))
                if best_distance is None or distance < best_distance:
                    best_key, best_distance = key, distance
            row.append(_PREVIEW_CHARS.get(best_key, "?"))
        lines.append("".join(row))
    return "\n".join(lines)


def _clip_of(index: int) -> str:
    cursor = 0
    for name, clip in CLIPS.items():
        count = len(clip["frames"])  # type: ignore[arg-type]
        if index < cursor + count:
            return f"{name}[{index - cursor}]"
        cursor += count
    return "?"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the pixel slime sprite sheets.")
    parser.add_argument("--preview", action="store_true", help="print frames as text instead of writing files")
    parser.add_argument("--frame", type=int, default=None, help="frame index to preview")
    parser.add_argument("--hat", action="store_true", help="preview the hatted variant")
    args = parser.parse_args()

    if args.preview:
        indices = [args.frame] if args.frame is not None else [0, 9, 12, 16, 19]
        for index in indices:
            print(f"--- frame {index} = {_clip_of(index)}{' (hat)' if args.hat else ''} ---")
            print(preview(index, hat=args.hat))
        return

    assets = Path(__file__).resolve().parent.parent / "web" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    outputs = {
        "pixel_slime_strip.png": build_strip(hat=False, eyes=True),
        "pixel_slime_hat_strip.png": build_strip(hat=True, eyes=True),
        "pixel_slime_base.png": build_strip(hat=False, eyes=False),
        "pixel_slime_base_hat.png": build_strip(hat=True, eyes=False),
    }
    for name, image in outputs.items():
        image.save(assets / name)
        print(f"wrote {name} {image.size}")

    icons = build_icon_sizes()
    icon_path = assets / "pixel_slime.ico"
    icons[-1].save(icon_path, format="ICO", sizes=[image.size for image in icons])
    print(f"wrote pixel_slime.ico {[image.size[0] for image in icons]}")
    icons[2].save(assets / "pixel_slime_icon.png")
    print("wrote pixel_slime_icon.png")

    (assets / "pixel_slime.json").write_text(json.dumps(metadata(), indent=2), encoding="utf-8")
    print("wrote pixel_slime.json")


if __name__ == "__main__":
    main()
