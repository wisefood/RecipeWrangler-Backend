"""Discover and verify intentional nutritional-facet changes, not cooking changes."""

import re

from .models import ComparisonComposition, ComparisonSnapshot, NutritionalFacetChange, PropertyAssertion, Evidence

BUTTER_FAT_DESCRIPTION = r'\b\d+(?:[.,]\d+)?(?:\s*[-–]\s*\d+(?:[.,]\d+)?)?\s*%\s*(?:milk)?fat(?:\s+minimum)?\b'


def recover_product_claims(properties: dict[str, PropertyAssertion], label: str, reference: str) -> dict[str, PropertyAssertion]:
    """Recover an explicit light-butter claim missed by the frozen annotation."""
    identity = properties.get('base_identity')
    fat = properties.get('fat_content_description')
    if ('fat_level' not in properties and identity is not None and identity.value == 'butter'
            and identity.evidence.status == 'stated' and fat is not None and fat.evidence.status == 'stated'
            and re.search(r'\blight\b', label, re.I) and re.search(BUTTER_FAT_DESCRIPTION, label, re.I)):
        return {**properties, 'fat_level': PropertyAssertion(value='reduced', evidence=Evidence(
            reference=reference + '/candidate/document', method='explicit_light_butter_label', version='1', status='stated'))}
    return properties


FACETS = {"fat_g": ("fat_level",), "saturated_fat_g": ("fat_level",),
          "sodium_mg": ("sodium_level",), "sugar_g": ("sugar_level",),
          "energy_kcal": ("fat_level", "sugar_level")}
VALUES = {"fat_level": ("low", "reduced", "fat_free", "skimmed", "semi_skimmed"),
          "sodium_level": ("low", "reduced", "no_added_salt"),
          "sugar_level": ("low", "reduced", "sugar_free", "no_added_sugar")}


def stated(composition: ComparisonComposition, field: str) -> str | bool | list[str] | None:
    fact = composition.properties.get(field)
    return fact.value if fact is not None and fact.evidence.status == "stated" else None


def product_signature(composition: ComparisonComposition, facet: str) -> str | None:
    """Strip only parser-supported nutrition claim spans from the source label.

    Powder, packing, cooking, brand and product-type words remain significant.
    Native-only/inferred claims cannot authorize a different product signature.
    """
    annotation = (composition.provenance.get("annotation") or {}).get("text_annotation") or {}
    text = annotation.get("parsed_text") or annotation.get("input_text")
    if not text:
        return None
    fields = {facet, "fat_content_description"} if facet == "fat_level" else {facet}
    chars = list(text)
    for modifier in annotation.get("modifiers") or []:
        start, end = modifier.get("start"), modifier.get("end")
        if (modifier.get("field") in fields and modifier.get("polarity") == "asserted"
                and isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text)
                and text[start:end] == modifier.get("quote")):
            chars[start:end] = " " * (end - start)
    if facet == 'fat_level' and stated(composition, 'base_identity') == 'yogurt':
        # CoFID describes the original plain product as "yogurt, whole milk, plain".
        for match in re.finditer(r'\bwhole milk\b', text, re.I):
            chars[match.start():match.end()] = ' ' * (match.end() - match.start())
    if facet == 'fat_level' and stated(composition, 'base_identity') == 'butter':
        for match in re.finditer(BUTTER_FAT_DESCRIPTION, text, re.I):
            chars[match.start():match.end()] = ' ' * (match.end() - match.start())
        if stated(composition, 'fat_level') == 'reduced':
            for match in re.finditer(r'\blight\b', text, re.I):
                chars[match.start():match.end()] = ' ' * (match.end() - match.start())
    signature = "".join(chars).casefold()
    return " ".join(re.findall(r"\w+", signature))


def describe_change(source: ComparisonComposition, target: ComparisonComposition,
                    metric: str) -> NutritionalFacetChange | None:
    for facet in FACETS.get(metric, ()):
        value = stated(target, facet)
        previous = stated(source, facet)
        if value in VALUES[facet] and previous != value:
            return NutritionalFacetChange(facet=facet, from_value=previous, to_value=value, metric=metric)
    return None


def variant_errors(source: ComparisonComposition, target: ComparisonComposition,
                   change: NutritionalFacetChange) -> list[str]:
    expected = describe_change(source, target, change.metric)
    if expected != change:
        return ["nutritional_facet_change_not_supported"]
    identity = stated(source, "base_identity")
    if not identity or identity != stated(target, "base_identity") or identity in {"food", "sauce", "meat", "fish"}:
        return ["nutritional_variant_product_identity_mismatch"]
    before, after = product_signature(source, change.facet), product_signature(target, change.facet)
    if not before or before != after:
        return ["nutritional_variant_product_description_mismatch"]
    values = (source.values_per_100g.get(change.metric), target.values_per_100g.get(change.metric))
    if None in values:
        return ["nutritional_variant_nutrient_unknown"]
    if values[1] >= values[0]:
        return ["nutritional_variant_has_no_numeric_improvement"]
    return []


def nutritional_variant_records(snapshot: ComparisonSnapshot, position: int, metric: str) -> list[dict]:
    """Use existing released facet annotations; never infer a claim from numbers."""
    from sqlalchemy import text
    from recipe_wrangler.utils.nutrition_postgres import get_engine
    source = next(item.composition for item in snapshot.ingredients if item.position == position)
    facets = FACETS.get(metric, ())
    identity = stated(source, "base_identity") if source is not None else None
    if not facets or not identity or source.values_per_100g.get(metric) is None:
        return []
    with get_engine().connect() as connection, connection.begin():
        connection.execute(text('SET TRANSACTION READ ONLY'))
        connection.execute(text("SET LOCAL statement_timeout='60s'"))
        rows = []
        for facet in facets:
            rows.extend(dict(row) for row in connection.execute(text(r'''SELECT c.*
            FROM matching_compositions c JOIN matching_releases r USING(release_id)
            WHERE c.release_id=:release AND r.status='ready'
            AND c.collection IN ('nutritional_ingredients_eu', :regional)
            AND (CAST(:other_sources AS boolean) OR (c.annotation->'source'->>'dataset'=:dataset
                AND c.annotation->'source'->>'release'=:version))
            AND c.annotation->'text_annotation'->'facets'->>'base_identity'=:identity
            AND (c.annotation->'text_annotation'->'facets'->>:facet=ANY(:values)
                OR (:identity='butter' AND :facet='fat_level' AND c.candidate->>'document' ~* '\mlight\M'))
            AND (c.annotation->'values_per_100g'->>:metric)::numeric < :before
            ORDER BY (c.collection=:regional) DESC,
                (c.annotation->'source'->>'dataset'=:dataset AND c.annotation->'source'->>'release'=:version) DESC,
                (c.annotation->'values_per_100g'->>:metric)::numeric,c.collection,c.candidate_id
            LIMIT 100'''),
            {"release": snapshot.release_id, "regional": "nutritional_ingredients_" + snapshot.nutrition_source,
             "dataset": source.source_dataset, "version": source.source_release, "identity": identity,
             "facet": facet, "values": list(VALUES[facet]), "metric": metric,
             "before": source.values_per_100g[metric], "other_sources": snapshot.input_policy == "production_estimate"}).mappings())
        return rows


def claim_text(facet: str, value: str) -> str:
    if value == "regular":
        return "regular"
    if facet == "fat_level":
        return {"low": "low-fat", "reduced": "reduced-fat", "whole": "whole",
                "fat_free": "fat-free", "skimmed": "skimmed", "semi_skimmed": "semi-skimmed"}[value]
    if facet == "sodium_level":
        return {"low": "low-sodium", "reduced": "reduced-sodium", "no_added_salt": "no-added-salt"}[value]
    return {"low": "low-sugar", "reduced": "reduced-sugar", "sugar_free": "sugar-free",
            "no_added_sugar": "no-added-sugar"}[value]
