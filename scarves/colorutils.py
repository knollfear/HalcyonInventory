"""
Color math for the public games: the matching board's deal and the name quiz's
distractors.

A game is only a real test of knowledge if the options are drawn from one color
family — telling Blueberry from Midnight Sky from Aegean Sea is the skill worth
drilling, whereas telling Mufasa from Forest Fire is not. So both deals
deliberately seek out near neighbours instead of spreading things out.

A recipe is treated as a *palette* — one point per dye — and never as a single
averaged color. That distinction is the whole model, and getting it wrong is
easy: the dyes are not blended into one homogeneous shade. A three-dye scarf
shows all three distinctly and flows between them, so a red-and-blue scarf reads
as red and blue, not as the purple you get by averaging them. Averaging invents
a color that appears nowhere on the cloth, and then matches scarves on it.

So similarity here means *shared colors*. Two scarves are confusable when one's
colors turn up on the other, which is why `palette_distance` leads with the
closest matching pair of dyes: a red-and-blue scarf belongs next to a
red-and-yellow one, even though their averages are on opposite sides of the
wheel.

Still a proxy, for the reasons it always was: these are swatch hexes, and the
photographed cloth drifts from them. Sampling colors from the actual photo would
be more faithful but far too slow per deal — it would need a precompute step.

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


def recipe_palette(recipe):
    """The colors a scarf actually shows: one Lab point per dye, unaveraged.

    Empty if none of the dyes have a usable swatch.

    RecipeDye.ratio is deliberately ignored. Ratio governs how much cloth each
    dye covers, not whether it's visible — the 10% dye still gets its own band —
    and the game asks "have you seen this color on this scarf", not "how much of
    it was there".
    """
    palette = []
    for rd in recipe.recipe_dyes.all():
        lab = hex_to_lab(rd.dye.hex_color)
        if lab:
            palette.append(lab)
    return palette


def palette_distance(a, b):
    """How confusable two palettes are, as a sort key. Lower is more alike.

    Returns a `(closest_pair, spread)` tuple, compared left to right:

    * `closest_pair` — the distance between the most similar dye in each. This
      leads because sharing one visible color is the strongest signal that two
      scarves get mixed up, and it's what makes red/blue sit beside red/yellow
      rather than beside a solid purple.
    * `spread` — the mean distance from each dye to its nearest counterpart in
      the other palette, both ways round. `closest_pair` alone ties far too
      much (every recipe with a black accent would read as identical to every
      other), so this breaks those ties by asking whether the *rest* of the two
      palettes line up too.

    None if either palette is empty, since there's nothing to compare.
    """
    if not a or not b:
        return None

    grid = [[delta_e(x, y) for y in b] for x in a]
    closest = min(min(row) for row in grid)
    forward = sum(min(row) for row in grid) / len(a)
    backward = sum(min(col) for col in zip(*grid)) / len(b)
    return (closest, (forward + backward) / 2)


def nearest_by_color(candidates, target, n, recipe_of=None, rng=None):
    """The `n` candidates sharing the most color with `target`.

    This is the distractor picker for the name quiz. Offering Midnight Sky and
    Aegean Sea against a photo of Blueberry is the question worth asking;
    offering Mufasa and Forest Fire is a color-wheel question a stranger can
    answer, which is not the skill being drilled.

    `candidates` need not be recipes — pass `recipe_of` to pull the recipe off
    each one (the quiz passes finished products). Candidates with no usable
    swatches can't be ranked, so they're used only as filler when the palettes
    run out.
    """
    rng = rng or random
    recipe_of = recipe_of or (lambda item: item)
    if n <= 0:
        return []

    target_palette = recipe_palette(target)
    if not target_palette:
        return list(rng.sample(candidates, min(n, len(candidates))))

    scored = []
    unpainted = []
    for item in candidates:
        distance = palette_distance(target_palette, recipe_palette(recipe_of(item)))
        if distance is None:
            unpainted.append(item)
        else:
            scored.append((distance, item))

    # Sorting on the distance tuple alone would compare the items themselves on
    # a tie, which model instances don't support.
    scored.sort(key=lambda pair: pair[0])
    picked = [item for _, item in scored[:n]]

    if len(picked) < n and unpainted:
        rng.shuffle(unpainted)
        picked += unpainted[: n - len(picked)]

    return picked[:n]


def pick_color_cluster(recipes, n, rng=None):
    """Pick `n` recipes that look alike: a random seed, plus its nearest
    neighbours by color.

    The seed is random on every call so replays land on different families — a
    blues board, then a reds board — rather than always serving the same tight
    cluster.

    Recipes with no usable swatches can't be clustered, so they're used only as
    filler when there aren't enough painted ones to fill the board. Returns at
    most `n`, fewer only if the pool itself is smaller.
    """
    rng = rng or random
    if n <= 0:
        return []

    painted = []
    unpainted = []
    for recipe in recipes:
        palette = recipe_palette(recipe)
        (painted if palette else unpainted).append((recipe, palette))

    if not painted:
        return [r for r, _ in rng.sample(unpainted, min(n, len(unpainted)))]

    seed, seed_palette = rng.choice(painted)
    others = [
        (palette_distance(seed_palette, palette), recipe)
        for recipe, palette in painted
        if recipe.pk != seed.pk
    ]
    others.sort(key=lambda pair: pair[0])

    picked = [seed] + [recipe for _, recipe in others[: n - 1]]

    # Only pad with colorless recipes if the painted ones ran out.
    if len(picked) < n and unpainted:
        rng.shuffle(unpainted)
        picked += [r for r, _ in unpainted[: n - len(picked)]]

    return picked[:n]
