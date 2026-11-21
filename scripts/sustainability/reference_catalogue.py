"""Build/query an offline source-aware sustainability catalogue. No models or live writes.

Food identity groups references, not their numerical values. Only the small explicit
garlic alias set is reviewed across forms; other names retain their qualifiers.
"""

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import resource
import sqlite3
import sys
import threading
import time
import unicodedata


REPO = Path(__file__).resolve().parents[2]
REFERENCE_ROOT = REPO / "evaluation/sustainability"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from inspect_sources import xlsx_rows  # Source-only parser; imports no application/model code.


def normalize(value):
    return " ".join(re.sub(r"[^\w\s]", " ", unicodedata.normalize("NFKC", value).casefold()).split())


GARLIC_ALIASES = {normalize(name) for name in (
    "garlic", "garlic raw", "raw garlic", "garlic fresh", "fresh garlic", "garlic clove", "garlic cloves",
)}


class ReferenceConflict(ValueError):
    pass


def food_key(name):
    key = normalize(name)
    return "garlic" if key in GARLIC_ALIASES else key


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def initialize(db):
    db.executescript("""
        PRAGMA foreign_keys=ON;
        PRAGMA cache_size=-8192;
        CREATE TABLE IF NOT EXISTS foods(food_key TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS references_data(
            reference_id TEXT PRIMARY KEY, food_key TEXT NOT NULL REFERENCES foods,
            source TEXT NOT NULL, version TEXT NOT NULL, source_record_id TEXT NOT NULL,
            market TEXT NOT NULL, payload TEXT NOT NULL,
            UNIQUE(source, version, source_record_id, market));
        CREATE INDEX IF NOT EXISTS reference_food ON references_data(food_key);
        CREATE TABLE IF NOT EXISTS foodex2_links(link_id TEXT PRIMARY KEY, agb_code TEXT, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS quarantined(reference_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
    """)


def reference_id(record):
    identity = [record[key] for key in ("source", "version", "source_record_id")] + [record.get("market", "")]
    return hashlib.sha256(encode(identity).encode()).hexdigest()


def add_reference(db, record):
    for key in ("name", "source", "version", "source_record_id"):
        if not str(record.get(key, "")).strip():
            raise ValueError("Missing reference identity: " + key)
    for metric in record["metrics"].values():
        value = metric["value"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Non-finite/invalid environmental value")
    record = dict(record)
    record["food_key"] = food_key(record["name"])
    key = reference_id(record)
    record["reference_id"] = key
    payload = encode(record)
    previous = db.execute("SELECT payload FROM references_data WHERE reference_id=?", (key,)).fetchone()
    if previous:
        if previous[0] != payload:
            raise ReferenceConflict("Conflicting values for one source/version/record/market; review required: " + record["name"])
        return False
    db.execute("INSERT OR IGNORE INTO foods VALUES(?)", (record["food_key"],))
    db.execute("INSERT INTO references_data VALUES(?,?,?,?,?,?,?)", (key, record["food_key"],
        record["source"], record["version"], record["source_record_id"], record.get("market", ""), payload))
    return True


def add_legacy_reference(db, record):
    key = reference_id(record)
    if db.execute("SELECT 1 FROM quarantined WHERE reference_id=?", (key,)).fetchone():
        return "quarantined_skipped"
    try:
        return "inserted" if add_reference(db, record) else "duplicate_skipped"
    except ReferenceConflict:
        previous = db.execute("SELECT payload FROM references_data WHERE reference_id=?", (key,)).fetchone()[0]
        db.execute("INSERT INTO quarantined VALUES(?,?)", (key, encode({"reason": "conflicting_legacy_identity",
                   "previous": json.loads(previous), "conflicting": record})))
        db.execute("DELETE FROM references_data WHERE reference_id=?", (key,))
        return "quarantined_conflict"


def lookup(db, name, source=None, market=None, metric="climate_change"):
    query = "SELECT payload FROM references_data WHERE food_key=?"
    params = [food_key(name)]
    if source:
        query += " AND source=?"
        params.append(source)
    if market:
        query += " AND market=?"
        params.append(market.upper())
    query += " ORDER BY source,version,source_record_id,market"
    refs = [json.loads(row[0]) for row in db.execute(query, params)]
    refs = [record for record in refs if metric in record["metrics"]]
    return {"query": name, "food_key": food_key(name), "requested_metric": metric,
            "status": "not_found" if not refs else "matched" if len(refs)==1 else "multiple_references",
            "references": refs, "selection": None,
            "note": "References are not averaged. Select a compatible source/method/basis policy before recipe calculation."}


def source_info(source, version, source_url, license_name, path):
    return {"source": source, "version": version, "source_url": source_url, "license": license_name,
            "source_file": str(path.relative_to(REPO)), "source_sha256": digest_file(path)}


def public_records(root):
    raw = root / "data/reference-downloads-2026-11-03"
    path = raw / "agribalyse-3.2-food-products.xlsx"
    source = source_info("AGRIBALYSE", "3.2-food-AOUT25", "https://doi.org/10.57745/XTENSJ", "Etalab-2.0", path)
    titles = {}
    units = {}
    for index, row in xlsx_rows(path, "Synthese"):
        if index == 2:
            titles = row
        if index == 3:
            units = row
        # AGB has meaningful IDs such as 13167_1 / 13167_2 (powder vs rehydrated).
        if index <= 3 or not row.get("A") or not row.get("N"):
            continue
        # The workbook explicitly supplies corrected counterparts for seven packaging errors.
        if "software version - with packaging error" in row.get("F", ""):
            yield "superseded", {"source": source["source"], "code": row["A"], "row": index, "label": row["F"]}
            continue
        name = row["F"].replace(" (packaging fixed)", "").strip()
        metrics = {}
        for column, label in titles.items():
            if "/kg" not in units.get(column, "") or column == "M" or not row.get(column):
                continue
            try:
                value = float(row[column])
            except ValueError:
                continue
            metrics["climate_change" if column == "N" else "agribalyse:" + column] = {
                "name": label, "value": value, "unit": units[column], "source_column": column}
        yield "record", {**source, "source_record_id": row["A"], "ciqual_code": row.get("B"),
            "name": name, "source_label": row["F"], "sheet": "Synthese", "row": index,
            "market": "FR", "boundary": "consumer_product_including_intermediate_losses_and_declared_preparation",
            "method": "AGRIBALYSE_EF3.1", "basis": "kg_consumed_product", "quality_dqr": float(row["L"]),
            "scenario": {key: row.get(column, "").strip() for key, column in
                         (("season", "G"), ("airfreight", "H"), ("delivery", "I"), ("packaging", "J"), ("preparation", "K"))},
            "metrics": metrics, "usage": "reference_only_method_and_recipe_basis_must_be_checked"}
    path = root / "data/concito-v1.2-public-table.csv"
    source = source_info("CONCITO", "1.2-public-displayed-table", "https://thebigclimatedatabase.com/", "CC-BY-4.0", path)
    with path.open(newline="") as stream:
        for row in csv.DictReader(stream):
            yield "record", {**source, "source_record_id": row["source_id"], "name": row["product"],
                "market": row["market"], "boundary": "agriculture_through_retail_including_iluc",
                "method": "consequential_LCA", "basis": "kg_source_product",
                "metrics": {"climate_change": {"name": "Climate change", "value": float(row["total_kg_co2e_per_kg"]), "unit": "kg CO2e/kg"}},
                "stages_kg_co2e_per_kg": {key: float(row[key]) for key in ("agriculture", "iluc", "processing", "packaging", "transport", "retail")},
                "usage": "reference_only_method_and_recipe_basis_must_be_checked"}
    yield from su_eatable_records(raw / "su-eatable-life-v2.xlsx")


def su_eatable_records(path):
    source = source_info("SU-EATABLE LIFE", "figshare-v2-27921765", "https://doi.org/10.6084/m9.figshare.13271111.v2", "CC-BY-4.0", path)
    items = {}
    for sheet, metric, unit in (("SEL CF for users", "climate_change", "kg CO2e/(kg OR litre)"),
                                ("SEL WF for users", "water_footprint", "litre water/(kg OR litre)")):
        for index, row in xlsx_rows(path, sheet):
            try:
                value = float(row.get("C", ""))
            except ValueError:
                continue
            name = row["B"].strip()
            # Source suffixes are case-sensitive: CUCUMBER (G) and (g) have different factors.
            record = items.setdefault(name, {**source, "source_record_id": name, "name": name,
                "boundary": "distribution_harmonized_no_household_cooking", "method": "literature_synthesis",
                "basis": "kg_or_litre_unresolved", "metrics": {}, "usage": "blocked_until_functional_unit_and_recommended_level_are_resolved"})
            if metric in record["metrics"]:
                raise ReferenceConflict("Repeated SU-EATABLE metric needs review: " + name)
            record["metrics"][metric] = {"name": metric, "value": value, "unit": unit, "uncertainty": row.get("D"),
                "recommended_level": row.get("E"), "typology": row.get("F"), "typology_value_raw": row.get("G"),
                "subtypology": row.get("H"), "subtypology_value_raw": row.get("I"), "sheet": sheet, "row": index}
    for record in items.values():
        yield "record", record


def legacy_records(path):
    source = source_info("Sustainable FooDB (legacy)", "local-sha256:" + digest_file(path)[:16],
                         "unverified_original_source", "unverified", path)
    with path.open(newline="") as stream:
        for row in csv.DictReader(stream):
            name = normalize(row["Ingredient"])
            yield {**source, "source_record_id": hashlib.sha256(name.encode()).hexdigest(), "name": name,
                   "category": row["Category"], "boundary": "unverified", "method": "unverified",
                   "basis": "kg_assumed_by_existing_runtime_not_verified_from_original_study",
                   "metrics": {"climate_change": {"name": "Climate change", "value": float(row["CF_val"]), "unit": "kg CO2e/kg (legacy assumption)"}},
                   "usage": "legacy_provenance_unverified"}


def build(output, root, legacy, replay=False):
    if output.exists() and not replay:
        raise FileExistsError("Preserving existing snapshot; use --replay for an idempotence check")
    output.parent.mkdir(parents=True, exist_ok=True)
    stats, superseded = Counter(), []
    db = sqlite3.connect(output)
    initialize(db)
    with db:
        for kind, record in public_records(root):
            if kind == "superseded":
                superseded.append(record)
                continue
            stats[record["source"] + (":inserted" if add_reference(db, record) else ":duplicate_skipped")] += 1
        for record in legacy_records(legacy):
            stats["legacy:" + add_legacy_reference(db, record)] += 1
        raw = root / "data/reference-downloads-2026-11-03/Matching_FoodEx2v15.2_Agribalyse_v3.2.xlsx"
        headers = None
        for index, row in xlsx_rows(raw, "Matching"):
            if index == 1:
                headers = row
                continue
            item = {label: row.get(column) for column, label in headers.items()}
            if not item.get("agb_code"):
                continue
            # Normalize only numeric AGB IDs, not arbitrary FoodEx2 or source IDs.
            code = str(int(item["agb_code"])) if item["agb_code"].isdigit() else item["agb_code"]
            payload = encode(item)
            key = hashlib.sha256(payload.encode()).hexdigest()
            db.execute("INSERT OR IGNORE INTO foodex2_links VALUES(?,?,?)", (key, code, payload))
        db.execute("DELETE FROM foods WHERE NOT EXISTS (SELECT 1 FROM references_data r WHERE r.food_key=foods.food_key)")
    report = {"built_at_utc": datetime.now(timezone.utc).isoformat(), "stats": dict(stats), "superseded_source_rows": superseded,
        "foods": db.execute("SELECT count(*) FROM foods").fetchone()[0],
        "references": db.execute("SELECT count(*) FROM references_data").fetchone()[0],
        "source_counts": dict(db.execute("SELECT source,count(*) FROM references_data GROUP BY source")),
        "foodex2_links": db.execute("SELECT count(*) FROM foodex2_links").fetchone()[0],
        "quarantined": [json.loads(row[0]) for row in db.execute("SELECT payload FROM quarantined ORDER BY reference_id")],
        "garlic": lookup(db, "garlic"), "catalogue_sha256": None,
        "limitations": ["Offline prototype; no production imports or automatic factor selection.",
                         "Only listed garlic aliases reviewed; all other forms/names retained.",
                         "Additional normalized names are not validated corpus coverage gains."]}
    db.close()
    report["catalogue_sha256"] = digest_file(output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["build", "lookup"])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--name", default="garlic")
    parser.add_argument("--source")
    parser.add_argument("--market")
    parser.add_argument("--metric", default="climate_change")
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    if args.operation == "lookup":
        db = sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)
        print(json.dumps(lookup(db, args.name, args.source, args.market, args.metric), indent=2, ensure_ascii=False))
        db.close()
        return
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    resource.setrlimit(resource.RLIMIT_CPU, (900, 900))
    started = time.monotonic()
    group = Path("/sys/fs/cgroup") / Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1].lstrip("/")
    telemetry = {"min_host_available_bytes": 2**63, "peak_cgroup_bytes": 0, "swap_samples": [], "pressure_samples": []}
    stop = threading.Event()

    def monitor():
        while not stop.is_set():
            available = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:"))) * 1024
            telemetry["min_host_available_bytes"] = min(available, telemetry["min_host_available_bytes"])
            telemetry["peak_cgroup_bytes"] = max(telemetry["peak_cgroup_bytes"], int((group/"memory.current").read_text()))
            swap = int((group/"memory.swap.current").read_text())
            telemetry["swap_samples"].append(swap)
            telemetry["pressure_samples"].append(Path("/proc/pressure/memory").read_text().strip())
            if available <= 10 * 1024**3 or swap:
                os._exit(75)
            stop.wait(1)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        report = build(args.database, REFERENCE_ROOT, REPO/"data/sustainability/SustainableFooDB_Ingredient_CF.csv", args.replay)
    finally:
        stop.set()
        thread.join(timeout=2)
    telemetry.update(elapsed_seconds=round(time.monotonic()-started, 3), peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    report["telemetry"] = telemetry
    path = args.database.with_suffix(".replay-report.json" if args.replay else ".build-report.json")
    if path.exists():
        raise FileExistsError(path)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: report[key] for key in ("foods", "references", "stats", "source_counts", "foodex2_links", "telemetry")}, indent=2))


if __name__ == "__main__":
    main()
