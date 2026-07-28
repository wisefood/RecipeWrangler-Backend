# FATO/FoodOn Neo4j Integration

This integration enriches the existing Neo4j food graph. Neo4j remains the
canonical evidence source; compact recipe-level allergen and suitability
evidence is projected into Elasticsearch for explanation and filtering.

## Implemented state (2026-07-27)

Before this work, the relevant graph was:

```text
(Recipe)-[:HAS_INGREDIENT]->(Ingredient)
(Ingredient)-[:HAS_CLASS]->(FoodOnClass)
(Ingredient)-[:HAS_ALLERGEN]->(Allergen)
```

The integration added:

```text
(Ingredient)-[:HAS_DECLARATION]->(AllergenDeclaration)
(AllergenDeclaration)-[:CONCERNS]->(Allergen)
(Ingredient)-[:SUITABILITY_FOR]->(ConsumerGroup)
(Recipe)-[:SUITABILITY_FOR]->(ConsumerGroup)
(FoodOnClass)-[:INDICATES_ORIGIN]->(DietaryOrigin)
```

The live migration produced:

- 11,776 inferred allergen declarations, matching the 11,776 existing
  ingredient/allergen relationships, with no orphan declarations.
- Three rule-backed consumer groups: `coeliac`, `vegan`, and `vegetarian`.
- Explicit `vegan-vegetarian-v1` assessments for both groups on every one of
  the 32,202 ingredients and 811,540 recipes.
- Recipe totals: vegan — 3,311 suitable, 670,970 not suitable, and 137,259
  unknown; vegetarian — 15,005 suitable, 307,902 not suitable, and 488,633
  unknown.
- Evidence metadata on allergen relationships and declarations, including
  `presence`, `evidence_status`, `sources`, matched FoodOn classes or keywords,
  and `classification_version`.
- FATO class IRIs and FoodOn allergen-label claim identifiers on the existing
  allergen model.

No new `Ingredient-[:HAS_CLASS]->FoodOnClass` relationships were created.
The integration consumes the FoodOn mappings that were already in the graph:
15,383 of 32,202 ingredient nodes are mapped, including 8,743 of the 20,290
distinct ingredients currently used by recipes.

The allergen tagger was not replaced. It still combines existing FoodOn
ancestry with keyword matching, but now preserves evidence and creates
declarations. Matching was also corrected so explicit animal-derived terms
such as `whey powder` are not suppressed merely because an existing FoodOn
class also has plant ancestry, and keyword matching uses safer boundaries and
exclusions.

Known mapping caveat: the generic ingredient `whey powder` is currently linked
to FoodOn class `FOODON_03311498` (`soybean and cheese whey powder`). Its soy
ancestor therefore produces an inferred soy declaration in addition to the
milk declaration inferred from the `whey` keyword. This mapping is more
specific than the ingredient name supports and should be audited before the
soy declaration is treated as reliable.

FATO contributed the standard declaration, allergen, consumer-group, and
suitability vocabulary. It did not supply recipe ingredients, new FoodOn
matches, manufacturer declarations, or ready-made suitability facts. The
ingredient-level `SUITABILITY_FOR` relationship and the standards-based rules
are Recipe Wrangler extensions of FATO's product-level suitability concept.

## Scope

The implementation reuses existing nodes:

```text
(Ingredient)-[:HAS_CLASS]->(FoodOnClass)
(Ingredient)-[:HAS_ALLERGEN]->(Allergen)
(Ingredient)-[:HAS_DECLARATION]->(AllergenDeclaration)
(AllergenDeclaration)-[:CONCERNS]->(Allergen)
```

It adds canonical ontology metadata and ingredient suitability:

```text
(Ingredient)-[:SUITABILITY_FOR {
  status,
  reason_codes,
  sources,
  classification_version
}]->(ConsumerGroup)
```

FATO defines seven consumer groups: `coeliac`, `vegan`, `vegetarian`, `halal`,
`kosher`, `infant`, and `elderly`. The first three are currently materialized.
The vegan and vegetarian classifier writes an explicit `unknown` when evidence
is insufficient; absence of evidence never means suitable. The other four are
planned and require evidence beyond ingredient names.

`Allergen` nodes receive the FATO `Allergen` class IRI and the corresponding
FoodOn allergen-label claim ID. Existing `HAS_ALLERGEN` relationships retain
their current evidence and gain `presence`, `evidence_status`, and
`classification_version`.

Every current `HAS_ALLERGEN` relationship is also materialized as an
`AllergenDeclaration` with type `inferred_ingredient_presence`. The declaration
copies the FoodOn IDs, keyword matches, sources, presence, and evidence status,
so inferred knowledge is explicit without being presented as a manufacturer
label declaration.

Future product-specific facts such as explicit label declarations,
derivatives, processing aids, and cross-contact warnings should use separate
declaration types and their original provenance.

## Ingredient matching

FATO defines the semantics of ingredients, allergens, declarations, and
consumer groups; it is not an ingredient catalogue. Ingredient identity and
taxonomy matching therefore continue to use the graph's existing FoodOn links:

```text
(Recipe)-[:HAS_INGREDIENT]->(Ingredient)-[:HAS_CLASS]->(FoodOnClass)
```

This enrichment consumes those links and does not rematch previously unmapped
ingredients. An ingredient without a FoodOn link can still receive conservative
keyword or allergen evidence, but missing evidence remains unknown.

## Suitability rules

Rules are deliberately conservative:

- Any blocking allergen, FoodOn ancestor, or ingredient term produces
  `not_suitable`.
- Plant FoodOn ancestry or an explicit suitability term can produce
  `suitable` for vegan or vegetarian.
- Only explicit gluten-free evidence produces `suitable` for coeliac.
- Anything without sufficient evidence remains `unknown`.

Recipe-level suitability can be derived from its existing ingredients:

1. Any `not_suitable` ingredient makes the recipe `not_suitable`.
2. Every ingredient must be explicitly `suitable` for the recipe to be
   `suitable`.
3. Incomplete coverage produces `unknown`.

The proposed standards-based replacement for the initial vegan and vegetarian
rules, including its source documents and FoodOn mapping design, is documented
in [VEGAN_VEGETARIAN_RULES.md](VEGAN_VEGETARIAN_RULES.md).

## Migration

Refresh supported ingredient-allergen relationships:

```bash
PYTHONPATH=src uv run python scripts/neo4j/tag_allergens.py
```

Preview suitability candidate counts without changing Neo4j:

```bash
PYTHONPATH=src uv run python scripts/neo4j/enrich_fato_foodon.py
```

Apply the idempotent enrichment:

```bash
PYTHONPATH=src uv run python scripts/neo4j/enrich_fato_foodon.py --apply
```

Apply the standards-based vegan/vegetarian classifier:

```bash
PYTHONPATH=src uv run python \
  scripts/neo4j/classify_vegan_vegetarian.py --apply
```

Project allergen evidence and both recipe assessments into Elasticsearch:

```bash
PYTHONPATH=src uv run python \
  scripts/elasticsearch/sync_recipe_evidence_to_es.py --apply
```

The migration preserves the existing ingredient, FoodOn, allergen, and recipe
model. It clears and regenerates only suitability relationships owned by the
current `classification_version`; manually curated relationships using another
version are preserved. The Elasticsearch sync updates only the four projected
fields (`allergens`, `allergen_evidence`, `consumer_suitability`, and
`suitable_for`); it preserves recipe vectors and all unrelated fields.

## Inspection queries

Inspect an ingredient:

```cypher
MATCH (i:Ingredient)
WHERE toLower(i.name) = "whey powder"
OPTIONAL MATCH (i)-[a:HAS_ALLERGEN]->(allergen:Allergen)
OPTIONAL MATCH (i)-[s:SUITABILITY_FOR]->(group:ConsumerGroup)
RETURN i, a, allergen, s, group;
```

Inspect its declarations and their evidence:

```cypher
MATCH (i:Ingredient)-[:HAS_DECLARATION]->(d:AllergenDeclaration)
      -[:CONCERNS]->(a:Allergen)
WHERE toLower(i.name) = "whey powder"
RETURN i.name, a.name, d.declaration_type, d.presence,
       d.evidence_status, d.sources, d.foodon_ids, d.keyword_matches;
```

Summarize suitability:

```cypher
MATCH (:Ingredient)-[s:SUITABILITY_FOR]->(g:ConsumerGroup)
RETURN g.name, s.status, count(*) AS ingredients
ORDER BY g.name, s.status;
```

Find unresolved ingredients for one group:

```cypher
MATCH (i:Ingredient)
WHERE NOT (i)-[:SUITABILITY_FOR]->(:ConsumerGroup {name: "vegan"})
RETURN i.name
ORDER BY i.name;
```
