"""Which rainbow section a color belongs in.

The reference sheets are alphabetical, which is the one thing you never know
about a scarf you're holding. You know it's *red*. This module is what lets a
sheet be organised that way instead.

A recipe claims one or more bands and is printed in every section it claims —
a red-and-orange scarf appears under both. That follows from how the cloth
actually works: the dyes are not blended, so a two-dye scarf shows both colors
distinctly (see `colorutils` for the same principle applied to the games).
Averaging a recipe down to a single band would file it under a color that
appears nowhere on it.

## Three axes, not one

Hue alone picks the wrong band constantly, because it has an opinion about
colors that have no hue worth naming:

    639 Jet Black   #000000   hue 0     -> "red"
    413 True Black  #000001   hue 240   -> "blue"
    Slate           #708090   hue 210   -> "blue"
    488 Ivory       #f3ead7   hue 41    -> "orange"

So hue picks the band, **saturation** asks whether it's a color at all, and
**lightness** catches the tints and shades nobody names by hue (cream, black,
and brown — brown being the one case that needs all three at once: dark, dull,
and orange).

## This is a suggestion, not an answer

Measured against the 41 dyes actually used in recipes, the hex path gets about
85-90% right; the photo path gets the dominant color of a scarf right about 4
times in 5, and finds every band on a multi-color scarf far less often than
that. Magenta (`412 Pink Orchid`, `425 Amethyst`) is genuinely undecidable
between pink and purple at these thresholds, and dark greens shot in shadow
read as black.

Which is why nothing here writes to the database. `Recipe.color_bands` is set
by a human on the classification page; these functions only fill the form in.
A wrong band is worse than no band — it sends someone to the orange section
for a scarf filed under red, and the failure is silent.
"""
import colorsys

from .colorutils import hex_to_rgb

#: The sections, in the order they print. Slug, label, and the chip color the
#: UI paints the toggle with — a representative of the band, not a boundary.
#:
#: Indigo is deliberately absent. The blues in stock run 219-248 degrees (Navy,
#: Sapphire, Midnight) and the violets 254-277, leaving indigo no territory of
#: its own: the section would be empty or arbitrary, and worse, you'd never be
#: sure whether to look there or in blue. Pink and brown are here for the
#: opposite reason — both are what someone actually says out loud about a
#: scarf, and both are real sections of the stock rather than edge cases.
BANDS = (
    ("red", "Red", "#c0392b"),
    ("orange", "Orange", "#e67e22"),
    ("yellow", "Yellow", "#e8c31a"),
    ("green", "Green", "#27924f"),
    ("blue", "Blue", "#2657a8"),
    ("purple", "Purple", "#6a3d9a"),
    ("pink", "Pink", "#e07aa8"),
    ("brown", "Brown", "#7a4b2a"),
    ("grey", "Grey", "#8a8f96"),
    ("black", "Black", "#2b2b2b"),
)

BAND_CHOICES = [(slug, label) for slug, label, _ in BANDS]
BAND_SLUGS = [slug for slug, _, _ in BANDS]
BAND_LABELS = {slug: label for slug, label, _ in BANDS}
BAND_COLORS = {slug: color for slug, _, color in BANDS}

#: The two achromatic sections. They were one band, `neutral`, which was the
#: wrong shape for the shelf: "neutral" is a category name, not a thing anyone
#: says about a scarf, and it filed jet black next to ivory. Someone holding a
#: black scarf looks under black. Grey keeps the rest — silver, slate, cream,
#: ivory — because those do read as one family to the eye, and none of them is
#: a section on its own.
GREY = "grey"
BLACK = "black"
NEUTRALS = (GREY, BLACK)

#: The chromatic bands, in print order. Everything except grey and black.
CHROMATIC = tuple(s for s in BAND_SLUGS if s not in NEUTRALS)


#: Where yellow stops and green starts, in degrees of hue. Named rather than
#: inlined because it is the one boundary anybody has actually examined, and
#: `public/color-bands/` prints the number on the page — a page quoting 70
#: while the code said 61 would be worse than no page.
YELLOW_ENDS = 61.0


#: Every hue line `band_for_hsl` draws, going round the wheel. Listed here so
#: `public/color-bands/` can offer them all rather than only the one that got
#: examined. The numbers are restated rather than shared, because threading
#: seven constants through the function below would cost more clarity than it
#: buys — `EdgesMatchTheRuleTests` pins them by probing the classifier either
#: side of each line, which catches drift without anyone restating anything.
HUE_EDGES = (
    ("red-orange", 15.0),
    ("orange-yellow", 45.0),
    ("yellow-green", YELLOW_ENDS),
    ("green-blue", 178.0),
    ("blue-purple", 250.0),
    ("purple-pink", 330.0),
    ("pink-red", 345.0),
)

#: Saturation and lightness for asking "which bands does this line divide?".
#: Deliberately saturated and mid-toned, so the answer is about hue: a duller
#: or darker probe gets caught by the brown and grey rules first and would
#: label half the lines "brown".
EDGE_PROBE = (0.90, 0.50)


def edge_bands(degrees):
    """The two bands a hue line divides, at `EDGE_PROBE`.

    Computed rather than written down, so a label on the page can't disagree
    with what the classifier does. The red end genuinely splits by lightness
    as well — above 330 a pale colour is pink and a dark one red — so this
    reports what a mid-tone gets and the page says as much.
    """
    sat, light = EDGE_PROBE
    return (
        band_for_hsl(degrees - 0.5, sat, light),
        band_for_hsl(degrees + 0.5, sat, light),
    )


def band_for_hsl(h, s, ll, *, black=0.12, grey=0.18, brown_l=0.60,
                 yellow_ends=YELLOW_ENDS):
    """The band for one color, given hue (0-360) and HLS saturation/lightness.

    `black`, `grey` and `brown_l` are all loosened or tightened by the photo
    path, where a dye in a shadowed fold is genuinely dark without being black,
    and where "dark" itself means something different than on a swatch card.
    Changing them for swatch hexes would be wrong — a swatch is lit evenly and
    means what it says.
    """
    # --- axis 2 and 3 first: is this a color at all? ---
    if ll <= black:
        return BLACK                         # black, whatever its nominal hue
    if ll >= 0.88 and s < 0.50:
        return GREY                          # white; a pale *pink* stays pink
    if s < grey:
        return GREY                          # grey, ditto
    if ll >= 0.78 and s < 0.65 and h < 70:
        # This 70 is *not* the yellow/green boundary below, which is 61. It is
        # how far up the warm end cream reaches, and moving one with the other
        # would drop `488 Ivory` (hue 41, but pale and dull) into yellow.
        return GREY                          # cream / ivory / champagne

    # --- brown: dark, dull, and warm. The only band needing all three axes ---
    if ll < 0.35 and s < 0.55 and (h < 45 or h > 340):
        return "brown"
    if 20 <= h < 45 and ll < brown_l and s < 0.75:
        return "brown"

    # --- axis 1: hue picks the section ---
    if h < 15 or h >= 345:
        return "pink" if ll > 0.62 else "red"
    if h < 45:
        return "orange"
    if h < yellow_ends:
        # 61, not 70. Sorted by hue the catalogue has an empty corridor from
        # 69.2 to 79.3 — no dye lives there — so a line anywhere in it classifies
        # nothing, which is how 70 survived unexamined. Below the corridor sits a
        # tight cluster that every one of us reads as green: `465 Lichen` (62.4),
        # `628 Chartreuse (Neon)` (62.6), `461 Avocado` (64.9), `479 Radioactive`
        # (66.6), `448 Chartreuse` (69.2). The nearest true yellow underneath is
        # `445 Fluorescent Lemon` at exactly 60.0, so 61 takes the whole cluster
        # and leaves the yellows alone with a degree to spare.
        return "yellow"
    if h < 178:
        # 178, not 170: `452 Forest Green` (#0b473e) sits at hue 171 and was
        # landing in blue. `631 Teal` (193) and `624 Turquoise` (196) stay blue.
        return "green"
    if h < 250:
        return "blue"
    if h < 330:
        return "purple"
    return "pink" if ll > 0.55 else "red"


def band_for_rgb(rgb, **kwargs):
    """The band for an (r, g, b) 0-255 triple."""
    r, g, b = (c / 255.0 for c in rgb)
    h, ll, s = colorsys.rgb_to_hls(r, g, b)
    return band_for_hsl(h * 360, s, ll, **kwargs)


def band_for_hex(value, **kwargs):
    """The band for a swatch hex. None if it can't be parsed."""
    rgb = hex_to_rgb(value)
    return band_for_rgb(rgb, **kwargs) if rgb else None


def sort_bands(bands):
    """Bands in print order, de-duplicated. The order is the rainbow, so a
    stored list always reads red-first however it was clicked."""
    seen = set(bands)
    return [slug for slug in BAND_SLUGS if slug in seen]


def bands_from_dyes(recipe):
    """The bands a recipe's recorded dyes imply — one per dye, de-duplicated.

    Empty if the recipe has no dyes yet, which is the honest answer: it means
    nothing is known, not that the scarf is colorless.

    `RecipeDye.ratio` is ignored on purpose, for the same reason `colorutils`
    ignores it: ratio says how much cloth a dye covers, not whether you can see
    it. The 10% accent dye still gets its own band, because someone looking for
    that color will still spot it on the scarf.

    Grey and black are the exception, and only claim the recipe when one of
    them is the *only* thing there. Black, grey and cream are working dyes
    here, not colorways: they ground and shade the colors beside them. Every
    recipe in stock that reads as achromatic reads as something else too — `turq-mid-black`,
    `russet-cab-black`, `grey-forest-navy` — and nobody hunting for any of
    those looks in the grey or black section. Left in, they would have been
    the largest section on the sheet between them, at 21 of 38, without one
    scarf in either that anybody would call grey or black.
    """
    bands = []
    for rd in recipe.recipe_dyes.all():
        band = band_for_hex(rd.dye.hex_color)
        if band:
            bands.append(band)

    chromatic = [b for b in bands if b not in NEUTRALS]
    return sort_bands(chromatic or bands)


#: How much of a photo's *chromatic* area a band needs before it's suggested.
#: Measured against the 25 photos on hand: below this, single-band scarves start
#: dragging in stray bands off the granite countertop and the JPEG fringing
#: around the folds.
PHOTO_BAND_SHARE = 0.07

#: ...and this much of the whole crop, background included. A band has to clear
#: both bars.
#:
#: The share-of-color bar alone breaks down exactly where there is barely any
#: color: on a near-grey scarf, dividing by a tiny chromatic mass amplifies
#: sensor noise and JPEG fringing into confident-looking bands, and the grey
#: RECTAN-SEASMO came back claiming orange, blue and brown off 10% chroma.
#: Requiring a band to actually cover some visible area of the cloth costs
#: nothing on a real colorway and silences that.
PHOTO_BAND_FLOOR = 0.04

#: The crop taken before sampling, as (left, top, right, bottom) fractions.
#: Every product photo is shot the same way — scarf on white posterboard on
#: speckled granite, barcode card laid at the bottom — so the card and most of
#: the counter can be cut off geometrically rather than guessed at. The counter
#: that survives is grey and drops out as neutral anyway.
PHOTO_CROP = (0.08, 0.04, 0.92, 0.72)

#: When this much of a photo has no nameable hue, grey or black is suggested
#: *as well as* whatever colors cleared the bar — not instead of them. Which of
#: the two is whichever covers more of the crop.
#:
#: The alternative was a floor: below some share of color, call the whole scarf
#: grey. It doesn't survive the actual photos. The two shots of RECTAN-FURIOS
#: (slate blue with rust) come in at 20% and 15% chromatic, and a genuinely
#: grey scarf comes in at 10% — far too thin a gap to hang a rule on, and the
#: rule would have thrown away the rust.
#:
#: Suggesting both is the honest reading anyway: a scarf that is four-fifths
#: unnameable grey with rust in the folds is a muted colorway, and it plausibly
#: belongs in the neutral section *and* the brown one. Which is a judgement,
#: so it goes to the person confirming rather than being settled here.
PHOTO_NEUTRAL_SHARE = 0.70


def bands_from_image(fp, *, share=PHOTO_BAND_SHARE):
    """The bands visible in a product photo. `fp` is anything PIL can open.

    Grey and black pixels are counted but never suggested on their own merits,
    and the share each band needs is measured against the *chromatic* pixels
    only. That's what makes
    the background self-cancelling: posterboard, barcode card and granite are
    all neutral, so however much of the frame they take, they don't dilute the
    scarf's own colors.

    The black and grey cutoffs are loosened from the swatch defaults because
    silk photographs badly for this purpose: it's specular, so highlights blow
    out to white, and deep dyes in a shadowed fold crush toward black. A deep
    green scarf that reads as 'mostly grey' is a lighting artefact, not a
    description.

    Brown is *tightened* in the opposite direction, for a related reason: lit
    gold silk sits at a lightness a swatch card never reaches, so the swatch
    rule for "dark, dull and warm" swallowed the gold half of RECTAN-PRINCE
    whole. Requiring genuine darkness keeps cocoa brown and hands the gold back
    to orange.

    Returns [] rather than raising if the file can't be read — a broken image
    should leave the row unsuggested, not break the page.
    """
    from collections import Counter

    from PIL import Image

    try:
        im = Image.open(fp)
        im.load()
        if im.mode != "RGB":
            im = im.convert("RGB")
    except Exception:
        return []

    w, h = im.size
    left, top, right, bottom = PHOTO_CROP
    im = im.crop((int(w * left), int(h * top), int(w * right), int(h * bottom)))
    # Downsampling is the point, not just speed: averaging neighbours knocks
    # out the JPEG fringing along every fold, which otherwise invents thin
    # bands of whatever hue sits between the two sides of an edge.
    im.thumbnail((160, 160), Image.LANCZOS)

    counts = Counter(
        band_for_rgb(px, black=0.08, grey=0.15, brown_l=0.45)
        for px in im.getdata()
    )
    total = sum(counts.values())
    neutral_total = sum(counts.get(b, 0) for b in NEUTRALS)
    chromatic = total - neutral_total
    if not chromatic:
        # A genuinely colorless scarf — the greys are the scarf, not the
        # counter. Which of the two it is comes from which one it actually is.
        return [max(NEUTRALS, key=lambda b: counts.get(b, 0))]

    # Each band's share is measured against the *chromatic* pixels only, which
    # is what makes the background self-cancelling: posterboard, barcode card
    # and granite are all neutral, so however much of the frame they take, they
    # can't dilute the scarf's own colors below the bar.
    found = [
        band
        for band, n in counts.items()
        if band not in NEUTRALS
        and n / chromatic >= share
        and n / total >= PHOTO_BAND_FLOOR
    ]
    if neutral_total / total >= PHOTO_NEUTRAL_SHARE:
        found.append(max(NEUTRALS, key=lambda b: counts.get(b, 0)))
    return sort_bands(found)
