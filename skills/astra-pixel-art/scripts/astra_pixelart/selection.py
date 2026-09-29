"""Rectangle snapshots and palette-preserving pixel transforms.

RotSprite mode follows the Scale2x x8 / transform / nearest-sample pipeline
used by Aseprite, with pixel-centre inverse mapping and an independent
selection footprint. It is not a byte-for-byte port of Aseprite's rasteriser.
Scale2x rules: https://www.scale2x.it/algorithm
"""
from __future__ import annotations

import math
import operator

from .canvas import Canvas, CanvasError, over

# Bound the largest raster, including the 8x RotSprite intermediate.
_MAX_PIXELS = 16_777_216


def _integer(value, name):
    if isinstance(value, bool):
        raise CanvasError(f'{name} must be an integer')
    try:
        return operator.index(value)
    except TypeError:
        raise CanvasError(f'{name} must be an integer') from None


def _size(w, h):
    w, h = _integer(w, 'width'), _integer(h, 'height')
    if w <= 0 or h <= 0:
        raise CanvasError('selection dimensions must be positive')
    if w * h > _MAX_PIXELS:
        raise CanvasError(f'selection raster exceeds {_MAX_PIXELS} pixels')
    return w, h


def _same(a, b):
    # Hidden RGB must not create false edges at fully transparent pixels.
    return a == b or (a[3] == 0 and b[3] == 0)


def _scale2x(pixels, w, h):
    out = [None] * (w * h * 4)
    ow = w * 2
    for y in range(h):
        for x in range(w):
            i = y * w + x
            e = pixels[i]
            b = pixels[i - w] if y else e
            d = pixels[i - 1] if x else e
            f = pixels[i + 1] if x + 1 < w else e
            k = pixels[i + w] if y + 1 < h else e
            j = y * 2 * ow + x * 2
            if not _same(b, k) and not _same(d, f):
                out[j] = d if _same(d, b) else e
                out[j + 1] = f if _same(b, f) else e
                out[j + ow] = d if _same(d, k) else e
                out[j + ow + 1] = f if _same(k, f) else e
            else:
                out[j] = out[j + 1] = out[j + ow] = out[j + ow + 1] = e
    return out, ow, h * 2


class Selection:
    """An immutable transform recipe over a rectangular RGBA snapshot.

    resize/rotate replace their respective parameters; flip toggles an axis.
    The recipe is always evaluated in flip, resize, rotate order from the
    original pixels, never by resampling a previous preview. A move validates
    the original source before clearing it, so stale snapshots cannot erase
    subsequent edits. Select again after a committed move.
    """

    def __init__(self, source, x, y, w, h):
        x, y = _integer(x, 'x'), _integer(y, 'y')
        w, h = _size(w, h)
        if x < 0 or y < 0 or x + w > source.w or y + h > source.h:
            raise CanvasError('selection rectangle must be entirely inside its source canvas')
        self._source = source
        self._rect = (x, y, w, h)
        self._pixels = tuple(source.pal.rgba(source.px[yy * source.w + xx])
                             for yy in range(y, y + h) for xx in range(x, x + w))
        self._width, self._height = w, h
        self._flip_h = self._flip_v = False
        self._angle = 0.0
        self._algorithm = 'nearest'
        self._cache = None

    def _with(self, **changes):
        out = object.__new__(Selection)
        out.__dict__ = dict(self.__dict__, _cache=None, **changes)
        return out

    def resize(self, width, height):
        """Set the pre-rotation size in pixels, sampling by nearest neighbour."""
        width, height = _size(width, height)
        return self._with(_width=width, _height=height)

    def flip(self, axis='h'):
        """Toggle a horizontal or vertical reflection of the original snapshot."""
        if axis not in ('h', 'v'):
            raise CanvasError("axis must be 'h' or 'v'")
        key = '_flip_h' if axis == 'h' else '_flip_v'
        return self._with(**{key: not getattr(self, key)})

    def rotate(self, angle, algorithm='nearest'):
        """Set the absolute clockwise angle in degrees around the patch centre."""
        if isinstance(angle, bool):
            raise CanvasError('angle must be a finite number')
        try:
            angle = float(angle)
        except (TypeError, ValueError, OverflowError):
            raise CanvasError('angle must be a finite number') from None
        if not math.isfinite(angle):
            raise CanvasError('angle must be a finite number')
        if algorithm not in ('nearest', 'rotsprite'):
            raise CanvasError("algorithm must be 'nearest' or 'rotsprite'")
        return self._with(_angle=angle % 360, _algorithm=algorithm)

    def _geometry(self):
        angle = self._angle
        if angle % 90 == 0:
            co, si = ((1, 0), (0, 1), (-1, 0), (0, -1))[int(angle / 90)]
        else:
            rad = math.radians(angle)
            co, si = math.cos(rad), math.sin(rad)
        w, h = self._width, self._height
        ow = math.ceil(abs(w * co) + abs(h * si))
        oh = math.ceil(abs(w * si) + abs(h * co))
        _size(ow, oh)
        return ow, oh, co, si

    @property
    def size(self):
        """Expanded output (width, height), before destination clipping."""
        return self._geometry()[:2]

    def _raster(self):
        if self._cache is not None:
            return self._cache
        ow, oh, co, si = self._geometry()
        sw, sh = self._rect[2:]
        pixels = self._pixels
        if self._algorithm == 'rotsprite' and self._angle % 90:
            _size(sw * 8, sh * 8)
            for _ in range(3):
                pixels, sw, sh = _scale2x(pixels, sw, sh)
        w, h = self._width, self._height
        out = [None] * (ow * oh)
        for y in range(oh):
            dy = y + 0.5 - oh / 2
            for x in range(ow):
                dx = x + 0.5 - ow / 2
                sx = co * dx + si * dy + w / 2
                sy = -si * dx + co * dy + h / 2
                # None is outside the geometric footprint. An RGBA with alpha
                # zero is still SELECTED, and replaces/erases on normal paste.
                if 0 <= sx < w and 0 <= sy < h:
                    ix = min(sw - 1, int(sx * sw / w))
                    iy = min(sh - 1, int(sy * sh / h))
                    if self._flip_h:
                        ix = sw - 1 - ix
                    if self._flip_v:
                        iy = sh - 1 - iy
                    out[y * ow + x] = pixels[iy * sw + ix]
        self._cache = (ow, oh, tuple(out))
        return self._cache

    def preview(self, name=None):
        """Return a detached, tightly sized Canvas; source pixels stay untouched."""
        w, h, pixels = self._raster()
        out = Canvas(w, h, palette=self._source.pal, name=name)
        mapping = {rgba: out.pal.register(rgba) for rgba in dict.fromkeys(pixels) if rgba is not None}
        out.px = [0 if rgba is None else mapping[rgba] for rgba in pixels]
        return out

    def copy_to(self, x, y, canvas=None, mode='replace'):
        """Paste the transformed snapshot at an absolute destination top-left."""
        return self._paste(x, y, canvas, mode, move=False)

    def move_to(self, x, y, canvas=None, mode='replace'):
        """Clear the unchanged original rectangle and paste, safely across overlap."""
        return self._paste(x, y, canvas, mode, move=True)

    def _paste(self, x, y, canvas, mode, move):
        x, y = _integer(x, 'x'), _integer(y, 'y')
        dst = self._source if canvas is None else canvas
        if not isinstance(dst, Canvas):
            raise CanvasError('destination must be a Canvas')
        if mode not in ('replace', 'over'):
            raise CanvasError("mode must be 'replace' or 'over'")
        source = self._source
        rx, ry, rw, rh = self._rect
        if move:
            live = tuple(source.pal.rgba(source.px[yy * source.w + xx])
                         for yy in range(ry, ry + rh) for xx in range(rx, rx + rw))
            if live != self._pixels:
                raise CanvasError('selection source has changed; select the region again before moving')
        w, h, pixels = self._raster()
        target = list(dst.px)
        cleared = target if dst is source else list(source.px)
        if move:
            for yy in range(ry, ry + rh):
                start = yy * source.w + rx
                cleared[start:start + rw] = [0] * rw
        mapping = {}
        for py in range(max(0, -y), min(h, dst.h - y)):
            for px in range(max(0, -x), min(w, dst.w - x)):
                rgba = pixels[py * w + px]
                if rgba is None:
                    continue
                at = (y + py) * dst.w + x + px
                if mode == 'over':
                    if rgba[3] == 0:
                        continue
                    rgba = over(rgba, dst.pal.rgba(target[at]))
                if rgba not in mapping:
                    mapping[rgba] = dst.pal.register(rgba)
                target[at] = mapping[rgba]
        # No canvas pixels are changed until validation and rasterisation finish.
        if move and source is not dst:
            source.px[:] = cleared
        dst.px[:] = target
        return dst

    def __repr__(self):
        return f'Selection(rect={self._rect}, size={self.size}, angle={self._angle}, algorithm={self._algorithm!r})'
