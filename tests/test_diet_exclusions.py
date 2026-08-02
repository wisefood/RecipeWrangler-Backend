"""Diet exclusions, and the plural that walked through them.

The filter matched `egg` as a phrase against `ingredients.name` and `title`.
Both fields use the `default_text` analyzer, which does not stem, so `eggs` was
a different word to it. The result, on a request that said vegan:

    breakfast -> Poached eggs
                 Mexican eggs in the pan

131 recipes carried the plural and were never excluded. For several fragments
the singular was the *minority* spelling: `meatballs` (67 in the corpus)
outnumbers `meatball` (30), and `rashers` (8) outnumbers `rasher` (3).
"""

from __future__ import annotations

import json

import pytest

from recipe_wrangler.catalog.diets import (
    canonical_diet,
    contradiction,
    exclusion_filters,
    token_variants,
)


def _phrases(clauses) -> set[str]:
    """Every `match_phrase` value anywhere in a clause tree."""
    out: set[str] = set()
    if isinstance(clauses, dict):
        for key, value in clauses.items():
            if key == "match_phrase" and isinstance(value, dict):
                out |= {str(v) for v in value.values()}
            else:
                out |= _phrases(value)
    elif isinstance(clauses, list):
        for item in clauses:
            out |= _phrases(item)
    return out


class TestTokenVariants:
    @pytest.mark.parametrize(
        "fragment,plural",
        [
            ("egg", "eggs"),
            ("steak", "steaks"),
            ("prawn", "prawns"),
            ("anchovy", "anchovies"),            # y -> ies
            ("rasher", "rashers"),
            ("meatball", "meatballs"),
            ("burger patty", "burger patties"),  # only the last word inflects
        ],
    )
    def test_the_plural_is_generated(self, fragment, plural):
        assert token_variants(fragment) == (fragment, plural)

    @pytest.mark.parametrize("junk", ["", "   ", None])
    def test_nothing_in_nothing_out(self, junk):
        assert token_variants(junk) == ()


class TestExclusionFilters:
    def test_vegan_excludes_both_egg_and_eggs(self):
        """The reported bug: "Poached eggs" served to a vegan."""
        phrases = _phrases(exclusion_filters(["vegan"]))

        assert "egg" in phrases
        assert "eggs" in phrases, "the plural is what let Poached eggs through"

    def test_every_fragment_carries_its_plural(self):
        phrases = _phrases(exclusion_filters(["vegan"]))

        for singular, plural in (("prawn", "prawns"), ("steak", "steaks"),
                                 ("anchovy", "anchovies"), ("meatball", "meatballs")):
            assert singular in phrases and plural in phrases

    def test_both_the_ingredient_list_and_the_title_are_read(self):
        """"Bacon and sweetcorn baked potato" lists its bacon as "rashers"."""
        must_not = exclusion_filters(["vegan"])[0]["bool"]["must_not"]

        assert any("nested" in c for c in must_not), "ingredients"
        assert any("match_phrase" in c and "title" in c["match_phrase"] for c in must_not)

    def test_phrase_matching_not_substring(self):
        """`*egg*` excluded all 176 eggplant recipes; `*chop*` matched 227."""
        blob = json.dumps(exclusion_filters(["vegan"]))

        assert "wildcard" not in blob
        assert "match_phrase" in blob

    def test_one_clause_per_diet(self):
        assert len(exclusion_filters(["vegan", "gluten_free"])) == 2

    def test_an_unverifiable_diet_contributes_nothing(self):
        """`low_carb` is a real tag with no ingredient contradiction. Excluding
        on it would be worse than not verifying it."""
        assert exclusion_filters(["low_carb"]) == []
        assert canonical_diet("low_carb") is None

    def test_no_diet_no_filter(self):
        assert exclusion_filters([]) == []


class TestContradiction:
    def test_the_client_side_check_catches_the_plural_too(self):
        """Substring matching already covered plurals — this pins that it stays."""
        assert contradiction("Poached eggs", ["vegan"]) is not None
        assert contradiction("Mexican eggs in the pan", ["vegan"]) is not None

    def test_a_compliant_dish_passes(self):
        assert contradiction("Toasted muesli with berries", ["vegan"]) is None

    def test_the_diet_and_the_offending_word_are_both_named(self):
        """The caller has to be able to say *why* something was dropped."""
        found = contradiction("Grilled sirloin steak", ["vegan"])

        assert found is not None
        diet, fragment = found
        assert diet == "vegan"
        assert fragment in ("steak", "sirloin")
