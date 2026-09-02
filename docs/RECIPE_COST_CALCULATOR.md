# Recipe Cost Calculator

## Purpose

`recipe_wrangler.pricing.cost_calculator` calculates a recipe's economic reference cost from normalized ingredient weights, a serving count, and a region (`EU`, `IE`, `HU`, or `SI`). It supports both one newly analysed recipe and an iterable of existing recipe records.

It calculates total and per-serving EUR estimates, ingredient contributions, match coverage, fallback use, and a plain-language explanation. It deliberately does not assign a recipe-level `€ / €€ / €€€` tier because recipe cost-per-serving thresholds have not yet been calibrated.

## Ingredient resolution

Resolution is deterministic and uses this order:

1. Exact supported detailed product, such as `chicken breast fillet`.
2. Exact canonical base product, such as `chicken`.
3. A reviewed alias from `cost_ingredient_aliases.json`, such as `chicken wings → chicken` or `ground beef → beef minced meat`.
4. A conservative whole-phrase base fallback, such as `boneless chicken pieces → chicken`.
5. Unmatched, with no invented price.

The calculator consumes an upstream `canonical_name` or `cost_ingredient_id` when supplied. This lets a Neo4j relationship or other canonicalization layer take priority without changing the costing formula. Canonical products, regional prices, and aliases are read from PostgreSQL at runtime; the generated files under `data/cost/processed/` are import artefacts.

Base fallback uses the existing general base-product price, which is the **median of its available detailed-product reference prices**. It is not a newly calculated mean. The output records `price_scope`, `match_method`, and `cost_match_confidence`, so a direct detail price and a general fallback never look identical.

Concepts that merely contain a food word are guarded. For example, `chicken stock`, `beef broth`, `apple juice`, and `wheat flour` do not automatically inherit chicken, beef, apple, or wheat prices. Reviewed aliases should be added only after confirming economic equivalence is acceptable for the use case.

## New-recipe analysis

The profiling node passes its normalized names, gram weights, serving count, and region to the calculator. Its result is available as:

```text
cost_profile
full_profile.cost_profile
pipeline_trace.profiling.cost_profile
```

If operational price assets are not installed in a deployment, profiling continues and reports `cost_profile.status = "unavailable"`.

## Batch use

```python
from recipe_wrangler.pricing import calculate_recipe_batch

results = calculate_recipe_batch(existing_recipes, country="SI")
```

Each recipe must contain `serves` and ingredient dictionaries with a name plus `weight_g`, `weight_grams`, or `ingredient_quantity_kg`.

Incomplete matches are not silently treated as complete recipe costs. The calculator returns the matched contribution as `matched_cost_lower_bound_eur`, reports unmatched products and unresolved weights, and withholds `estimated_recipe_cost_total_eur` until every ingredient can be costed.

## Graph integration

Keywords alone should not be the long-term source of truth. The recommended graph model is an explicit reviewed relationship such as:

```text
(:Ingredient)-[:HAS_COST_REFERENCE {
  match_method,
  review_status,
  cost_reference_version
}]->(:CostProduct {product_id})
```

The current deterministic resolver can generate candidate mappings for review. Once approved relationships exist, imports can pass `cost_ingredient_id` directly and bypass name fallback. The runtime price catalogue and the active regional calibration are stored in PostgreSQL; the graph stores the semantic link, not a second copy of the pricing rules.

## One-off graph linker

The one-off linker is `scripts/one_off/link_cost_products_to_ingredients.py`. Its default mode is read-only with respect to Neo4j:

```bash
uv run python scripts/one_off/link_cost_products_to_ingredients.py
```

It writes the following local review artifacts under `data/cost/processed/cost_graph_mapping/`:

- `cost_foodon_anchor_review.csv`: cost products that still need a reviewed FoodOn anchor;
- `ingredient_cost_mapping_review.csv`: every graph Ingredient, proposed product, method, confidence, ontology evidence, and status;
- `ingredient_decisions_template.csv`: uncertain proposals that can be manually approved or rejected;
- `summary.json`: counts and mapping/calibration versions.

Only `approved_automatic` mappings are eligible for the first application. Safe automatic mappings require an exact cost name, a reviewed alias, or a unique short FoodOn descendant path whose Ingredient→FoodOn link came from exact-label evidence. Approximate embeddings, broad paths, blocked processed contexts, and equal-distance ambiguity remain review-only.

After reviewing the reports, apply the automatically approved mappings with:

```bash
uv run python scripts/one_off/link_cost_products_to_ingredients.py --apply
```

To include human decisions, copy/edit the decision template, set `decision` to `approved` or `rejected`, and run:

```bash
uv run python scripts/one_off/link_cost_products_to_ingredients.py \
  --decisions path/to/reviewed_decisions.csv \
  --apply
```

Application is additive and idempotent. It upserts versioned `CostProduct` nodes, creates approved `HAS_FOODON_ANCHOR` links, and replaces only `HAS_COST_REFERENCE` relationships previously managed by this script. It does not delete or rewrite Ingredient, Recipe, FoodOn, or substitution data.
