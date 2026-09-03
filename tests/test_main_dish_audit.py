"""Guards on the main-dish audit: what it demotes, and what it must never touch.

The script's whole value is that it does *not* act on the title, so the tests
that matter are the ones proving a genuine main dish with "sauce" in its name
survives, and that a failed or contradictory verdict leaves the corpus alone.
"""

import importlib.util
import unittest
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "catalog" / "audit_main_dish_claims.py"
)
_spec = importlib.util.spec_from_file_location("audit_main_dish_claims", _SCRIPT)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)  # safe: the ES client is built lazily in main()


def doc(title, courses=("main-dish",), **extra):
    return {"urn": f"urn:recipe:{title}", "recipe_id": title, "title": title,
            "course_types": list(courses), **extra}


class VerdictValidationTests(unittest.TestCase):
    """`verdict_for` must sanitise whatever the model returns."""

    def _verdict(self, raw, recipe=None):
        audit.call_model = lambda *a, **k: raw
        return audit.verdict_for(recipe or doc("Bolognese sauce"), llm=object())

    def test_out_of_vocabulary_values_are_dropped(self):
        v = self._verdict({"standalone_main_dish": False,
                           "course_types": ["condiment", "sauce", "side"],
                           "confidence": 0.9})
        self.assertEqual(v["course_types"], ["side"])

    def test_variant_spellings_are_folded(self):
        v = self._verdict({"standalone_main_dish": False,
                           "course_types": ["side-dish"], "confidence": 0.8})
        self.assertEqual(v["course_types"], ["side"])

    def test_a_rejection_may_not_smuggle_main_dish_back_in(self):
        # The model said "not a main dish" and then listed main-dish anyway.
        # The explicit answer wins, or nothing would ever be demoted.
        v = self._verdict({"standalone_main_dish": False,
                           "course_types": ["main-dish", "side"], "confidence": 0.7})
        self.assertNotIn("main-dish", v["course_types"])

    def test_non_numeric_confidence_becomes_none(self):
        v = self._verdict({"standalone_main_dish": False, "course_types": ["side"],
                           "confidence": "high"})
        self.assertIsNone(v["confidence"])

    def test_at_most_two_courses(self):
        v = self._verdict({"standalone_main_dish": False,
                           "course_types": ["side", "salad", "soup"], "confidence": 0.6})
        self.assertEqual(len(v["course_types"]), 2)


class ResolveChangeTests(unittest.TestCase):
    """`resolve_change` is the last gate before a write."""

    def test_confirmed_main_dish_is_left_alone(self):
        # "Chicken in barbecue sauce" is nominated by the title vocabulary and
        # must survive the audit untouched. This is the regression that matters.
        verdict = {"standalone_main_dish": True, "course_types": ["main-dish"],
                   "confidence": 0.95, "reason": ""}
        self.assertIsNone(
            audit.resolve_change(doc("Chicken in barbecue sauce"), verdict)
        )

    def test_confirmed_main_dish_is_left_alone_even_if_it_proposed_a_change(self):
        verdict = {"standalone_main_dish": True, "course_types": ["salad"],
                   "confidence": 0.5, "reason": ""}
        self.assertIsNone(audit.resolve_change(doc("Warm chicken salad"), verdict))

    def test_rejection_without_a_replacement_writes_nothing(self):
        # Writing [] would make the recipe unreachable by every planner slot,
        # which is worse than being miscategorised.
        verdict = {"standalone_main_dish": False, "course_types": [],
                   "confidence": 0.9, "reason": ""}
        self.assertIsNone(audit.resolve_change(doc("Bolognese sauce"), verdict))

    def test_genuine_demotion_is_returned(self):
        verdict = {"standalone_main_dish": False, "course_types": ["side"],
                   "confidence": 0.9, "reason": ""}
        change = audit.resolve_change(doc("Bolognese sauce"), verdict)
        self.assertIsNotNone(change)
        self.assertEqual(change[0], ["side"])

    def test_double_tagged_recipe_loses_only_the_main_dish_claim(self):
        verdict = {"standalone_main_dish": False, "course_types": ["side"],
                   "confidence": 0.9, "reason": ""}
        change = audit.resolve_change(doc("Pasta sauce", ("side", "main-dish")), verdict)
        self.assertEqual(change[0], ["side"])

    def test_no_op_is_not_written(self):
        verdict = {"standalone_main_dish": False, "course_types": ["side"],
                   "confidence": 0.9, "reason": ""}
        self.assertIsNone(audit.resolve_change(doc("Roasted Potatoes", ("side",)), verdict))


class CandidateQueryTests(unittest.TestCase):
    def test_default_set_is_a_recall_net_over_main_dish_only(self):
        q = audit.candidate_query(double_tagged_only=False, audit_all=False)["bool"]
        self.assertIn({"term": {"course_types": "main-dish"}}, q["filter"])
        self.assertEqual(q["minimum_should_match"], 1)
        self.assertIn({"match_phrase": {"title": "sauce"}}, q["should"])
        self.assertIn({"term": {"course_types": "side"}}, q["should"])

    def test_double_tagged_only_is_a_conjunction_with_no_title_matching(self):
        q = audit.candidate_query(double_tagged_only=True, audit_all=False)["bool"]
        self.assertIn({"term": {"course_types": "side"}}, q["filter"])
        self.assertNotIn("should", q)

    def test_all_drops_the_nomination_net_entirely(self):
        q = audit.candidate_query(double_tagged_only=False, audit_all=True)["bool"]
        self.assertNotIn("should", q)

    def test_disabled_recipes_are_never_candidates(self):
        for kwargs in ({"double_tagged_only": False, "audit_all": False},
                       {"double_tagged_only": True, "audit_all": False},
                       {"double_tagged_only": False, "audit_all": True}):
            q = audit.candidate_query(**kwargs)["bool"]
            self.assertIn({"terms": {"status": ["disabled", "deleted"]}}, q["must_not"])


class PromptTests(unittest.TestCase):
    def test_stored_course_types_are_withheld_from_the_model(self):
        # Stating the value under suspicion anchors the model to it — the trap
        # annotate_recipes.py documents.
        prompt = audit.build_user_prompt(doc("Pasta sauce", ("side", "main-dish")))
        self.assertNotIn("main-dish", prompt)

    def test_prompt_carries_the_evidence_the_verdict_needs(self):
        prompt = audit.build_user_prompt(
            doc("Pasta sauce", ingredient_names=["tomato", "basil"], ingredient_count=5)
        )
        self.assertIn("Pasta sauce", prompt)
        self.assertIn("tomato", prompt)

    def test_system_prompt_offers_exactly_the_closed_vocabulary(self):
        # The allowed-values line must track sources.COURSE_TYPES, so adding a
        # `condiment` course later cannot silently leave the prompt behind.
        from recipe_wrangler.catalog import sources as S
        line = next(
            line for line in audit.SYSTEM_PROMPT.splitlines()
            if line.startswith("breakfast,")
        )
        self.assertEqual([v.strip() for v in line.split(",")], list(S.COURSE_TYPES))


if __name__ == "__main__":
    unittest.main()
