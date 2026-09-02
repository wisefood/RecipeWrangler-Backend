"""Content digests — making drift between the owners and the index detectable.

Neo4j and Postgres own a recipe; Elasticsearch holds a projection of it plus
annotations no owner can reproduce. The projection is rebuilt on every write,
which is correct in principle and unverifiable in practice: if a write failed,
or an owner changed by some path that forgot to re-project, nothing anywhere
says so. The index simply serves an older answer, indefinitely, and looks
healthy while doing it.

A digest fixes that by making the question answerable. It is computed from the
owner-owned fields only, stamped into both stores on every projection, and
compared by `scripts/maintenance/reconcile.py`. Equal digests mean the index
reflects the owners. Unequal means it does not, and says which recipe.

Three properties matter, and each is a decision rather than an accident:

**Owner fields only.** Annotations live only in Elasticsearch, so including
them would make every digest differ by construction. The digest answers "does
the index reflect its owners", not "are the two stores byte-identical" — they
never are, by design.

**Order-independent for sets.** Ingredients and tags come back from Cypher in
whatever order the planner produced. Hashing that order would report drift on
every projection of an unchanged recipe, and a checker that cries wolf is worse
than no checker: it trains you to ignore it.

**Normalised before hashing.** `2.0` and `2` are the same duration; `" Soup "`
and `"soup"` are the same tag. Without normalisation the digest reports
formatting as a content change.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

# Bumped when the field set or normalisation changes. It is part of the hash
# input, so an old digest can never accidentally compare equal to a new one —
# a version change invalidates every stored digest at once, which is what you
# want: the alternative is a silent mix of two schemes that agree by luck.
DIGEST_VERSION = "1"

# The owner-owned fields the digest covers, in a fixed order.
#
# Deliberately excludes:
#
# - every entry in `projection.ES_OWNED_FIELDS` — no owner can reproduce them,
#   so including them would make every digest differ by construction;
# - `has_profile` — derived from Postgres presence, not from content;
# - the nutrition profile — it has its own Postgres trace and changes on
#   re-profiling without the recipe changing, which would report drift for a
#   recipe nobody touched;
# - `status` — disable/enable keep Elasticsearch in step through their own
#   sync, including a bulk `_update_by_query` path that deliberately does not
#   re-project thousands of recipes one at a time. A digest covering status
#   would therefore go stale on every disable and report drift where both
#   stores actually agree. Status is verified separately in `reconcile.py`,
#   by comparing counts per state — cheap, and it cannot cry wolf.
DIGESTED_FIELDS: tuple[str, ...] = (
    "recipe_id",
    "title",
    "description",
    "instructions",
    "url",
    "image_url",
    "source",
    "source_id",
    "duration",
    "serves",
    "cost_category",
    "cost_category_code",
    "cost_category_status",
    "cost_price_coverage",
    "expert_recipe",
    "ingredients",
    "allergens",
    "tags",
    "diet_tags",
    "suitable_for",
)

# Fields whose order carries no meaning and is not stable across queries.
_SET_FIELDS: frozenset[str] = frozenset(
    {"ingredients", "allergens", "tags", "diet_tags", "suitable_for"}
)


def _normalise_scalar(value: Any) -> Any:
    """Reduce a scalar to the form the digest compares.

    Numbers collapse to int where they are whole, because Cypher returns `2.0`
    for a duration Elasticsearch stores as `2` and neither is more correct.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number) if number.is_integer() else round(number, 6)
    text = str(value).strip()
    return text or None


# Keys an object-shaped list entry might carry its value under, most specific
# first. `ingredients` is stored nested as {name, position}; other lists are
# plain strings.
_VALUE_KEYS = ("name", "value", "label", "title")


def _entry_text(item: Any) -> str:
    """The comparable text of one list entry, whatever shape it arrived in.

    The same ingredient list exists in two shapes: `["onion"]` as the
    projection builds it, and `[{"name": "onion", "position": 0}]` after
    `Recipe.validate` normalises it for the nested mapping. A digest that saw
    those as different could never be recomputed from a stored document — it
    would report drift on every recipe, forever, which is indistinguishable
    from the checker being broken.

    `position` is deliberately ignored: it is an artifact of storing an ordered
    list in a nested field, and these sets are compared order-free anyway.
    """
    if isinstance(item, dict):
        for key in _VALUE_KEYS:
            if item.get(key) is not None:
                return str(item[key])
        return ""
    return str(item)


def _normalise_set(values: Any) -> list[str]:
    """Lowercase, strip, de-duplicate and sort — an order-free comparison."""
    if not isinstance(values, (list, tuple, set)):
        return []
    cleaned = {
        text.strip().lower()
        for text in (_entry_text(item) for item in values if item is not None)
        if text.strip()
    }
    return sorted(cleaned)


def digest_payload(document: dict[str, Any]) -> dict[str, Any]:
    """The normalised subset a digest is computed over.

    Exposed separately from `content_digest` so a mismatch can be explained
    field by field rather than as two opaque hashes — `reconcile.py` diffs
    these to report *what* drifted, which is the difference between a usable
    report and an alarm.
    """
    payload: dict[str, Any] = {"_v": DIGEST_VERSION}
    for field in DIGESTED_FIELDS:
        raw = document.get(field)
        if field in _SET_FIELDS:
            payload[field] = _normalise_set(raw)
        else:
            payload[field] = _normalise_scalar(raw)
    return payload


def content_digest(document: dict[str, Any]) -> str:
    """A stable hash of a recipe's owner-owned content.

    Same content in, same digest out, regardless of which store the document
    came from or what order its lists arrived in.
    """
    payload = digest_payload(document)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def digest_differences(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    """Which digested fields differ between two documents.

    Empty means the digests agree. A caller that only needs the boolean should
    compare `content_digest` values — this exists for the reporting path, where
    "titles differ" is actionable and "digests differ" is not.
    """
    a, b = digest_payload(left), digest_payload(right)
    return [field for field in DIGESTED_FIELDS if a.get(field) != b.get(field)]


def summarise_difference(
    field: str, left: dict[str, Any], right: dict[str, Any], *, width: int = 60
) -> str:
    """A one-line, truncated before/after for a single drifted field."""
    a = digest_payload(left).get(field)
    b = digest_payload(right).get(field)

    def show(value: Any) -> str:
        text = json.dumps(value, default=str) if isinstance(value, list) else str(value)
        return text if len(text) <= width else text[: width - 1] + "…"

    return f"{field}: owners={show(a)} index={show(b)}"


def iter_digest_mismatches(
    pairs: Iterable[tuple[str, dict[str, Any], dict[str, Any]]],
) -> Iterable[tuple[str, list[str]]]:
    """Yield `(recipe_id, drifted_fields)` for each pair that disagrees."""
    for recipe_id, owner_doc, index_doc in pairs:
        drifted = digest_differences(owner_doc, index_doc)
        if drifted:
            yield recipe_id, drifted
