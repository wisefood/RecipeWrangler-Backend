"""The allergen vocabulary every caller is folded to.

A member's profile said "dairy", "nuts" or "shellfish"; the graph's
``Allergen.name`` says ``milk``, ``tree_nut`` and ``crustacean_shellfish``; the
Cypher compared them with ``IN`` and excluded nothing. These tests pin the
canonical identifiers to the EU's fourteen declaration groups and prove that
the request models, the parameter search, the Elasticsearch search and the
foodchat candidate filters all read the same spelling the same way.
"""

import pytest

from recipe_wrangler.catalog.foodchat import ALLERGEN_INGREDIENT_TERMS, _allergen_exclusions
from recipe_wrangler.schemas.models import (
    RecipeCreateRequest,
    RecipeSearchFilters,
    RecipeSearchRequest,
    RecipeUpdateRequest,
)
from recipe_wrangler.tools.param_search import _build_where_clause
from recipe_wrangler.utils.food_ontology import (
    ALLERGEN_ALIASES,
    ALLERGEN_DETECTION_RULES,
    ALLERGEN_NAME_TERMS,
    ALLERGEN_ONTOLOGY_MAPPINGS,
    CANONICAL_ALLERGEN_IDS,
    EU_ALLERGEN_GROUPS,
    allergen_name_terms,
    canonical_allergen_ids,
    expand_allergen_exclusions,
    normalize_allergen_names,
)


class TestTheVocabularyIsTheEUList:
    def test_fourteen_groups_in_annex_order(self):
        assert EU_ALLERGEN_GROUPS == (
            "gluten", "crustacean_shellfish", "egg", "fish", "peanut", "soy",
            "milk", "tree_nut", "celery", "mustard", "sesame", "sulphites",
            "lupin", "molluscs",
        )

    def test_every_canonical_id_is_tagged_and_mapped(self):
        """A group the tagger never writes could be excluded but never matched."""
        assert set(ALLERGEN_DETECTION_RULES) == set(CANONICAL_ALLERGEN_IDS)
        assert set(ALLERGEN_ONTOLOGY_MAPPINGS) == set(CANONICAL_ALLERGEN_IDS)
        assert set(ALLERGEN_NAME_TERMS) == set(CANONICAL_ALLERGEN_IDS)

    def test_aliases_only_point_at_canonical_ids(self):
        for alias, targets in ALLERGEN_ALIASES.items():
            assert alias == alias.casefold().strip(), alias
            assert set(targets) <= set(CANONICAL_ALLERGEN_IDS), alias

    def test_each_eu_group_has_a_term_the_graph_can_see(self):
        for cid in EU_ALLERGEN_GROUPS:
            assert ALLERGEN_NAME_TERMS[cid], cid


class TestNormalization:
    @pytest.mark.parametrize("sent, expected", [
        ("milk", ("milk",)),
        ("Dairy", ("milk",)),
        ("lactose", ("milk",)),
        ("eggs", ("egg",)),
        ("Egg", ("egg",)),
        ("nuts", ("tree_nut",)),
        ("Tree Nuts", ("tree_nut",)),
        ("tree_nut", ("tree_nut",)),
        ("almonds", ("tree_nut",)),
        ("peanuts", ("peanut",)),
        ("Crustacean-Shellfish", ("crustacean_shellfish",)),
        ("prawns", ("crustacean_shellfish",)),
        ("shellfish", ("crustacean_shellfish", "molluscs")),
        ("mollusks", ("molluscs",)),
        ("mussels", ("molluscs",)),
        ("sulfites", ("sulphites",)),
        ("Sulphur dioxide", ("sulphites",)),
        ("soya", ("soy",)),
        ("cereals containing gluten", ("gluten",)),
        ("rye", ("gluten",)),
        ("celeriac", ("celery",)),
        ("sesame seeds", ("sesame",)),
        ("lupini beans", ("lupin",)),
        ("wheat", ("wheat",)),
    ])
    def test_the_spellings_callers_send(self, sent, expected):
        assert canonical_allergen_ids(sent) == expected

    def test_an_unknown_allergy_is_kept_not_dropped(self):
        """"kiwi" is a real allergy: it must reach the name backstops as itself."""
        assert canonical_allergen_ids("kiwi") is None
        assert normalize_allergen_names(["  Kiwi Fruit "]) == ["kiwi fruit"]

    def test_dedupes_after_mapping_and_keeps_order(self):
        assert normalize_allergen_names(["Dairy", "milk", "eggs", "", None, "egg"]) == ["milk", "egg"]

    def test_gluten_implies_wheat_on_exclusion_only(self):
        assert normalize_allergen_names(["gluten"]) == ["gluten"]
        assert expand_allergen_exclusions(["gluten"]) == ["gluten", "wheat"]

    def test_name_terms_expand_canonical_ids_and_pass_unknowns(self):
        terms = allergen_name_terms(["crustacean_shellfish", "kiwi"])
        assert "prawn" in terms and "crab" in terms and "kiwi" in terms
        assert "tree nut" in allergen_name_terms(["nuts"])


class TestRequestModelsFoldOnTheWayIn:
    def test_natural_language_search(self):
        req = RecipeSearchRequest(exclude_allergens=["Dairy", "eggs", "nuts"])
        assert req.exclude_allergens == ["milk", "egg", "tree_nut"]

    def test_parameter_search(self):
        assert RecipeSearchFilters(exclude_allergens=["shellfish"]).exclude_allergens == [
            "crustacean_shellfish", "molluscs",
        ]

    def test_create_and_update_do_not_mint_stray_allergen_nodes(self):
        assert RecipeCreateRequest(
            title="t", ingredients=["1 cup flour"], duration=10, serves=2,
            allergens=["Dairy", "Peanuts"],
        ).allergens == ["milk", "peanut"]
        assert RecipeUpdateRequest(allergens=["tree nuts"]).allergens == ["tree_nut"]
        assert RecipeUpdateRequest(title="x").allergens is None


class TestTheMatchingPathsAgree:
    def test_cypher_compares_canonical_ids(self):
        _, params = _build_where_clause(RecipeSearchFilters(exclude_allergens=["gluten", "dairy"]))
        assert params["exclude_allergens"] == ["gluten", "wheat", "milk"]

    def test_elastic_candidates_exclude_by_canonical_term_and_by_name(self):
        flat = _flatten(_allergen_exclusions("Tree Nuts"))
        assert {"allergens": "tree_nut"} in [c.get("term") for c in _allergen_exclusions("Tree Nuts") if "term" in c]
        assert "*almond*" in flat and "*cashew*" in flat

    def test_a_canonical_id_the_table_never_keyed_now_reaches_ingredients(self):
        """`crustacean_shellfish` used to fall back to a bare wildcard that no
        prawn, crab or lobster row contains."""
        flat = _flatten(_allergen_exclusions("crustacean_shellfish"))
        assert "*prawn*" in flat and "*lobster*" in flat
        flat = _flatten(_allergen_exclusions("molluscs"))
        assert "*mussel*" in flat and "*squid*" in flat

    def test_the_catalog_table_is_the_shared_one(self):
        assert ALLERGEN_INGREDIENT_TERMS is ALLERGEN_NAME_TERMS


def _flatten(value, out=None):
    out = [] if out is None else out
    if isinstance(value, dict):
        for v in value.values():
            _flatten(v, out)
    elif isinstance(value, list):
        for v in value:
            _flatten(v, out)
    else:
        out.append(value)
    return out
