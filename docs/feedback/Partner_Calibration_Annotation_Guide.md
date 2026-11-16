# Partner calibration annotation guide

**Version:** 2026-10-08  
**Scope:** `partner_calibration_30.csv`  
**Status:** calibration exercise, not a gold standard

Use `partner_calibration_reviewer_template.xlsx`, save one independently completed copy per reviewer, and do not overwrite the blank template.

## Purpose

All reviewers independently annotate the same 30 cases. The exercise checks that the facet schema and match labels are understandable before a larger expert evaluation. Machine proposals are evidence to inspect, not answers to copy.

Use the original `display`, `measurement`, `parser_note`, recipe title and ingredient fields together. An empty facet means “not stated”, not that the property is absent.

## What to record

1. Put corrected facets in `expert_facet_corrections` as `facet=value` pairs separated by semicolons. Include only corrections.
2. Set `expert_foodon_decision` to `exact`, `acceptable`, `parent`, `incorrect`, `abstain`, `decompose`, or `discussion`.
3. Put the preferred FoodOn identifier in `expert_foodon_id`. If several entities are equally valid, put all valid identifiers, separated by semicolons, in `expert_foodon_acceptable_ids`.
4. Set `expert_composition_decision` to `exact`, `acceptable`, `incorrect`, `abstain`, `decompose`, or `discussion`. Record the preferred source and record identifier. Put other equally valid `source:record_id` pairs in `expert_composition_acceptable_records`.
5. Set `expert_abstention_is_valid` to `yes` or `no` when either machine decision abstains.
6. Set `expert_choice_or_compound_decision` to `single_food`, `choice`, `compound`, `search_as_product`, or `discussion`.
7. Give a short reason in `expert_rationale`, then add reviewer name and review date.

## Matching rules

- Treat identity, biological source, food part, raw/cooked state, cooking method, preservation, concentration and material form as potentially match-changing.
- Do not accept raw and cooked foods as equivalent. If no state-correct entity or composition row exists, prefer a broader parent or abstention and explain why.
- Ordinary preparation such as chopped, sliced, grated or peeled is normally non-blocking, but it remains useful facet evidence.
- A FoodOn parent can be useful without being an exact match; label it `parent`.
- `or` denotes alternatives, while `and` or `with` may denote a compound. Review the original phrase before deciding. Do not assign a shared quantity independently to every alternative unless the text states that.
- Abstention is a valid safety outcome when the text or available candidates do not support a defensible link.

## Review process

Reviewers should complete the sheet independently and should not discuss individual cases first. Afterwards, compare disagreements, refine unclear definitions, and adjudicate a single reference answer. Preserve both the independent sheets and the adjudication record. Only the adjudicated result may later be called gold data.

After at least two completed workbooks return, build the disagreement sheet with:

```bash
uv run python scripts/composition/compare_partner_calibration_reviews.py \
  reviewer_a.xlsx reviewer_b.xlsx \
  --output partner_calibration_adjudication.csv
```
