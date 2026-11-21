"""Source-pinned environmental references; lexical lookup never loads a model."""

import math
import os
import re
import unicodedata

import requests


REFERENCE_ALIAS = "sustainability_references"
POLICY_VERSION = "agribalyse-3.2-no-preparation-exact-v1"


def normalize(name: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", unicodedata.normalize("NFKC", name).casefold()).split())


# Reviewed names only. Qualifiers on all other input names are retained.
REVIEWED_NAMES = {
    **{name: "garlic" for name in ("garlic", "garlic raw", "raw garlic", "fresh garlic", "garlic fresh", "garlic clove", "garlic cloves")},
    "carrot": "carrot raw", "carrots": "carrot raw",
    "onion": "onion raw", "onions": "onion raw",
    "tomato": "tomato raw", "tomatoes": "tomato raw",
    "broccoli": "broccoli raw", "spinach": "spinach raw",
    "garlic powder": "garlic powder dried",
    "extra virgin olive oil": "olive oil extra virgin",
    "rapeseed oil": "rapeseed oil",
    "dried lentils": "lentil dried", "red lentils dried": "lentil pink or red dried",
    "brown sugar": "sugar brown", "white sugar": "sugar white",
    "fresh basil": "basil fresh", "fresh parsley": "parsley fresh",
}


def food_key(name: str) -> str:
    key = normalize(name)
    return REVIEWED_NAMES.get(key, key)


def eligible(record: dict) -> bool:
    metric = record.get("metrics", {}).get("climate_change", {})
    value = metric.get("value")
    return (
        record.get("source") == "AGRIBALYSE"
        and record.get("version") == "3.2-food-AOUT25"
        and record.get("method") == "AGRIBALYSE_EF3.1"
        and record.get("market") == "FR"
        and record.get("basis") == "kg_consumed_product"
        and record.get("boundary") == "consumer_product_including_intermediate_losses_and_declared_preparation"
        and record.get("scenario", {}).get("preparation") == "Pas de préparation"
        and metric.get("unit") == "kg CO2 eq/kg de produit"
        and isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and value >= 0
        and all(record.get(key) for key in ("reference_id", "source_record_id", "source_url", "source_sha256"))
    )


def select_reference(name: str, records: list[dict]) -> dict | None:
    """Abstain on ambiguous identities; never choose by factor magnitude."""
    matches = [record for record in records if eligible(record) and food_key(record["name"]) == food_key(name)]
    return matches[0] if len(matches) == 1 else None


def lookup_references(names: list[str]) -> tuple[dict[str, dict], str]:
    """One bounded query per 100 names. An unavailable catalogue preserves legacy behavior."""
    url = os.getenv("ELASTIC_URL", "http://localhost:9200").rstrip("/")
    index = os.getenv("SUSTAINABILITY_REFERENCE_INDEX", REFERENCE_ALIAS)
    keys = sorted({food_key(name) for name in names})
    records = []
    try:
        for offset in range(0, len(keys), 100):
            response = requests.post(f"{url}/{index}/_search", json={
                "size": 100, "timeout": "3s", "track_total_hits": True,
                "query": {"bool": {"filter": [
                    {"term": {"calculation_eligible": True}},
                    {"terms": {"food_key": keys[offset:offset + 100]}},
                ]}},
            }, timeout=4)
            response.raise_for_status()
            result = response.json()
            total = result["hits"]["total"]
            if result.get("timed_out") or total.get("relation") != "eq" or total["value"] > 100:
                return {}, "reference_query_incomplete"
            records.extend(hit["_source"]["reference"] for hit in result["hits"]["hits"])
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return {}, "reference_catalogue_unavailable"
    return {name: record for name in names if (record := select_reference(name, records)) is not None}, "available"


def complete_recipe_selection(names: list[str], weights: list[float], selected: dict[str, dict]) -> bool:
    """A recipe uses one coherent source policy or retains its complete legacy calculation."""
    return bool(names) and len(names) == len(weights) and all(
        math.isfinite(weight) and weight >= 0 and (weight == 0 or name in selected)
        for name, weight in zip(names, weights)
    )
