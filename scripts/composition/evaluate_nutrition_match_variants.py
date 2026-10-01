#!/usr/bin/env python3
"""Compare experimental nutrition-matcher variants without changing production.

This is an ablation harness, not another matcher implementation to ship. It reuses
the production cleaner, retrieval, gates, aliases, and constants, then varies only
the candidate metadata gate and final ranking policy.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recipe_wrangler.tools import nutrition_match as nm
from recipe_wrangler.utils.non_food_ingredients import (
    is_unambiguous_non_food_ingredient,
)


@dataclass(frozen=True)
class Variant:
    metadata_gate: bool = False
    single_lexical_signal: bool = False
    facet_penalty: bool = False
    identity_gate: bool = False
    hard_facet_gate: bool = False
    stock_pot_form: bool = False
    conservative_confidence: bool = False
    reject_non_food: bool = False
    stable_pool_scoring: bool = False
    top_identity_veto: bool = False


VARIANTS = {
    "current": Variant(),
    "metadata": Variant(metadata_gate=True),
    "single_lexical": Variant(single_lexical_signal=True),
    "metadata_single": Variant(metadata_gate=True, single_lexical_signal=True),
    "facet_penalty": Variant(facet_penalty=True),
    "identity_gate": Variant(identity_gate=True, stable_pool_scoring=True),
    "identity_facets": Variant(
        identity_gate=True,
        hard_facet_gate=True,
        stock_pot_form=True,
        reject_non_food=True,
        stable_pool_scoring=True,
    ),
    "stock_pot": Variant(stock_pot_form=True),
    "conservative": Variant(conservative_confidence=True),
    "reject_non_food": Variant(reject_non_food=True),
    "top_identity_veto": Variant(
        reject_non_food=True,
        top_identity_veto=True,
    ),
    "targeted": Variant(
        facet_penalty=True,
        stock_pot_form=True,
        reject_non_food=True,
    ),
    "combined": Variant(
        metadata_gate=True,
        single_lexical_signal=True,
        facet_penalty=True,
        stock_pot_form=True,
        conservative_confidence=True,
        reject_non_food=True,
    ),
}


_CIQUAL_TYPES = {
    "meat, egg and fish": {"animal_protein", "egg"},
    "fruits, vegetables, legumes and nuts": {
        "fruit", "vegetable", "leafy_green", "legume", "nut_seed",
    },
    "sugar and confectionery": {"sweetener"},
    "milk and milk products": {"dairy"},
    "beverages": {"beverage", "alcohol"},
    "cereal products": {"grain_cereal"},
    "fats and oils": {"oil_fat"},
}

_NEVO_TYPES = {
    "vegetables": {"vegetable", "leafy_green", "legume"},
    "meat and poultry": {"animal_protein"},
    "pastry and biscuits": {"grain_cereal", "sweetener"},
    "cereal products and types of flour": {"grain_cereal"},
    "milk and milk products": {"dairy"},
    "sugar, sweets and sweet sauces": {"sweetener"},
    "bread": {"grain_cereal"},
    "non-alcoholic beverages": {"beverage"},
    "fruits": {"fruit"},
    "fish, crustacean and shellfish": {"animal_protein"},
    "savoury sauces": {"condiment_sauce"},
    "cheese": {"dairy"},
    "fats and oils": {"oil_fat"},
    "cold meat cuts": {"animal_protein"},
    "herbs and spices": {"spice_herb"},
    "potatoes and tubers": {"vegetable"},
    "alcoholic beverages": {"alcohol"},
    "legumes": {"legume"},
    "nuts and seeds": {"nut_seed"},
    "eggs": {"egg"},
}

# Official CoFID Appendix-B top-level groups. Sub-group codes inherit the first
# letter, which is a safer broad gate than re-deriving type from the food name.
_COFID_TYPES = {
    "A": {"grain_cereal"},
    "B": {"dairy"},
    "C": {"egg"},
    "D": {"vegetable", "leafy_green", "legume"},
    "F": {"fruit"},
    "G": {"nut_seed"},
    "H": {"spice_herb"},
    "J": {"animal_protein"},
    "M": {"animal_protein"},
    "O": {"oil_fat"},
    "P": {"beverage"},
    "Q": {"alcohol"},
    "W": {"condiment_sauce"},
}

_SLOVENIAN_GROUP_TYPES = {
    "vegetables": {"vegetable", "leafy_green", "legume"},
    "cereals and grain products": {"grain_cereal"},
    "milled cereal products": {"grain_cereal"},
    "bread": {"grain_cereal"},
    "fruits": {"fruit"},
    "oilseeds and olive oil": {"nut_seed", "oil_fat"},
}

_FORM_NORMALIZATION = {
    "dry": "dried",
    "dehydrated": "dried",
    "fillets": "fillet",
    "seeds": "seed",
}
_RISKY_FACETS = {
    "skin", "seed", "spread", "juice", "puree", "powder", "syrup",
    "dried", "salted", "smoked", "cured", "concentrate", "concentrated",
}

# Words that describe an ingredient without naming its food identity. They are
# useful as facets later, but must not by themselves make two foods identical.
_IDENTITY_MODIFIERS = {
    "and", "with", "of", "the", "in", "a", "an", "fresh", "whole",
    "large", "small", "medium", "prepared", "made", "ready", "mixed",
    "mix", "blend", "baby", "chopped", "diced", "sliced", "minced",
    "grated", "shredded", "crushed", "ground", "stewed", "smoked",
    "dry", "dried", "frozen", "canned", "cooked", "raw", "roasted",
    "fried", "boiled", "steamed", "baked", "firm", "soft", "wild",
    "light", "mild", "savoy", "english", "european", "italian",
    "spanish", "french", "greek", "low", "reduced", "free", "sodium",
    "fat", "boneless", "skinless", "unsalted", "salted", "white",
    "black", "brown", "red", "green", "yellow", "purple", "pink", "curly",
}
_IDENTITY_TRAILING_FORMS = {
    "pot", "tub", "can", "jar", "bottle", "packet", "bag", "package",
    "fillet", "slice", "piece", "chunk", "stick", "flake", "section",
    "segment", "half", "halves", "cube", "carcass", "head", "leaf",
    "stalk", "spear", "tip", "kit", "liquid", "floret", "filet",
    "quill", "steak", "ring", "strip", "sliver", "shaving", "shell",
    "round", "ribbon", "granule",
}
_PRODUCT_HEADS = {
    "stock", "broth", "bouillon", "juice", "oil", "flour", "meal",
    "butter", "milk", "cheese", "yogurt", "yoghurt", "cream", "sauce",
    "paste", "puree", "powder", "extract", "syrup", "wine", "beer",
    "vinegar", "rice", "pasta", "noodle", "bread", "soup", "salad",
    "cereal", "sugar", "tofu", "water", "margarine", "spray", "spread",
}
_COLOR_SENSITIVE_HEADS = {
    "pepper", "fish", "bean", "rice", "wine", "sugar", "chocolate",
    "bread", "tea", "lentil", "pea",
}
_SOURCE_REQUIRED_HEADS = _PRODUCT_HEADS | {"shoot", "leaf", "seed"}
_IDENTITY_EQUIVALENTS = (
    {"stock", "broth", "bouillon"},
    {"yogurt", "yoghurt"},
    {"chili", "chilli"},
    {"corn", "maize"},
    {"apple", "cider"},
)
_PRODUCT_SUBTYPE_WORDS = {
    "fettuccine", "fusilli", "linguine", "macaroni", "penne", "rigatoni",
    "spaghetti", "tagliatelle", "vermicelli",
}
_FACET_WORDS = {
    "dried": {"dry", "dried", "dehydrated", "flake", "flakes"},
    "cooked": {
        "cooked", "boiled", "steamed", "roasted", "fried", "baked",
        "stewed", "grilled", "casseroled", "simmered", "poached",
        "toasted", "chargrilled", "braised",
    },
    "smoked": {"smoked"},
    "cured": {"cured"},
    "pickled": {"pickled", "fermented"},
    "canned": {"canned", "tinned"},
    "frozen": {"frozen"},
    "salted": {"salted", "brined"},
    "juice": {"juice"},
    "oil": {"oil"},
    "spread": {"spread"},
    "extract": {"extract"},
    "paste": {"paste", "puree", "pureed"},
    "powder": {"powder", "powdered"},
    "syrup": {"syrup"},
    "concentrate": {"concentrate", "concentrated"},
    "skin": {"skin"},
    "seed": {"seed", "seeds", "kernel", "kernels"},
    "shoot": {"shoot", "shoots", "tops"},
    "organ": {"liver", "heart", "kidney", "giblet", "giblets"},
    "stock": {"stock", "broth", "bouillon"},
    "soup": {"soup"},
    "salad": {"salad"},
    "composite": {"pie", "cake", "pudding", "biscuit", "pastry", "pasty", "sandwich", "pizza", "casserole", "burger"},
    "drink": {"drink", "beverage", "smoothie", "shake"},
}
_STRICT_EXTRA_FACETS = frozenset(_FACET_WORDS) - {"seed", "shoot", "salad"}
_STRICT_REQUIRED_FACETS = _STRICT_EXTRA_FACETS - {"skin", "organ", "salted"}


def _candidate_types(candidate: dict[str, Any]) -> set[str] | None:
    meta = candidate.get("metadata") or {}
    source = str(meta.get("source") or "").strip().casefold()
    group = str(meta.get("food_group") or "").strip()
    group_key = group.casefold()
    if source == "ciqual":
        return _CIQUAL_TYPES.get(group_key)
    if source == "nevo":
        return _NEVO_TYPES.get(group_key)
    if source == "cofid" and group:
        code = group.upper()
        if code.startswith(("SC", "SE")):
            return {"sweetener"}
        if code.startswith("SN"):
            return None
        return _COFID_TYPES.get(group[0].upper())
    if candidate.get("_source_key") == "slovenian":
        direct = _SLOVENIAN_GROUP_TYPES.get(group_key)
        if direct:
            return direct
        if any(word in group_key for word in ("fish", "meat", "beef", "pork", "veal", "poultry", "deer", "venison", "mutton", "lamb", "goose", "turkey", "duck", "rabbit", "boar", "hare", "horse", "roe")):
            return {"animal_protein"}
    return None


def _metadata_compatible(query_class: str, candidate: dict[str, Any]) -> bool:
    types = _candidate_types(candidate)
    if query_class == "other" or not types:
        return True
    if "beverage" in types:
        return query_class in {"alcohol", "other"}
    return any(nm.classes_compatible(query_class, candidate_type) for candidate_type in types)


def _form_compatible(query: str, candidate_name: str, variant: Variant) -> bool:
    if variant.stock_pot_form:
        words = set(nm._TOKEN_RE.findall(query.casefold()))
        if "pot" in words and {"stock", "broth", "bouillon"} & words:
            query = f"{query} cube"
    return nm.ingredient_forms_compatible(query, candidate_name)


def _normalized_raw_words(value: str) -> set[str]:
    words = set(nm._TOKEN_RE.findall(nm._ascii_fold(value).casefold()))
    return {_FORM_NORMALIZATION.get(word, word) for word in words}


def _facet_penalty(query: str, candidate_name: str) -> float:
    query_facets = _RISKY_FACETS & _normalized_raw_words(query)
    candidate_facets = _RISKY_FACETS & _normalized_raw_words(candidate_name)
    return -0.20 * len(candidate_facets - query_facets)


def _raw_singular_tokens(value: str) -> list[str]:
    return [nm._singular(word) for word in nm._TOKEN_RE.findall(nm._ascii_fold(value).casefold())]


def _expanded_token_set(tokens: list[str]) -> set[str]:
    result = set(tokens)
    for token in tuple(result):
        synonym = nm._SYNONYMS.get(token) or nm._SYNONYMS_REV.get(token)
        if synonym:
            result.add(nm._singular(synonym))
    for equivalents in _IDENTITY_EQUIVALENTS:
        if result & equivalents:
            result.update(equivalents)
    return result


def _identity_signature(value: str) -> tuple[str, set[str], set[str]] | None:
    tokens = _raw_singular_tokens(value)
    content = [
        token for token in tokens
        if token not in _IDENTITY_MODIFIERS and token not in _IDENTITY_TRAILING_FORMS
    ]
    if not content:
        return None
    head = content[-1]
    sources = {
        token for token in content[:-1]
        if (
            head in _SOURCE_REQUIRED_HEADS
            and token not in _PRODUCT_SUBTYPE_WORDS
        )
    }
    # Colour is part of identity for a small set of established food types
    # (black pepper, white fish, brown rice, red wine), but merely descriptive
    # for most produce (white turnip, brown onion).
    if head in _COLOR_SENSITIVE_HEADS:
        sources.update(
            token for token in tokens
            if token in {"black", "white", "brown", "red", "green", "yellow"}
        )
    return (
        head,
        sources,
        _expanded_token_set(content),
    )


def _identity_signatures(value: str) -> list[tuple[str, set[str], set[str]]]:
    parts = [part.strip() for part in re.split(r"\bor\b", value.casefold()) if part.strip()]
    if len(parts) == 1:
        signature = _identity_signature(parts[0])
        return [signature] if signature else []
    last = _identity_signature(parts[-1])
    last_head = last[0] if last else None
    signatures: list[tuple[str, set[str], set[str]]] = []
    for index, part in enumerate(parts):
        signature = _identity_signature(part)
        if not signature:
            continue
        head, sources, anchors = signature
        if index < len(parts) - 1 and last_head in _PRODUCT_HEADS and head not in _PRODUCT_HEADS:
            sources = _expanded_token_set([head, *sources])
            head = last_head
        signatures.append((head, sources, anchors))
    return signatures


def _token_matches(token: str, candidates: set[str]) -> bool:
    if token in candidates:
        return True
    if len(token) >= 4 and any(
        len(candidate) >= 4 and (candidate.startswith(token) or token.startswith(candidate))
        for candidate in candidates
    ):
        return True
    joined = "".join(sorted(candidates))
    return len(token) >= 5 and token in joined


def _identity_compatible(query: str, candidate_name: str) -> bool:
    candidate_tokens = _expanded_token_set(_raw_singular_tokens(candidate_name))
    for head, sources, anchors in _identity_signatures(query):
        if head in _SOURCE_REQUIRED_HEADS:
            head_matches = _token_matches(head, candidate_tokens)
            source_matches = not sources or all(
                _token_matches(source, candidate_tokens) for source in sources
            )
            if head_matches and source_matches:
                return True
        elif _token_matches(head, candidate_tokens):
            return True
    return False


def _has_core_identity_overlap(query: str, candidate_name: str) -> bool:
    """Broad top-result sanity check; unknown identities pass unchanged."""
    signatures = _identity_signatures(query)
    if not signatures:
        return True
    candidate_tokens = _expanded_token_set(_raw_singular_tokens(candidate_name))
    return any(
        _token_matches(anchor, candidate_tokens)
        for _, _, anchors in signatures
        for anchor in anchors
    )


def _facets(value: str) -> set[str]:
    folded = nm._ascii_fold(value).casefold()
    words = set(nm._TOKEN_RE.findall(folded))
    facets = {
        facet for facet, variants in _FACET_WORDS.items()
        if words & variants
    }
    if "without skin" in folded or "skinless" in words:
        facets.discard("skin")
    if "without seed" in folded or "seedless" in words:
        facets.discard("seed")
    if (
        "unsalted" in words
        or "un-salted" in folded
        or "no salt" in folded
        or "without salt" in folded
    ):
        facets.discard("salted")
    if nm.food_class(value) != "animal_protein":
        facets.discard("organ")
        facets.discard("skin")
    return facets


def _hard_facets_compatible(query: str, candidate_name: str) -> bool:
    query_facets = _facets(query)
    candidate_facets = _facets(candidate_name)
    if (candidate_facets - query_facets) & _STRICT_EXTRA_FACETS:
        return False
    if (query_facets - candidate_facets) & _STRICT_REQUIRED_FACETS:
        return False
    return True


def _candidate_pools(source: str, query: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for source_key, function in nm._candidate_pools(source):
        try:
            hits = function(query) or []
        except Exception as exc:
            raise RuntimeError(f"{source_key} retrieval failed: {exc}") from exc
        for candidate in hits:
            if isinstance(candidate, dict):
                candidates.append({**candidate, "_source_key": source_key})
    return candidates


def _alias_result(raw_name: str, cleaned: str, source: str) -> dict[str, Any] | None:
    alias = nm._curated_alias_lookup(raw_name, cleaned)
    if alias is None:
        return None
    for source_key, food_id in ((source, alias.get(source)), ("eu", alias.get("eu_food_id"))):
        if not food_id:
            continue
        record = nm.get_nutrition_candidate_by_source_id(nm._REGION_COLLECTIONS[source_key], food_id)
        if record is not None:
            return {
                "id": record.get("id"),
                "matched_name": nm._candidate_name(record),
                "source_key": source_key,
                "similarity": None,
                "confidence": "curated",
                "reason": "alias",
                "score": None,
                "margin": None,
            }
    return None


def evaluate_name(
    name: str,
    source: str,
    variant_names: list[str] | None = None,
    *,
    identity_name: str | None = None,
) -> dict[str, dict[str, Any]]:
    selected_variants = {
        variant_name: VARIANTS[variant_name]
        for variant_name in (variant_names or list(VARIANTS))
    }
    cleaned = nm.clean_query(name) or str(name or "").strip().casefold()
    identity_raw = identity_name or name
    identity_cleaned = (
        nm.clean_query(identity_raw) or str(identity_raw or "").strip().casefold()
    )
    alias = _alias_result(name, cleaned, source)
    if alias is not None:
        return {variant_name: dict(alias) for variant_name in selected_variants}

    query_tokens = nm._tokens(cleaned)
    query_set = set(query_tokens)
    query_class = nm.food_class(identity_cleaned)
    retrieval_extra = {
        synonym
        for raw_token in nm._TOKEN_RE.findall(cleaned)
        for synonym in (nm._SYNONYMS.get(raw_token), nm._SYNONYMS_REV.get(raw_token))
        if synonym and synonym not in cleaned
    }
    retrieval_query = f"{cleaned} {' '.join(sorted(retrieval_extra))}".strip()
    candidates = _candidate_pools(source, retrieval_query)
    results: dict[str, dict[str, Any]] = {}
    production_survivors = [
        candidate
        for candidate in candidates
        if nm.classes_compatible(
            query_class, nm.food_class(nm._candidate_name(candidate))
        )
        and nm.animal_kinds_compatible(
            identity_cleaned, nm._candidate_name(candidate)
        )
        and _form_compatible(cleaned, nm._candidate_name(candidate), Variant())
    ]

    for variant_name, variant in selected_variants.items():
        if variant.reject_non_food and is_unambiguous_non_food_ingredient(identity_raw):
            results[variant_name] = {
                "id": None, "matched_name": None, "source_key": source,
                "similarity": None, "confidence": "none", "reason": "non_food",
                "score": None, "margin": None,
            }
            continue

        survivors = [
            candidate
            for candidate in candidates
            if nm.classes_compatible(query_class, nm.food_class(nm._candidate_name(candidate)))
            and nm.animal_kinds_compatible(identity_cleaned, nm._candidate_name(candidate))
            and _form_compatible(cleaned, nm._candidate_name(candidate), variant)
            and (not variant.metadata_gate or _metadata_compatible(query_class, candidate))
            and (
                not variant.identity_gate
                or _identity_compatible(identity_cleaned, nm._candidate_name(candidate))
            )
            and (not variant.hard_facet_gate or _hard_facets_compatible(name, nm._candidate_name(candidate)))
        ]
        if not survivors:
            results[variant_name] = {
                "id": None, "matched_name": None, "source_key": source,
                "similarity": None, "confidence": "none",
                "reason": "no_semantically_compatible_candidates", "score": None,
                "margin": None,
            }
            continue

        scoring_pool = survivors
        if variant.stable_pool_scoring:
            production_ids = {id(candidate) for candidate in production_survivors}
            scoring_pool = production_survivors + [
                candidate for candidate in survivors
                if id(candidate) not in production_ids
            ]
        scoring_names = [nm._candidate_name(candidate) for candidate in scoring_pool]
        scoring_tokenized = [nm._tokens(candidate_name) for candidate_name in scoring_names]
        scoring_bm25 = (
            nm._bm25_scores(query_tokens, scoring_tokenized)
            if query_tokens else [0.0] * len(scoring_pool)
        )
        lexical_by_candidate = {
            id(candidate): lexical_score
            for candidate, lexical_score in zip(scoring_pool, scoring_bm25)
        }
        names = [nm._candidate_name(candidate) for candidate in survivors]
        tokenized = [nm._tokens(candidate_name) for candidate_name in names]
        bm25 = [lexical_by_candidate[id(candidate)] for candidate in survivors]
        query_compound = "".join(query_tokens) if len(query_tokens) > 1 else None
        query_raw_words = _normalized_raw_words(name)
        max_rrf = max(
            (float(candidate.get("rrf_score") or 0.0) for candidate in scoring_pool),
            default=0.0,
        )
        ranked: list[tuple[float, float, int, dict[str, Any], str]] = []

        for candidate, candidate_name, candidate_tokens, lexical_score in zip(
            survivors, names, tokenized, bm25
        ):
            distance = candidate.get("distance")
            similarity = 1.0 - float(distance) if distance is not None else 0.0
            candidate_token_set = set(candidate_tokens)
            overlap = len(query_set & candidate_token_set)
            if overlap == 0:
                candidate_compound = "".join(candidate_tokens) if len(candidate_tokens) > 1 else None
                if (query_compound and query_compound in candidate_token_set) or (
                    candidate_compound and candidate_compound in query_set
                ):
                    overlap = 1
                elif len(query_tokens) == 1:
                    query_token = query_tokens[0]
                    if (
                        query_class != "other"
                        and query_class == nm.food_class(candidate_name)
                        and len(query_token) >= 4
                        and any(
                            len(token) >= 4
                            and (token.startswith(query_token) or query_token.startswith(token))
                            for token in candidate_tokens
                        )
                    ):
                        overlap = 1

            rrf = float(candidate.get("rrf_score") or 0.0) / max_rrf if max_rrf else 0.0
            penalty = 0.0
            if overlap == 0 and similarity < nm._HIGH_SIM_NO_OVERLAP:
                penalty -= 0.5
            candidate_raw_words = _normalized_raw_words(candidate_name)
            if (nm._PROCESSED_MARKERS & candidate_raw_words) - query_raw_words:
                penalty -= 0.12
            elif "raw" in candidate_raw_words and not (nm._COOKING_STATES & query_raw_words):
                penalty += 0.06
            if variant.facet_penalty:
                penalty += _facet_penalty(name, candidate_name)

            overlap_fraction = min(1.0, overlap / max(1, len(query_tokens)))
            if variant.single_lexical_signal:
                score = 0.75 * similarity + 0.15 * rrf + 0.10 * overlap_fraction + penalty
            else:
                score = (
                    0.60 * similarity
                    + 0.30 * lexical_score
                    + 0.10 * rrf
                    + 0.15 * overlap_fraction
                    + penalty
                )
            ranked.append((score, similarity, overlap, candidate, candidate_name))

        ranked.sort(key=lambda item: -item[0])
        score, similarity, overlap, winner, winner_name = ranked[0]
        margin = score - ranked[1][0] if len(ranked) > 1 else score
        if variant.top_identity_veto and not _has_core_identity_overlap(
            identity_cleaned, winner_name
        ):
            confidence = "none"
            match_id = None
            reason = "top_identity_mismatch"
        elif score < nm._WEAK_SCORE:
            confidence = "none"
            match_id = None
            reason = f"below_floor:{score:.2f}"
        else:
            strong = (
                score >= nm._STRONG_SCORE
                and similarity >= 0.70
                and (overlap > 0 or similarity >= nm._HIGH_SIM_NO_OVERLAP)
            )
            if variant.conservative_confidence:
                strong = strong and margin >= 0.04 and overlap > 0
            confidence = "strong" if strong else "weak"
            match_id = winner.get("id") if confidence != "none" else None
            reason = "" if strong else f"weak:{score:.2f}"

        results[variant_name] = {
            "id": match_id,
            "matched_name": winner_name,
            "source_key": winner.get("_source_key", source),
            "similarity": similarity,
            "confidence": confidence,
            "reason": reason,
            "score": score,
            "margin": margin,
        }
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--names", type=Path, required=True)
    parser.add_argument("--source", choices=("eu", "irish", "hungarian", "slovenian"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--variants", nargs="+", choices=tuple(VARIANTS), default=None)
    args = parser.parse_args()

    names = json.loads(args.names.read_text())
    started = time.time()
    rows = []
    for index, item in enumerate(names, start=1):
        if isinstance(item, dict):
            name = str(item.get("query_with_note") or item.get("name") or "")
            identity_name = str(item.get("name") or name)
        else:
            name = str(item)
            identity_name = None
        try:
            variants = evaluate_name(
                name, args.source, args.variants, identity_name=identity_name
            )
            row = {"query": name, "variants": variants}
            if identity_name is not None:
                row["identity_query"] = identity_name
            rows.append(row)
        except Exception as exc:
            rows.append({"query": name, "error": f"{type(exc).__name__}: {exc}"})
        if index % 200 == 0:
            print(f"{index}/{len(names)} in {time.time() - started:.0f}s", flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    print(f"wrote {args.output} in {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()
