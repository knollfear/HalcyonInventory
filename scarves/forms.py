# scarves/forms.py
from django import forms
from django.db import transaction

from .models import Dye, Recipe, RecipeDye, RawProduct  # RecipeDye is your through model

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



