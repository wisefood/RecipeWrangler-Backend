"""Convert source-country representatives to target-country equivalents."""

from __future__ import annotations

import pandas as pd

from .constants import PROCESSED_DIR, TARGET_COUNTRIES
from .io import write_table


def convert_country_prices(country_prices: pd.DataFrame, pli: pd.DataFrame) -> pd.DataFrame:
    pli_years = pli["year"].unique()
    if len(pli_years) != 1:
        raise ValueError(f"Expected one PLI year, found {pli_years}")
    lookup = pli.set_index(["country_code", "pli_category"])["pli"].to_dict()
    targets = ("EU27",) + TARGET_COUNTRIES
    rows: list[dict[str, object]] = []
    for record in country_prices.to_dict("records"):
        source_code = record["source_country_code"]
        category = record["pli_category"]
        source_pli = 100.0 if source_code == "EU27" else lookup.get((source_code, category))
        if source_pli is None:
            raise ValueError(f"Missing source PLI for {(source_code, category)}")
        for target_code in targets:
            target_pli = 100.0 if target_code == "EU27" else lookup.get((target_code, category))
            if target_pli is None:
                raise ValueError(f"Missing target PLI for {(target_code, category)}")
            rows.append(
                {
                    **record,
                    "target_country": target_code,
                    "source_pli": source_pli,
                    "target_pli": target_pli,
                    "converted_eur_per_kg": (
                        record["representative_eur_per_kg"] * target_pli / source_pli
                    ),
                    "is_direct_target_observation": source_code == target_code,
                    "pli_year": int(pli_years[0]),
                }
            )
    result = pd.DataFrame(rows)
    assert result["converted_eur_per_kg"].gt(0).all()
    expected = len(country_prices) * len(targets)
    assert len(result) == expected, (len(result), expected)
    return result


def main() -> None:
    country = pd.read_csv(PROCESSED_DIR / "ec_prices_country_aggregated.csv")
    pli = pd.read_csv(PROCESSED_DIR / "eurostat_food_pli.csv")
    converted = convert_country_prices(country, pli)
    write_table(converted, PROCESSED_DIR / "ec_prices_country_converted.csv")
    print(f"Wrote {len(converted)} converted country representatives")


if __name__ == "__main__":
    main()
