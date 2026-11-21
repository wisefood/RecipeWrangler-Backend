"""Local, provenance-preserving FoodOn dictionary; no composition decisions."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from recipe_wrangler.matching.facets import extract_mention_facets
from recipe_wrangler.utils.food_identity import compare_identities

FOOD_MATERIAL = "http://purl.obolibrary.org/obo/FOODON_00002403"
# Explicit local interpretation of definitions, not synonyms asserted by FoodOn.
REVIEWED_ALIASES = {
    "egg": ("FOODON_00001274", "Generic egg food product; no producer inferred"),
    "egg white": ("FOODON_02020001", "Generic animal egg white; no producer inferred"),
    "egg yolk": ("FOODON_02010009", "Generic animal egg yolk; no producer inferred"),
}

# Reviewed lexicalized names: their cooking word is part of the food name.
LEXICALIZED_IDENTITIES = {"baked beans": "FOODON_03301946"}


def normalize_label(text: str) -> str:
    """Normalize spacing/case and conservative food plurals, preserving word order."""
    words = re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)
    words = [{'chili':'chilli', 'chilies':'chillies'}.get(word,word) for word in words]
    irregular = {"eggs": "egg", "beans": "bean", "apples": "apple", "potatoes": "potato", "tomatoes": "tomato", "whites": "white", "yolks": "yolk", "breasts": "breast", "strawberries": "strawberry", "raspberries": "raspberry", "blueberries": "blueberry", "berries": "berry", "chillies": "chilli", "leaves": "leaf", "yoghurt": "yogurt", "yoghurts": "yogurt", "courgette": "zucchini", "courgettes": "zucchini"}
    return " ".join(irregular.get(word, word) for word in words)


@dataclass(frozen=True)
class DictionaryHit:
    term_id: str
    preferred_label: str
    matched_label: str
    label_type: str
    definition: str
    curation_status: str


class IngredientDictionary:
    def __init__(self, payload: dict) -> None:
        if payload.get("schema_version") != 1:
            raise ValueError("Unsupported ingredient dictionary schema")
        self.payload = payload
        self.index: dict[str, list[DictionaryHit]] = defaultdict(list)
        for term in payload["terms"]:
            for label in term["labels"]:
                hit = DictionaryHit(term["term_id"], term["preferred_label"], label["text"], label["type"], term["definition"], term["curation_status"])
                self.index[normalize_label(label["text"])].append(hit)

    @classmethod
    def load(cls, path: Path) -> IngredientDictionary:
        return cls(json.loads(path.read_text()))

    def lookup(self, text: str) -> tuple[DictionaryHit, ...]:
        """Return every matching concept; shared labels never imply merged IDs."""
        hits = self.index.get(normalize_label(text), ())
        # A preferred label and synonym can normalize to the same concept.
        return tuple({hit.term_id: hit for hit in hits}.values())

    def extract(self, text: str) -> dict:
        """Annotate a single parsed ingredient phrase, without guessing unknown foods."""
        if not text.strip():
            raise ValueError("Ingredient text is required")
        extraction = extract_mention_facets(text, text)
        # Preserve complete lexical food names before existing facet stripping.
        # Prepared record labels remain qualified concepts, not new base identities.
        direct = self.lookup(text)
        preserve = bool(direct) and (
            extraction.facets.preparation_state is None
            or normalize_label(text) in self.payload.get("lexicalized_identities", {})
        )
        base = extraction.facets.base_identity or text
        # Existing base normalization sorts tokens for comparison. Restore source
        # order before phrase lookup ("peanut butter", never "butter peanut").
        remaining = set(base.split())
        ordered = []
        for word in normalize_label(text).split():
            for token in compare_identities(word, word).mention_key:
                if token in remaining:
                    ordered.append(token)
                    remaining.remove(token)
        identity = normalize_label(text) if preserve else " ".join(ordered + sorted(remaining))
        hits = self.lookup(identity)
        needs_context = bool(re.search(r"\b(?:not|never|for|or)\b", text, re.I))
        return {
            "original_text": text,
            "main_ingredient": identity,
            "identity_status": "needs_context_review" if needs_context else "resolved" if len(hits) == 1 else "ambiguous" if hits else "unresolved",
            "context_warning": "Rule facets may misinterpret negation, intended use or alternatives" if needs_context else None,
            "identity_method": "source_label_lookup_with_rule_facets",
            "concepts": [asdict(hit) for hit in hits],
            "facets": asdict(extraction.facets),
            "extraction_evidence": extraction.evidence,
            "dictionary_provenance": self.payload["provenance"],
        }


def build_dictionary(source_dir: Path) -> IngredientDictionary:
    """Use asserted food-material descendants and English exact synonyms only."""
    def rows(name: str) -> list[dict]:
        with (source_dir / name).open(newline="", encoding="utf-8") as stream:
            return list(csv.DictReader(stream))

    children: dict[str, set[str]] = defaultdict(set)
    for edge in rows("foodon_named_parents.csv"):
        children[edge["parent_iri"]].add(edge["child_iri"])
    eligible: set[str] = set()
    pending = list(children[FOOD_MATERIAL])
    while pending:
        iri = pending.pop()
        if iri in eligible:
            continue
        eligible.add(iri)
        pending.extend(children[iri])
    # Reviewed edible parts live under anatomy rather than food material.
    reviewed_parts = {"http://purl.obolibrary.org/obo/" + code for code, _ in REVIEWED_ALIASES.values()}
    eligible.update(reviewed_parts)
    terms = {}
    for row in rows("foodon_terms.csv"):
        if row["iri"] not in eligible or row["deprecated"].casefold() != "false" or not row["label"].strip():
            continue
        terms[row["iri"]] = {
            "term_id": row["iri"], "preferred_label": row["label"],
            "definition": row["definition"], "curation_status": row["curation_status"],
            "labels": [{"text": row["label"], "type": "preferred"}],
        }
    for row in rows("foodon_synonyms.csv"):
        if row["iri"] in terms and row["type"] == "hasExactSynonym" and row["language"].casefold() in {"", "en", "en-gb", "en-us"}:
            terms[row["iri"]]["labels"].append({"text": row["label"], "type": "exact_synonym"})
    for alias, (term_id, reason) in REVIEWED_ALIASES.items():
        iri = "http://purl.obolibrary.org/obo/" + term_id
        if iri not in terms:
            raise ValueError(f"Reviewed alias target is missing or ineligible: {iri}")
        terms[iri]["labels"].append({"text": alias, "type": "local_reviewed_alias", "reason": reason})
    filenames = ("foodon_terms.csv", "foodon_synonyms.csv", "foodon_named_parents.csv", "inventory_summary.json")
    provenance = {"source": "FoodOn", "root": FOOD_MATERIAL, "explicit_additional_concepts": sorted(reviewed_parts),
                  "files": {name: hashlib.sha256((source_dir / name).read_bytes()).hexdigest() for name in filenames},
                  "snapshot_metadata": json.loads((source_dir / "inventory_summary.json").read_text()),
                  "policy": "asserted food-material descendants; nondeprecated; preferred labels and English exact synonyms; explicit local aliases"}
    lexicalized = {}
    for label, code in LEXICALIZED_IDENTITIES.items():
        iri = "http://purl.obolibrary.org/obo/" + code
        if iri not in terms or not any(item["text"] == label for item in terms[iri]["labels"]):
            raise ValueError(f"Lexicalized name lacks source label evidence: {label}")
        lexicalized[normalize_label(label)] = iri
    return IngredientDictionary({"schema_version": 1, "provenance": provenance,
        "lexicalized_identities": lexicalized,
        "terms": sorted(terms.values(), key=lambda term: term["term_id"])})
