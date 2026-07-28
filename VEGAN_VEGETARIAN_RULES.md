# Vegan and Vegetarian Suitability Rules

## Purpose and status

This document translates two stakeholder standards into an implementable,
evidence-based design for Recipe Wrangler. The source documents are proposals
and voluntary standards, not a statement that an EU-wide legally binding
definition currently applies. Their different scopes and requirements remain
visible in the evidence attached to each rule.

No rule in this document treats missing information as proof of suitability.
The supported outcomes are `suitable`, `not_suitable`, and `unknown`.

## Source documents

### Source A — FoodDrinkEurope, EuroCommerce, and EVU

**Title:** *Seeking a legally-binding definition of the terms “Food suitable
for vegans” and “Food suitable for vegetarians” in accordance with Regulation
(EU) No. 1169/2011*

**File:** [Joint statement on vegan/vegetarian definitions
(PDF)](https://www.fooddrinkeurope.eu/wp-content/uploads/2021/09/Joint-statement-on-vegan-vegetarian-definitions.pdf)

The proposed definitions are in Annex I, pages 3–4 of the PDF.

### Source B — Safe Food Advocacy Europe (SAFE)

**Title:** *Vegan definitions and standards*

**File:** [SAFE Vegan standards
(PDF)](https://www.safefoodadvocacy.eu/wp-content/uploads/2020/04/SAFE-Vegan-standards.pdf)

The definitions and proposed standards are on pages 2–3 of the PDF. This source
provides detailed vegan requirements; it does not provide an equivalent
executable vegetarian definition.

## Extracted rules

### Source A: food suitable for vegans

A food is suitable for vegans only when:

1. The food itself is not a product of animal origin.
2. No intentionally used input of animal origin is used at any stage of
   production or processing, regardless of amount or whether it remains in the
   final product.
3. The checked inputs include:
   - ingredients;
   - additives;
   - carriers;
   - flavourings;
   - enzymes;
   - processing aids; and
   - substances used like processing aids.
4. Unintended presence of a non-vegan substance does not automatically prevent
   the claim when it is unavoidable despite appropriate precautions and good
   manufacturing practices.

The source distinguishes deliberate use from unavoidable cross-contact.
Allergen precautionary information remains a separate concern.

### Source A: food suitable for vegetarians

The vegan production rules apply, except that the following materials and
their components or derivatives may be intentionally used:

1. milk and dairy products;
2. colostrum;
3. eggs;
4. honey;
5. beeswax;
6. propolis; and
7. wool grease, including lanolin derived from living sheep.

Other deliberately used animal-origin materials remain incompatible. The same
unavoidable-presence exception applies when appropriate precautions and good
manufacturing practices were followed.

### Source B: additional vegan requirements

SAFE's proposed vegan standard adds or emphasizes:

1. `Animal` covers the animal kingdom, including vertebrates and
   multicellular invertebrates.
2. An ambiguous ingredient is one that may have either animal or non-animal
   origin. Its source must be disclosed by the supplier through written
   evidence before a vegan claim or logo is accepted.
3. A product containing an animal-derived ingredient must not be marketed as
   vegan, vegan-friendly, or suitable for vegans.
4. Product development and manufacture, including controlled third parties,
   must not involve animal testing initiated by or on behalf of the
   manufacturer.
5. GMO development or production must not involve animal genes or
   animal-derived substances; GMO presence must be declared.
6. Vegan dishes must be prepared separately from non-vegan dishes. At minimum,
   surfaces and utensils must be thoroughly cleaned first; dedicated equipment
   is preferred.

## Recipe Wrangler rule version

Recipe Wrangler will initially maintain one versioned vegan/vegetarian rule
set. Source A supplies the composition rules for vegan and vegetarian food.
Source B adds stricter vegan evidence requirements for ambiguous ingredients,
preparation, animal testing, and GMO production.

The graph and API store `classification_version` and the source references that
support each conclusion. This keeps the initial interface simple without
discarding provenance.

The implemented version is `vegan-vegetarian-v1`. It classifies composition
only; it does not claim certification or prove supplier, cross-contact,
preparation, animal-testing, or GMO-process requirements.

## Executable three-state rules

### Ingredient or production input

For the current classification version:

- `not_suitable`: reliable evidence shows a deliberately used origin or process
  prohibited by that profile.
- `suitable`: reliable evidence positively establishes an allowed origin and
  no prohibited evidence exists.
- `unknown`: the ingredient is unmapped, has an ambiguous origin, has
  conflicting evidence, or lacks evidence required by the profile.

Absence of an animal FoodOn ancestor is not positive proof of non-animal
origin.

### Recipe or product

1. If any deliberately used input is `not_suitable`, the recipe is
   `not_suitable`.
2. The checked inputs must include ordinary ingredients and, when applicable,
   additives, carriers, flavourings, enzymes, and processing aids.
3. A recipe is compositionally `suitable` only when every deliberately used
   input is positively suitable.
4. Any unknown or ambiguous input makes the result `unknown`.
5. Source-specific process evidence is evaluated separately:
   - Source A requires appropriate precautions and good manufacturing evidence
     when unavoidable cross-contact is relevant.
   - Source B additionally requires its preparation, animal-testing, and GMO
     evidence.
6. A final claim is `suitable` only when both composition and all evidence
   required by the selected profile pass.

## Connecting the rules to FoodOn

### Existing graph

Recipe Wrangler already has:

```text
(Recipe)-[:HAS_INGREDIENT]->(Ingredient)
(Ingredient)-[:HAS_CLASS]->(FoodOnClass)
(FoodOnClass)-[:SUBCLASS_OF]->(FoodOnClass)
```

No new FoodOn classification was created by the FATO integration. At the time
of this specification, 15,383 of 32,202 Ingredient nodes have at least one
FoodOn mapping. Unmapped ingredients must remain unknown.

### FoodOn origin rule registry

Create a versioned rule registry that maps reviewed FoodOn roots to dietary
origin classes rather than maintaining lists of individual ingredient names:

```text
(FoodOnClass)-[:INDICATES_ORIGIN {
  policy_version,
  evidence_source,
  reviewed_at
}]->(DietaryOrigin)
```

Suggested `DietaryOrigin` values:

- `animal`;
- `dairy`;
- `egg`;
- `bee_product`;
- `live_sheep_wool_derivative`;
- `plant`;
- `fungal`;
- `microbial`;
- `mineral`;
- `synthetic`;
- `ambiguous`.

The specific FoodOn root identifiers must be reviewed before activation.
FoodOn ancestry can then classify all descendants globally:

```text
(Ingredient)-[:HAS_CLASS]->(FoodOnClass)
             -[:SUBCLASS_OF*0..]->(ReviewedFoodOnRoot)
             -[:INDICATES_ORIGIN]->(DietaryOrigin)
```

### Translating origin into policy suitability

| Evidence | Vegan | Vegetarian under Source A |
| --- | --- | --- |
| Animal meat, fish, shellfish, or other disallowed animal origin | not suitable | not suitable |
| Dairy or colostrum | not suitable | suitable |
| Egg | not suitable | suitable |
| Honey, beeswax, or propolis | not suitable | suitable |
| Lanolin derived from living sheep | not suitable | suitable |
| Positively established plant, fungal, microbial, mineral, or non-animal synthetic origin | suitable | suitable |
| Ambiguous, conflicting, or missing origin | unknown | unknown |

Positive microbial or synthetic classifications require evidence that the
production medium, carrier, enzyme, or precursor is also allowed. Their names
alone are not enough.

### Graph output

Ingredient-level results remain a Recipe Wrangler extension with an
explicit composition scope:

```text
(Ingredient)-[:SUITABILITY_FOR {
  status: "suitable" | "not_suitable" | "unknown",
  scope: "ingredient_composition",
  reason_codes,
  sources,
  classification_version
}]->(ConsumerGroup)
```

Recipe-level evaluation retains the responsible components:

```text
(Recipe)-[:SUITABILITY_FOR {
  status,
  scope: "recipe_composition",
  blocking_ingredients,
  unknown_ingredients,
  sources,
  classification_version
}]->(ConsumerGroup)
```

Neo4j stores one explicit three-state relationship for both `vegan` and
`vegetarian` on every Ingredient and Recipe. Elasticsearch stores both compact
recipe-level assessments in `consumer_suitability`; `suitable_for` duplicates
only positive group names as a fast search-filter field.

## Implemented run (2026-07-27)

Run the dry-run-by-default classifier with:

```bash
PYTHONPATH=src uv run python scripts/neo4j/classify_vegan_vegetarian.py --apply
```

The live Neo4j result is:

| Group | Suitable | Not suitable | Unknown | Total recipes |
| --- | ---: | ---: | ---: | ---: |
| Vegan | 3,311 | 670,970 | 137,259 | 811,540 |
| Vegetarian | 15,005 | 307,902 | 488,633 | 811,540 |

All 811,540 recipes and all 32,202 ingredients have exactly one relationship
for each group under `vegan-vegetarian-v1`.

Project the graph evidence into existing recipe documents without rebuilding
their vectors:

```bash
PYTHONPATH=src uv run python \
  scripts/elasticsearch/sync_recipe_evidence_to_es.py --apply
```

The live sync updated all 811,540 `recipes_v2` documents with zero failures.
The nested Elasticsearch status totals exactly match Neo4j.

## Evidence that FoodOn cannot provide

FoodOn taxonomy can support ingredient-origin classification, but it cannot
establish every condition in the source standards. The following require
additional declarations or process data:

- whether an ambiguous additive, enzyme, carrier, flavouring, or processing aid
  was animal-derived;
- whether a substance was deliberately used or only unintentionally present;
- supplier identity and written origin declarations;
- good-manufacturing and cross-contact precautions;
- preparation surfaces and utensils;
- animal-testing history; and
- animal-derived genes or substances used in GMO development.

These facts should use evidence nodes with provenance, issuer, validity dates,
and policy version. They must not be inferred from the absence of an ingredient
or allergen relationship.

## Implementation order

1. Review and version FoodOn roots for each dietary-origin class.
2. Run a dry classification report showing suitable, not-suitable, ambiguous,
   conflicting, and unmapped ingredients.
3. Audit high-frequency ingredients and every rule conflict.
4. Materialize explicit three-state ingredient results, including unknown.
5. Extend the recipe model to include additives and other production inputs
   when that data is available.
6. Add supplier and process declarations for requirements FoodOn cannot answer.
7. Aggregate recipe-level vegan and vegetarian results using the current
   classification version.
8. Validate against a curated benchmark before enabling search filters or
   adaptation claims.
9. Project only validated recipe-level summaries and blocker evidence into
   Elasticsearch; keep the full evidence graph canonical in Neo4j.
