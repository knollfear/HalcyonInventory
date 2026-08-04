"""
Color math for the matching game's board deal.

The game is only a real test of knowledge if a board is drawn from one color
family — telling Blueberry from Midnight Sky from Aegean Sea is the skill worth
drilling, whereas telling Mufasa from Forest Fire is not. So the deal
deliberately seeks out near neighbours instead of spreading the board out.

A recipe's color here is the ratio-weighted average of its dyes' swatch hexes.
That is a *proxy*: dye mixing is subtractive, so the photographed scarf can drift
from the average of the swatches. It's plenty good enough to group blues with
blues, which is all the clustering needs; it is not reliable for fine ordering
within a family. Sampling the dominant color from the actual photo would be more
accurate but far too slow to do per deal — it would need a precompute step.

No third-party dependencies: the sRGB -> Lab conversion is written out below.
"""
import random

# D65 reference white, the illuminant sRGB is defined against.
_D65 = (95.047, 100.000, 108.883)


def hex_to_rgb(value):
    """'#1a2b3c' (or '1a2b3c', or '#abc') -> (26, 43, 60). None if unparseable.

    ColorField stores '#RRGGBB', but hand-edited data and fixtures are not
    always so tidy, so be forgiving rather than raising mid-deal.
    """
    if not value:
        return None
    v = str(value).strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        return None
    try:
        return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def rgb_to_lab(rgb):
    """sRGB 0-255 -> CIE L*a*b* (D65).

    Lab is used rather than raw RGB because RGB distance badly misjudges
    perceptual closeness among dark colors — precisely the dark-blues case this
    module exists to serve.
    """
    # sRGB -> linear RGB (undo the gamma companding)
    linear = []
    for c in rgb:
        c = c / 255.0
        linear.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = linear

    # linear RGB -> XYZ (sRGB matrix, D65), scaled to 0-100
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) * 100
    y = (r * 0.2126 + g * 0.7152 + b * 0.0722) * 100
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) * 100

    # XYZ -> Lab
    def f(t):
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t) + (16 / 116)

    fx, fy, fz = (f(x / _D65[0]), f(y / _D65[1]), f(z / _D65[2]))
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def hex_to_lab(value):
    """Convenience: swatch hex straight to Lab. None if unparseable."""
    rgb = hex_to_rgb(value)
    return rgb_to_lab(rgb) if rgb else None


def delta_e(lab1, lab2):
    """CIE76 color difference. Plain Euclidean distance in Lab.

    CIE76 is the crude one — CIEDE2000 is more faithful — but we only need a
    *ranking* of near-vs-far to build a family, not a calibrated number.
    """
    return sum((a - b) ** 2 for a, b in zip(lab1, lab2)) ** 0.5


def recipe_color(recipe):
    """One representative Lab color for a recipe, or None if it has no usable
    swatches.

    Averages the dyes' hexes weighted by RecipeDye.ratio where it's set, so a
    recipe that is 90% Cerulean and 10% Jet reads as blue rather than blue-grey.
    Recipes with no ratios fall back to an equal-weight average.

    Averaging happens in linear-ish RGB space then converts once, which is what
    you want: averaging Lab coordinates directly would distort the result.
    """
    total_weight = 0.0
    acc = [0.0, 0.0, 0.0]

    for rd in recipe.recipe_dyes.all():
        rgb = hex_to_rgb(rd.dye.hex_color)
        if rgb is None:
            continue
        weight = float(rd.ratio) if rd.ratio else 1.0
        if weight <= 0:
            continue
        for i in range(3):
            acc[i] += rgb[i] * weight
        total_weight += weight

    if not total_weight:
        return None
    return rgb_to_lab(tuple(a / total_weight for a in acc))


def pick_color_cluster(recipes, n, rng=None):
    """Pick `n` recipes that look alike: a random seed, plus its nearest
    neighbours by color.

    The seed is random on every call so replays land on different families — a
    blues board, then a reds board — rather than always serving the same tight
    cluster.

    Recipes whose color can't be computed (no dyes, unparseable swatches) can't
    be clustered, so they're used only as filler when there aren't enough
    colored ones to fill the board. Returns at most `n`, fewer only if the pool
    itself is smaller.
    """
    rng = rng or random
    if n <= 0:
        return []

    colored = []
    uncolored = []
    for recipe in recipes:
        lab = recipe_color(recipe)
        (colored if lab else uncolored).append((recipe, lab))

    if not colored:
        return [r for r, _ in rng.sample(uncolored, min(n, len(uncolored)))]

    seed, seed_lab = rng.choice(colored)
    others = [(r, lab) for r, lab in colored if r.pk != seed.pk]
    others.sort(key=lambda pair: delta_e(seed_lab, pair[1]))

    picked = [seed] + [r for r, _ in others[: n - 1]]

    # Only pad with colorless recipes if the colored ones ran out.
    if len(picked) < n and uncolored:
        rng.shuffle(uncolored)
        picked += [r for r, _ in uncolored[: n - len(picked)]]

    return picked[:n]
