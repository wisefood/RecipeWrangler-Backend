"""Caller identity and field-level redaction.

RecipeWrangler performs **no authentication and no authorization**. It is a
ClusterIP service behind wisefood-api, which owns Keycloak, validates the token
and decides who may call what. This module's job is narrower: read the identity
wisefood-api has already established, attribute writes to it, and hide the
fields a member should not see.

The trust boundary matters. These headers are only trustworthy because the
service is not directly reachable — anything that can set them can already
reach the pod. If RecipeWrangler is ever exposed, this becomes spoofable and
must be replaced with token verification, not patched.

Two things are expert-only:

``creator``
    A Keycloak subject id identifies a person. Returning it to every caller
    leaks who authored a recipe to anyone who can list recipes, which is a
    privacy question rather than a product one.

inactive recipes
    A disabled recipe is one someone decided to withdraw. Members see the
    corpus as curated; experts and admins need to find withdrawn items to
    review or restore them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from fastapi import Header

# Headers wisefood-api must forward once it verifies the token.
#
# It does not forward them yet: `backend/recipewrangler.RECIPEWRANGLER` is a
# plain httpx client that sends no identity at all. Until that changes every
# caller here is anonymous, which is safe — creator is hidden and withdrawn
# recipes stay hidden — just not useful.
SUB_HEADER = "X-User-Sub"
ROLES_HEADER = "X-User-Roles"
USERNAME_HEADER = "X-User-Name"

# Roles allowed to see creator attribution and withdrawn recipes.
#
# These are Keycloak role names exactly as wisefood-api resolves them in
# `auth._extract_roles` — realm_access.roles merged with
# resource_access[client].roles, lowercased. Its recipewrangler routes are
# guarded with `auth("admin,expert")`, so those two are the vocabulary that
# actually exists; `agent` is included because wisefood-data-api uses it for
# machine callers and the same token would reach here.
#
# Deliberately not a new vocabulary: inventing one would mean two systems
# disagreeing about who an expert is.
PRIVILEGED_ROLES: frozenset[str] = frozenset({"admin", "expert", "agent"})

# Fields stripped from responses for unprivileged callers.
REDACTED_FIELDS: tuple[str, ...] = ("creator",)


@dataclass(frozen=True)
class Caller:
    """Who is asking, as established upstream."""

    sub: str | None = None
    username: str | None = None
    roles: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_privileged(self) -> bool:
        return bool(self.roles & PRIVILEGED_ROLES)

    @property
    def creator_id(self) -> str | None:
        """The value written to ``creator`` on a recipe this caller creates.

        The Keycloak subject, not the username: a username can be changed or
        reused, a subject cannot, and attribution has to survive that.
        """
        return self.sub


ANONYMOUS = Caller()


def _parse_roles(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(
        part.strip().lower() for part in str(raw).replace(",", " ").split() if part.strip()
    )


def build_caller(
    sub: str | None = None,
    roles: str | None = None,
    username: str | None = None,
) -> Caller:
    """Construct a Caller from raw header values.

    Kept separate from the FastAPI dependency below because that function's
    defaults are ``Header`` objects, not ``None`` — calling it outside dependency
    injection silently yields a Caller whose ``sub`` is a ``Header`` instance.
    The logic belongs somewhere it can be called and tested directly.

    Absent values mean an anonymous caller, which is a valid state: reads are
    open, and an unattributed write simply records no creator rather than being
    rejected. Rejecting here would duplicate an authorization decision that
    wisefood-api has already made.
    """
    return Caller(
        sub=(sub or "").strip() or None,
        username=(username or "").strip() or None,
        roles=_parse_roles(roles),
    )


def get_caller(
    x_user_sub: str | None = Header(None, alias=SUB_HEADER),
    x_user_roles: str | None = Header(None, alias=ROLES_HEADER),
    x_user_name: str | None = Header(None, alias=USERNAME_HEADER),
) -> Caller:
    """FastAPI dependency resolving the caller from upstream headers."""
    return build_caller(sub=x_user_sub, roles=x_user_roles, username=x_user_name)


def redact(payload: Any, caller: Caller) -> Any:
    """Strip expert-only fields from a response for unprivileged callers.

    Walks lists and dicts so it can be applied once to a whole search envelope
    rather than at every construction site — a field is missed by forgetting,
    and forgetting is what leaks it.
    """
    if caller.is_privileged:
        return payload
    if isinstance(payload, list):
        return [redact(item, caller) for item in payload]
    if isinstance(payload, dict):
        return {
            key: redact(value, caller)
            for key, value in payload.items()
            if key not in REDACTED_FIELDS
        }
    return payload


def status_filter(caller: Caller, *, include_inactive: bool = False) -> list[dict[str, Any]]:
    """Elasticsearch filter restricting a caller to what they may see.

    Unprivileged callers never see withdrawn recipes, whatever they ask for —
    ``include_inactive`` is a privileged capability, not a query parameter.
    """
    if caller.is_privileged and include_inactive:
        return []
    return [
        {"bool": {"must_not": [{"terms": {"status": ["disabled", "deleted", "archived"]}}]}}
    ]


def visible_to(document: dict[str, Any], caller: Caller) -> bool:
    """Whether a single fetched document may be returned to this caller."""
    if caller.is_privileged:
        return True
    return str(document.get("status") or "active") == "active"
