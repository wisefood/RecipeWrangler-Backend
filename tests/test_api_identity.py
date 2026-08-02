"""Caller identity and field-level redaction.

RecipeWrangler authenticates nobody — wisefood-api validates the Keycloak token
and forwards the result. What is tested here is that the forwarded identity is
used correctly: writes are attributed to the subject, and two things are shown
only to privileged callers.

``creator`` is a person's Keycloak subject. Returning it to every caller leaks
who authored a recipe to anyone who can list recipes.

Withdrawn recipes are ones someone decided to remove. A member should see the
corpus as curated; an expert needs to find withdrawn items to review them.
"""

from __future__ import annotations

import pytest

from recipe_wrangler.api.identity import (
    ANONYMOUS,
    PRIVILEGED_ROLES,
    REDACTED_FIELDS,
    Caller,
    build_caller,
    redact,
    status_filter,
    visible_to,
)

MEMBER = Caller(sub="sub-member", roles=frozenset({"member"}))
EXPERT = Caller(sub="sub-expert", roles=frozenset({"expert"}))
ADMIN = Caller(sub="sub-admin", roles=frozenset({"admin"}))
AGENT = Caller(sub="sub-agent", roles=frozenset({"agent"}))


class TestCallerResolution:
    def test_headers_are_parsed(self):
        caller = build_caller(sub="abc-123", roles="expert,member", username="dpetrou")
        assert caller.sub == "abc-123"
        assert caller.username == "dpetrou"
        assert caller.roles == {"expert", "member"}

    @pytest.mark.parametrize("raw", ["expert,member", "expert member", " EXPERT , member "])
    def test_role_separators_and_case_tolerated(self, raw):
        assert "expert" in build_caller(roles=raw).roles

    def test_absent_headers_yield_an_anonymous_caller(self):
        """Anonymous is valid: reads are open and an unattributed write simply
        records no creator. Rejecting here would duplicate an authorization
        decision wisefood-api already made."""
        caller = build_caller()
        assert caller.sub is None
        assert not caller.is_privileged
        assert caller.roles == frozenset()

    def test_blank_headers_treated_as_absent(self):
        assert build_caller(sub="   ", username="").sub is None

    def test_creator_id_is_the_subject_not_the_username(self):
        """A username can be changed or reused; a subject cannot, and
        attribution must survive a rename."""
        caller = Caller(sub="abc-123", username="dpetrou")
        assert caller.creator_id == "abc-123"


class TestPrivilege:
    @pytest.mark.parametrize("caller", [EXPERT, ADMIN, AGENT])
    def test_privileged_roles(self, caller):
        assert caller.is_privileged

    @pytest.mark.parametrize("caller", [MEMBER, ANONYMOUS])
    def test_unprivileged_roles(self, caller):
        assert not caller.is_privileged

    def test_privileged_set_is_explicit(self):
        assert PRIVILEGED_ROLES == {"admin", "expert", "agent"}


class TestRedaction:
    def test_creator_hidden_from_members(self):
        out = redact({"title": "X", "creator": "sub-123"}, MEMBER)
        assert out == {"title": "X"}

    def test_creator_visible_to_experts(self):
        doc = {"title": "X", "creator": "sub-123"}
        assert redact(doc, EXPERT) == doc

    def test_creator_hidden_from_anonymous(self):
        assert "creator" not in redact({"creator": "sub-123"}, ANONYMOUS)

    def test_redaction_reaches_nested_results(self):
        """Applied once to a whole envelope rather than at each construction
        site — a field is missed by forgetting, and forgetting is what leaks
        it."""
        envelope = {
            "total": 2,
            "results": [{"title": "A", "creator": "s1"}, {"title": "B", "creator": "s2"}],
        }
        out = redact(envelope, MEMBER)
        assert all("creator" not in row for row in out["results"])
        assert out["total"] == 2

    def test_redaction_reaches_deeply_nested_plans(self):
        plan = {"days": [{"slots": [{"recipes": [{"title": "A", "creator": "s1"}]}]}]}
        out = redact(plan, MEMBER)
        assert "creator" not in out["days"][0]["slots"][0]["recipes"][0]

    def test_scalars_pass_through(self):
        assert redact("text", MEMBER) == "text"
        assert redact(7, MEMBER) == 7
        assert redact(None, MEMBER) is None

    def test_redacted_field_set_is_explicit(self):
        assert "creator" in REDACTED_FIELDS


class TestStatusScoping:
    def test_members_never_see_withdrawn_recipes(self):
        clauses = status_filter(MEMBER)
        assert clauses, "an unprivileged caller must always be scoped"
        assert "disabled" in str(clauses)

    def test_members_cannot_opt_into_inactive(self):
        """include_inactive is a privileged capability, not a query parameter —
        otherwise it is a self-service escalation."""
        assert status_filter(MEMBER, include_inactive=True) == status_filter(MEMBER)

    def test_experts_may_opt_into_inactive(self):
        assert status_filter(EXPERT, include_inactive=True) == []

    def test_experts_are_still_scoped_by_default(self):
        """Privilege is permission to ask, not a change of default."""
        assert status_filter(EXPERT, include_inactive=False) != []

    def test_anonymous_is_scoped(self):
        assert status_filter(ANONYMOUS, include_inactive=True) != []


class TestDocumentVisibility:
    def test_member_cannot_fetch_a_withdrawn_recipe(self):
        assert not visible_to({"status": "disabled"}, MEMBER)

    def test_expert_can(self):
        assert visible_to({"status": "disabled"}, EXPERT)

    def test_missing_status_means_active(self):
        """Backward compatibility with the soft-delete spec: absent status is
        active, not hidden."""
        assert visible_to({}, MEMBER)

    def test_active_recipe_visible_to_everyone(self):
        assert visible_to({"status": "active"}, ANONYMOUS)
