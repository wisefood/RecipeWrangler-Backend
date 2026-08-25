"""Declarative Elasticsearch index definitions.

This module is authoritative for index structure; ``docs/specs/recipes_v3_mapping.json``
is a generated artefact of it (regenerate with
``python scripts/catalog/dump_mappings.py``, and ``tests/test_catalog_es_schema.py``
fails if the two drift).

Two indices, both fed from the owners named in the elastic-backbone spec §3.1:

``recipes``
    One document per recipe. Carries content, graph-derived classification,
    governance, evidence, annotations, and a denormalized profile summary so
    filtering and sorting never need a second store.

``recipe_profiles``
    One document per ``(recipe_id, nutrition_source)`` — exactly the grain of
    the Postgres primary key, so the migration is 1:1.

Design notes that differ from a naive port of the v2 index:

- ``instructions`` and ``description`` are indexed. ``recipes_v2`` had neither,
  so full-text search only ever saw ``title``, ``ingredients`` and ``tags``.
- Per-region scores exist both as a ``profiles`` nested array (add a region
  without a mapping change) and as flat ``nutri_*_<region>`` fields (cheap
  sorting and the v2 compatibility surface). ``default_nutri_score`` is the one
  a list and a detail page must agree on.
- Human-authoritative classification and its AI-derived counterpart are always
  separate fields. A model-guessed allergen or cuisine must never be
  indistinguishable from a curated one.
- ``dynamic: strict`` — an unmapped field is a bug, not a silent new column.
"""

from __future__ import annotations

from typing import Any

DEFAULT_DIM = 384

DEFAULT_SETTINGS: dict[str, Any] = {
    "index": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
        "max_result_window": 100000,
        "refresh_interval": "5s",
    },
    "analysis": {
        "analyzer": {
            "default_text": {
                "tokenizer": "standard",
                "filter": ["lowercase", "asciifolding"],
            },
            # edge_ngram at index time, plain analysis at search time — the
            # correct asymmetry. Analysing the query with ngrams too would make
            # every prefix of the query match every prefix of the field.
            "autocomplete": {
                "tokenizer": "standard",
                "filter": ["lowercase", "asciifolding", "edge_ngram_filter"],
            },
        },
        "filter": {
            "edge_ngram_filter": {
                "type": "edge_ngram",
                "min_gram": 2,
                "max_gram": 20,
            }
        },
        "normalizer": {
            "lowercase_keyword": {
                "type": "custom",
                "filter": ["lowercase", "asciifolding"],
            }
        },
    },
}

_DATE = {"type": "date", "format": "strict_date_optional_time||epoch_millis"}

# Regions with a full score/colour/rank/points quartet. 4 supported nutrition
# sources (3 regional + 1 global) per 2026-08-21 decision — USDA ("us") dropped,
# Slovenian uses the full-word suffix (nutri_score_slovenian, not the old
# nutri_score_si) so there's exactly one Slovenian field, not two.
SCORE_REGIONS: tuple[str, ...] = ("eu", "ie", "hu", "slovenian")

# Regions that only ever carry a label and a colour.
LABEL_ONLY_REGIONS: tuple[str, ...] = ("planeat",)


def _embedding(dim: int) -> dict[str, Any]:
    return {
        "type": "dense_vector",
        "dims": dim,
        "index": True,
        "similarity": "cosine",
        "index_options": {"type": "int8_hnsw", "m": 16, "ef_construction": 100},
    }


def _regional_score_fields() -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for region in SCORE_REGIONS:
        fields[f"nutri_score_{region}"] = {"type": "keyword"}
        fields[f"nutri_color_{region}"] = {"type": "keyword"}
        fields[f"nutri_rank_{region}"] = {"type": "byte"}
        fields[f"nutri_points_{region}"] = {"type": "float"}
    for region in LABEL_ONLY_REGIONS:
        fields[f"nutri_score_{region}"] = {"type": "keyword"}
        fields[f"nutri_color_{region}"] = {"type": "keyword"}
    return fields


# --------------------------------------------------------------------------- #
# Evidence — mirrors utils/es_recipe_evidence.RECIPE_EVIDENCE_MAPPING_PROPERTIES
# --------------------------------------------------------------------------- #

def _evidence_fields() -> dict[str, Any]:
    return {
        "suitable_for": {"type": "keyword"},
        "allergen_evidence": {
            "type": "nested",
            "properties": {
                "allergen": {"type": "keyword"},
                "ingredient": {"type": "keyword"},
                "ingredient_id": {"type": "keyword"},
                "declaration_id": {"type": "keyword"},
                "presence": {"type": "keyword"},
                "evidence_status": {"type": "keyword"},
                "sources": {"type": "keyword"},
                "foodon_ids": {"type": "keyword"},
                "keyword_matches": {"type": "keyword"},
                "classification_version": {"type": "keyword"},
            },
        },
        "consumer_suitability": {
            "type": "nested",
            "properties": {
                "group": {"type": "keyword"},
                "status": {"type": "keyword"},
                "blocking_ingredients": {"type": "keyword"},
                "reason_codes": {"type": "keyword"},
                "sources": {"type": "keyword"},
                "classification_version": {"type": "keyword"},
            },
        },
    }


# --------------------------------------------------------------------------- #
# Discovery annotations
# --------------------------------------------------------------------------- #

def _annotation_fields() -> dict[str, Any]:
    """Facets the corpus needs for browsing but does not currently have.

    ``course_types`` supersedes v2's ``dish_types`` (kept alongside for one
    release). ``food_groups`` and ``flavor_profiles`` are derivable from data
    already in the graph — FoodOn class ancestry and FlavorDB compounds — so
    they carry ``method='foodon_derived'``/``'flavordb_derived'`` provenance
    rather than being guessed. ``cuisines`` and ``moods`` have no substrate in
    any store today and can only be model-assigned, which is why they have
    ``ai_*`` counterparts and why every value is accompanied by an
    ``annotation_evidence`` entry recording how it was arrived at.
    """
    return {
        # One field per facet, whatever produced the value.
        #
        # These previously had parallel `ai_*` twins so a model-assigned value
        # could be told from a curated one. That is redundant here:
        # `annotation_evidence` already records method, confidence and sources
        # for every individual value, and `enhancements[].before` retains what
        # was replaced. The twin field added nothing except a second name per
        # concept — the same duplication that let main-dish and main_dish drift
        # apart — and forced every consumer to coalesce two fields.
        #
        # It also protected an authority that does not exist: the stored course
        # types were scraped source tags, 79.8% of them "main-dish" including
        # cakes and puddings. There was no curation to defend.
        #
        # No `flavor_compounds` on purpose either. FlavorDB stores molecules
        # ("(+)-3-Carene"), which mean nothing to a diner and carry no sensory
        # mapping. Its value is the PAIRS_WITH graph for substitution, which is
        # ingredient-level data.
        "course_types": {"type": "keyword", "copy_to": "all_text"},
        "cuisines": {"type": "keyword", "copy_to": "all_text"},
        "food_groups": {"type": "keyword"},
        "flavor_profiles": {"type": "keyword"},
        "moods": {"type": "keyword"},
        "convenience": {"type": "keyword"},
        "annotation_evidence": {
            "type": "nested",
            "properties": {
                "facet": {"type": "keyword"},
                "value": {"type": "keyword"},
                "status": {"type": "keyword"},
                "method": {"type": "keyword"},
                "evidence_status": {"type": "keyword"},
                "confidence": {"type": "half_float"},
                "sources": {"type": "keyword"},
                "foodon_ids": {"type": "keyword"},
                "supporting_ingredients": {"type": "keyword"},
                "classification_version": {"type": "keyword"},
            },
        },
    }


def recipe_index(dim: int = DEFAULT_DIM) -> dict[str, Any]:
    """The ``recipes`` index definition."""
    properties: dict[str, Any] = {
        # identity & linkage
        "urn": {"type": "keyword"},
        "id": {"type": "keyword"},
        "recipe_id": {"type": "keyword"},
        "external_id": {"type": "keyword", "ignore_above": 256},
        "schema_version": {"type": "short"},
        "content_digest": {"type": "keyword", "doc_values": False},
        # core content
        "title": {
            "type": "text",
            "analyzer": "default_text",
            "search_analyzer": "default_text",
            "copy_to": "all_text",
            "fields": {
                "kw": {
                    "type": "keyword",
                    "ignore_above": 512,
                    "normalizer": "lowercase_keyword",
                },
                "exact": {"type": "keyword", "ignore_above": 512},
                "autocomplete": {
                    "type": "text",
                    "analyzer": "autocomplete",
                    "search_analyzer": "default_text",
                },
            },
        },
        "title_normalized": {"type": "keyword", "ignore_above": 512},
        "description": {
            "type": "text",
            "analyzer": "default_text",
            "search_analyzer": "default_text",
            "copy_to": "all_text",
        },
        # Indexed, but deliberately NOT copied into `all_text` and not among the
        # default search fields. Method text is dense with generic cooking nouns
        # ("add the pasta water", "reserve the oil"), so letting it feed general
        # retrieval manufactures false positives: a recipe that merely mentions
        # pasta is not a pasta recipe. It stays queryable for explicit opt-in
        # (an advanced "mentions sous vide" search) via `search_fields`.
        "instructions": {
            "type": "text",
            "analyzer": "default_text",
            "search_analyzer": "default_text",
        },
        "all_text": {
            "type": "text",
            "analyzer": "default_text",
            "search_analyzer": "default_text",
        },
        # provenance
        "source": {"type": "keyword"},
        "source_name": {"type": "keyword", "ignore_above": 128},
        "source_id": {"type": "keyword", "ignore_above": 256},
        "collection_urn": {"type": "keyword"},
        "source_rank": {"type": "short"},
        "language": {"type": "keyword"},
        "region": {"type": "keyword"},
        "url": {"type": "keyword", "index": False, "doc_values": False},
        "image_url": {"type": "keyword", "index": False, "doc_values": False},
        "has_image": {"type": "boolean"},
        # ingredients — nested so "1 tbsp of X" is queryable, plus a flattened
        # keyword copy so include/exclude stays a cheap terms filter. Exclusion
        # drives allergen safety, so it must not depend on a nested query.
        "ingredients": {
            "type": "nested",
            "properties": {
                "name": {
                    "type": "text",
                    "analyzer": "default_text",
                    "copy_to": "all_text",
                    "fields": {
                        "kw": {
                            "type": "keyword",
                            "ignore_above": 256,
                            "normalizer": "lowercase_keyword",
                        }
                    },
                },
                "quantity": {"type": "float"},
                "unit": {"type": "keyword"},
                "measurement": {"type": "text", "index": False},
                "canonical_urn": {"type": "keyword"},
                "position": {"type": "short"},
            },
        },
        "ingredient_names": {"type": "keyword"},
        "ingredient_count": {"type": "short"},
        "ingredient_class_ancestors": {"type": "keyword"},
        # classification (human-authoritative)
        "tags": {"type": "keyword", "copy_to": "all_text"},
        "diet_tags": {"type": "keyword"},
        "nutrition_claims": {"type": "keyword"},
        "seasonality": {"type": "keyword"},
        # No `dish_types`. It was the v2 name for the same thing as
        # `course_types` and carried byte-identical values; keeping both is what
        # let main-dish/main_dish drift apart across two writers. `course_types`
        # is also the more precise name — a *dish family* (pasta, curry) is a
        # separate facet, not a course.
        "allergens": {"type": "keyword"},
        "allergen_confidence": {"type": "half_float"},
        # Allergens keep their AI twin, and only allergens. Everywhere else the
        # provenance in `annotation_evidence` is sufficient; here it is not.
        # A model-guessed allergen rendered indistinguishably from a declared
        # one is a safety failure, not a taxonomy one — someone avoiding peanuts
        # must be able to see that "peanut-free" was inferred rather than
        # declared. `ai_generated_fields` lists which fields hold model output,
        # so a UI can badge them without a parallel field per facet.
        "ai_tags": {"type": "keyword"},
        "ai_allergens": {"type": "keyword"},
        "ai_generated_fields": {"type": "keyword"},
        "enhancements": {
            "type": "nested",
            "properties": {
                "agent": {"type": "keyword"},
                "run_id": {"type": "keyword"},
                "enhanced_at": _DATE,
                "fields": {"type": "keyword"},
                "before": {"type": "object", "enabled": False},
                "after": {"type": "object", "enabled": False},
            },
        },
        # preparation
        "duration": {"type": "float"},
        "serves": {"type": "float"},
        "cost_category": {"type": "keyword"},
        # denormalized profile summary (truth lives in recipe_profiles)
        "profiles": {
            "type": "nested",
            "properties": {
                "nutrition_source": {"type": "keyword"},
                "kind": {"type": "keyword"},
                "region": {"type": "keyword"},
                "composition_table": {"type": "keyword"},
                "is_ground_truth": {"type": "boolean"},
                "nutri_score": {"type": "keyword"},
                "nutri_rank": {"type": "byte"},
                "nutri_points": {"type": "float"},
                "nutri_color": {"type": "keyword"},
                "energy_kcal_per_serving": {"type": "float"},
                "protein_g_per_serving": {"type": "float"},
                "fat_g_per_serving": {"type": "float"},
                "sat_fat_g_per_serving": {"type": "float"},
                "carbs_g_per_serving": {"type": "float"},
                "sugar_g_per_serving": {"type": "float"},
                "fibre_g_per_serving": {"type": "float"},
                "salt_g_per_serving": {"type": "float"},
                "sodium_mg_per_serving": {"type": "float"},
                "serves": {"type": "float"},
                # Sustainability is computed per (recipe, nutrition_source)
                # exactly like the nutrition figures, so it belongs on the
                # profile rather than only in the document-level rollup.
                "sust_total": {"type": "float"},
                "sust_per_serving": {"type": "float"},
                "coverage": {"type": "half_float"},
                "low_coverage": {"type": "boolean"},
                "match_confidence": {"type": "half_float"},
                "pipeline_version": {"type": "keyword"},
                "computed_at": _DATE,
            },
        },
        "regions_available": {"type": "keyword"},
        "nutrition_sources": {"type": "keyword"},
        "profile_kinds": {"type": "keyword"},
        "best_nutri_rank": {"type": "byte"},
        # The single score a list view and a detail view must agree on.
        "default_nutri_score": {"type": "keyword"},
        "default_nutri_rank": {"type": "byte"},
        "sust_score": {"type": "float"},
        "sust_per_serving": {"type": "float"},
        "expert_recipe": {"type": "boolean"},
        "has_profile": {"type": "boolean"},
        "pipeline_version": {"type": "keyword"},
        "profiled_at": _DATE,
        "has_ground_truth_nutrition": {"type": "boolean"},
        "has_rcsi_nutrition": {"type": "boolean"},
        "has_planeat_nutrition": {"type": "boolean"},
        "has_slovenian_nutrition": {"type": "boolean"},
        "ground_truth_nutrition_source": {"type": "keyword"},
        # governance
        "status": {"type": "keyword"},
        "review_status": {"type": "keyword"},
        "visibility": {"type": "keyword"},
        "creator": {"type": "keyword"},
        # Eligibility for automated meal planning and agent surfaces.
        #
        # Deliberately separate from `status`. `status` answers "does this
        # recipe exist for anyone?"; this answers "may a planner serve it
        # unattended?". A recipe can be perfectly good to find by searching and
        # still be a poor thing to put in someone's week — incomplete nutrition,
        # an unresolved ingredient, a novelty item — and conflating the two
        # means the only way to keep it out of plans is to hide it entirely.
        #
        #   preferred — curated source, profiled, fully annotated: pick first
        #   standard  — eligible, unremarkable
        #   excluded  — never returned by planning/agent endpoints, still
        #               searchable and directly retrievable
        "planning_tier": {"type": "keyword"},
        "planning_excluded_reason": {"type": "keyword"},
        "disabled_at": _DATE,
        "disabled_reason": {"type": "keyword", "index": False, "doc_values": False},
        "created_at": _DATE,
        "updated_at": _DATE,
        # semantic
        "embedding": _embedding(dim),
        "embedding_model": {"type": "keyword"},
        # `text` has no doc_values parameter at all (the spec JSON carried one
        # and would have been rejected at index creation). Not indexing it is
        # enough: it is stored for re-embedding and never queried.
        "embedding_text": {"type": "text", "index": False},
        "embedded_at": _DATE,
        "extras": {"type": "object", "enabled": False},
    }
    properties.update(_regional_score_fields())
    properties.update(_evidence_fields())
    properties.update(_annotation_fields())

    return {
        "settings": DEFAULT_SETTINGS,
        "mappings": {"dynamic": "strict", "properties": properties},
    }


def recipe_profile_index(dim: int = DEFAULT_DIM) -> dict[str, Any]:
    """The ``recipe_profiles`` index — one doc per (recipe, nutrition_source).

    ``trace`` and the raw nutrient blobs are stored but not indexed: they are
    tens of KB per recipe and are never searched, only returned.
    ``total_nutrients_per_serving`` is nested key/value so "under 500 kcal with
    over 20g protein" becomes one query — impossible today without Postgres.
    """
    properties: dict[str, Any] = {
        "urn": {"type": "keyword"},
        "id": {"type": "keyword"},
        "recipe_urn": {"type": "keyword"},
        "recipe_id": {"type": "keyword"},
        "nutrition_source": {"type": "keyword"},
        "kind": {"type": "keyword"},
        "region": {"type": "keyword"},
        "composition_table": {"type": "keyword"},
        "is_ground_truth": {"type": "boolean"},
        "title": {"type": "text", "analyzer": "default_text"},
        "source": {"type": "keyword"},
        "collection_urn": {"type": "keyword"},
        "schema_version": {"type": "short"},
        "content_digest": {"type": "keyword", "doc_values": False},
        "total_nutrients": {"type": "object", "enabled": False},
        "total_nutrients_per_serving": {
            "type": "nested",
            "properties": {
                "nutrient": {"type": "keyword"},
                "value": {"type": "float"},
                "unit": {"type": "keyword"},
            },
        },
        "nutri_score": {
            "properties": {
                "label": {"type": "keyword"},
                "points": {"type": "float"},
                "color": {"type": "keyword"},
                "rank": {"type": "byte"},
            }
        },
        "nutri_score_breakdown": {"type": "object", "enabled": False},
        "sustainability": {
            "properties": {
                "total": {"type": "float"},
                "per_serving": {"type": "float"},
                "per_kg": {"type": "float"},
            }
        },
        "coverage": {
            "properties": {
                "nutrition": {"type": "half_float"},
                "sustainability": {"type": "half_float"},
                "matched_weight_g": {"type": "float"},
            }
        },
        "quality": {
            "properties": {
                "serves_source": {"type": "keyword"},
                "weights_capped": {"type": "boolean"},
                "low_coverage": {"type": "boolean"},
                "match_confidence": {"type": "half_float"},
            }
        },
        "nutrition_profiling_details": {
            "type": "nested",
            "properties": {
                "ingredient": {"type": "keyword"},
                "matched": {"type": "keyword"},
                "canonical_food_id": {"type": "keyword"},
                "weight_g": {"type": "float"},
                "distance": {"type": "half_float"},
                "energy_kcal": {"type": "float"},
                "protein_g": {"type": "float"},
                "fat_g": {"type": "float"},
                "sat_fat_g": {"type": "float"},
                "carbs_g": {"type": "float"},
                "sugar_g": {"type": "float"},
                "fibre_g": {"type": "float"},
                "sodium_mg": {"type": "float"},
            },
        },
        "sustainability_profiling_details": {"type": "object", "enabled": False},
        "nutrition_profiling_debug": {"type": "object", "enabled": False},
        "trace": {"type": "object", "enabled": False},
        "serves": {"type": "float"},
        "pipeline_version": {"type": "keyword"},
        "computed_at": _DATE,
        "created_at": _DATE,
        "updated_at": _DATE,
        "status": {"type": "keyword"},
        "extras": {"type": "object", "enabled": False},
    }
    return {
        "settings": DEFAULT_SETTINGS,
        "mappings": {"dynamic": "strict", "properties": properties},
    }


INDEX_DEFINITIONS = {
    "recipes": recipe_index,
    "recipe_profiles": recipe_profile_index,
}
