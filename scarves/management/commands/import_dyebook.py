import difflib

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from scarves.models import Dye, Recipe, RecipeDye


# The dye book, transcribed. One page of a notebook, and the only place the
# sales-floor name and the dye formula are written down together — the app has
# both halves as *separate* recipes ("Wasteland" with no dyes, `ecru-aub-avo`
# with some), and nothing joining them.
#
# Keyed on the recipe name as this database spells it, which is not always how
# the notebook spells it (Aegean/Agean, Blueberry/Bluebrry, Moony/Mooney). The
# notebook spelling is kept beside it so the page can be checked against this
# table by eye — that is the only way anyone will ever audit a transcription.
#
# The tokens are the notebook's own shorthand, left as written. Resolving them
# to dyes happens below and is allowed to fail; see why there rather than
# quietly picking one.
NOTEBOOK = [
    # (recipe name in this database, name on the page, dye shorthand in order)
    ("Emerald City", "Emerald City", ["Sage", "Forest", "MidEm"]),
    ("Lost Woods", "Lost Woods", ["Pist", "Sage", "L. Avoc"]),
    ("Dionysus", "Dionysus", ["AmNavy", "Forest", "Brown"]),
    ("Summer Shoals", "Summer Shoals", ["Champ", "Slate", "Avo"]),
    ("Coffee and Roses", "Coffee & Roses", ["Ecru", "L. Mauve", "Light Brown"]),
    ("Wasteland", "Wasteland", ["Ecru", "Aub", "Avo"]),
    ("Sunflower Fields", "Sunflower Fields", ["Ochre", "Oran", "Brown"]),
    ("Autumn Leaves", "Autumn Leaves", ["Saffr", "Russet", "Brown"]),
    ("Orange Sherbert", "Orange Sherbert", ["Cham", "Salm", "Scar"]),
    ("Mufasa", "Mufasa", ["Ochre", "Oran", "Russet"]),
    ("Drucilla", "Druscilla", ["Rus", "Cab", "Black"]),
    ('The "L" Word', 'Other "L" Word', ["Pink", "Saffr", "Orch", "Cab"]),
    ("Mermaid Queen", "Mermaid Queen", ["Chart", "Turq", "Electric Violet"]),
    ("Agean Sea", "Aegean Sea", ["Grey", "Lilac", "Electric Violet"]),
    ("Bluebrry", "Blueberry", ["Turq", "Electric Violet"]),
    ("Dragon Heart", "Dragonheart", ["Grey", "Am", "J Purple"]),
    ("Night Sky", "Night Sky", ["Turq", "Mid", "Black"]),
    ("Lavendar Haze", "Lavender Haze", ["Lilac", "Electric Violet", "J Purple"]),
    ("Clarke", "Clarke", ["Orch", "Electric Violet", "Turq"]),
    ("Blue Eyes", "Blue Eyes", ["Blue Eyes", "Mid"]),
    ("Mooney", "Moony", ["Slate", "AmNav", "Forest"]),
    ("Princess B", "Princess Buttercup", ["Ecru", "Vanilla", "Champ", "Salm"]),
    ("Furiosa", "Furiosa", ["Gray", "Gun", "Brown"]),
    ("Valentine", "Valentine", ["Ballet", "Orch", "Verm"]),
    ("Forest Fire", "Forest Fire", ["Russ", "Saffr", "Forest"]),
    ("Sea Smoke", "Sea Smoke", ["Grey", "Gun", "Black"]),
    ("Belladonna", "Belladonna", ["Em", "Teal", "Saph"]),
    ("Peacock", "Peacock", ["Teal", "Saph", "Electric Violet"]),
    ("Twilight Forest", "Twilight Forest", ["Grey", "Forest", "Navy"]),
]

# Shorthand that a search can't resolve on its own, settled by hand.
#
# Everything here is a case where the written form is not a prefix of the dye's
# name — an abbreviation that drops letters from the middle, or a word the
# catalogue spells differently. The value is the dye's full name including its
# catalogue number, so this table can be read against the jars.
#
# What is deliberately *not* here is the interesting part: a token whose
# shorthand matches two dyes equally well gets no entry, and the recipes using
# it are skipped and named. Nearly all of those were the same question asked
# repeatedly — Dharma and Jacquard both sell a Turquoise, a Sapphire, an
# Emerald, a Teal, a Lilac, a Chartreuse, a Navy and a Black, and the page
# never says which jar. Guessing costs a wrong hex, which reaches the rainbow
# sheets as a band the scarf was never dyed in, and the symptom is a customer
# looking under red for something filed in orange. Answer the question once
# here and every recipe that was waiting on it goes in on the next run.
#
# Note where those answers came from: the shelf. Nothing in the database can
# settle a brand — both catalogues are imported whole and every one of the 132
# rows is flagged in stock, which is the importer's default and not a count
# anybody took. The formula-named recipes look like evidence and are not; two
# of them agreed with the answers below and two contradicted them, which is
# what a coin does.
#
# A value may name *two* jars. Almost every token is one word for one jar, and
# the dyes in a colourway normally stay distinct — the scarf flows between
# them, which is why nothing here ever averages a recipe to one colour. Two
# tokens genuinely break that: `MidEm` and `AmNavy` are baths where the two
# dyes are mixed before they touch the cloth. Recording both is the honest
# option; picking one would drop the other with nothing to say so.
ALIASES = {
    "Sage": "450 Sage Leaf",
    "Forest": "452 Forest Green",
    "Pist": "434 Pistachio",
    "Avo": "Avocado",
    # The notebook's "L." reads as *light*, and neither catalogue sells a
    # light version of either of these — there is one avocado and one mauve
    # on file. So the prefix is a description of the bath, not of the jar.
    "L. Avoc": "Avocado",
    "L. Mauve": "432 Antique Mauve",
    "Ecru": "600 Ecru",
    "Aub": "475 Aubergine",
    "Saffr": "460 Saffron Spice",
    "Russet": "616 Russet",
    "Russ": "616 Russet",
    "Rus": "616 Russet",
    "Brown": "635 Brown",
    "Champ": "486 Champagne",
    "Cham": "486 Champagne",
    "Cab": "458 Cabernet",
    "Ochre": "636 Golden Ochre",
    "Salm": "607 Salmon",
    "Scar": "609 Bright Scarlet",
    "Ballet": "481 Ballerina Pink",
    "Orch": "412 Pink Orchid",
    "Electric Violet": "444 Electric Violet",
    "Gun": "637 Gun Metal",
    "Vanilla": "449 Vanilla Cream",
    "Verm": "611 Vermilion",
    "Am": "425 Amethyst",
    # Corroborated by the one formula row whose links are plainly right:
    # `turq-mid-black` holds 415 Midnight Blue and 413 True Black.
    "Mid": "415 Midnight Blue",
    "Blue Eyes": "466 Baby Blue Eyes",

    # Which brand's jar, answered off the shelf. Dharma numbers 4xx, Jacquard
    # 6xx, so the number in each value is also the answer to the question.
    "Black": "413 True Black (Primary)*",
    "Navy": "409 Dark Navy*",
    "Turq": "624 Turquoise (Primary)",
    "Saph": "622 Sapphire Blue",
    "Lilac": "612 Lilac",
    "Em": "629 Emerald",
    # Same "light touch" as the two above: the obvious jar, used sparingly.
    # The formula row for this colourway is named `ecru-Lmauve-brown`, so
    # whoever named it read the page the same way.
    "Light Brown": "635 Brown",

    # The two mixed baths. See the note above the table for why these are a
    # pair rather than a pick.
    "MidEm": ("415 Midnight Blue", "629 Emerald"),
    "AmNavy": ("425 Amethyst", "409 Dark Navy*"),
    "AmNav": ("425 Amethyst", "409 Dark Navy*"),
}


class Command(BaseCommand):
    """Put the dye book's formulas onto the colourways they belong to.

    The recipes that carry products are named for the sales floor — Wasteland,
    Peacock, Blue Eyes — and have no dyes. The rows that have dyes are named
    for the formula — `ecru-aub-avo` — and carry nothing. `ecru-aub-avo` is no
    use at a stall and "Wasteland" is no use at the shelf; the notebook is the
    only record that says they are the same colourway.

    So this reads the page, not the formula rows. Those rows were typed by a
    person and are a mix of right and wrong (`turq-mid-black` is correct;
    `sage-forest-emmid` holds two blues and a kelly green), and they are
    incomplete besides — a third of them list one dye where the page names
    three. Copying them would spread that around under names people trust.
    """

    help = "Apply the transcribed dye book to the colourway recipes it names."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the whole plan and change nothing.",
        )

    @staticmethod
    def _stem(name):
        """The dye's name without its catalogue number.

        Same reasoning as `Dye.sort_name`: Dharma and Jacquard number their
        jars, so `616 Russet` has to answer to `Russet`.
        """
        head, _, rest = name.partition(" ")
        return rest if head.isdigit() else name

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        dyes_on_file = list(Dye.objects.all())
        by_name = {dye.name.casefold(): dye for dye in dyes_on_file}

        # Stems map to a *list*, never a single dye. Both brands sell a
        # Turquoise and a Sapphire, and a dict keyed on the stem would keep
        # whichever row came back last — turning the ambiguity this command
        # exists to surface into a silent arbitrary pick.
        stems = {}
        for dye in dyes_on_file:
            stems.setdefault(self._stem(dye.name.casefold()), []).append(dye)

        ready, blocked, missing_recipe, conflicts = [], [], [], []
        blocking = {}
        self.alias_gaps = {}

        for recipe_name, page_name, tokens in NOTEBOOK:
            recipe = Recipe.objects.filter(name=recipe_name).first()
            if recipe is None:
                missing_recipe.append((recipe_name, page_name))
                continue

            dyes, unresolved = [], []
            for token in tokens:
                found, candidates = self._resolve_with(token, by_name, stems)
                if found is None:
                    unresolved.append((token, candidates))
                else:
                    dyes.extend(found)

            if unresolved:
                blocked.append((recipe, page_name, unresolved))
                for token, candidates in unresolved:
                    blocking.setdefault(token, [set(), []])
                    blocking[token][0].add(recipe.name)
                    blocking[token][1] = candidates
                continue

            existing = list(recipe.dyes.all())
            extra = [d for d in existing if d not in dyes]
            if extra:
                # The page is canon, but a dye already on file that the page
                # doesn't mention was put there by somebody and might be the
                # correction. Same bargain `import_dyes` makes with a colour
                # that disagrees with the catalogue: name it, change nothing.
                conflicts.append((recipe, page_name, extra))
                continue

            ready.append((recipe, page_name, dyes, len(existing)))

        self._report(ready, blocked, blocking, conflicts, missing_recipe,
                     dry_run, dyes_on_file)

        if dry_run or not ready:
            return

        with transaction.atomic():
            for recipe, _page_name, dyes, _had in ready:
                recipe.recipe_dyes.all().delete()
                for order, dye in enumerate(dyes, start=1):
                    RecipeDye.objects.create(recipe=recipe, dye=dye, order=order)

        self.stdout.write(self.style.SUCCESS(
            f"Wrote {len(ready)} recipe(s) from the dye book."
        ))

    def _resolve_with(self, token, by_name, stems):
        """The jar or jars this word means, or the candidates it is stuck on.

        Returns a *list* even for the ordinary one-word-one-jar case, because
        two tokens name a mixed bath and the caller must not have to know
        which kind it is holding.
        """
        alias = ALIASES.get(token)
        if alias:
            names = [alias] if isinstance(alias, str) else list(alias)
            dyes = []
            for name in names:
                dye = by_name.get(name.casefold())
                if dye is None:
                    # Two causes, and the command can't tell them apart: a
                    # typo in the table, or a catalogue this database never
                    # imported. Neither is worth dying over — both are worth
                    # *saying*, in a section of their own, because a typo
                    # mixed in with the shorthand list reads as one more thing
                    # to look up later.
                    self.alias_gaps[token] = name
                    return None, []
                dyes.append(dye)
            return dyes, None

        wanted = token.casefold()
        exact = by_name.get(wanted)
        if exact is not None:
            return [exact], None
        matches = stems.get(wanted, [])
        if len(matches) == 1:
            return matches, None
        return None, matches

    def _suggest(self, token, dyes_on_file):
        """Jars a blocked word might mean — for the report, never for a write.

        Resolution stays strict: an exact stem or an explicit alias, nothing
        else. Suggesting is free precisely because it decides nothing, and
        without it the report's most useful line is its most misleading one —
        `Grey` blocks four recipes, and a substring search calls it unheard-of
        because the catalogue spells the colour `Gray`.

        So the comparison is fuzzy and per word: `Saph` finds both Sapphire
        Blues, `L. Mauve` finds Antique Mauve. A little tail noise is the
        price and it sorts to the bottom, where "could be" already says what
        the list is worth.
        """
        wanted = token.casefold().replace(".", "").strip()
        scored = []
        for dye in dyes_on_file:
            stem = self._stem(dye.name.casefold())
            words = stem.replace("(", " ").replace(")", " ").split()
            best = max(
                [difflib.SequenceMatcher(None, wanted, w).ratio() for w in words]
                + [difflib.SequenceMatcher(None, wanted, stem).ratio()]
            )
            if any(w.startswith(wanted) for w in words):
                best = max(best, 0.9)
            if best >= 0.6:
                scored.append((best, dye.name, dye))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [dye for _score, _name, dye in scored[:4]]

    def _report(self, ready, blocked, blocking, conflicts, missing_recipe,
                dry_run, dyes_on_file):
        header = "DRY RUN — would write:" if dry_run else "Writing:"
        self.stdout.write(self.style.WARNING(header) if dry_run else header)
        for recipe, page_name, dyes, had in ready:
            note = f" (replacing {had})" if had else ""
            self.stdout.write(
                f"  {recipe.name:<20} [{page_name}]{note}: "
                + ", ".join(d.name for d in dyes)
            )
        if not ready:
            self.stdout.write("  nothing")

        if conflicts:
            self.stdout.write(self.style.WARNING(
                f"\n{len(conflicts)} recipe(s) hold a dye the page doesn't "
                f"name — left alone:"
            ))
            for recipe, page_name, extra in conflicts:
                self.stdout.write(
                    f"  {recipe.name} [{page_name}]: has "
                    + ", ".join(d.name for d in extra)
                )

        if missing_recipe:
            self.stdout.write(self.style.WARNING(
                f"\n{len(missing_recipe)} name(s) on the page match no recipe:"
            ))
            for recipe_name, page_name in missing_recipe:
                self.stdout.write(f"  {recipe_name} [{page_name}]")

        if self.alias_gaps:
            self.stdout.write(self.style.WARNING(
                f"\n{len(self.alias_gaps)} shorthand(s) point at a dye this "
                f"database doesn't have — a typo in ALIASES, or a catalogue "
                f"nobody imported:"
            ))
            for token, alias in sorted(self.alias_gaps.items()):
                self.stdout.write(f"  {token!r} -> {alias!r}")

        if blocked:
            self.stdout.write(self.style.WARNING(
                f"\n{len(blocked)} recipe(s) blocked on shorthand nobody has "
                f"settled. Grouped by the word, because one answer usually "
                f"unblocks several:"
            ))
            for token, (recipes, candidates) in sorted(
                blocking.items(), key=lambda kv: (-len(kv[1][0]), kv[0])
            ):
                if candidates:
                    detail = "matches " + ", ".join(d.name for d in candidates)
                else:
                    guesses = self._suggest(token, dyes_on_file)
                    detail = (
                        "could be " + ", ".join(d.name for d in guesses)
                        if guesses else "nothing on file resembles it"
                    )
                self.stdout.write(
                    f"  {token!r} — {detail}\n"
                    f"      blocks {len(recipes)}: {', '.join(sorted(recipes))}"
                )
