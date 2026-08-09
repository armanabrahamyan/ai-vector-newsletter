#!/usr/bin/env python3
"""Deterministic generator for the AI Vector mark candidates.

Every coordinate, angle and stroke width in CANDIDATES below is a hand-made
design decision. This script only does the arithmetic a designer should not do
by eye: offsetting a centreline into a band with correct *perpendicular* width,
mitring the joins, and clipping the result to the square. That keeps the arms of
a V optically equal in weight even though they sit at different angles.

Output (byte-stable, safe to re-run):
    docs/internal/brand/candidates/<id>.svg   one hand-authored mark each
    docs/internal/brand/candidates/paths.json path data for the presentation

Candidates are internal working material and live under docs/internal/.
The RATIFIED mark is docs/brand/aiv-mark.svg (published), which is the
single source of truth for every derivative; this script produced its
geometry and is kept so the geometry can be regenerated, not redrawn.

Usage:  python3 tools/brand_marks.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "internal" / "brand" / "candidates"

SQUARE = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]


# ---------------------------------------------------------------- primitives


def _norm(v):
    x, y = v
    m = math.hypot(x, y)
    return (x / m, y / m)


def _left_normal(d):
    return (-d[1], d[0])


def band(points, half_width):
    """Offset an open polyline into a closed band polygon, mitred at joins.

    Width is measured perpendicular to each segment, so a shallow arm and a
    steep arm drawn with the same half_width carry the same optical weight.
    """
    pts = [(float(x), float(y)) for x, y in points]
    dirs = [_norm((b[0] - a[0], b[1] - a[1])) for a, b in zip(pts, pts[1:])]
    normals = [_left_normal(d) for d in dirs]

    def offset_at(i, sign):
        """Offset vector at vertex i (mitred where two segments meet)."""
        if i == 0:
            n = normals[0]
        elif i == len(pts) - 1:
            n = normals[-1]
        else:
            n1, n2 = normals[i - 1], normals[i]
            denom = 1.0 + (n1[0] * n2[0] + n1[1] * n2[1])
            n = ((n1[0] + n2[0]) / denom, (n1[1] + n2[1]) / denom)
        return (pts[i][0] + sign * half_width * n[0],
                pts[i][1] + sign * half_width * n[1])

    left = [offset_at(i, 1.0) for i in range(len(pts))]
    right = [offset_at(i, -1.0) for i in range(len(pts))]
    return left + right[::-1]


def clip_to_square(poly, rect=(0.0, 0.0, 100.0, 100.0)):
    """Sutherland-Hodgman clip against the square (a convex window)."""
    x0, y0, x1, y1 = rect
    edges = [
        (lambda p: p[0] >= x0, lambda a, b: _isect(a, b, "x", x0)),
        (lambda p: p[0] <= x1, lambda a, b: _isect(a, b, "x", x1)),
        (lambda p: p[1] >= y0, lambda a, b: _isect(a, b, "y", y0)),
        (lambda p: p[1] <= y1, lambda a, b: _isect(a, b, "y", y1)),
    ]
    out = list(poly)
    for inside, cut in edges:
        if not out:
            return []
        src, out = out, []
        for i, cur in enumerate(src):
            prev = src[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(cut(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cut(prev, cur))
    return _dedupe(out)


def _isect(a, b, axis, value):
    if axis == "x":
        t = (value - a[0]) / (b[0] - a[0])
        return (value, a[1] + t * (b[1] - a[1]))
    t = (value - a[1]) / (b[1] - a[1])
    return (a[0] + t * (b[0] - a[0]), value)


def _dedupe(poly, eps=1e-7):
    out = []
    for p in poly:
        if not out or abs(p[0] - out[-1][0]) > eps or abs(p[1] - out[-1][1]) > eps:
            out.append(p)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) <= eps and abs(out[0][1] - out[-1][1]) <= eps:
        out.pop()
    return out


def _n(v):
    """Round to 2dp and strip trailing zeros — byte-stable path output."""
    s = f"{round(v + 0.0, 2):.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def d(*polys):
    parts = []
    for poly in polys:
        if not poly:
            continue
        head, *rest = poly
        seg = [f"M{_n(head[0])} {_n(head[1])}"]
        seg += [f"L{_n(p[0])} {_n(p[1])}" for p in rest]
        seg.append("Z")
        parts.append("".join(seg))
    return "".join(parts)


def ray(origin, deg_from_vertical, length):
    """A point `length` away from origin, at `deg` clockwise from straight up.

    Positive degrees lean right — the direction the publication is heading.
    """
    r = math.radians(deg_from_vertical)
    return (origin[0] + length * math.sin(r), origin[1] - length * math.cos(r))


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


# ---------------------------------------------------------------- candidates


def mark_open_v():
    """A block of ink opened by the V that is never drawn.

    The two arms sit at 12 deg and 43 deg from vertical, so the letter leans
    like an italic and its longer arm runs off the top-right corner — the
    block is literally opened in the direction of travel.
    BITE: the missing corner. It is what makes the shape a heading and not a
    letter sitting in a box, and it is the detail that reads first at 16px.

    Angles chosen by rendering six variants at 16-72px and looking, not by
    calculation: at -8 deg the left arm stands too near vertical and the mark
    reads as a tick; at -18 deg the V turns symmetrical and loses its lean.
    """
    vertex = (41.0, 68.0)
    v_poly = band(
        [ray(vertex, -12.0, 130.0), vertex, ray(vertex, 43.0, 140.0)],
        8.0,
    )
    return d(SQUARE, clip_to_square(v_poly)), "evenodd"


def mark_waterline():
    """The publication's own rule, tilted, cut out of a block of ink.

    BITE: the tilt. A level rule is a divider; 21 degrees of rise is a bearing.
    """
    rule = band([(-20.0, 76.0), (120.0, 24.0)], 7.5)
    return d(SQUARE, clip_to_square(rule)), "evenodd"


def mark_serif_v():
    """A Newsreader-weight V whose thin arm keeps rising past the cap line.

    BITE: the overshoot. The ancestor's floating chevron stops floating and
    becomes the letter's own terminal — one shape where there were two.
    """
    poly = [
        (4.0, 12.0),    # thick arm, outer top
        (25.0, 12.0),   # thick arm, inner top
        (47.0, 64.0),   # inner junction
        (85.0, 2.0),    # thin arm, inner top (overshoots the cap line)
        (96.0, 9.0),    # thin arm, outer top
        (50.0, 92.0),   # apex
    ]
    return d(poly), "nonzero"


def mark_standard():
    """The slash of AI/Vector planted on the masthead rule it stands above.

    BITE: the rule runs edge to edge. It turns a floating diagonal into
    something printed, and gives the stroke a ground to rise from.
    """
    slash = band([(25.0, 92.0), (75.0, 8.0)], 10.0)
    rule = rect(0.0, 86.0, 100.0, 98.0)
    return d(slash, rule), "nonzero"


def mark_magnitude():
    """A vector reduced to its definition: a quantity that has a heading.

    BITE: the taper. Mass at the tail, none at the tip — direction stated
    without an arrowhead anywhere on the page.
    """
    a, b = (14.0, 86.0), (88.0, 16.0)
    dirv = _norm((b[0] - a[0], b[1] - a[1]))
    n = _left_normal(dirv)
    w0, w1 = 15.0, 3.0
    return d([
        (a[0] + w0 * n[0], a[1] + w0 * n[1]),
        (b[0] + w1 * n[0], b[1] + w1 * n[1]),
        (b[0] - w1 * n[0], b[1] - w1 * n[1]),
        (a[0] - w0 * n[0], a[1] - w0 * n[1]),
    ]), "nonzero"


def mark_caret():
    """The other half of the ancestor: the chevron kept, the V retired.

    BITE: the asymmetric apex. A symmetrical caret is a user-interface arrow;
    an off-centre one with unequal arms is a mark that leans.
    """
    apex = (52.0, 18.0)
    caret = band([ray(apex, -148.0, 56.0), apex, ray(apex, 124.0, 60.0)], 10.5)
    rule = rect(0.0, 76.0, 100.0, 89.0)
    return d(clip_to_square(caret), rule), "nonzero"


CANDIDATES = [
    ("a-open-v", "The Open Block", mark_open_v),
    ("b-waterline", "Waterline", mark_waterline),
    ("c-serif-v", "The Overshoot", mark_serif_v),
    ("d-standard", "The Standard", mark_standard),
    ("e-magnitude", "Magnitude", mark_magnitude),
    ("f-caret", "Caret &amp; Rule", mark_caret),
]

SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">\n'
    '  <path{rule} fill="currentColor" d="{d}"/>\n'
    "</svg>\n"
)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for slug, name, fn in CANDIDATES:
        path_d, fill_rule = fn()
        attr = f' fill-rule="{fill_rule}"' if fill_rule == "evenodd" else ""
        (OUT / f"{slug}.svg").write_text(SVG.format(rule=attr, d=path_d))
        manifest[slug] = {"name": name, "d": path_d, "fill_rule": fill_rule}
        print(f"{slug:14s} {len(path_d):4d} chars  fill-rule={fill_rule}")
    (OUT / "paths.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
