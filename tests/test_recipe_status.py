"""Recipe soft-delete (status) tests.

Read sites are string-asserted against the generated Cypher/ES bodies
(mirroring test_param_search.py); the write path patches run_query.
"""

import unittest
from unittest.mock import patch

from recipe_wrangler.schemas import RecipeSearchFilters
from recipe_wrangler.tools.es_recipe_search import (
    RecipeSearchConstraints,
    build_es_query,
)
from recipe_wrangler.tools.param_search import build_param_search_cypher
from recipe_wrangler.utils.recipe_status import (
    NEO4J_NOT_DISABLED,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    es_not_disabled_clause,
)

_ES_STATUS_CLAUSE = {"term": {"status": "disabled"}}


class ReadSiteFilterTests(unittest.TestCase):
    def test_param_search_filters_disabled_in_results_and_facets(self):
        query, facet_query, _ = build_param_search_cypher(
            RecipeSearchFilters(dish_types=["breakfast"], include_facets=True)
        )
        self.assertIn(NEO4J_NOT_DISABLED, query)
        self.assertIn(NEO4J_NOT_DISABLED, facet_query)

    def test_param_search_include_disabled_skips_filter(self):
        query, _, _ = build_param_search_cypher(
            RecipeSearchFilters(dish_types=["breakfast"], include_disabled=True)
        )
        self.assertNotIn(NEO4J_NOT_DISABLED, query)

    def test_param_search_results_carry_status(self):
        query, _, _ = build_param_search_cypher(RecipeSearchFilters(limit=5))
        self.assertIn("coalesce(r.status, 'active') AS status", query)

    def test_es_query_excludes_disabled_by_default(self):
        body = build_es_query(RecipeSearchConstraints())
        self.assertIn(_ES_STATUS_CLAUSE, body["query"]["bool"]["must_not"])

    def test_es_query_include_disabled_skips_clause(self):
        body = build_es_query(RecipeSearchConstraints(include_disabled=True))
        self.assertNotIn(_ES_STATUS_CLAUSE, body["query"]["bool"]["must_not"])

    def test_es_query_returns_status_field(self):
        body = build_es_query(RecipeSearchConstraints())
        self.assertIn("status", body["_source"])

    def test_foodchat_candidates_query_excludes_disabled(self):
        captured: list[str] = []

        def mock_run_query(query, params=None):
            captured.append(query)
            return []

        class _Req:
            class user_profile:
                allergies = []
                diet = []

            class constraints:
                exclude_ingredients = []
                include_ingredients = []
                exclude_recipe_ids = []
                favorite_recipe_ids = []
                nutrition_profile = None

            quotas = {"breakfast": 1}
            randomize = True

        with patch(
            "recipe_wrangler.repositories.neo4j_recipes.run_query",
            side_effect=mock_run_query,
        ):
            from recipe_wrangler.repositories.neo4j_recipes import fetch_foodchat_candidates

            fetch_foodchat_candidates(_Req)

        self.assertTrue(captured)
        self.assertIn(NEO4J_NOT_DISABLED, captured[0])

    def test_recipe_info_queries_exclude_disabled_unless_opted_in(self):
        from recipe_wrangler.tools import fetch_recipe_info as mod

        captured: list[str] = []

        def mock_run_query(query, params=None):
            captured.append(query)
            return []

        with patch.object(mod, "run_query", side_effect=mock_run_query):
            mod.fetch_recipe_info_by_id("abc")
            mod.fetch_recipe_info_by_id("abc", include_disabled=True)
            mod.fetch_recipe_info_by_ids(["abc"])
            mod.fetch_recipe_info_by_ids(["abc"], include_disabled=True)

        self.assertIn(NEO4J_NOT_DISABLED, captured[0])
        self.assertNotIn(NEO4J_NOT_DISABLED, captured[1])
        self.assertIn(NEO4J_NOT_DISABLED, captured[2])
        self.assertNotIn(NEO4J_NOT_DISABLED, captured[3])

    def test_count_recipes_excludes_disabled(self):
        from recipe_wrangler.repositories import neo4j_recipes as mod

        captured: list[str] = []
        with patch.object(
            mod, "run_query",
            side_effect=lambda q, *a, **k: captured.append(q) or [{"total": 0}],
        ):
            mod.count_recipes()
        self.assertIn(NEO4J_NOT_DISABLED, captured[0])

    def test_backward_compat_predicate_treats_missing_status_as_active(self):
        # The convention every read site relies on: no migration needed.
        self.assertIn("coalesce(r.status, 'active')", NEO4J_NOT_DISABLED)
        self.assertEqual(es_not_disabled_clause(), _ES_STATUS_CLAUSE)


class WritePathTests(unittest.TestCase):
    def test_set_recipe_status_disable_sets_fields_and_returns_ids(self):
        from recipe_wrangler.repositories import neo4j_recipes as mod

        calls: list[tuple[str, dict]] = []

        def mock_run_query(query, params=None):
            calls.append((query, params))
            return [{"recipe_id": rid} for rid in params["ids"]]

        with patch.object(mod, "run_query", side_effect=mock_run_query):
            updated = mod.set_recipe_status(["a", "b", "a", ""], STATUS_DISABLED, "spam")

        self.assertEqual(updated, ["a", "b"])  # de-duped, empties dropped
        query, params = calls[0]
        self.assertIn("UNWIND $ids AS rid", query)
        self.assertIn("SET r.status = $status", query)
        self.assertIn("r.disabled_at", query)
        self.assertIn("r.disabled_reason", query)
        self.assertEqual(params["status"], STATUS_DISABLED)
        self.assertEqual(params["reason"], "spam")

    def test_set_recipe_status_enable_clears_disabled_fields(self):
        from recipe_wrangler.repositories import neo4j_recipes as mod

        with patch.object(
            mod, "run_query",
            side_effect=lambda q, p: [{"recipe_id": rid} for rid in p["ids"]],
        ) as mock_rq:
            mod.set_recipe_status(["a"], STATUS_ACTIVE)

        query, params = mock_rq.call_args[0]
        # CASE WHEN $status = 'disabled' guards mean enable nulls both fields.
        self.assertIn("ELSE null END", query)
        self.assertEqual(params["status"], STATUS_ACTIVE)
        self.assertIsNone(params["reason"])

    def test_set_recipe_status_batches_large_id_sets(self):
        from recipe_wrangler.repositories import neo4j_recipes as mod

        ids = [f"r{i}" for i in range(mod._STATUS_BATCH_SIZE + 1)]
        with patch.object(
            mod, "run_query",
            side_effect=lambda q, p: [{"recipe_id": rid} for rid in p["ids"]],
        ) as mock_rq:
            updated = mod.set_recipe_status(ids, STATUS_DISABLED)

        self.assertEqual(mock_rq.call_count, 2)
        self.assertEqual(len(updated), len(ids))


class EsSyncTests(unittest.TestCase):
    def test_bulk_sync_targets_every_index_with_ndjson(self):
        from recipe_wrangler.utils import recipe_status as mod

        posted: list[tuple[str, str]] = []

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"items": [{"update": {"status": 200}}]}

        def mock_post(url, data=None, json=None, headers=None, timeout=None):
            posted.append((url, data or ""))
            return _Resp()

        def mock_put(url, json=None, timeout=None):
            return _Resp()

        with patch.object(mod.requests, "post", side_effect=mock_post), \
             patch.object(mod.requests, "put", side_effect=mock_put):
            stats = mod.sync_recipe_status_to_es(
                ["r1"], STATUS_DISABLED,
                es_url="http://es:9200", indices=["recipes_v2", "recipes"],
            )

        self.assertEqual(len(posted), 2)  # one _bulk per index
        for _, body in posted:
            self.assertIn('"update"', body)
            self.assertIn('"status": "disabled"', body)
        self.assertIn('"_index": "recipes_v2"', posted[0][1])
        self.assertIn('"_index": "recipes"', posted[1][1])
        self.assertEqual(stats["recipes_v2"]["updated"], 1)

    def test_bulk_sync_counts_missing_docs_without_failing(self):
        from recipe_wrangler.utils import recipe_status as mod

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"items": [{"update": {"status": 404}}]}

        with patch.object(mod.requests, "post", return_value=_Resp()), \
             patch.object(mod.requests, "put", return_value=_Resp()):
            stats = mod.sync_recipe_status_to_es(
                ["missing"], STATUS_ACTIVE, es_url="http://es:9200", indices=["recipes_v2"],
            )
        self.assertEqual(stats["recipes_v2"]["not_found"], 1)
        self.assertEqual(stats["recipes_v2"]["errors"], 0)

    def test_bulk_sync_sets_retry_on_conflict_on_every_action(self):
        from recipe_wrangler.utils import recipe_status as mod

        posted: list[str] = []

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"items": [{"update": {"status": 200}}]}

        def mock_post(url, data=None, json=None, headers=None, timeout=None):
            posted.append(data or "")
            return _Resp()

        with patch.object(mod.requests, "post", side_effect=mock_post), \
             patch.object(mod.requests, "put", return_value=_Resp()):
            mod.sync_recipe_status_to_es(
                ["r1"], STATUS_DISABLED, es_url="http://es:9200", indices=["recipes_v2"],
            )
        self.assertIn('"retry_on_conflict"', posted[0])

    def test_bulk_sync_counts_version_conflicts_separately(self):
        """Racing same-status writers converge — 409s must not count as errors
        (or log per-doc; two corpus-scale jobs once flooded the pod log buffer)."""
        from recipe_wrangler.utils import recipe_status as mod

        conflict = {
            "update": {
                "_id": "r1", "status": 409,
                "error": {"type": "version_conflict_engine_exception"},
            }
        }

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"items": [conflict]}

        with patch.object(mod.requests, "post", return_value=_Resp()), \
             patch.object(mod.requests, "put", return_value=_Resp()):
            stats = mod.sync_recipe_status_to_es(
                ["r1"], STATUS_DISABLED, es_url="http://es:9200", indices=["recipes_v2"],
            )
        self.assertEqual(stats["recipes_v2"]["conflicts"], 1)
        self.assertEqual(stats["recipes_v2"]["errors"], 0)


class StatusJobSingleFlightTests(unittest.TestCase):
    def test_second_claim_rejected_while_first_holds_slot(self):
        from recipe_wrangler.utils.recipe_status import StatusJobGuard

        guard = StatusJobGuard()
        self.assertIsNone(guard.try_claim(STATUS_DISABLED, 100))
        running = guard.try_claim(STATUS_DISABLED, 50)
        self.assertIsNotNone(running)
        self.assertEqual(running["status"], STATUS_DISABLED)
        self.assertEqual(running["requested"], 100)  # the FIRST job's info

    def test_release_frees_the_slot(self):
        from recipe_wrangler.utils.recipe_status import StatusJobGuard

        guard = StatusJobGuard()
        self.assertIsNone(guard.try_claim(STATUS_DISABLED, 1))
        guard.release()
        self.assertIsNone(guard.try_claim(STATUS_ACTIVE, 1))


if __name__ == "__main__":
    unittest.main()
