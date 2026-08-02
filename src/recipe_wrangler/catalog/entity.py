"""Entity base classes for the catalog.

Ported from wisefood-data-api's ``entity.py``, with one structural fix.

There, ``Entity.validate_existence`` is a hardcoded ``if/elif`` ladder over every
entity type in the system, living on the base class that every entity inherits
from. Adding an entity means editing the base class of everything, and the base
class has to import knowledge of its own subclasses. Here entities register
themselves in ``ENTITY_REGISTRY`` at construction and existence checks are a
dictionary lookup, so the base class knows nothing about its subclasses.

The template-method split is kept because it is the good idea in the original:
the base owns the bundler methods (identity resolution, cache invalidation,
system fields, re-read after write) and subclasses implement only the store
operations. Anything true of all entities is written once.

Timestamps are always timezone-aware UTC — data-api mixes ``datetime.now()`` and
``datetime.utcnow()`` into the same field.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Iterable, Sequence

from recipe_wrangler.catalog.elastic import get_catalog_client, utcnow

logger = logging.getLogger(__name__)


class EntityError(RuntimeError):
    """Base class for entity-layer failures."""


class NotFoundError(EntityError):
    pass


class ConflictError(EntityError):
    pass


class ValidationError(EntityError):
    pass


# Populated by Entity.__init__; keyed by the URN type segment.
ENTITY_REGISTRY: dict[str, "Entity"] = {}


def urn_type(urn: str) -> str:
    """Extract the type segment from ``urn:<type>:<slug>``."""
    parts = str(urn or "").split(":")
    if len(parts) < 3 or parts[0] != "urn":
        raise ValidationError(f"malformed URN: {urn!r}")
    return parts[1]


def validate_existence(urn: str) -> None:
    """Assert that the entity a URN names exists.

    A registry lookup, not a ladder: an entity type the registry has never
    heard of is a programming error, not a silent pass.
    """
    kind = urn_type(urn)
    entity = ENTITY_REGISTRY.get(kind)
    if entity is None:
        raise ValidationError(f"no entity registered for URN type {kind!r}")
    if not entity.exists(urn):
        raise NotFoundError(f"{kind} {urn} not found")


class Entity:
    """An Elasticsearch-backed resource addressed by URN."""

    def __init__(self, *, name: str, alias: str, register: bool = True):
        """
        :param name: URN type segment, e.g. ``recipe`` for ``urn:recipe:abc``.
        :param alias: the index alias this entity's documents live in.
        """
        self.name = name
        self.alias = alias
        if register:
            ENTITY_REGISTRY[name] = self

    # ------------------------------------------------------------------ #
    # Infrastructure
    # ------------------------------------------------------------------ #
    @property
    def es(self):
        # Resolved per call rather than stored, so importing an entity module
        # never opens a connection.
        return get_catalog_client()

    def make_urn(self, identifier: object) -> str:
        """Build this entity's URN from a bare identifier or pass one through."""
        value = str(identifier or "").strip()
        if not value:
            raise ValidationError(f"empty identifier for {self.name}")
        if value.startswith(f"urn:{self.name}:"):
            return value
        if value.startswith("urn:"):
            raise ValidationError(
                f"{value!r} is not a {self.name} URN"
            )
        return f"urn:{self.name}:{value}"

    def local_id(self, urn: str) -> str:
        """The identifier without the URN prefix."""
        return self.make_urn(urn).split(":", 2)[2]

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def exists(self, identifier: object) -> bool:
        return self.es.exists(self.alias, self.make_urn(identifier))

    def get(self, identifier: object) -> dict[str, Any] | None:
        return self.es.get(self.alias, self.make_urn(identifier))

    def get_or_raise(self, identifier: object) -> dict[str, Any]:
        doc = self.get(identifier)
        if doc is None:
            raise NotFoundError(f"{self.name} {self.make_urn(identifier)} not found")
        return doc

    def count(self, query: dict[str, Any] | None = None) -> int:
        return self.es.count(self.alias, query)

    def search(self, **kwargs: Any) -> dict[str, Any]:
        return self.es.search(self.alias, **kwargs)

    def scroll_all(self, **kwargs: Any) -> Iterable[dict[str, Any]]:
        return self.es.scroll_all(self.alias, **kwargs)

    # ------------------------------------------------------------------ #
    # System fields
    # ------------------------------------------------------------------ #
    def upsert_system_fields(
        self, doc: dict[str, Any], *, update: bool = False
    ) -> dict[str, Any]:
        """Stamp identity, status and timestamps.

        On update, ``creator`` and ``created_at`` are dropped rather than
        trusted from the caller — a patch must not be able to rewrite who
        created a record or when.
        """
        doc = dict(doc)
        if update:
            doc.pop("creator", None)
            doc.pop("created_at", None)
        else:
            doc.setdefault("id", str(uuid.uuid4()))
            doc.setdefault("status", "active")
            doc["created_at"] = utcnow()
        doc["updated_at"] = utcnow()
        return doc

    def validate(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Hook for subclasses; returns the document to be written."""
        return doc

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def create(
        self, doc: dict[str, Any], *, creator: str | None = None, overwrite: bool = False
    ) -> dict[str, Any]:
        urn = self.make_urn(doc.get("urn") or doc.get("id"))
        if not overwrite and self.exists(urn):
            raise ConflictError(f"{self.name} {urn} already exists")
        payload = dict(doc)
        payload["urn"] = urn
        if creator:
            payload["creator"] = creator
        payload = self.upsert_system_fields(payload, update=False)
        payload = self.validate(payload)
        self.es.index(self.alias, urn, payload)
        return self.get_or_raise(urn)

    def patch(
        self,
        identifier: object,
        changes: dict[str, Any],
        *,
        refresh: str = "wait_for",
        reread: bool = True,
    ) -> dict[str, Any] | None:
        """Apply a partial update.

        ``refresh`` defaults to ``wait_for`` so an interactive PATCH can read
        its own write. Batch callers must pass ``refresh="false"`` and refresh
        once at the end: the index refreshes every 5s, so waiting per document
        caps throughput at well under one write per second — which turned a
        7,221-recipe annotation pass into a ~16-hour job.

        ``reread=False`` additionally skips the confirmation GET, halving the
        round-trips when the caller does not need the stored document back.
        """
        urn = self.make_urn(identifier)
        if not self.exists(urn):
            raise NotFoundError(f"{self.name} {urn} not found")
        partial = {
            k: v for k, v in changes.items() if k not in {"urn", "id", "created_at"}
        }
        if not partial:
            return self.get_or_raise(urn) if reread else None
        partial = self.upsert_system_fields(partial, update=True)
        self.es.update(self.alias, urn, partial, refresh=refresh)
        return self.get_or_raise(urn) if reread else None

    def bulk_upsert(
        self, docs: Iterable[dict[str, Any]], *, refresh: bool = False, **kwargs: Any
    ) -> tuple[int, list[dict[str, Any]]]:
        """Write many documents through the same validation as single writes.

        data-api has no bulk path at all, so backfills there bypass the entity
        layer and its guarantees entirely. This does not.
        """

        def prepared():
            for doc in docs:
                urn = self.make_urn(doc.get("urn") or doc.get("id"))
                payload = dict(doc)
                payload["urn"] = urn
                payload = self.upsert_system_fields(payload, update=False)
                yield urn, self.validate(payload)

        return self.es.bulk_index(self.alias, prepared(), refresh=refresh, **kwargs)

    def set_status(
        self, identifier: object, status: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        changes: dict[str, Any] = {"status": status}
        if status == "disabled":
            changes["disabled_at"] = utcnow()
            if reason:
                changes["disabled_reason"] = reason
        return self.patch(identifier, changes)

    def delete(self, identifier: object) -> bool:
        return self.es.delete(self.alias, self.make_urn(identifier))

    def enhance(
        self,
        identifier: object,
        *,
        fields: dict[str, Any],
        agent: str,
        run_id: str | None = None,
        refresh: str = "wait_for",
        reread: bool = True,
    ) -> dict[str, Any] | None:
        """Apply AI-derived fields with a provenance record.

        The audit entry captures before/after so a model's contribution is
        always separable from curated content and always reversible.
        """
        urn = self.make_urn(identifier)
        current = self.get_or_raise(urn)
        event = {
            "agent": agent,
            "run_id": run_id or str(uuid.uuid4()),
            "enhanced_at": utcnow(),
            "fields": sorted(fields),
            "before": {k: current.get(k) for k in fields},
            "after": dict(fields),
        }
        self.es.enhance(
            self.alias, urn, fields=fields, enhancement_event=event, refresh=refresh
        )
        return self.get_or_raise(urn) if reread else None


class DependentEntity(Entity):
    """An entity that exists only in relation to a parent.

    Its URN embeds the parent's identity plus a discriminator, so the grain is
    visible in the key: ``urn:recipe_profile:<recipe_id>:<nutrition_source>``.
    """

    def __init__(
        self,
        *,
        name: str,
        alias: str,
        parent_field: str,
        parent_type: str,
        register: bool = True,
    ):
        super().__init__(name=name, alias=alias, register=register)
        self.parent_field = parent_field
        self.parent_type = parent_type

    def make_urn(self, identifier: object) -> str:
        value = str(identifier or "").strip()
        if not value:
            raise ValidationError(f"empty identifier for {self.name}")
        if value.startswith(f"urn:{self.name}:"):
            return value
        return f"urn:{self.name}:{value}"

    def compose_urn(self, parent_id: str, discriminator: str) -> str:
        return f"urn:{self.name}:{parent_id}:{discriminator}"

    def parent_urn_for(self, parent_id: str) -> str:
        return f"urn:{self.parent_type}:{parent_id}"

    def ensure_parent_exists(self, parent_urn: str) -> None:
        if urn_type(parent_urn) != self.parent_type:
            raise ValidationError(
                f"{parent_urn!r} is not a {self.parent_type} URN"
            )
        validate_existence(parent_urn)

    def list_for_parent(
        self, parent_urn: str, *, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        result = self.search(
            filters=[{"term": {self.parent_field: parent_urn}}],
            limit=limit,
            offset=offset,
            include_inactive=True,
        )
        return result["results"]

    def delete_for_parent(self, parent_urn: str) -> int:
        """Remove every dependent document belonging to a parent.

        Nothing does this today, which is why 150 nutrition profiles outlived
        the recipes they describe.
        """
        removed = 0
        for hit in self.scroll_all(
            query={"term": {self.parent_field: parent_urn}}, source=False
        ):
            if self.es.delete(self.alias, hit["_id"], refresh="false"):
                removed += 1
        if removed:
            self.es.refresh(self.alias)
        return removed
