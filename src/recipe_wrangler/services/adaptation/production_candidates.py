"""Reuse production graph candidates; resolve only exact released compositions."""

from collections.abc import Callable, Iterator

from .models import ComparisonComposition, ComparisonSnapshot, RawPartEdit
from .comparison_store import ComparisonLoadError
from .nutritional_variants import stated


def _same_table_identity_records(snapshot: ComparisonSnapshot, original, name: str) -> list[dict]:
    from recipe_wrangler.matching.anatomy import _label_annotator
    from recipe_wrangler.repositories import matching_store
    parsed = _label_annotator().annotate(name)
    identity = parsed.get('main_ingredient')
    if not identity and not parsed.get('foods'):
        from recipe_wrangler.utils.food_identity import compare_identities
        # A lexical proposal is a retrieval key only, never an asserted fact.
        key = compare_identities(name, name).mention_key
        identity = key[0] if len(key) == 1 else None
    if not identity or len(parsed.get('foods') or []) > 1 or identity in {'food', 'meat', 'fish', 'sauce'}:
        return []
    source = original.composition
    state = stated(source, 'preparation_state')
    return matching_store.identity_composition_records(snapshot.release_id,
        'nutritional_ingredients_' + snapshot.nutrition_source, dataset=source.source_dataset,
        source_release=source.source_release, identity=identity,
        state=state if isinstance(state, str) and state not in {'', 'unknown'} else None,
        **({'include_other_sources': True} if snapshot.input_policy == 'production_estimate' else {}))


def food_part_candidate_records(snapshot: ComparisonSnapshot, position: int) -> tuple[list[dict], list[str]]:
    """Discover all different supported raw parts, without food-specific targets."""
    from recipe_wrangler.repositories import matching_store
    source = next(item.composition for item in snapshot.ingredients if item.position == position)
    if source is None:
        return [], ["source_composition_missing"]
    for key in ("base_identity", "biological_source", "food_part", "preparation_state"):
        value = stated(source, key)
        if not isinstance(value, str) or value in {"", "unknown"}:
            return [], ["food_part_discovery_source_fact_missing:" + key]
    if stated(source, "preparation_state") != "raw":
        return [], ["food_part_discovery_requires_raw_source"]
    rows = matching_store.food_part_composition_records(snapshot.release_id,
        "nutritional_ingredients_" + snapshot.nutrition_source, dataset=source.source_dataset,
        source_release=source.source_release, identity=stated(source, "base_identity"),
        biological_source=stated(source, "biological_source"), source_food_part=stated(source, "food_part"))
    return rows[:500], ["food_part_candidate_scan_limit_reached"] if len(rows) > 500 else []


def _candidate_profile(snapshot: ComparisonSnapshot, name: str) -> dict | None:
    if snapshot.input_policy != "production_estimate":
        from . import service
        return service._fetch_candidate_profile(name, snapshot.nutrition_source)
    # The new adapter must not inherit the legacy tool's environment switch or
    # name-only cache. Resolve candidates against this exact baseline release.
    from recipe_wrangler.matching.runtime import engine
    result = engine(snapshot.release_id).calculate(title=name, ingredient_names=[name],
        original_texts=[name], weights=[100.0], source=snapshot.nutrition_source, serves=1.0)
    details = result.get("details") or []
    return details[0] if details else None


def make_substitution_edit(snapshot: ComparisonSnapshot, position: int, target: ComparisonComposition, *,
                           nutritional_metric: str | None = None) -> RawPartEdit:
    original = next(item for item in snapshot.ingredients if item.position == position)
    source = original.composition
    def fact(composition, key):
        value = composition.properties.get(key)
        if (composition is source and snapshot.input_policy == "production_estimate"
                and key in {"food_part", "biological_source"}
                and (value is None or value.evidence.status != "stated" or value.value in (None, "", "unknown"))):
            # Edit intent may use the recipe; comparison still requires table support.
            value = original.observed_properties.get(key)
        if value is None or value.evidence.status != "stated" or not isinstance(value.value, str) or value.value in {"", "unknown"}:
            if snapshot.input_policy == "production_estimate" and key in {"food_part", "biological_source"}:
                return None
            raise ComparisonLoadError("operation_source_or_target_fact_missing:" + key)
        return value.value
    identity, target_identity = fact(source, "base_identity"), fact(target, "base_identity")
    source_part, target_part = fact(source, "food_part"), fact(target, "food_part")
    change = None
    if identity == target_identity and source_part == target_part:
        from .nutritional_variants import describe_change
        change = describe_change(source, target, nutritional_metric)
        if change is None:
            raise ComparisonLoadError("no_executable_identity_or_part_change")
    return RawPartEdit(recipe_id=snapshot.recipe_id, baseline_run_id=snapshot.run_id,
        quantity_input_hash=snapshot.quantity_input_hash, position=position, from_composition=source.ref, to_composition=target.ref,
        quantity_policy="preserve_saved_input_mass" if snapshot.input_policy == "production_estimate" else "preserve_raw_edible_mass",
        nutritional_change=change,
        operation="REPLACE_PRODUCT_VARIANT" if identity == target_identity else "REPLACE_INGREDIENT",
        source_identity=identity, target_identity=target_identity,
        source_biological_source=fact(source, "biological_source"), target_biological_source=fact(target, "biological_source"),
        source_food_part=source_part, target_food_part=target_part)


def production_candidate_stream(snapshot: ComparisonSnapshot, position: int, *,
                                has_eligible: Callable[[], bool] | None = None) -> tuple[Iterator[dict], list[str]]:
    from recipe_wrangler.repositories import matching_store
    from . import service
    original = next(item for item in snapshot.ingredients if item.position == position)
    # A recipe-text node can have unrelated taxonomy edges. Try the pinned
    # identity as well; every result still passes occurrence-level validation.
    identity = stated(original.composition, "base_identity") if original.composition else None
    names = list(dict.fromkeys([identity, original.name])) if snapshot.input_policy == "production_estimate" else [original.name]
    graph_names = list(dict.fromkeys(graph for name in names if isinstance(name, str) and name
        if (graph := service._select_graph_name(name, []))))
    if not graph_names:
        return iter(()), ["production_graph_identity_unresolved"]
    candidates = {}
    for graph_name in graph_names:
        found = service.find_substitute_candidates(graph_name, expand=True) if snapshot.input_policy == "production_estimate" else service.find_substitute_candidates(graph_name)
        for candidate in found:
            if candidate.get("name"):
                candidates.setdefault(candidate["name"].casefold(), candidate)
    reasons, seen = [], set()
    if len(candidates) > 60:
        reasons.append("production_candidate_scan_limit_reached")
    def records():
        emitted = 0
        for candidate in list(candidates.values())[:60]:
            if emitted >= 500:
                reasons.append("production_record_scan_limit_reached")
                return
            name = candidate.get("name")
            if not name or not service._food_class_compatible(original.name, name):
                continue
            direct = _same_table_identity_records(snapshot, original, name) if snapshot.input_policy == 'production_estimate' else []
            if len(direct) > 20:
                reasons.append('production_identity_candidate_scan_limit_reached')
            for row in direct[:min(20, 500-emitted)]:
                key = row['collection'], row['candidate_id']
                if key not in seen:
                    seen.add(key)
                    emitted += 1
                    yield row
            if has_eligible is not None and has_eligible():
                return
            detail = _candidate_profile(snapshot, name)
            if not detail or detail.get("catalogue_release") != snapshot.release_id:
                reasons.append("production_candidate_has_no_pinned_composition:" + name)
                continue
            facts = detail.get("selected_composition_facts") or {}
            collection, identifier = detail.get("collection") or facts.get("collection"), detail.get("selected_candidate_id")
            # The released facts usually carry source_key rather than collection.
            if collection is None and facts.get("source_key"):
                collection = "nutritional_ingredients_" + facts["source_key"]
            if (collection not in {"nutritional_ingredients_eu", "nutritional_ingredients_" + snapshot.nutrition_source}
                    or (identifier is None and facts.get("native_id") is None)):
                reasons.append("production_candidate_exact_reference_missing:" + name)
                continue
            key = collection, str(identifier) if identifier is not None else "native:" + str(facts["native_id"])
            if key in seen:
                continue
            seen.add(key)
            resolved = matching_store.composition_records(snapshot.release_id, collection, **(
                {"candidate_id": str(identifier)} if identifier is not None else {"native_id": str(facts["native_id"])}))
            if len(resolved) == 1:
                emitted += 1
                yield resolved[0]
                if has_eligible is not None and has_eligible():
                    return
            else:
                reasons.append("production_candidate_exact_reference_ambiguous:" + name)
    return records(), reasons


def production_candidate_records(snapshot: ComparisonSnapshot, position: int) -> tuple[list[dict], list[str]]:
    rows, reasons = production_candidate_stream(snapshot, position)
    return list(rows), reasons
