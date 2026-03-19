#!/usr/bin/env python3
"""Add density metadata to USDA weights JSON using ground-truth volume units."""

import argparse
import json
from pathlib import Path

from recipe_wrangler.utils.weigh_calculation_usda_ import (
    DEFAULT_UNIT_VOLUMES,
    density_for_food,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--weights",
        type=Path,
        default=Path("data/processed/usda/usda-weights-v2.json"),
        help="Input USDA weights JSON (updated in place unless --output is set).",
    )
    p.add_argument(
        "--unit-volumes",
        type=Path,
        default=DEFAULT_UNIT_VOLUMES,
        help="Ground-truth unit volume JSON.",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output path. Defaults to in-place update.",
    )
    return p


def main() -> None:
    args = _build_parser().parse_args()
    weights_path = args.weights
    output_path = args.output or weights_path

    rows = json.loads(weights_path.read_text(encoding="utf-8"))
    enriched = 0
    cleared = 0
    for row in rows:
        usda_id = str(row.get("usda_id") or "").strip()
        if not usda_id:
            continue
        density = density_for_food(
            usda_id=usda_id,
            name=None,
            weights_path=weights_path,
            unit_volumes_path=args.unit_volumes,
        )
        if density is None:
            removed_any = False
            for key in (
                "density_g_per_ml",
                "density_candidate_count",
                "density_source_portion_desc",
                "density_source_unit",
                "density_source_unit_ml",
                "density_source_grams_per_unit",
                "density_source",
            ):
                if key in row:
                    row.pop(key, None)
                    removed_any = True
            if removed_any:
                cleared += 1
            continue

        row["density_g_per_ml"] = float(density["density_g_per_ml"])
        row["density_candidate_count"] = int(density["candidate_count"])
        row["density_source_portion_desc"] = density.get("source_portion_desc")
        row["density_source_unit"] = density.get("source_unit")
        row["density_source_unit_ml"] = density.get("source_unit_ml")
        row["density_source_grams_per_unit"] = density.get("source_grams_per_unit")
        row["density_source"] = "unit_volume_ground_truth"
        enriched += 1

    output_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"rows={len(rows)}")
    print(f"enriched={enriched}")
    print(f"cleared={cleared}")
    print(f"output={output_path}")


if __name__ == "__main__":
    main()
