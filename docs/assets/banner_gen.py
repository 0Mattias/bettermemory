#!/usr/bin/env python3
"""bettermemory README banner, in the anaphase light visual system.

Two geodesic icospheres (subdivision levels 0-3 drawn simultaneously,
coarse cages on slightly larger radii, depth fog on the far hemisphere,
ink glints near the silhouette, hub dots at coarse joints) joined by
spindle threads carrying fact-dots from the ghost sphere (the past) to
the solid sphere (the present). Wordmark + tagline are outlined
Bitstream Charter glyphs (freely licensed design), no font dependency.

Palette is strictly the site's: paper #f6f3ec, ink #211f19. No green.

Regenerate (writes banner.svg next to this file; seeded, reproducible):

    uv run --with fonttools --with uharfbuzz python docs/assets/banner_gen.py

Needs macOS system Charter for the wordmark outlines.
"""

import math
import random
import re

W, H = 1520, 400
PAPER = "#f6f3ec"
INK = "#211f19"
BORDER = "#d8d5ce"
RND = random.Random(11)


# ---------------------------------------------------------------- geometry
def icosphere_levels(max_level):
    """Return [(verts, edges)] per subdivision level, verts on unit sphere."""
    phi = (1 + 5**0.5) / 2

    def norm(v):
        n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
        return (v[0] / n, v[1] / n, v[2] / n)

    vs = [
        norm(v)
        for v in [
            (-1, phi, 0),
            (1, phi, 0),
            (-1, -phi, 0),
            (1, -phi, 0),
            (0, -1, phi),
            (0, 1, phi),
            (0, -1, -phi),
            (0, 1, -phi),
            (phi, 0, -1),
            (phi, 0, 1),
            (-phi, 0, -1),
            (-phi, 0, 1),
        ]
    ]
    faces = [
        (0, 11, 5),
        (0, 5, 1),
        (0, 1, 7),
        (0, 7, 10),
        (0, 10, 11),
        (1, 5, 9),
        (5, 11, 4),
        (11, 10, 2),
        (10, 7, 6),
        (7, 1, 8),
        (3, 9, 4),
        (3, 4, 2),
        (3, 2, 6),
        (3, 6, 8),
        (3, 8, 9),
        (4, 9, 5),
        (2, 4, 11),
        (6, 2, 10),
        (8, 6, 7),
        (9, 8, 1),
    ]
    levels = []
    for lvl in range(max_level + 1):
        edges = set()
        for f in faces:
            for a, b in ((f[0], f[1]), (f[1], f[2]), (f[2], f[0])):
                edges.add((min(a, b), max(a, b)))
        levels.append((list(vs), sorted(edges)))
        if lvl == max_level:
            break
        cache = {}

        def mid(a, b):
            key = (min(a, b), max(a, b))
            if key not in cache:
                va, vb = vs[a], vs[b]
                vs.append(
                    norm(
                        ((va[0] + vb[0]) / 2, (va[1] + vb[1]) / 2, (va[2] + vb[2]) / 2)
                    )
                )
                cache[key] = len(vs) - 1
            return cache[key]

        nf = []
        for a, b, c in faces:
            ab, bc, ca = mid(a, b), mid(b, c), mid(c, a)
            nf += [(a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)]
        faces = nf
    return levels


LEVELS_GEO = icosphere_levels(3)


def rotated(v, ax, ay, az):
    x, y, z = v
    c, s = math.cos(ax), math.sin(ax)
    y, z = c * y - s * z, s * y + c * z
    c, s = math.cos(ay), math.sin(ay)
    x, z = c * x + s * z, -s * x + c * z
    c, s = math.cos(az), math.sin(az)
    x, y = c * x - s * y, s * x + c * y
    return (x, y, z)


def project(v, cx, cy, radius):
    x, y, z = v
    p = 1.0 / (1.0 - 0.10 * z)  # slight perspective, +z toward viewer
    return (cx + radius * x * p, cy - radius * y * p, z)


# ---------------------------------------------------------------- sphere pass
# (level, radius_factor, width, base_opacity) - coarse cage slightly larger.
LEVEL_STYLE = [
    (0, 1.035, 1.30, 0.30),
    (1, 1.018, 0.92, 0.26),
    (2, 1.006, 0.62, 0.20),
    (3, 1.000, 0.42, 0.125),
]


def render_sphere(cx, cy, radius, rot, strength=1.0, glints=True):
    """Return svg strings for one sphere. strength ghosts a sphere."""
    parts = []
    groups = {}  # (width, opacity) -> [path segments]
    glint_edges = []
    for lvl, rf, wdt, base in LEVEL_STYLE:
        verts, edges = LEVELS_GEO[lvl]
        rv = [rotated(v, *rot) for v in verts]
        pts = [project(v, cx, cy, radius * rf) for v in rv]
        for a, b in edges:
            x1, y1, z1 = pts[a]
            x2, y2, z2 = pts[b]
            zm = (z1 + z2) / 2
            front = zm >= 0
            # limb weighting: rim proximity of the edge midpoint, projected
            rho = min(
                1.0, math.hypot((x1 + x2) / 2 - cx, (y1 + y2) / 2 - cy) / (radius * rf)
            )
            op = base * strength
            if front:
                op *= 0.38 + 0.62 * rho**2.2  # denser at the rim
                w = wdt
            else:
                op *= 0.26  # depth fog
                w = wdt * 0.82
            opq = round(max(op, 0.012) * 50) / 50
            key = (round(w, 2), opq)
            groups.setdefault(key, []).append(f"M{x1:.1f} {y1:.1f}L{x2:.1f} {y2:.1f}")
            if glints and lvl == 1 and front and zm < 0.45 and rho > 0.72:
                glint_edges.append((x1, y1, x2, y2, wdt))
    for (w, op), segs in sorted(groups.items()):
        parts.append(
            f'<path d="{"".join(segs)}" stroke="{INK}" '
            f'stroke-width="{w}" opacity="{op}" fill="none"/>'
        )
    # ink glints: a few near-silhouette edges surge toward ink, comet-style
    if glints and glint_edges:
        for x1, y1, x2, y2, wdt in RND.sample(glint_edges, min(8, len(glint_edges))):
            op = (0.30 + RND.random() * 0.18) * strength
            parts.append(
                f'<path d="M{x1:.1f} {y1:.1f}L{x2:.1f} {y2:.1f}" '
                f'stroke="{INK}" stroke-width="{wdt:.2f}" '
                f'opacity="{op:.2f}" fill="none" stroke-linecap="round"/>'
            )
    # hub dots at the level-0 / level-1 joints
    for lvl, rf, r_dot, op_f, op_b in (
        (0, 1.035, 2.3, 0.55, 0.13),
        (1, 1.018, 1.25, 0.26, 0.0),
    ):
        verts = LEVELS_GEO[lvl][0]
        for v in verts:
            x, y, z = project(rotated(v, *rot), cx, cy, radius * rf)
            if z > 0.08 and op_f:
                parts.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r_dot}" '
                    f'fill="{INK}" opacity="{op_f * strength:.2f}"/>'
                )
            elif z <= 0.08 and op_b:
                parts.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r_dot * 0.7:.1f}" '
                    f'fill="{INK}" opacity="{op_b * strength:.2f}"/>'
                )
    return parts


# ---------------------------------------------------------------- spindle
def qpoint(p0, c, p1, t):
    mt = 1 - t
    return (
        mt * mt * p0[0] + 2 * t * mt * c[0] + t * t * p1[0],
        mt * mt * p0[1] + 2 * t * mt * c[1] + t * t * p1[1],
    )


def spindle(bx, by, rb, ax, ay, ra):
    parts = []
    threads = []
    n = 9
    for i in range(n):
        th = math.radians(-50 + 100 * i / (n - 1))
        p0 = (bx + rb * math.cos(th) * 0.995, by + rb * math.sin(th))
        p1 = (ax - ra * math.cos(th * 0.88), ay + ra * math.sin(th * 0.88))
        midy = (p0[1] + p1[1]) / 2
        pinch = 0.50 + RND.random() * 0.22
        ctrl = (
            (p0[0] + p1[0]) / 2 + (RND.random() - 0.5) * 26,
            midy + ((by + ay) / 2 - midy) * pinch,
        )
        op = 0.11 + 0.20 * math.cos(th) ** 2
        parts.append(
            f'<path d="M{p0[0]:.1f} {p0[1]:.1f}Q{ctrl[0]:.1f} '
            f'{ctrl[1]:.1f} {p1[0]:.1f} {p1[1]:.1f}" stroke="{INK}" '
            f'stroke-width="0.5" opacity="{op:.2f}" fill="none"/>'
        )
        threads.append((p0, ctrl, p1))
    # fact dots in transit: pale as they leave the ghost, ink once verified
    for idx, t_ghost, t_solid in ((2, 0.22, 0.77), (4, 0.30, 0.72), (6, 0.18, 0.79)):
        p0, c, p1 = threads[idx]
        gx, gy = qpoint(p0, c, p1, t_ghost)
        sx, sy = qpoint(p0, c, p1, t_solid)
        parts.append(
            f'<circle cx="{gx:.1f}" cy="{gy:.1f}" r="1.8" fill="{INK}" opacity="0.26"/>'
        )
        parts.append(
            f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="2.2" fill="{INK}" opacity="0.66"/>'
        )
        parts.append(
            f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="4.4" '
            f'fill="none" stroke="{INK}" stroke-width="0.55" opacity="0.30"/>'
        )
    mx, my = qpoint(*threads[4], 0.5)
    parts.append(
        f'<circle cx="{mx:.1f}" cy="{my:.1f}" r="1.9" fill="{INK}" opacity="0.42"/>'
    )
    return parts


# ---------------------------------------------------------------- typesetting
def typeset(text, size, tracking=0.0):
    """Outline text with system Charter via harfbuzz; return (path_d, width)."""
    import uharfbuzz as hb
    from fontTools.ttLib import TTFont
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen

    path = "/System/Library/Fonts/Supplemental/Charter.ttc"
    blob = hb.Blob.from_file_path(path)
    face = hb.Face(blob, 0)
    font = hb.Font(face)
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf, {"kern": True, "liga": False})
    tt = TTFont(path, fontNumber=0)
    glyphset = tt.getGlyphSet()
    order = tt.getGlyphOrder()
    scale = size / face.upem
    x = 0.0
    cmds = []
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        pen = SVGPathPen(glyphset)
        tpen = TransformPen(
            pen, (scale, 0, 0, -scale, x + pos.x_offset * scale, -pos.y_offset * scale)
        )
        glyphset[order[info.codepoint]].draw(tpen)
        d = pen.getCommands()
        if d:
            cmds.append(d)
        x += pos.x_advance * scale + tracking
    d_all = " ".join(cmds)
    d_all = re.sub(r"(\d+\.\d{2})\d+", r"\1", d_all)
    return d_all, x - tracking


# ---------------------------------------------------------------- assemble
def build():
    ax_c, ay_c, ar = 1240.0, 201.0, 140.0  # solid sphere: the present
    bx_c, by_c, br = 858.0, 199.0, 121.0  # ghost sphere: the past

    wordmark_d, wm_w = typeset("bettermemory", 67)
    tagline_d, tg_w = typeset("memory that is checked before it is believed", 17, 0.25)

    svg = []
    svg.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'width="{W}" height="{H}" role="img" '
        f'aria-label="bettermemory: memory that is checked before it is believed">'
    )
    svg.append(
        "<!-- bettermemory. Artwork in the anaphase light system; "
        "wordmark outlines set in Bitstream Charter, "
        "(c) 1989-1992 Bitstream Inc., freely licensed. -->"
    )
    svg.append("<defs>")
    svg.append(
        f'<clipPath id="card"><rect x="0" y="0" width="{W}" height="{H}" '
        f'rx="14"/></clipPath>'
    )
    for gid, cx, cy, r in (
        ("glowA", ax_c, ay_c, ar * 1.85),
        ("glowB", bx_c, by_c, br * 1.7),
    ):
        svg.append(
            f'<radialGradient id="{gid}" cx="50%" cy="50%" r="50%">'
            f'<stop offset="0%" stop-color="#ffffff" stop-opacity="0.9"/>'
            f'<stop offset="48%" stop-color="#ffffff" stop-opacity="0.34"/>'
            f'<stop offset="100%" stop-color="#ffffff" stop-opacity="0"/>'
            f"</radialGradient>"
        )
    svg.append(
        '<linearGradient id="vig" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{INK}" stop-opacity="0.03"/>'
        f'<stop offset="10%" stop-color="{INK}" stop-opacity="0"/>'
        f'<stop offset="90%" stop-color="{INK}" stop-opacity="0"/>'
        f'<stop offset="100%" stop-color="{INK}" stop-opacity="0.03"/>'
        "</linearGradient>"
    )
    svg.append(
        '<filter id="grain" x="0" y="0" width="100%" height="100%">'
        '<feTurbulence type="fractalNoise" baseFrequency="0.8" '
        'numOctaves="2" seed="7" stitchTiles="stitch"/>'
        '<feColorMatrix type="matrix" values="0 0 0 0 0.129  0 0 0 0 0.122  '
        '0 0 0 0 0.098  0 0 0 0.055 0"/>'
        "</filter>"
    )
    svg.append("</defs>")

    svg.append('<g clip-path="url(#card)">')
    svg.append(f'<rect width="{W}" height="{H}" fill="{PAPER}"/>')
    # atmosphere: a broad neutral light lift behind the artwork
    svg.append(
        f'<ellipse cx="{(ax_c + bx_c) / 2:.0f}" cy="200" rx="560" ry="240" '
        f'fill="#ffffff" opacity="0.16"/>'
    )
    svg.append(
        f'<circle cx="{ax_c}" cy="{ay_c}" r="{ar * 1.85:.0f}" fill="url(#glowA)"/>'
    )
    svg.append(
        f'<circle cx="{bx_c}" cy="{by_c}" r="{br * 1.7:.0f}" fill="url(#glowB)" '
        f'opacity="0.7"/>'
    )

    svg += spindle(bx_c, by_c, br, ax_c, ay_c, ar)
    svg += render_sphere(bx_c, by_c, br, (0.42, -0.35, 0.10), strength=0.52)
    svg += render_sphere(ax_c, ay_c, ar, (0.33, 0.52, 0.08), strength=1.0)

    # wordmark + tagline (outlined Charter)
    svg.append(
        f'<g transform="translate(96 212)"><path d="{wordmark_d}" fill="{INK}"/></g>'
    )
    svg.append(
        f'<g transform="translate(98 258)"><path d="{tagline_d}" '
        f'fill="{INK}" opacity="0.62"/></g>'
    )

    svg.append(f'<rect width="{W}" height="{H}" fill="url(#vig)"/>')
    svg.append(f'<rect width="{W}" height="{H}" filter="url(#grain)" opacity="1"/>')
    svg.append("</g>")
    svg.append(
        f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="13.5" '
        f'fill="none" stroke="{BORDER}" stroke-width="1"/>'
    )
    svg.append("</svg>")
    return "\n".join(svg)


out = __file__.rsplit("/", 1)[0] + "/banner.svg"
doc = build()
with open(out, "w") as f:
    f.write(doc)
print(f"wrote {out}  ({len(doc) / 1024:.0f} KB)")
