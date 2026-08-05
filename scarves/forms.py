# scarves/forms.py
from django import forms
from django.db import transaction

from .models import Dye, Recipe, RecipeDye, RawProduct  # RecipeDye is your through model

class RecipeDyesForm(forms.Form):
    """Edit just the dye assignments of one existing recipe.

    Unlike QuickRecipeRowForm this never touches the recipe's name and never
    creates recipes — it is for filling in dyes on records that already exist.

    Deliberately offers *all* dyes, not just in_stock ones: this records what a
    recipe historically used, and a dye going out of stock must not make its
    recipes un-editable. (Every dye is in stock today, so this is a latent trap
    rather than a current bug.)
    """

    SLOTS = 5  # RecipeDye.order validates 1..5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = Dye.objects.select_related("brand").order_by("brand__name", "name")
        for i in range(1, self.SLOTS + 1):
            self.fields[f"dye{i}"] = forms.ModelChoiceField(
                queryset=queryset, required=False, label=f"Dye {i}"
            )

    def clean(self):
        cleaned = super().clean()
        chosen = [cleaned.get(f"dye{i}") for i in range(1, self.SLOTS + 1)]
        picked = [d.pk for d in chosen if d]
        if len(picked) != len(set(picked)):
            self.add_error(None, "Please don't select the same dye more than once.")
        return cleaned

    def selected_dyes(self):
        """The chosen dyes in slot order, gaps removed."""
        return [
            d
            for d in (self.cleaned_data.get(f"dye{i}") for i in range(1, self.SLOTS + 1))
            if d
        ]

    @transaction.atomic
    def save(self, recipe):
        """Replace the recipe's dyes with the selected ones.

        Replace rather than merge, matching QuickRecipeRowForm: it makes the
        form a straightforward picture of the final state, and lets a row be
        cleared by emptying every slot.
        """
        RecipeDye.objects.filter(recipe=recipe).delete()
        for order, dye in enumerate(self.selected_dyes(), start=1):
            RecipeDye.objects.create(recipe=recipe, dye=dye, order=order)
        return recipe


class QuickRecipeRowForm(forms.Form):
    name = forms.CharField(max_length=150, required=False)  # allow blank rows
    dye1 = forms.ModelChoiceField(queryset=Dye.objects.filter(in_stock=True), required=False)
    dye2 = forms.ModelChoiceField(queryset=Dye.objects.filter(in_stock=True), required=False)
    dye3 = forms.ModelChoiceField(queryset=Dye.objects.filter(in_stock=True), required=False)
    dye4 = forms.ModelChoiceField(queryset=Dye.objects.filter(in_stock=True), required=False)

    def clean(self):
        cleaned = super().clean()
        name = (cleaned.get("name") or "").strip()
        dyes = [cleaned.get("dye1"), cleaned.get("dye2"), cleaned.get("dye3"), cleaned.get("dye4")]

        # If totally empty row: OK (skip)
        if not name and not any(dyes):
            cleaned["_skip"] = True
            return cleaned

        # If partially filled: require name
        if not name:
            self.add_error("name", "Please enter a recipe name for this row.")
            return cleaned

        # Optional: prevent duplicate dyes in the same recipe row
        chosen = [d.pk for d in dyes if d]
        if len(chosen) != len(set(chosen)):
            self.add_error(None, "Please don’t select the same dye more than once in a recipe.")
        return cleaned

    @transaction.atomic
    def save(self):
        if self.cleaned_data.get("_skip"):
            return None

        name = self.cleaned_data["name"].strip()
        recipe, _created = Recipe.objects.get_or_create(name=name, defaults={"is_active": True})

        # Replace existing dyes each save (predictable + fast entry)
        RecipeDye.objects.filter(recipe=recipe).delete()

        dyes = [self.cleaned_data.get("dye1"), self.cleaned_data.get("dye2"), self.cleaned_data.get("dye3"), self.cleaned_data.get("dye4")]
        order = 1
        for dye in dyes:
            if not dye:
                continue

            # --- IMPORTANT: adjust this block to match your RecipeDye fields ---
            # Variant A (common): RecipeDye has (recipe, dye, order) only
            RecipeDye.objects.create(recipe=recipe, dye=dye, order=order)

            # Variant B (also common): RecipeDye requires ratio/parts (e.g. parts=IntegerField)
            # RecipeDye.objects.create(recipe=recipe, dye=dye, order=order, parts=1)

            order += 1

        return recipe



