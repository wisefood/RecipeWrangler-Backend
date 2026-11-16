"""Shared ingredient/recipe dietary classification used by batch and projection."""
from __future__ import annotations

import re
from typing import Any

from recipe_wrangler.utils.consumer_suitability import (
    dietary_keyword_evidence, dietary_title_evidence, DEFINITION_SOURCES, DIETARY_ORIGINS, GROUP_RULES, PLANT_STAPLE_PATTERNS,
    POSITIVE_EVIDENCE_EXCLUSIONS, PESCATARIAN_NAME_EXCLUSIONS, ORIGIN_CLASSIFICATION_VERSION,
    SUPPORTED_CONSUMER_GROUPS, VEGAN_NAME_EXCLUSIONS,
    VEGETARIAN_NAME_EXCLUSIONS, classification_version, keyword_regex, positive_keyword_regex,
)
from recipe_wrangler.utils.foodon_matching import ingredient_link_batches
from recipe_wrangler.utils.es_recipe_evidence import allergen_filter_values
from recipe_wrangler.utils.dietary_ingredient_evidence import canonical_ingredient_name, ingredient_diet_evidence

# A plant/dairy ancestor of a prepared food does not describe every component.
# Independent whole-name rules or external dietary properties can still confirm it.
_COMPOUND_ORIGIN = re.compile(
    r'\b(?:mix|mixes|sauce|sauces|stock|broth|dressing|spread|filling|prepared|'
    r'sandwich|soup|stew|casserole|cake|cakes|cookie|cookies|biscuit|biscuits|'
    r'pastry|pie|pies|pancake|pancakes|waffle|waffles|wrap|wraps)\b'
)


def _run(session, query: str, **params: Any):
    from neo4j import Query, Session
    # Explicit projection transactions already have a 60-second timeout.
    statement = Query(query, timeout=60) if isinstance(session, Session) else query
    return session.run(statement, **params)


def _scalar(session, query: str, **params: Any) -> int:
    record = _run(session, query, **params).single()
    return int(record["count"]) if record else 0


def _group_params(group: str) -> dict[str, Any]:
    rule = GROUP_RULES[group]
    return {
        "group": group,
        "blocking_allergens": allergen_filter_values(rule["blocking_allergens"]),
        "positive_allergens": rule["positive_allergens"],
        "blocking_keywords": rule["blocking_keywords"],
        "blocking_keyword_regexes": [
            keyword_regex(keyword) for keyword in rule["blocking_keywords"]
        ],
        "positive_keywords": rule["positive_keywords"],
        "positive_keyword_regexes": [
            positive_keyword_regex(keyword)
            for keyword in rule["positive_keywords"]
        ],
        "negative_exclusions": (
            VEGAN_NAME_EXCLUSIONS
            if group == "vegan"
            else PESCATARIAN_NAME_EXCLUSIONS if group == "pescatarian" else VEGETARIAN_NAME_EXCLUSIONS
        ),
        "positive_exclusions": POSITIVE_EVIDENCE_EXCLUSIONS,
        "plant_staple_regexes": PLANT_STAPLE_PATTERNS,
        "definition_sources": DEFINITION_SOURCES[group],
        "version": classification_version(group),
        "origin_version": ORIGIN_CLASSIFICATION_VERSION,
        # Generic organs/gelatin/collagen do not establish a land-animal source.
        "blocking_roots": ["FOODON_00001006", "FOODON_00001131", "FOODON_00002201", "FOODON_00001237"],
        "positive_roots": [root for origin in DIETARY_ORIGINS
                           if origin["name"] not in {"animal_meat", "animal_derivative"}
                           for root in origin["roots"]],
    }


def _initialize_unknown(session, group: str, ingredient_ids: list[str] | None = None) -> int:
    return _scalar(
        session,
        """
        MATCH (i:Ingredient)
        WHERE $ingredient_ids IS NULL OR elementId(i) IN $ingredient_ids
        MATCH (g:ConsumerGroup {name: $group})
        MERGE (i)-[rel:SUITABILITY_FOR]->(g)
        SET rel.status = "unknown",
            rel.scope = "ingredient_composition",
            rel.reason_codes = ["insufficient_evidence"],
            rel.sources = ["rules"],
            rel.definition_sources = $definition_sources,
            rel.classification_version = $version,
            rel.updated_at = datetime()
        RETURN count(i) AS count
        """,
        ingredient_ids=ingredient_ids, **_group_params(group),
    )


def _classify_negative(session, group: str, ingredient_ids: list[str] | None = None) -> int:
    status_property = (
        "vegan_status" if group == "vegan" else "vegetarian_status"
    )
    query = f"""
    UNWIND $candidates AS item
    MATCH (i:Ingredient)
    WHERE elementId(i) = item.element_id AND i.name = item.name
    OPTIONAL MATCH (i)-[:HAS_CLASS]->(mapped:FoodOnClass)
                   -[:SUBCLASS_OF*0..]->(root:FoodOnClass)
    WHERE mapped.foodon_id IN item.class_ids
    OPTIONAL MATCH (root)-[origin_rel:INDICATES_ORIGIN]->(origin:DietaryOrigin)
    WITH i, item,
         [value IN collect(DISTINCT CASE
            WHEN ($group = 'pescatarian' AND root.foodon_id IN $blocking_roots)
              OR ($group <> 'pescatarian' AND origin_rel.classification_version = $origin_version
                  AND origin.{status_property} = "not_suitable")
            THEN coalesce(origin.name, root.foodon_id) END)
          WHERE value IS NOT NULL] AS blocking_origins
    OPTIONAL MATCH (i)-[:HAS_ALLERGEN]->(allergen:Allergen)
    OPTIONAL MATCH (i)-[:HAS_DECLARATION]->(d:AllergenDeclaration)-[:CONCERNS]->(declared:Allergen)
    WITH i, item, blocking_origins,
         [value IN collect(DISTINCT toLower(allergen.name))
              + collect(DISTINCT CASE WHEN toLower(coalesce(d.presence, 'contains')) IN ['contains', 'present', '']
                                     THEN toLower(declared.name) END)
          WHERE value IN $blocking_allergens] AS blocking_allergens,
         item.keyword_hits AS keyword_hits
    WHERE (
      (size(blocking_origins) > 0
       AND none(pattern IN $negative_exclusions WHERE toLower(i.name) =~ pattern))
      OR size(blocking_allergens) > 0
      OR size(keyword_hits) > 0
    )
    MATCH (i)-[rel:SUITABILITY_FOR]->
          (:ConsumerGroup {{name: $group}})
    SET rel.status = "not_suitable",
        rel.scope = "ingredient_composition",
        rel.reason_codes =
          blocking_origins + blocking_allergens + keyword_hits,
        rel.sources =
          ["rules"]
          + CASE WHEN any(hit IN keyword_hits WHERE hit STARTS WITH 'open_food_facts:')
                 THEN ["open_food_facts_ingredients"] ELSE [] END
          + CASE WHEN size(blocking_origins) > 0 THEN ["foodon"] ELSE [] END
          + CASE
              WHEN size(blocking_allergens) > 0
              THEN ["allergen_evidence"] ELSE []
            END,
        rel.definition_sources = $definition_sources,
        rel.classification_version = $version,
        rel.updated_at = datetime()
    RETURN count(DISTINCT i) AS count
    """
    count = 0
    for batch in ingredient_link_batches(session, ingredient_ids=ingredient_ids):
        for item in batch:
            hits, presence = dietary_keyword_evidence(item["name"], group)
            item["keyword_hits"] = hits if presence == "contains" else []
        count += _scalar(session, query, candidates=batch, **_group_params(group))
    return count


def _classify_positive(session, group: str, ingredient_ids: list[str] | None = None) -> int:
    status_property = (
        "vegan_status" if group == "vegan" else "vegetarian_status"
    )
    query = f"""
    UNWIND $candidates AS item
    MATCH (i:Ingredient)
    WHERE elementId(i) = item.element_id AND i.name = item.name
    OPTIONAL MATCH (i)-[:HAS_CLASS]->(mapped:FoodOnClass)-[:SUBCLASS_OF*0..]->
                   (root:FoodOnClass)
    WHERE mapped.foodon_id IN item.class_ids
    OPTIONAL MATCH (root)-[origin_rel:INDICATES_ORIGIN]->(origin:DietaryOrigin)
    WITH i, item,
         CASE WHEN item.allow_origin THEN [value IN collect(DISTINCT CASE
            WHEN ($group = 'pescatarian' AND root.foodon_id IN $positive_roots)
              OR ($group <> 'pescatarian' AND origin_rel.classification_version = $origin_version
                  AND origin.{status_property} = "suitable")
            THEN coalesce(origin.name, root.foodon_id) END)
          WHERE value IS NOT NULL] ELSE [] END AS suitable_origins,
         [idx IN range(0, size($positive_keywords) - 1)
          WHERE item.evidence_name =~
                $positive_keyword_regexes[idx]
          | $positive_keywords[idx]]
         + CASE WHEN any(pattern IN $plant_staple_regexes
                         WHERE item.evidence_name =~ pattern)
                THEN ["plant_staple_name"] ELSE [] END
         + item.external_positive AS keyword_hits
    OPTIONAL MATCH (i)-[:HAS_ALLERGEN]->(allergen:Allergen)
    OPTIONAL MATCH (i)-[:HAS_DECLARATION]->(d:AllergenDeclaration)-[:CONCERNS]->(declared:Allergen)
    WITH i, suitable_origins, keyword_hits,
         [value IN collect(DISTINCT toLower(allergen.name))
          WHERE value IN $positive_allergens] AS allowed_allergens,
         [value IN collect(DISTINCT CASE WHEN toLower(d.presence) IN ['possible', 'may_contain', 'unknown']
                                       THEN toLower(declared.name) END)
          WHERE value IN $blocking_allergens] AS possible_allergens
    WHERE (
        size(suitable_origins) > 0
        OR size(allowed_allergens) > 0
        OR size(keyword_hits) > 0
      )
      AND size(possible_allergens) = 0
      AND none(pattern IN $positive_exclusions
               WHERE toLower(coalesce(i.name, "")) =~ pattern)
    MATCH (i)-[rel:SUITABILITY_FOR]->
          (:ConsumerGroup {{name: $group}})
    WHERE rel.status <> "not_suitable"
    SET rel.status = "suitable",
        rel.scope = "ingredient_composition",
        rel.reason_codes =
          suitable_origins + allowed_allergens + keyword_hits,
        rel.sources =
          ["rules"]
          + CASE WHEN any(hit IN keyword_hits WHERE hit STARTS WITH 'open_food_facts:')
                 THEN ["open_food_facts_ingredients"] ELSE [] END
          + CASE WHEN size(suitable_origins) > 0 THEN ["foodon"] ELSE [] END
          + CASE
              WHEN size(allowed_allergens) > 0
              THEN ["allergen_evidence"] ELSE []
            END,
        rel.definition_sources = $definition_sources,
        rel.classification_version = $version,
        rel.updated_at = datetime()
    RETURN count(DISTINCT i) AS count
    """
    count = 0
    for batch in ingredient_link_batches(session, ingredient_ids=ingredient_ids, positive_evidence=True):
        for item in batch:
            item['evidence_name'] = canonical_ingredient_name(item['name'])
            evidence = ingredient_diet_evidence(item['name'], group)
            item['allow_origin'] = (
                not _COMPOUND_ORIGIN.search(item['evidence_name'])
                and (evidence or {}).get('status') != 'unknown'
            )
            item['external_positive'] = (
                ['open_food_facts:' + evidence['ingredient_id']]
                if evidence and evidence['status'] == 'suitable' else []
            )
        count += _scalar(session, query, candidates=batch, **_group_params(group))
    return count


def _aggregate_recipe_batch(
    session,
    recipe_ids: list[str],
    group: str,
) -> int:
    title_rows = _run(session,
        "UNWIND $recipe_ids AS rid MATCH (r:Recipe {recipe_id:rid}) RETURN rid, r.title AS title",
        recipe_ids=recipe_ids,
    )
    blocked_titles = [row["rid"] for row in title_rows
                      if (result := dietary_title_evidence(row["title"] or "", group))[0]
                      and result[1] == "contains"]
    return _scalar(
        session,
        """
        UNWIND $recipe_ids AS rid
        MATCH (r:Recipe {recipe_id: rid})
        MATCH (g:ConsumerGroup {name: $group})
        OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
        OPTIONAL MATCH (i)-[ingredient_rel:SUITABILITY_FOR]->(g)
        WHERE ingredient_rel.classification_version = $version
        OPTIONAL MATCH (i)-[:HAS_ALLERGEN]->(direct:Allergen)
        OPTIONAL MATCH (i)-[:HAS_DECLARATION]->(d:AllergenDeclaration)-[:CONCERNS]->(declared:Allergen)
        OPTIONAL MATCH (r)-[:HAS_ALLERGEN]->(recipe_direct:Allergen)
        OPTIONAL MATCH (r)-[:HAS_DECLARATION]->(recipe_d:AllergenDeclaration)-[:CONCERNS]->(recipe_declared:Allergen)
        WITH r, g,
             [allergen IN collect(DISTINCT toLower(direct.name))
                + collect(DISTINCT toLower(recipe_direct.name))
                + collect(DISTINCT CASE WHEN toLower(coalesce(recipe_d.presence, 'contains')) IN ['contains', 'present', '']
                                       THEN toLower(recipe_declared.name) END)
                + collect(DISTINCT CASE WHEN toLower(coalesce(d.presence, 'contains')) IN ['contains', 'present', '']
                                       THEN toLower(declared.name) END)
              WHERE allergen IN $blocking_allergens] AS ingredient_allergen_blockers,
             [allergen IN collect(DISTINCT CASE WHEN toLower(d.presence) IN ['possible', 'may_contain', 'unknown']
                                               THEN toLower(declared.name) END)
                + collect(DISTINCT CASE WHEN toLower(recipe_d.presence) IN ['possible', 'may_contain', 'unknown']
                                               THEN toLower(recipe_declared.name) END)
              WHERE allergen IN $blocking_allergens] AS possible_allergen_blockers,
             count(DISTINCT i) AS ingredient_count,
             [value IN collect(DISTINCT CASE
                WHEN ingredient_rel.status = "not_suitable"
                THEN i.name END)
              WHERE value IS NOT NULL] AS blocking_ingredients,
             [value IN collect(DISTINCT CASE
                WHEN ingredient_rel.status IS NULL
                  OR ingredient_rel.status = "unknown"
                THEN i.name END)
              WHERE value IS NOT NULL] AS unknown_ingredients,
             count(DISTINCT CASE
                WHEN ingredient_rel.status = "suitable"
                THEN i END) AS suitable_count
        // The title names the dish, so "Bacon and sweetcorn baked potato"
        // must block regardless of ingredient-level evidence -- an
        // ingredient like "rashers" can have no FoodOn class link and match
        // no keyword, and the title is real evidence that scan misses.
        WITH r, g, blocking_ingredients, unknown_ingredients, ingredient_count,
             ingredient_allergen_blockers, possible_allergen_blockers,
             suitable_count,
             [allergen IN coalesce(r.declared_allergens, [])
              WHERE toLower(allergen) IN $blocking_allergens | toLower(allergen)] AS declared_blockers,
             r.recipe_id IN $blocked_titles
             AS title_blocked
        WITH r, g, blocking_ingredients, unknown_ingredients, title_blocked, declared_blockers,
             ingredient_allergen_blockers, possible_allergen_blockers,
             CASE
               WHEN size(blocking_ingredients) > 0 OR title_blocked
                 OR size(declared_blockers) > 0
                 OR size(ingredient_allergen_blockers) > 0 THEN "not_suitable"
               WHEN size(possible_allergen_blockers) > 0 THEN "unknown"
               WHEN ingredient_count > 0
                 AND suitable_count = ingredient_count
                 AND size(unknown_ingredients) = 0 THEN "suitable"
               ELSE "unknown"
             END AS status
        MERGE (r)-[rel:SUITABILITY_FOR]->(g)
        SET rel.status = status,
            rel.scope = "recipe_composition",
            rel.blocking_ingredients = blocking_ingredients,
            rel.unknown_ingredients = unknown_ingredients,
            rel.blocking_declared_allergens = declared_blockers,
            rel.blocking_allergens = ingredient_allergen_blockers,
            rel.possible_allergens = possible_allergen_blockers,
            rel.reason_codes = CASE status
              WHEN "not_suitable" THEN
                CASE WHEN size(blocking_ingredients) > 0 THEN ["blocking_ingredient"] ELSE [] END
                + CASE WHEN title_blocked THEN ["blocking_title"] ELSE [] END
                + CASE WHEN size(declared_blockers) > 0 THEN ["recipe_declared_allergen"] ELSE [] END
                + CASE WHEN size(ingredient_allergen_blockers) > 0 THEN ["allergen_evidence"] ELSE [] END
              WHEN "suitable" THEN ["all_ingredients_suitable"]
              ELSE CASE WHEN size(possible_allergen_blockers) > 0
                        THEN ["possible_allergen_presence"] ELSE ["incomplete_ingredient_evidence"] END END,
            rel.sources = ["ingredient_suitability"]
                + CASE WHEN size(declared_blockers) > 0 THEN ["recipe_declaration"] ELSE [] END
                + CASE WHEN size(ingredient_allergen_blockers) > 0 THEN ["allergen_evidence"] ELSE [] END,
            rel.definition_sources = $definition_sources,
            rel.classification_version = $version,
            rel.updated_at = datetime()
        RETURN count(DISTINCT r) AS count
        """,
        recipe_ids=recipe_ids,
        blocked_titles=blocked_titles,
        **_group_params(group),
    )


def refresh_recipe_suitability(recipe_ids: list[str]) -> int:
    """Refresh current ingredients and recipe assessments before catalogue projection.

    Each recipe commits atomically; failures leave its projection job retryable.
    This does not rewrite source tags or infer allergen absence.
    """
    from neo4j import unit_of_work
    from recipe_wrangler.utils.neo4j_utils import driver

    @unit_of_work(timeout=60)
    def refresh(tx, recipe_id):
        row = tx.run(
            """MATCH (r:Recipe {recipe_id:$recipe_id})
            OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
            RETURN count(DISTINCT r) AS recipe_count,
                   collect(DISTINCT elementId(i)) AS ingredient_ids""",
            recipe_id=recipe_id,
        ).single()
        if row is None or row["recipe_count"] != 1:
            raise ValueError("Recipe disappeared before suitability refresh")
        ingredient_ids = row["ingredient_ids"]
        for group in SUPPORTED_CONSUMER_GROUPS:
            tx.run(
                "MERGE (g:ConsumerGroup {name:$group})",
                group=group,
            ).consume()
            for start in range(0, len(ingredient_ids), 100):
                batch = ingredient_ids[start:start + 100]
                _initialize_unknown(tx, group, batch)
                _classify_negative(tx, group, batch)
                _classify_positive(tx, group, batch)
            if _aggregate_recipe_batch(tx, [recipe_id], group) != 1:
                raise ValueError("Recipe suitability refresh did not complete")
        return 1

    completed = 0
    with driver.session() as session:
        for recipe_id in dict.fromkeys(recipe_ids):
            completed += session.execute_write(refresh, recipe_id)
    return completed

