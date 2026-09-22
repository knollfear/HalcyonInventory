"""The public games: the matching board and the quiz, plus the public colour-bands page."""
import colorsys
import random
import uuid

from django.http import HttpResponse
from django.urls import reverse
from django.db.models import Q
from django.views.decorators.http import require_http_methods
from django.shortcuts import render
from django.db.models import Prefetch

from ..sitemap import page_meta
from ..models import Dye, FinishedProduct, RecipeDye
from .. import colorbands
from ..colorutils import hex_to_rgb, nearest_by_color, pick_color_cluster
from ..models import Recipe


# --------------------------------------------------------------------------
# Matching game
#
# Public, and designed to be embedded on other origins (the Shopify store), so
# the whole game ships as one self-contained htmx fragment. The server deals a
# board; the browser plays it. Doing the flips server-side would be 50-100
# requests per game and would need a session cookie, which is blocked as a
# third-party cookie inside an embed.
# --------------------------------------------------------------------------

GAME_PAIR_SIZES = (4, 6, 8)
GAME_DEFAULT_PAIRS = 6


def _recipe_game_pool():
    """Active recipes that have at least one photographed active product.

    Unlike the PDF reference sheet (`_select_recipe_photos`), externally-hosted
    images are fine here — the browser fetches them itself — so we don't filter
    down to images with an uploaded file.
    """
    return list(
        Recipe.objects.filter(
            is_active=True,
            finished_products__is_active=True,
        )
        .filter(
            Q(finished_products__images__image__gt="")
            | Q(finished_products__images__image_url__gt=""),
        )
        .distinct()
        .prefetch_related(
            Prefetch(
                "recipe_dyes",
                queryset=RecipeDye.objects.select_related("dye").order_by("order", "id"),
            ),
            Prefetch(
                "finished_products",
                queryset=FinishedProduct.objects.active().prefetch_related("images"),
            ),
        )
    )


def _recipe_images(recipe):
    """Every usable image across a recipe's active products."""
    return [
        img
        for fp in recipe.finished_products.all()
        for img in fp.images.all()
        if img.image or img.image_url
    ]


def _deal_board(pairs, pool=None, rng=None, family=False):
    """Deal `pairs` photo/name card pairs, shuffled.

    One pair per *recipe*, never per product: an infinity and a rectangle from
    the same dye bath photograph almost identically, so dealing both would make
    the board unwinnable by sight.

    `family=True` draws the board from a single color family instead of at
    random, which is a much harder and more useful drill (Blueberry vs Midnight
    Sky vs Aegean Sea). It's opt-in because it leans on dye-swatch hexes as a
    proxy for the photographed color — see `colorutils` — and is only as good as
    the recipe/dye color data behind it.
    """
    rng = rng or random
    pool = _recipe_game_pool() if pool is None else pool

    if family:
        chosen = pick_color_cluster(pool, pairs, rng=rng)
    else:
        chosen = rng.sample(pool, min(pairs, len(pool)))

    cards = []
    for pair_id, recipe in enumerate(chosen, start=1):
        images = _recipe_images(recipe)
        if not images:
            continue
        image = rng.choice(images)
        dyes = [
            {"name": rd.dye.name, "hex_color": rd.dye.hex_color}
            for rd in recipe.recipe_dyes.all()
        ]
        cards.append({
            "pair_id": pair_id,
            "kind": "photo",
            "image_url": image.url,
            "alt_text": image.alt_text or recipe.name,
            "name": recipe.name,
            "dyes": dyes,
        })
        cards.append({
            "pair_id": pair_id,
            "kind": "name",
            "name": recipe.name,
            "dyes": dyes,
        })

    rng.shuffle(cards)
    return cards, len(cards) // 2


def _cors_headers(response):
    """Open up the game endpoints so any page can embed them.

    htmx sends the custom `HX-Request` header, which makes the browser fire a
    preflight — so `Allow-Origin` on its own is not enough, and getting this
    half-right fails silently in the console. The board is anonymous public
    read-only data with no cookies and no writes, hence the wildcard.
    """
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response["Access-Control-Allow-Headers"] = (
        "HX-Request, HX-Current-URL, HX-Target, HX-Trigger, HX-Trigger-Name, HX-Boosted"
    )
    response["Access-Control-Max-Age"] = "86400"
    return response


@require_http_methods(["GET", "OPTIONS"])
def game_board(request):
    """The game itself, as an embeddable fragment. Anonymous; no CSRF (GET only)."""
    if request.method == "OPTIONS":
        return _cors_headers(HttpResponse(status=204))

    try:
        requested = int(request.GET.get("pairs", GAME_DEFAULT_PAIRS))
    except (TypeError, ValueError):
        requested = GAME_DEFAULT_PAIRS
    if requested not in GAME_PAIR_SIZES:
        requested = GAME_DEFAULT_PAIRS

    # Opt-in: deal from one color family instead of at random. Off by default
    # until the recipe/dye color data is known to be trustworthy.
    family = request.GET.get("family") in ("1", "true", "yes")

    pool = _recipe_game_pool()
    cards, dealt = _deal_board(min(requested, len(pool)), pool=pool, family=family)

    # Absolute URLs throughout: this fragment is rendered into pages on *other*
    # origins, where a relative path would resolve against the host site and
    # 404. Testing only on the Django page would never catch it.
    for card in cards:
        url = card.get("image_url")
        if url and not url.startswith(("http://", "https://", "//")):
            card["image_url"] = request.build_absolute_uri(url)

    response = render(request, "scarves/partials/game_board.html", {
        "cards": cards,
        "pairs": dealt,
        "requested_pairs": requested,
        # Only offer sizes the catalog can actually fill.
        "sizes": [n for n in GAME_PAIR_SIZES if n <= len(pool)],
        "board_url": request.build_absolute_uri(reverse("game_board")),
        # Carried through so "play again" and the size toggle keep the mode.
        "family_qs": "&family=1" if family else "",
        "too_few": dealt < 2,
        # Scopes this instance's CSS and JS, so two embeds on one page don't
        # collide and a re-swap can't leave a stale listener behind.
        "instance_id": uuid.uuid4().hex[:8],
    })
    return _cors_headers(response)


@page_meta(
    title="Colour Bands",
    description="How a dye's hex becomes a section of the rainbow reference "
                "sheet. Every dye on file as a swatch, plotted by hue, with any "
                "of the boundaries draggable so the judgement calls are visible "
                "rather than asserted.",
    category="Public",
    note="No login required. ?edge=green-blue picks the boundary. Changes nothing.",
)
def color_bands_page(request):
    """A piece about the colour classifier, readable by anyone.

    Almost all template — but not a static file, for two reasons. The dyes are
    read live, so the page picks up whatever gets bought next week rather than
    freezing at the catalogue as it stood the day it was written. And the
    boundaries come from `colorbands.HUE_EDGES`, so a page quoting 70 after
    somebody moved it to 61 can't happen.

    Which boundary you're looking at rides in the query string rather than a
    session, so a particular argument is a link somebody can send.

    Each dye carries the band *Python* gave it. The slider re-classifies in the
    browser for the sake of exploring, and the page says so when the two
    disagree — the same rule the rest of the colour code follows: show the
    guess, never let it pass as the answer.
    """
    edges = []
    for slug, degrees in colorbands.HUE_EDGES:
        below, above = colorbands.edge_bands(degrees)
        if below == above:
            # A line the classifier draws that no mid-tone can see across:
            # 345 separates two zones that differ only in how light a colour
            # has to be to read pink. Offering it would be a slider that
            # appears to do nothing.
            continue
        edges.append({
            "slug": slug,
            "degrees": degrees,
            "below": below,
            "above": above,
            "label": f"{colorbands.BAND_LABELS[below]} / {colorbands.BAND_LABELS[above]}",
        })

    # Yellow/green is the default because it is the argument people actually
    # have — chartreuse and avocado are the jars two reasonable people fall out
    # over. Opening on red/orange, first only because it is first round the
    # wheel, buries that behind a boundary nobody disputes.
    wanted = request.GET.get("edge")
    fallback = next(
        (e for e in edges if e["slug"] == "yellow-green"), edges[0]
    )
    edge = next((e for e in edges if e["slug"] == wanted), None) or fallback

    # Where the slider is sitting rides in the URL beside which line it is,
    # because the page's job is to start an argument and an argument you can't
    # send is one you have to win in person. Parsed defensively for the same
    # reason `done=` is: a hand-edited or stale link should land somewhere
    # readable rather than erroring. The name is the slider's own, so the URL
    # is what the form would have serialised.
    at = edge["degrees"]
    try:
        typed = float(request.GET["cut"])
    except (KeyError, TypeError, ValueError):
        pass
    else:
        if 0 <= typed <= 360:
            at = round(typed * 2) / 2       # the slider's half-degree step

    dyes = []
    for dye in Dye.objects.select_related("brand"):
        rgb = hex_to_rgb(dye.hex_color)
        if not rgb:
            # A dye with no colour contributes nothing here, exactly as it
            # contributes nothing to a band, a palette or a rainbow sheet.
            continue
        r, g, b = (c / 255.0 for c in rgb)
        h, ll, sat = colorsys.rgb_to_hls(r, g, b)
        dyes.append({
            "name": dye.name,
            "hex": dye.hex_color,
            "hue": round(h * 360, 1),
            "sat": round(sat, 3),
            "light": round(ll, 3),
            "band": colorbands.band_for_hex(dye.hex_color),
            "brand": dye.brand.name if dye.brand_id else "",
        })
    dyes.sort(key=lambda d: d["hue"])

    return render(request, "scarves/color_bands.html", {
        "dyes_json": dyes,
        "bands": colorbands.BANDS,
        "edges": edges,
        "edges_json": {e["slug"]: e["degrees"] for e in edges},
        "edge": edge,
        "at": at,
        "dye_count": len(dyes),
    })


@page_meta(
    title="Scarf Matching Game",
    description="Public memory game: match each scarf photo to its dye recipe "
                "name. Boards are dealt from a single color family, so it drills "
                "the distinctions that actually matter.",
    category="Public",
    note="No login required; embeddable on other sites.",
)
def game_page(request):
    """Thin public shell. The game arrives via htmx so it can also be dropped
    into the Shopify storefront with the same endpoint."""
    return render(request, "scarves/game.html", {
        "board_url": reverse("game_board"),
        "default_pairs": GAME_DEFAULT_PAIRS,
        "quiz_url": reverse("quiz_page"),
        "embed_origin": request.build_absolute_uri("/").rstrip("/"),
    })


# --------------------------------------------------------------------------
# Name quiz
#
# Multiple-choice sibling of the matching game, and deliberately the same shape:
# one self-contained htmx fragment, dealt whole by the server and played in the
# browser, so it drops into the Shopify storefront through the same endpoint
# with no session cookie (blocked third-party inside an embed) and no per-answer
# round trip.
#
# The consequence of dealing whole is that the answers sit in the DOM, so the
# score is not tamper-proof. That's the accepted trade for embeddability — it's
# a shop-window game, not a leaderboard.
# --------------------------------------------------------------------------

QUIZ_LENGTHS = (5, 10, 15)
QUIZ_DEFAULT_QUESTIONS = 10
QUIZ_CHOICES = 4
# Below this there aren't enough names to build a question that isn't a giveaway.
QUIZ_MIN_POOL = QUIZ_CHOICES

# Scoring lives here rather than in the template's JS so it's tunable in one
# place and assertable in a test: a right answer is worth QUIZ_POINTS_CORRECT,
# plus a bonus that starts at QUIZ_SPEED_BONUS and decays to nothing over
# QUIZ_SPEED_WINDOW seconds of thinking.
QUIZ_POINTS_CORRECT = 100
QUIZ_SPEED_BONUS = 50
QUIZ_SPEED_WINDOW = 10


def _quiz_product_pool():
    """Active, photographed finished products — at most one per recipe.

    The one-per-recipe rule is the same guarantee the matching game makes, for
    the same reason: an infinity and a rectangle from one dye bath photograph
    near-identically, so putting both names under one photo makes the question
    unanswerable rather than hard. Because every option in the quiz — the answer
    and all its distractors — is drawn from this one list, dedupe here fixes it
    everywhere.
    """
    products = (
        FinishedProduct.objects.dyed()
        .filter(Q(images__image__gt="") | Q(images__image_url__gt=""))
        .distinct()
        .select_related("recipe")
        .prefetch_related(
            "images",
            Prefetch(
                "recipe__recipe_dyes",
                queryset=RecipeDye.objects.select_related("dye").order_by("order", "id"),
            ),
        )
    )

    seen = set()
    pool = []
    for product in products:
        if product.recipe_id in seen:
            continue
        seen.add(product.recipe_id)
        pool.append(product)
    return pool


def _product_images(product):
    """Every usable image on a product."""
    return [img for img in product.images.all() if img.image or img.image_url]


def _deal_quiz(questions, pool=None, rng=None, family=False, choices=QUIZ_CHOICES):
    """Deal `questions` multiple-choice questions: a photo and `choices` names.

    `family=True` draws the distractors from the answer's nearest color
    neighbours instead of at random — a far harder drill, and the whole point of
    the exercise. It's opt-in for the same reason it is on the matching game:
    it leans on dye-swatch hexes as a proxy for the photographed color, so it's
    only as good as the recipe/dye data behind it.
    """
    rng = rng or random
    pool = _quiz_product_pool() if pool is None else pool

    asked = rng.sample(pool, min(questions, len(pool)))

    out = []
    for product in asked:
        images = _product_images(product)
        if not images:
            continue

        others = [p for p in pool if p.pk != product.pk]
        wanted = min(choices - 1, len(others))
        if family:
            distractors = nearest_by_color(
                others, product.recipe, wanted,
                recipe_of=lambda p: p.recipe, rng=rng,
            )
        else:
            distractors = rng.sample(others, wanted)

        options = [{"name": p.name, "correct": False} for p in distractors]
        options.append({"name": product.name, "correct": True})
        rng.shuffle(options)

        out.append({
            "image_url": rng.choice(images).url,
            "answer": product.name,
            "recipe_name": product.recipe.name,
            "options": options,
            "dyes": [
                {"name": rd.dye.name, "hex_color": rd.dye.hex_color}
                for rd in product.recipe.recipe_dyes.all()
            ],
        })

    return out


@require_http_methods(["GET", "OPTIONS"])
def quiz_board(request):
    """The quiz itself, as an embeddable fragment. Anonymous; no CSRF (GET only)."""
    if request.method == "OPTIONS":
        return _cors_headers(HttpResponse(status=204))

    try:
        requested = int(request.GET.get("questions", QUIZ_DEFAULT_QUESTIONS))
    except (TypeError, ValueError):
        requested = QUIZ_DEFAULT_QUESTIONS
    if requested not in QUIZ_LENGTHS:
        requested = QUIZ_DEFAULT_QUESTIONS

    family = request.GET.get("family") in ("1", "true", "yes")

    pool = _quiz_product_pool()
    questions = _deal_quiz(requested, pool=pool, family=family)

    # Absolute, for the same reason as the matching board: this fragment renders
    # into pages on other origins, where a relative path resolves against the
    # host site and 404s. Testing only on the Django page would never catch it.
    for question in questions:
        url = question["image_url"]
        if url and not url.startswith(("http://", "https://", "//")):
            question["image_url"] = request.build_absolute_uri(url)

    response = render(request, "scarves/partials/quiz_board.html", {
        "questions": questions,
        "asked": len(questions),
        "requested_questions": requested,
        # Only offer lengths the catalog can actually fill.
        "lengths": [n for n in QUIZ_LENGTHS if n <= len(pool)],
        "board_url": request.build_absolute_uri(reverse("quiz_board")),
        "family_qs": "&family=1" if family else "",
        "too_few": len(pool) < QUIZ_MIN_POOL,
        "points_correct": QUIZ_POINTS_CORRECT,
        "speed_bonus": QUIZ_SPEED_BONUS,
        "speed_window": QUIZ_SPEED_WINDOW,
        # Scopes this instance's CSS and JS, so two embeds on one page don't
        # collide and a re-swap can't leave a stale listener behind.
        "instance_id": uuid.uuid4().hex[:8],
    })
    return _cors_headers(response)


@page_meta(
    title="Name That Scarf",
    description="Public multiple-choice quiz: a scarf photo and four names, "
                "ten times over. Scored on how many you get right and how fast "
                "you answer.",
    category="Public",
    note="No login required; embeddable on other sites.",
)
def quiz_page(request):
    """Thin public shell, same as the matching game's."""
    return render(request, "scarves/quiz.html", {
        "board_url": reverse("quiz_board"),
        "default_questions": QUIZ_DEFAULT_QUESTIONS,
        "game_url": reverse("game_page"),
        "embed_origin": request.build_absolute_uri("/").rstrip("/"),
    })
