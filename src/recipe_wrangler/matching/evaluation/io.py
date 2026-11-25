"""Portable JSONL storage for reviewed matching benchmark items."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from recipe_wrangler.matching.evaluation.benchmark import BenchmarkItem, BenchmarkTarget
from recipe_wrangler.matching.models import FoodFacets, FoodMention, RecipeContext


def write_benchmark_jsonl(path: Path, items: Iterable[BenchmarkItem]) -> None:
    """Write reviewed annotations in a diff-friendly, one-item-per-line format."""
    with path.open("w", encoding="utf-8") as stream:
        for item in items:
            stream.write(json.dumps(_item_to_dict(item), sort_keys=True) + "\n")


def read_benchmark_jsonl(path: Path) -> tuple[BenchmarkItem, ...]:
    """Read benchmark annotations written by :func:`write_benchmark_jsonl`."""
    items: list[BenchmarkItem] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                items.append(_item_from_dict(json.loads(line)))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"Invalid benchmark item at {path}:{line_number}: {exc}"
                ) from exc
    return tuple(items)


def _item_to_dict(item: BenchmarkItem) -> dict[str, object]:
    mention = item.mention
    return {
        "item_id": item.item_id,
        "mention": {
            "text": mention.text,
            "identity": mention.identity,
            "facets": _facets_to_dict(mention.facets),
            "context": _context_to_dict(mention.context),
            "target_region": mention.target_region,
        },
        "acceptable_targets": [
            {
                "source_id": target.source_id,
                "record_id": target.record_id,
                "relation": target.relation,
                "preference": target.preference,
            }
            for target in item.acceptable_targets
        ],
        "abstain_is_valid": item.abstain_is_valid,
        "risk_categories": sorted(item.risk_categories),
        "rationale": item.rationale,
    }


def _item_from_dict(data: dict[str, object]) -> BenchmarkItem:
    raw_mention = _as_dict(data["mention"])
    raw_context = raw_mention.get("context")
    context = None if raw_context is None else _context_from_dict(_as_dict(raw_context))
    mention = FoodMention(
        text=str(raw_mention["text"]),
        identity=str(raw_mention["identity"]),
        facets=_facets_from_dict(_as_dict(raw_mention["facets"])),
        context=context,
        target_region=_optional_string(raw_mention.get("target_region")),
    )
    raw_targets = data["acceptable_targets"]
    if not isinstance(raw_targets, list):
        raise TypeError("acceptable_targets must be a list")
    targets = tuple(
        BenchmarkTarget(
            source_id=str(raw_target["source_id"]),
            record_id=str(raw_target["record_id"]),
            relation=str(raw_target["relation"]),  # type: ignore[arg-type]
            preference=_optional_int(raw_target.get("preference")),
        )
        for target in raw_targets
        for raw_target in [_as_dict(target)]
    )
    raw_risks = data["risk_categories"]
    if not isinstance(raw_risks, list) or not all(isinstance(risk, str) for risk in raw_risks):
        raise TypeError("risk_categories must be a list of strings")
    abstain_is_valid = data["abstain_is_valid"]
    if not isinstance(abstain_is_valid, bool):
        raise TypeError("abstain_is_valid must be a boolean")
    return BenchmarkItem(
        item_id=str(data["item_id"]),
        mention=mention,
        acceptable_targets=targets,
        abstain_is_valid=abstain_is_valid,
        risk_categories=frozenset(raw_risks),
        rationale=str(data["rationale"]),
    )


def _facets_to_dict(facets: FoodFacets) -> dict[str, object]:
    return {
        "identity": facets.identity,
        "base_identity": facets.base_identity,
        "biological_source": facets.biological_source,
        "food_part": facets.food_part,
        "preparation_state": facets.preparation_state,
        "processes": sorted(facets.processes),
        "preservation_methods": sorted(facets.preservation_methods),
        "physical_form": facets.physical_form,
        "colour": facets.colour,
        "sodium_level": facets.sodium_level,
        "skin_status": facets.skin_status,
        "bone_status": facets.bone_status,
        "packing_medium": facets.packing_medium,
        "compound": facets.compound,
        "gluten_free": facets.gluten_free,
        "fat_level": facets.fat_level,
        "sugar_level": facets.sugar_level,
        "grain_refinement": facets.grain_refinement,
        "size": facets.size,
        "size_target": facets.size_target,
        "thickness": facets.thickness,
        "variety": facets.variety,
        "ripeness": facets.ripeness,
        "cooking_extent": facets.cooking_extent,
        "texture": facets.texture,
        "particle_size": facets.particle_size,
        "drainage_status": facets.drainage_status,
        "reconstitution_state": facets.reconstitution_state,
        "meat_fat_description": facets.meat_fat_description,
        "fat_content_description": facets.fat_content_description,
        "fortification": facets.fortification,
        "seed_status": facets.seed_status,

    }


def _facets_from_dict(data: dict[str, object]) -> FoodFacets:
    return FoodFacets(
        identity=_optional_string(data.get("identity")),
        base_identity=_optional_string(data.get("base_identity")),
        biological_source=_optional_string(data.get("biological_source")),
        food_part=_optional_string(data.get("food_part")),
        preparation_state=_optional_string(data.get("preparation_state")),  # type: ignore[arg-type]
        processes=frozenset(_string_list(data.get("processes"), "processes")),
        preservation_methods=frozenset(
            _string_list(data.get("preservation_methods"), "preservation_methods")
        ),
        physical_form=_optional_string(data.get("physical_form")),
        colour=_optional_string(data.get("colour")),
        sodium_level=_optional_string(data.get("sodium_level")),  # type: ignore[arg-type]
        skin_status=_optional_string(data.get("skin_status")),
        bone_status=_optional_string(data.get("bone_status")),
        packing_medium=_optional_string(data.get("packing_medium")),
        compound=data.get("compound") if isinstance(data.get("compound"), bool) else None,
        gluten_free=data.get("gluten_free") if isinstance(data.get("gluten_free"), bool) else None,
        fat_level=_optional_string(data.get("fat_level")),  # type: ignore[arg-type]
        sugar_level=_optional_string(data.get("sugar_level")),  # type: ignore[arg-type]
        grain_refinement=_optional_string(data.get("grain_refinement")),  # type: ignore[arg-type]
        size=_optional_string(data.get("size")),
        size_target=_optional_string(data.get("size_target")),  # type: ignore[arg-type]
        thickness=_optional_string(data.get("thickness")),
        variety=_optional_string(data.get("variety")),
        ripeness=_optional_string(data.get("ripeness")),
        cooking_extent=_optional_string(data.get("cooking_extent")),
        texture=_optional_string(data.get("texture")),
        particle_size=_optional_string(data.get("particle_size")),
        drainage_status=_optional_string(data.get("drainage_status")),
        reconstitution_state=_optional_string(data.get("reconstitution_state")),
        meat_fat_description=_optional_string(data.get("meat_fat_description")),
        fat_content_description=_optional_string(data.get("fat_content_description")),
        fortification=_optional_string(data.get("fortification")),
        seed_status=_optional_string(data.get("seed_status")),

    )


def _context_to_dict(context: RecipeContext | None) -> dict[str, object] | None:
    if context is None:
        return None
    return {
        "title": context.title,
        "cuisine": context.cuisine,
        "neighbouring_ingredients": list(context.neighbouring_ingredients),
        "amount_g": context.amount_g,
        "unit": context.unit,
        "ingredient_role": context.ingredient_role,
        "optional": context.optional,
        "quantity_qualifier": context.quantity_qualifier,
        "usage_note": context.usage_note,
    }


def _context_from_dict(data: dict[str, object]) -> RecipeContext:
    amount_g = data.get("amount_g")
    if amount_g is not None and not isinstance(amount_g, (int, float)):
        raise TypeError("amount_g must be numeric")
    return RecipeContext(
        title=_optional_string(data.get("title")),
        cuisine=_optional_string(data.get("cuisine")),
        neighbouring_ingredients=tuple(
            _string_list(data.get("neighbouring_ingredients"), "neighbouring_ingredients")
        ),
        amount_g=None if amount_g is None else float(amount_g),
        unit=_optional_string(data.get("unit")),
        ingredient_role=_optional_string(data.get("ingredient_role")),
        optional=data.get("optional") if isinstance(data.get("optional"), bool) else None,
        quantity_qualifier=_optional_string(data.get("quantity_qualifier")),
        usage_note=_optional_string(data.get("usage_note")),
    )


def _as_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("expected an object")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("expected a string or null")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise TypeError("expected an integer or null")
    return value


def _string_list(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{field_name} must be a list of strings")
    return value
