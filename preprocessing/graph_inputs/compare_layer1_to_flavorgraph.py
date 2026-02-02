import argparse
import csv
import difflib
import re
from pathlib import Path

# Purpose: Compare layer1 ingredients to FlavorGraph ingredients and emit match CSVs.

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - optional dependency
    def tqdm(iterable, **_kwargs):
        return iterable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare layer1 ingredients to FlavorGraph ingredients (direct or fuzzy)."
        )
    )
    parser.add_argument(
        "--layer1",
        default="data/layer1_unique_ingredients.csv",
        help="CSV with layer1 ingredients (column: ingredient).",
    )
    parser.add_argument(
        "--flavorgraph",
        default="data/all_flavorgraph_ingredients.csv",
        help="CSV with FlavorGraph ingredients (column: name).",
    )
    parser.add_argument(
        "--matched-out",
        default="data/layer1_direct_matches.csv",
        help="Output CSV for direct or fuzzy matches.",
    )
    parser.add_argument(
        "--unmatched-out",
        default="data/layer1_no_match.csv",
        help="Output CSV for missing matches.",
    )
    parser.add_argument(
        "--fuzzy",
        action="store_true",
        help="Enable fuzzy matching for non-direct matches.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.7,
        help="Minimum similarity score for fuzzy matches.",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=200,
        help="Maximum candidates to score per ingredient during fuzzy matching.",
    )
    return parser.parse_args()


def load_column(path: Path, column: str):
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or column not in reader.fieldnames:
            raise ValueError(
                "Missing column '{}' in {}".format(column, path)
            )
        for row in reader:
            value = row.get(column)
            if value:
                yield value


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_text(text):
    text = text.lower()
    text = text.replace("_", " ")
    text = _NON_ALNUM.sub(" ", text)
    return " ".join(text.split())


def tokenize(text):
    return [token for token in text.split() if len(token) > 1]


def main() -> None:
    args = parse_args()
    layer1_path = Path(args.layer1)
    fg_path = Path(args.flavorgraph)
    matched_out = Path(args.matched_out)
    unmatched_out = Path(args.unmatched_out)

    fg_raw = list(load_column(fg_path, "name"))
    fg_names = set(fg_raw)
    fg_norm = [normalize_text(name) for name in fg_raw]
    token_index = {}
    for idx, name in enumerate(fg_norm):
        for token in set(tokenize(name)):
            token_index.setdefault(token, []).append(idx)

    matched = []
    unmatched = []
    for ingredient in tqdm(load_column(layer1_path, "ingredient"), desc="Comparing"):
        if ingredient in fg_names:
            matched.append((ingredient, ingredient, "direct", 1.0))
            continue

        if not args.fuzzy:
            unmatched.append((ingredient, "", ""))
            continue

        norm_ing = normalize_text(ingredient)
        tokens = tokenize(norm_ing)
        if not tokens:
            unmatched.append((ingredient, "", ""))
            continue

        candidate_counts = {}
        for token in tokens:
            for idx in token_index.get(token, []):
                candidate_counts[idx] = candidate_counts.get(idx, 0) + 1

        if not candidate_counts:
            unmatched.append((ingredient, "", ""))
            continue

        if len(candidate_counts) > args.max_candidates:
            ranked = sorted(
                candidate_counts.items(),
                key=lambda item: item[1],
                reverse=True,
            )
            candidate_indices = [idx for idx, _score in ranked[:args.max_candidates]]
        else:
            candidate_indices = list(candidate_counts.keys())

        best_score = 0.0
        best_idx = None
        for idx in candidate_indices:
            score = difflib.SequenceMatcher(None, norm_ing, fg_norm[idx]).ratio()
            if score > best_score:
                best_score = score
                best_idx = idx

        if best_idx is not None and best_score >= args.threshold:
            matched.append(
                (ingredient, fg_raw[best_idx], "fuzzy", best_score)
            )
        else:
            best_name = fg_raw[best_idx] if best_idx is not None else ""
            best_score_text = "{:.4f}".format(best_score) if best_idx is not None else ""
            unmatched.append((ingredient, best_name, best_score_text))

    matched_out.parent.mkdir(parents=True, exist_ok=True)
    with matched_out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ingredient", "flavorgraph_name", "match_type", "score"])
        for ingredient, fg_name, match_type, score in matched:
            writer.writerow([ingredient, fg_name, match_type, "{:.4f}".format(score)])

    unmatched_out.parent.mkdir(parents=True, exist_ok=True)
    with unmatched_out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ingredient", "best_candidate", "best_score"])
        for ingredient, best_candidate, best_score in unmatched:
            writer.writerow([ingredient, best_candidate, best_score])

    print(
        "Matched: {}, Unmatched: {}. Wrote {} and {}".format(
            len(matched), len(unmatched), matched_out, unmatched_out
        )
    )


if __name__ == "__main__":
    main()
