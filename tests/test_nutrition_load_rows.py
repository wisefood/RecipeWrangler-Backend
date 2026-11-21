import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
needs_data = pytest.mark.skipif(not (ROOT / "analysis" / "nutrition-adaptation-2026-11-11" / "inputs").exists(), reason="needs the local analysis capture")


@needs_data
def test_rows_cover_the_frozen_results():
    spec = importlib.util.spec_from_file_location("nutrition_loader", ROOT / "scripts" / "nutrition" / "load_adaptations.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = mod.build_rows()
    assert len(rows) == 537 and len({r["recipe_id"] for r in rows}) == 537
    assert sum(r["status"] == "adapted" for r in rows) == 488
    assert all(r["mode"] == "nutrition" and r["region"] in ("IE", "HU", "SI") for r in rows)
    assert all(r["adapted"]["lines"] and r["benefit"]["tip"] for r in rows if r["status"] == "adapted")
    assert all(r["reason_code"] and r["provenance"]["reason"] for r in rows if r["status"] == "no_suggestion")
