import json
import uuid
from pathlib import Path

from tqdm import tqdm


# Purpose: Build canonical ingredient mappings from Recipe1M layer1 + det_ingrs and write canonical/invalid lists.

def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def main():
    repo_root = Path(__file__).resolve().parents[2]
    layer1_path = repo_root / "notebooks" / "layer1.json"
    det_ingrs_path = repo_root / "foodkg.github.io" / "src" / "verify" / "data" / "det_ingrs.json"
    out_canonical_path = repo_root / "data" / "recipe1m_canonical_ingredients.json"
    out_invalid_path = repo_root / "data" / "recipe1m_invalid_ingredients.json"

    layer1 = load_json(layer1_path)
    det_ingrs = load_json(det_ingrs_path)

    # Map recipe id -> original ingredient texts from layer1.
    def strip_quantity_unit(text: str) -> str:
        units = {
            "cup",
            "cups",
            "ounce",
            "ounces",
            "oz",
            "teaspoon",
            "teaspoons",
            "tsp",
            "tablespoon",
            "tablespoons",
            "tbsp",
            "pound",
            "pounds",
            "lb",
            "lbs",
            "gram",
            "grams",
            "g",
            "kilogram",
            "kilograms",
            "kg",
            "milliliter",
            "milliliters",
            "ml",
            "liter",
            "liters",
            "l",
            "pinch",
            "dash",
            "clove",
            "cloves",
            "package",
            "packages",
            "can",
            "cans",
            "stick",
            "sticks",
            "slice",
            "slices",
        }
        tokens = text.strip().lower().replace(",", " ").split()
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token in units:
                i += 1
                continue
            if any(ch.isdigit() for ch in token) or "/" in token or "-" in token:
                i += 1
                continue
            break
        cleaned = " ".join(tokens[i:]).strip()
        return cleaned

    layer1_by_id = {}
    for recipe in tqdm(layer1, desc="Indexing layer1"):
        rid = recipe.get("id")
        if not rid:
            continue
        texts = [ing.get("text", "").strip() for ing in recipe.get("ingredients", [])]
        layer1_by_id[rid] = texts

    canonical_map = {}
    invalid_entries = []
    missing_layer1 = 0

    for rec in tqdm(det_ingrs, desc="Processing det_ingrs"):
        rid = rec.get("id")
        if not rid:
            continue
        canonical_ings = rec.get("ingredients", [])
        valid_flags = rec.get("valid", [])
        original_texts = layer1_by_id.get(rid)
        if original_texts is None:
            missing_layer1 += 1
            original_texts = []

        limit = min(len(canonical_ings), len(valid_flags))
        for i in range(limit):
            canon_text = canonical_ings[i].get("text", "").strip()
            orig_text = original_texts[i].strip() if i < len(original_texts) else ""
            alias_text = strip_quantity_unit(orig_text) if orig_text else ""
            if not canon_text:
                continue
            if not valid_flags[i]:
                invalid_entries.append({"recipe_id": rid, "text": canon_text})
                continue

            entry = canonical_map.get(canon_text)
            if entry is None:
                entry = {
                    "canonical_id": str(uuid.uuid4()),
                    "canonical": canon_text,
                    "aliases": set(),
                    "recipe_ids": set(),
                }
                canonical_map[canon_text] = entry
            if alias_text:
                entry["aliases"].add(alias_text)
            entry["recipe_ids"].add(rid)

    canonical_list = []
    for entry in canonical_map.values():
        canonical_list.append(
            {
                "canonical_id": entry["canonical_id"],
                "canonical": entry["canonical"],
                "aliases": sorted(entry["aliases"]),
                "recipe_ids": sorted(entry["recipe_ids"]),
            }
        )

    out_canonical_path.parent.mkdir(parents=True, exist_ok=True)
    with out_canonical_path.open("w", encoding="utf-8") as f:
        json.dump(canonical_list, f, ensure_ascii=True, indent=2)

    with out_invalid_path.open("w", encoding="utf-8") as f:
        json.dump(invalid_entries, f, ensure_ascii=True, indent=2)

    print(f"canonical entries: {len(canonical_list)}")
    print(f"invalid entries: {len(invalid_entries)}")
    print(f"recipes missing in layer1: {missing_layer1}")


if __name__ == "__main__":
    main()
