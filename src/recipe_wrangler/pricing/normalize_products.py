"""Explicit product canonicalization rules for the supplied EC exports."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .map_pli_categories import pli_category_for


@dataclass(frozen=True)
class ProductMapping:
    raw_product: str
    canonical_product: str
    product_detail: str
    food_group: str
    pli_category: str
    mapping_status: str = "explicit"
    mapping_notes: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


_MAPPINGS = json.loads(
    Path(__file__).with_name("product_mapping_overrides.json").read_text(encoding="utf-8")
)
CEREAL_PRODUCTS = _MAPPINGS["cereals"]
DAIRY_PRODUCTS = _MAPPINGS["dairy"]
FRUIT_VEGETABLE_PRODUCTS = _MAPPINGS["fruit_vegetables"]
OILSEED_PRODUCTS = _MAPPINGS["oilseeds"]
PROTEIN_CROP_PRODUCTS = _MAPPINGS["protein_crops"]
FISH_PRODUCTS = _MAPPINGS["fish_products"]
FISH_ITEM_PRODUCT_OVERRIDES = _MAPPINGS["fish_item_product_overrides"]


def _mapping(raw: str, product: str, detail: str, group: str, **kwargs: str) -> ProductMapping:
    return ProductMapping(
        raw_product=raw,
        canonical_product=product,
        product_detail=detail,
        food_group=group,
        pli_category=pli_category_for(group),
        **kwargs,
    )


def _clean_detail(value: Any) -> str:
    if value is None:
        return ""
    value = str(value).strip()
    return "" if value.lower() in {"", "nan", "n.a.", "not defined"} else value


def map_fish_product(product: Any, item: Any, category: Any) -> ProductMapping:
    """Map one EUMOFA product/form series to a canonical seafood concept."""

    raw_product = _clean_detail(product)
    item_name = _clean_detail(item)
    category_name = _clean_detail(category)
    canonical = FISH_ITEM_PRODUCT_OVERRIDES.get(item_name)
    if canonical is None:
        canonical = FISH_PRODUCTS.get(raw_product)
    if canonical is None:
        raise ValueError(f"ambiguous_fish_product:{raw_product}|{item_name}")

    form = (item_name or raw_product).lower()
    prefixes = {
        "alaska pollock": r"^alaska pollock",
        "anchovy": r"^anchovy",
        "cod": r"^cod",
        "gilthead seabream": r"^gilthead seabream",
        "herring": r"^herring",
        "lumpfish roe": r"^lumpfish",
        "mackerel": r"^mackerel",
        "pike-perch": r"^pike-perch",
        "plaice": r"^(?:european )?plaice",
        "salmon": r"^salmon",
        "sardine": r"^sardine",
        "seabass": r"^seabass",
        "shrimp": r"^shrimps?",
        "squid": r"^squid",
        "surimi": r"^surimi",
        "trout": r"^trout",
        "tuna": r"^tuna",
    }
    if canonical in prefixes:
        form = re.sub(prefixes[canonical], "", form).strip(" ,=-")
    if canonical == "trout" and form == "gutted trout":
        form = "gutted"
    if canonical == "tuna":
        form = "canned" if form == "canned tuna" else f"canned {form}".strip()
    form = form.removeprefix("in slices").strip() or (
        "slices" if item_name == "Salmon in slices" else ""
    )
    category_detail = {
        "Fresh": "fresh",
        "Frozen": "frozen",
        "Prepared-preserved": "prepared/preserved",
        "Smoked": "smoked",
    }.get(category_name)
    if category_detail is None:
        raise ValueError(f"ambiguous_fish_category:{category_name}")
    detail = ", ".join(part for part in (category_detail, form) if part)
    return _mapping(
        " | ".join(part for part in (raw_product, item_name, category_name) if part),
        canonical,
        detail,
        "fish and seafood",
        mapping_status="rule",
        mapping_notes="EUMOFA category and item-on-sale form retained as detail",
    )


def map_product(dataset: str, row: Mapping[str, Any]) -> ProductMapping:
    """Map one source row without discarding economically meaningful detail."""

    if dataset == "beef_cuts":
        raw = str(row["Category"]).strip()
        return _mapping(raw, "beef", raw.lower(), "meat")
    if dataset == "pigmeat_cuts":
        raw = str(row["Category"]).strip()
        return _mapping(raw, "pork", raw.lower(), "meat")
    if dataset == "poultry":
        raw = str(row["Product"]).strip()
        return _mapping(raw, "chicken", raw.lower(), "meat")
    if dataset == "sheep_goat_meat":
        raw = str(row["Category"]).strip()
        return _mapping(raw, "lamb", raw.lower(), "meat")
    if dataset == "dairy":
        raw = str(row["Product"]).strip()
        try:
            product, detail = DAIRY_PRODUCTS[raw]
        except KeyError as exc:
            raise ValueError(f"ambiguous_product:{raw}") from exc
        return _mapping(raw, product, detail, "dairy and eggs")
    if dataset == "raw_milk":
        raw = str(row["Product"]).strip()
        detail = "organic raw" if raw.lower().startswith("organic") else "raw"
        return _mapping(raw, "milk", detail, "dairy and eggs")
    if dataset == "eggs":
        detail = str(row["Farming Method"]).strip().lower()
        return _mapping(f"Eggs - {row['Farming Method']}", "egg", detail, "dairy and eggs")
    if dataset == "cereals":
        raw = str(row["Product Name"]).strip()
        try:
            product, detail = CEREAL_PRODUCTS[raw]
        except KeyError as exc:
            raise ValueError(f"ambiguous_product:{raw}") from exc
        return _mapping(raw, product, detail, "cereals")
    if dataset == "rice":
        stage = _clean_detail(row["Stage"])
        rice_type = _clean_detail(row["Rice Type"])
        variety = _clean_detail(row["Variety"])
        detail = ", ".join(part.lower() for part in (stage, rice_type, variety) if part)
        raw = " | ".join(part for part in (stage, rice_type, variety) if part)
        return _mapping(raw, "rice", detail, "cereals")
    if dataset == "fruit_vegetables":
        raw = str(row["Product Variety"]).strip()
        organic = raw.startswith("Organic-")
        without_organic = raw.removeprefix("Organic-")
        base, separator, detail = without_organic.partition(" - ")
        try:
            product, group = FRUIT_VEGETABLE_PRODUCTS[base]
        except KeyError as exc:
            raise ValueError(f"ambiguous_product:{raw}") from exc
        details = []
        if organic:
            details.append("organic")
        if "for processing" in base.lower():
            details.append("for processing")
        if base == "Kiwis Hayward":
            details.append("hayward")
        if base == "Bananas – EU – All types and varieties":
            details.extend(["eu origins", "all types and varieties"])
        if separator and detail:
            details.append(detail.strip().lower())
        return _mapping(raw, product, ", ".join(details), group, mapping_status="rule")
    if dataset == "olive_oil":
        raw = str(row["Product"]).strip()
        detail = re.sub(r"\s+olive(?:-pomace)? oil", "", raw, flags=re.I).strip().lower()
        return _mapping(raw, "olive oil", detail, "oils and fats")
    if dataset == "oilseeds":
        raw = str(row["Product"]).strip()
        if raw not in OILSEED_PRODUCTS:
            raise ValueError(f"non_food_feed_product:{raw}")
        return _mapping(
            raw,
            OILSEED_PRODUCTS[raw],
            _clean_detail(row["Product Type"]).lower(),
            "oils and fats",
        )
    if dataset == "protein_crops":
        raw = str(row["Product"]).strip()
        return _mapping(raw, PROTEIN_CROP_PRODUCTS[raw], _clean_detail(row["Product Type"]).lower(), "vegetables and pulses")
    if dataset == "sugar":
        return _mapping("Sugar", "sugar", "", "sugar and confectionery")
    raise ValueError(f"unsupported_dataset:{dataset}")
