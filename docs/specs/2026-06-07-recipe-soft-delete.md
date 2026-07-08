# Recipe Soft-Delete (Disable) — Design Spec

**Date:** 2026-06-07
**Status:** Implemented 2026-07-08 — wrangler `feat/recipe-soft-delete`,
wisefood-api `feat/recipe-soft-delete-proxy`, wisefood-ui `feat/recipe-soft-delete-console`
**Repo:** RecipeWrangler-Backend

## Goal

Let recipes be **disabled** (soft-deleted) so they are never served to any
consumer — FoodChat meal-plan candidates, all recipe search variants, get-by-id,
details, autocomplete — while the data is retained and the action is reversible.
Support single (admin endpoint), by-ID bulk, and by-query bulk disable/enable at
up to ~1M-recipe scale.

## Data model

Reuse the **existing but dormant** `r.status` property on `:Recipe`
(`neo4j_recipes.py:250` already writes `r.status = 'active'` on every upsert,
but it is read nowhere today).

- `status = 'active'`  → served (default)
- `status = 'disabled'` → hidden everywhere
- Add `r.disabled_at` (datetime) + `r.disabled_reason` (optional str) on disable;
  cleared on enable.

**Filter convention (uniform across every read site):** treat missing/legacy
status as active — `coalesce(r.status,'active') <> 'disabled'` (Neo4j) and
`must_not: {term: {status: "disabled"}}` (ES). This makes the feature backward
compatible without a data migration.

Elasticsearch: add a `status` keyword field to **both** indices —
`recipes_v2` (mapping in `index_recipes_v2.py:45-86`, reindex query
`88-122`, `fetch_from_neo4j` `157-191`) and `recipes` (the
`settings.elastic_index` used by autocomplete/fallbacks/dual-write). Default
`"active"` when absent.

## Enforcement — all 10 read sites (v1 scope: everywhere)

| # | Path | File / insertion | Store |
|---|------|------------------|-------|
| 1 | FoodChat candidates | `neo4j_recipes.py:702-707` add `AND coalesce(r.status,'active') <> 'disabled'` | Neo4j |
| 2 | Primary ES search / param_search | `es_recipe_search.py` `build_es_query` → `must_not.append({"term":{"status":"disabled"}})` | recipes_v2 |
| 3 | Legacy Neo4j param_search | `param_search.py` `_build_where_clause` add predicate (covers results+count+facets) | Neo4j |
| 4 | Legacy Neo4j `/search` | `text2cypher.py:627` add predicate beside recipe1m exclusion | Neo4j |
| 5 | `_random_myplate_from_elastic` fallback | `recipes.py:202-208` add `must_not` | recipes |
| 6 | `_search_elastic_keyword` fallback | `recipes.py:253-265` add `must_not` | recipes |
| 7 | get-recipe-by-id | `fetch_recipe_info.py:21` add to `WHERE {match_predicate}` | Neo4j |
| 8 | batch `/details` | `fetch_recipe_info.py:169` add to WHERE | Neo4j |
| 9 | `/autocomplete` | `recipes.py:936-943` add filter `must_not` | recipes |
| 10 | `/count` | `neo4j_recipes.py:34` add WHERE (so counts exclude disabled) | Neo4j |

**Defense-in-depth (optional, later):** the four `*_by_ids` enrichment helpers
(`neo4j_recipes.py:38/61/78/99`) are attach-only and don't independently serve —
skip in v1.

A single shared constant/snippet for the Neo4j predicate and the ES clause keeps
all 10 consistent and greppable.

## Write path

### Repository (Neo4j)
Mirror `update_recipe_in_neo4j` (`neo4j_recipes.py:119-213`). New functions:

- `set_recipe_status(recipe_ids: list[str], status: str, reason: str|None) -> int`
  — batched `UNWIND $ids AS rid MATCH (r:Recipe) WHERE r.recipe_id = rid OR r.id = rid
  SET r.status=$status, r.disabled_at=(CASE WHEN $status='disabled' THEN datetime() ELSE null END),
  r.disabled_reason=$reason RETURN count(r)`. Returns affected count.
- `disable_recipes_by_query(...)` — resolves matching IDs via the param_search
  Cypher (reusing `_build_where_clause`), then calls `set_recipe_status` in batches.

### Elasticsearch sync (dual-index, bulk)
Both indices must be updated or disabled recipes linger in search. Mirror the
existing `_bulk` sync pattern (`scripts/elasticsearch/sync_allergens_to_es.py`,
`sync_image_url_source_id_from_neo4j.py:86-96`): emit
`{"update":{"_id":id,"_index":idx}}\n{"doc":{"status":"disabled"}}\n` lines in
chunks against `recipes_v2` AND `recipes`, per batch, right after each Neo4j
batch commits. Best-effort with retry; log per-index failures.

## API endpoints (mirror `recipe_substitute`, `recipes.py:2086`)

No auth exists in this service today; these endpoints follow the same
(unauthenticated) pattern as existing mutating endpoints. **Flag:** if these
should be admin-gated, that's net-new auth — called out as a follow-up, not v1.

- `POST /recipes/{recipe_id}/disable`  body `{reason?}` → `set_recipe_status([id],'disabled',reason)` + ES sync + `cache_delete(id)`; 404 when count==0.
- `POST /recipes/{recipe_id}/enable`   → status `active`.
- `POST /recipes/disable`  body `{recipe_ids:[...], reason?}` → batched by-ID bulk.
- `POST /recipes/disable-by-query` body = RecipeSearchFilters (+reason) → by-query bulk; returns affected count. Async/streamed for very large sets (see Scale).

## Scale (~1M)

- Neo4j: batched `UNWIND` (e.g. 5k IDs/tx), not per-recipe.
- ES: `_bulk` in chunks (e.g. 1–5k ops), not per-doc `_update`.
- `disable-by-query` at 1M: resolve IDs in pages, process in batches, return a
  job summary (counts) rather than blocking on the whole set inline. A CLI
  (`scripts/disable_recipes.py`) is the primary tool for the largest operations;
  the endpoint covers moderate sets. **No silent caps** — log total matched vs
  processed.

## Testing (mirror `tests/test_param_search.py` — string-assert + patch(run_query))

1. Each of the 10 read sites: assert the generated Cypher/ES body contains the
   status filter. Pure-function query builders (`build_es_query`,
   `build_param_search_cypher`) asserted directly; `run_query`-based paths via
   `patch(...run_query)` + assert the query string.
2. `set_recipe_status`: patch `run_query`, assert the UNWIND/SET query + params +
   returned count; enable clears `disabled_at`.
3. ES bulk sync: assert both indices targeted and NDJSON action/doc lines shape.
4. Backward-compat: a recipe with no `status` property is treated as active
   (the `coalesce(...,'active')` / absent-term behavior).

## Non-goals (v1)

- Auth on the endpoints (flagged; separate decision).
- Hard delete (this is soft-delete only).
- Filtering the attach-only `*_by_ids` enrichment helpers.
- A UI (endpoints + CLI only).

## Rollout

1. Add ES `status` field to both mappings + include in reindex; backfill via a
   one-time `_bulk` set-active (or rely on coalesce/absent-term default).
2. Ship read-site filters + write path + endpoints + CLI together (filters are
   harmless before anything is disabled).
3. No data migration required (missing status == active by convention).

---

## Management UI (repo: wisefood-ui — Vue/Nuxt)

The recipe console at `app/pages/console/assets/recipes/` gets disable/enable.

**API layer — `app/services/recipeApi.ts`:**
- Add `status?: 'active' | 'disabled'` to the `Recipe` interface.
- New methods: `disableRecipe(id, reason?)`, `enableRecipe(id)` (→ the new
  `POST /recipes/{id}/disable|enable`), and optionally `disableRecipes(ids, reason?)`
  for bulk from a multi-select.

**List page — `console/assets/recipes/index.vue`:**
- Add a **status column / badge** (Active vs Disabled) to `recipeColumns`.
- Add a per-row **Disable / Enable** action button (mirrors the existing row
  action buttons at lines 154-248), calling the new API + refreshing the row.
- Optionally a **status filter** (show active / disabled / all) in
  `RecipeFilters.vue`, and a **bulk-disable** action if the table supports
  multi-select.
- Confirmation dialog before disabling (destructive-ish, reversible).

**Detail page — `console/assets/recipes/[id].vue`:**
- Show the status; add a Disable/Enable toggle with the reason field.

**Consumer note:** the public/browse recipe views
(`app/pages/recipe-wrangler/`) need NO change — the backend already excludes
disabled recipes from all serving endpoints, so they simply stop appearing.
Only the **console** (admin) surfaces disabled recipes, because it will call
search with an explicit "include disabled" flag (add an optional
`include_disabled` passthrough to the console's search call + the backend
search filter, so admins can still find and re-enable them).

**UI testing:** follow the repo's existing component-test pattern (check for
vitest/@vue/test-utils); at minimum assert the API methods hit the right paths
and the status badge/action renders per row state.

### Backend addition for the console
The console must be able to *see* disabled recipes to re-enable them. Add an
optional `include_disabled: bool = False` to the search filters
(`RecipeSearchFilters`) and, when true, skip the status `must_not`/predicate in
the search read sites (#2, #3). All other serving paths remain
disabled-excluding unconditionally.
