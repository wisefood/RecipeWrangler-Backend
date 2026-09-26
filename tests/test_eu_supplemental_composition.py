import importlib.util
from pathlib import Path


def _build_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_eu_global_dataset.py"
    spec = importlib.util.spec_from_file_location("build_eu_global_dataset", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_reviewed_stock_rows_retain_source_and_profiling_nutrients():
    rows = {row["id"]: row for row in _build_module().SUPPLEMENTAL_FOODS}

    vegetable = rows["fineli:29026"]
    assert vegetable["source_url"].endswith("/29026")
    assert vegetable["nutrients"]["Sodium, Na"]["value"] == 543.8

    beef = rows["frida:534"]
    assert beef["source_url"].endswith("/534")
    assert beef["nutrients"]["Protein"]["value"] == 1.1

    chicken = rows["frida:277"]
    assert chicken["source_url"].endswith("/277")
    assert set(chicken["nutrients"]) >= {
        "Energy", "Protein", "Carbohydrate, by difference", "Total lipid (fat)",
        "Sugars, total including NLEA", "Fatty acids, total saturated",
        "Sodium, Na", "Fiber, total dietary",
    }


def test_reviewed_frida_gap_rows_retain_exact_variant_and_provenance():
    rows = {row["id"]: row for row in _build_module().SUPPLEMENTAL_FOODS}

    berries = rows["frida:1831"]
    assert berries["food_name"] == "Mixed berries, frozen"
    assert berries["source_url"].endswith("/1831?lang=en")
    assert berries["nutrients"]["Fiber, total dietary"]["value"] == 3.35

    feta = rows["frida:1771"]
    assert "5% fat" in feta["food_name"]
    assert feta["nutrients"]["Total lipid (fat)"]["value"] == 9.23

    dressing = rows["frida:1962"]
    assert dressing["food_name"] == "Salad dressing, Italian"
    assert dressing["nutrients"]["Sodium, Na"]["value"] == 935


def test_reviewed_european_condiment_and_shell_rows_retain_provenance():
    rows = {row["id"]: row for row in _build_module().SUPPLEMENTAL_FOODS}

    hoisin = rows["matvaretabellen:10.197"]
    assert hoisin["food_name"] == "Hoisin sauce, home-made"
    assert hoisin["nutrients"]["Sodium, Na"]["value"] == 2716

    taco_shells = rows["slv:2557"]
    assert taco_shells["country"] == "SE"
    assert taco_shells["nutrients"]["Total lipid (fat)"]["value"] == 23.5

    pickled_ginger = rows["matvaretabellen:06.140"]
    assert pickled_ginger["food_group"] == "Pickles"
    assert "Fineli" in pickled_ginger["nutrients"]["Protein"]["source_code"]

    wasabi = rows["slv:4025"]
    assert wasabi["food_name"] == "Wasabi paste"
    assert wasabi["nutrients"]["Sodium, Na"]["value"] == 3390


def test_concentrated_stock_rows_are_as_sold_not_prepared():
    rows = {row["id"]: row for row in _build_module().SUPPLEMENTAL_FOODS}

    beef = rows["frida:1253"]["nutrients"]
    assert beef["Sodium, Na"]["value"] == 22500
    chicken = rows["fineli:29009"]["nutrients"]
    assert chicken["Fatty acids, total saturated"]["value"] == 13
    assert rows["frida:534"]["nutrients"]["Sodium, Na"]["value"] < 1000  # prepared broth
    pot = rows["retail:knorr-fr-chicken-stock-pot"]["nutrients"]
    assert pot["Sugars, total including NLEA"]["value"] == 3.9
    for row in rows.values():
        if row["id"].startswith("retail:"):
            assert row["source_url"].startswith("https://")
