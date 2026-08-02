"""The catalog's Elasticsearch client.

Ported from wisefood-data-api's ``backend/elastic.py`` with four deliberate
differences, each of which was a defect there:

1. **Partial updates.** data-api's ``update_entity`` does GET → merge in Python
   → write the whole document back. That is two round-trips and a lost-update
   race: two concurrent patches both read the old document and the second
   overwrites the first's field. Here updates go through ES's own partial
   update API, which merges server-side under the document lock.

2. **Cached facet fields.** data-api calls ``indices.get_mapping`` on *every*
   search to work out which fields are facetable — a network round-trip per
   query. Here the result is cached per index and invalidated on rebuild.

3. **Weighted search fields.** ``multi_match {"fields": ["*"]}`` scores a term
   in ``instructions`` as highly as one in ``title``. Fields are weighted
   explicitly, with ``all_text`` as the catch-all.

4. **UTC everywhere.** data-api mixes ``datetime.now()`` and
   ``datetime.utcnow()`` into the same ``updated_at`` field — naive-local and
   naive-UTC values in one column. Timestamps here are always aware UTC.

Transport
---------
This speaks the Elasticsearch HTTP API over the repo's shared pooled
``requests`` session rather than ``elasticsearch-py``. That is not a
preference: ``pyproject.toml`` declares ``elasticsearch>=9.3.0`` while the
deployed server is 8.14.3 (the compose file pins 8.15.0), and a 9.x client
sends a ``compatible-with=9`` media type that an 8.x server rejects outright —
even ``info()`` fails with ``media_type_header_exception``. No module in the
repo has ever imported the client; every existing ES call already uses
``requests``. This keeps that convention and drops a dependency that cannot
talk to the cluster.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from recipe_wrangler.api.config import get_settings
from recipe_wrangler.catalog.es_schema import INDEX_DEFINITIONS
from recipe_wrangler.utils.http_pool import get_http_session

logger = logging.getLogger(__name__)

JSON_HEADERS = {"Content-Type": "application/json"}
NDJSON_HEADERS = {"Content-Type": "application/x-ndjson"}

# Fields that must never be offered as facets: free text, vectors, audit blobs.
FACET_EXCLUDE = frozenset(
    {
        "all_text", "title", "description", "instructions",
        "embedding", "embedding_text", "embedding_model", "embedded_at",
        "content_digest", "extras", "url", "image_url", "disabled_reason",
        "created_at", "updated_at", "profiled_at", "disabled_at",
        "urn", "id", "recipe_id", "external_id", "source_id",
        "title_normalized",
    }
)

FACETABLE_TYPES = frozenset({"keyword", "boolean", "byte", "short", "integer"})

# Relative weights for free-text search. Deliberately explicit: a term in the
# title means far more than the same term appearing in a description.
#
# `instructions` is absent by design. Method text is dominated by generic
# cooking nouns, so including it retrieves every recipe that happens to mention
# an ingredient rather than the recipes actually about it. It is neither listed
# here nor copied into `all_text`; callers wanting it must ask explicitly via
# the `search_fields` argument.
DEFAULT_SEARCH_FIELDS: tuple[str, ...] = (
    "title^6",
    "title.autocomplete^2",
    "ingredients.name^3",
    "tags^2",
    "course_types^2",
    "cuisines^2",
    "description",
    "all_text",
)

# Served-to-users predicate. RecipeWrangler soft-deletes with `disabled`;
# missing status means active, per the soft-delete spec.
INACTIVE_STATUSES: tuple[str, ...] = ("disabled", "deleted", "archived")


def utcnow() -> str:
    """Timezone-aware UTC timestamp — the only clock the catalog uses."""
    return datetime.now(timezone.utc).isoformat()


class ElasticError(RuntimeError):
    """An Elasticsearch request failed."""

    def __init__(self, status: int, body: str, *, method: str, path: str):
        super().__init__(f"{method} {path} -> {status}: {body[:400]}")
        self.status = status
        self.body = body


class CatalogElasticClient:
    """Thread-safe singleton over the Elasticsearch HTTP API."""

    _instance: "CatalogElasticClient | None" = None
    _lock = threading.Lock()

    def __new__(cls) -> "CatalogElasticClient":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._init()
                    cls._instance = instance
        return cls._instance

    def _init(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._base = settings.elastic_url.rstrip("/")
        self._timeout = max(settings.elastic_timeout, 30.0)
        self._facet_cache: dict[str, dict[str, str]] = {}
        self._facet_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        raw_body: str | None = None,
        timeout: float | None = None,
        ok_statuses: tuple[int, ...] = (200, 201),
    ) -> dict[str, Any]:
        url = f"{self._base}/{path.lstrip('/')}"
        data = raw_body if raw_body is not None else (
            json.dumps(body) if body is not None else None
        )
        response = get_http_session().request(
            method,
            url,
            data=data.encode() if isinstance(data, str) else data,
            params=params,
            headers=headers or (JSON_HEADERS if data is not None else None),
            timeout=timeout or self._timeout,
        )
        if response.status_code not in ok_statuses:
            raise ElasticError(
                response.status_code, response.text, method=method, path=path
            )
        if not response.content:
            return {}
        return response.json()

    def _head(self, path: str) -> bool:
        response = get_http_session().head(
            f"{self._base}/{path.lstrip('/')}", timeout=self._timeout
        )
        return response.status_code == 200

    def ping(self) -> dict[str, Any]:
        return self._request("GET", "/")

    # ------------------------------------------------------------------ #
    # Bootstrap
    # ------------------------------------------------------------------ #
    def alias_exists(self, alias: str) -> bool:
        return self._head(f"_alias/{alias}")

    def index_exists(self, index: str) -> bool:
        return self._head(index)

    def ensure_indices(self, *, aliases: dict[str, str] | None = None) -> None:
        """Create any missing catalog index behind its alias.

        Unlike data-api this does not run at import time. Creating indices as a
        side effect of importing a module makes tests, CLIs and migrations all
        pay for a cluster round-trip, and hides ordering bugs.
        """
        settings = self._settings
        aliases = aliases or {
            "recipes": settings.catalog_recipes_alias,
            "recipe_profiles": settings.catalog_profiles_alias,
        }
        for kind, alias in aliases.items():
            if self.alias_exists(alias) or self.index_exists(alias):
                continue
            concrete = f"{alias}_v1"
            body = INDEX_DEFINITIONS[kind](settings.catalog_embedding_dim)
            logger.info("creating index %s behind alias %s", concrete, alias)
            self._request("PUT", concrete, body=body)
            self._request("PUT", f"{concrete}/_alias/{alias}")

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def get(self, index: str, doc_id: str) -> dict[str, Any] | None:
        try:
            return self._request("GET", f"{index}/_doc/{doc_id}")["_source"]
        except ElasticError as exc:
            if exc.status == 404:
                return None
            raise

    def exists(self, index: str, doc_id: str) -> bool:
        return self._head(f"{index}/_doc/{doc_id}")

    def count(self, index: str, query: dict[str, Any] | None = None) -> int:
        body = {"query": query} if query else None
        return int(self._request("POST", f"{index}/_count", body=body)["count"])

    @staticmethod
    def active_query() -> dict[str, Any]:
        return {"bool": {"must_not": [{"terms": {"status": list(INACTIVE_STATUSES)}}]}}

    def scroll_all(
        self,
        index: str,
        *,
        query: dict[str, Any] | None = None,
        source: Sequence[str] | bool = True,
        page_size: int = 1000,
    ) -> Iterable[dict[str, Any]]:
        """Stream every matching document via search_after.

        search_after rather than from/size: it has no result-window ceiling and
        no scroll context to leak if a consumer stops early.
        """
        after: list[Any] | None = None
        while True:
            body: dict[str, Any] = {
                "size": page_size,
                "sort": [{"_doc": "asc"}],
                "query": query or {"match_all": {}},
            }
            if source is not True:
                body["_source"] = source
            if after:
                body["search_after"] = after
            hits = self._request("POST", f"{index}/_search", body=body)["hits"]["hits"]
            if not hits:
                return
            for hit in hits:
                yield hit
            after = hits[-1]["sort"]

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def index(
        self,
        index: str,
        doc_id: str,
        document: dict[str, Any],
        *,
        refresh: str = "wait_for",
    ) -> None:
        self._request(
            "PUT", f"{index}/_doc/{doc_id}", body=document, params={"refresh": refresh}
        )

    def update(
        self,
        index: str,
        doc_id: str,
        partial: dict[str, Any],
        *,
        refresh: str = "wait_for",
        upsert: bool = False,
    ) -> None:
        """Merge ``partial`` into the stored document server-side.

        This is the fix for data-api's read-merge-write: no second round-trip,
        and no lost update when two patches race.
        """
        body: dict[str, Any] = {"doc": partial}
        if upsert:
            body["doc_as_upsert"] = True
        self._request(
            "POST", f"{index}/_update/{doc_id}", body=body, params={"refresh": refresh}
        )

    def delete(self, index: str, doc_id: str, *, refresh: str = "wait_for") -> bool:
        try:
            self._request(
                "DELETE", f"{index}/_doc/{doc_id}", params={"refresh": refresh}
            )
            return True
        except ElasticError as exc:
            if exc.status == 404:
                return False
            raise

    def bulk_index(
        self,
        index: str,
        documents: Iterable[tuple[str, dict[str, Any]]],
        *,
        chunk_size: int | None = None,
        refresh: bool = False,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Index (doc_id, document) pairs in bulk.

        ``refresh=False`` by default: a backfill that waits for a refresh per
        document turns minutes into hours. Refresh once when the run finishes.
        """
        chunk = chunk_size or self._settings.catalog_bulk_chunk_size
        succeeded = 0
        failures: list[dict[str, Any]] = []
        batch: list[str] = []
        pending = 0

        def flush() -> None:
            nonlocal batch, pending, succeeded
            if not batch:
                return
            result = self._request(
                "POST",
                "_bulk",
                raw_body="".join(batch),
                headers=NDJSON_HEADERS,
                timeout=max(self._timeout, 120.0),
            )
            for item in result.get("items", []):
                outcome = item.get("index") or item.get("create") or {}
                if outcome.get("error"):
                    failures.append(outcome)
                else:
                    succeeded += 1
            batch = []
            pending = 0

        for doc_id, doc in documents:
            batch.append(json.dumps({"index": {"_index": index, "_id": doc_id}}) + "\n")
            batch.append(json.dumps(doc) + "\n")
            pending += 1
            if pending >= chunk:
                flush()
        flush()

        if refresh:
            self.refresh(index)
        if failures:
            logger.error(
                "bulk_index: %s failure(s), first: %s", len(failures), failures[0]
            )
        return succeeded, failures

    def search_body(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        """Run a caller-built query body and return the raw response.

        The keyword-argument `search` below covers the shapes the catalog
        endpoints need. This is for callers that build their own query —
        function_score, nested boosts, multi-key sorts — where expressing it
        through named parameters would mean adding a parameter per query shape.
        """
        return self._request("POST", f"{index}/_search", body=body)

    def refresh(self, index: str) -> None:
        self._request("POST", f"{index}/_refresh")

    def force_merge(self, index: str, *, only_expunge_deletes: bool = True) -> None:
        self._request(
            "POST",
            f"{index}/_forcemerge",
            params={
                "only_expunge_deletes": str(only_expunge_deletes).lower(),
                "wait_for_completion": "true",
            },
            timeout=1800.0,
        )

    def enhance(
        self,
        index: str,
        doc_id: str,
        *,
        fields: dict[str, Any],
        enhancement_event: dict[str, Any],
        refresh: str = "wait_for",
    ) -> None:
        """Apply AI-derived fields and append the audit entry atomically.

        One painless script, so the values and the record of where they came
        from can never be written apart from each other.
        """
        self._request(
            "POST",
            f"{index}/_update/{doc_id}",
            params={"refresh": refresh},
            body={
                "script": {
                    "lang": "painless",
                    "source": """
                        if (ctx._source.enhancements == null) { ctx._source.enhancements = []; }
                        ctx._source.enhancements.add(params.event);
                        if (ctx._source.ai_generated_fields == null) { ctx._source.ai_generated_fields = []; }
                        for (entry in params.fields.entrySet()) {
                            ctx._source[entry.getKey()] = entry.getValue();
                            if (!ctx._source.ai_generated_fields.contains(entry.getKey())) {
                                ctx._source.ai_generated_fields.add(entry.getKey());
                            }
                        }
                        ctx._source.updated_at = params.updated_at;
                    """,
                    "params": {
                        "event": enhancement_event,
                        "fields": fields,
                        "updated_at": utcnow(),
                    },
                }
            },
        )

    # ------------------------------------------------------------------ #
    # Facets
    # ------------------------------------------------------------------ #
    def facet_fields(self, index: str) -> dict[str, str]:
        """Facetable fields for an index, derived from the mapping and cached.

        A new keyword field becomes facetable automatically — but the mapping is
        read once per index, not once per query.
        """
        cached = self._facet_cache.get(index)
        if cached is not None:
            return cached
        with self._facet_lock:
            cached = self._facet_cache.get(index)
            if cached is not None:
                return cached
            mapping = self._request("GET", f"{index}/_mapping")
            # With an alias this returns the concrete index as the sole key.
            props = next(iter(mapping.values()))["mappings"].get("properties", {})
            fields = {
                name: spec["type"]
                for name, spec in props.items()
                if spec.get("type") in FACETABLE_TYPES and name not in FACET_EXCLUDE
            }
            self._facet_cache[index] = fields
            return fields

    def invalidate_facet_cache(self, index: str | None = None) -> None:
        with self._facet_lock:
            if index is None:
                self._facet_cache.clear()
            else:
                self._facet_cache.pop(index, None)

    # ------------------------------------------------------------------ #
    # Rebuild
    # ------------------------------------------------------------------ #
    def point_alias(self, alias: str, index: str) -> str | None:
        """Repoint an alias at an already-populated index, atomically.

        Returns the index it previously pointed at. Add and remove travel in
        one aliases action, so no request ever observes the alias resolving to
        nothing — or to both indices at once.
        """
        old: str | None = None
        actions: list[dict[str, Any]] = []
        if self.alias_exists(alias):
            old = next(iter(self._request("GET", f"_alias/{alias}").keys()))
            if old == index:
                return old
            actions.append({"remove": {"index": old, "alias": alias}})
        elif self.index_exists(alias):
            # A concrete index is occupying the alias name; it must go first.
            old = alias
            self._request("DELETE", alias)
        actions.append({"add": {"index": index, "alias": alias}})
        self._request("POST", "_aliases", body={"actions": actions})
        self.invalidate_facet_cache(alias)
        return old

    def rebuild_index(
        self,
        *,
        alias: str,
        new_index: str,
        body: dict[str, Any],
        reindex_from: str | None = None,
        delete_old: bool = False,
    ) -> str | None:
        """Create ``new_index``, optionally reindex into it, swap the alias.

        Returns the previously-aliased index, if any. The swap is a single
        atomic aliases action, so no request ever sees neither index.
        """
        old_index: str | None = None
        if self.alias_exists(alias):
            old_index = next(iter(self._request("GET", f"_alias/{alias}").keys()))
        elif self.index_exists(alias):
            old_index = alias  # one-time migration from a concrete index

        if self.index_exists(new_index):
            raise RuntimeError(f"index {new_index!r} already exists")

        self._request("PUT", new_index, body=body)

        source = reindex_from if reindex_from is not None else old_index
        if source:
            self._request(
                "POST",
                "_reindex",
                body={"source": {"index": source}, "dest": {"index": new_index}},
                params={"wait_for_completion": "true", "refresh": "true"},
                timeout=7200.0,
            )

        actions: list[dict[str, Any]] = []
        if old_index and old_index != alias:
            actions.append({"remove": {"index": old_index, "alias": alias}})
        elif old_index == alias:
            # Cannot alias over a concrete index of the same name.
            self._request("DELETE", old_index)
        actions.append({"add": {"index": new_index, "alias": alias}})
        self._request("POST", "_aliases", body={"actions": actions})

        self.invalidate_facet_cache(alias)

        if delete_old and old_index and old_index != alias:
            self._request("DELETE", old_index)
        return old_index

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #
    def search(
        self,
        index: str,
        *,
        q: str | None = None,
        filters: Sequence[dict[str, Any]] | None = None,
        should: Sequence[dict[str, Any]] | None = None,
        limit: int = 20,
        offset: int = 0,
        sort: Sequence[dict[str, Any]] | None = None,
        source_fields: Sequence[str] | None = None,
        facets: Sequence[str] | None = None,
        facet_limit: int = 30,
        include_inactive: bool = False,
        highlight_fields: Sequence[str] | None = None,
        search_fields: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """One search contract for every caller.

        Returns ``{results, facets, total, max_result_window}`` — the same
        envelope as the catalog service, so one client and one UI component
        work against both.
        """
        must: list[dict[str, Any]] = []
        if q:
            must.append(
                {
                    "multi_match": {
                        "query": q,
                        "fields": list(search_fields or DEFAULT_SEARCH_FIELDS),
                        "type": "best_fields",
                        "operator": "or",
                    }
                }
            )

        filter_clauses = list(filters or [])
        if not include_inactive:
            filter_clauses.append(self.active_query())

        body: dict[str, Any] = {
            "from": offset,
            "size": limit,
            "track_total_hits": True,
            "query": {"bool": {"must": must, "filter": filter_clauses}},
        }
        # Ranking-only clauses. Deliberately without `minimum_should_match`: a
        # `should` here must lift a document's score, never decide whether it is
        # eligible. Setting it would silently turn a favourites boost into a
        # favourites-only filter.
        if should:
            body["query"]["bool"]["should"] = list(should)
        if sort:
            body["sort"] = list(sort)
        if source_fields:
            body["_source"] = list(source_fields)
        if highlight_fields:
            body["highlight"] = {"fields": {f: {} for f in highlight_fields}}

        requested = list(facets or [])
        if requested:
            available = self.facet_fields(index)
            aggs = {
                name: {
                    "terms": {"field": name, "size": facet_limit, "min_doc_count": 1}
                }
                for name in requested
                if name in available
            }
            if aggs:
                body["aggs"] = aggs

        response = self._request("POST", f"{index}/_search", body=body)

        results = []
        for hit in response["hits"]["hits"]:
            row = dict(hit["_source"])
            row["_score"] = hit.get("_score")
            if "highlight" in hit:
                row["_highlight"] = hit["highlight"]
            results.append(row)

        facet_results = {
            name: [{"value": b["key"], "count": b["doc_count"]} for b in agg["buckets"]]
            for name, agg in (response.get("aggregations") or {}).items()
        }

        return {
            "results": results,
            "facets": facet_results,
            "total": response["hits"]["total"]["value"],
            "max_result_window": self._settings.elastic_max_result_window,
        }


def get_catalog_client() -> CatalogElasticClient:
    """Return the shared client.

    A function rather than a module-level instance: importing this module must
    not open a connection, which is what makes it usable from tests and CLIs.
    """
    return CatalogElasticClient()
