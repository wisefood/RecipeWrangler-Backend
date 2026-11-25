"""Prepare, import, verify and activate an isolated environmental reference release.

No embeddings, recipe writes or changes to the legacy vector collection. Every phase
runs in a caller-provided private cgroup; source files and prior reports are preserved.
"""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import resource
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
from recipe_wrangler.repositories.sustainability_references import (
    POLICY_VERSION, REFERENCE_ALIAS, complete_recipe_selection, eligible, food_key, select_reference,
)
from recipe_wrangler.utils.env_loader import load_runtime_env
from recipe_wrangler.utils.elastic_auth import elastic_request

load_runtime_env()
ROOT = REPO / "evaluation/sustainability"
RELEASE = "sustainability-references-20261103-v1"
OUTPUT = ROOT / "data" / RELEASE
DATABASE = ROOT / "data/catalogue-pilot-v3.sqlite"


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False, default=str)
        stream.write("\n")


def request(method, path, body=None, *, raw=False, missing_ok=False):
    url = os.getenv("ELASTIC_URL", "http://localhost:9200").rstrip("/")
    payload = body if raw else None if body is None else json.dumps(body).encode()
    req = elastic_request(url + path, data=payload, method=method,
        headers={"Content-Type": "application/x-ndjson" if raw else "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=65) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        if missing_ok and error.code == 404:
            return None
        raise RuntimeError(f"Elasticsearch {method} {path}: HTTP {error.code}") from None
    if result.get("timed_out") or result.get("_shards", {}).get("failed", 0):
        raise RuntimeError("Incomplete Elasticsearch response")
    return result


def documents():
    with (OUTPUT / "references.jsonl").open() as stream:
        for line in stream:
            yield json.loads(line)


def batches(items, size=100):
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def prepare():
    OUTPUT.mkdir(parents=True, exist_ok=False)
    db = sqlite3.connect(DATABASE.resolve().as_uri() + "?mode=ro", uri=True)
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
    records = [json.loads(row[0]) for row in db.execute("SELECT payload FROM references_data ORDER BY reference_id")]
    source_files = {record["source_file"]: record["source_sha256"] for record in records}
    for filename, expected in source_files.items():
        if digest(REPO / filename) != expected:
            raise ValueError("Source checksum mismatch: " + filename)
    assert len({record["reference_id"] for record in records}) == len(records)
    counts = Counter()
    with (OUTPUT / "references.jsonl").open("x") as stream:
        for record in records:
            active = eligible(record)
            counts[record["source"]] += 1
            counts["calculation_eligible"] += int(active)
            doc = {"reference_id": record["reference_id"], "release": RELEASE,
                   "food_key": food_key(record["name"]), "source": record["source"],
                   "version": record["version"], "calculation_eligible": active, "reference": record}
            stream.write(json.dumps(doc, ensure_ascii=False, allow_nan=False) + "\n")
    manifest = {"release": RELEASE, "policy": POLICY_VERSION, "references": len(records), "counts": counts,
                "catalogue_sha256": digest(DATABASE), "documents_sha256": digest(OUTPUT / "references.jsonl"),
                "source_sha256": source_files, "quarantined_groups": db.execute("SELECT count(*) FROM quarantined").fetchone()[0],
                "automatic_synonym_merge": False,
                "recipe_policy": "All positive-mass ingredients must resolve uniquely under the same AGRIBALYSE policy; otherwise preserve the legacy recipe calculation. No cross-source totals.",
                "scope": "FR consumer-product factors with no additional consumer preparation; edible gram input assumed; no household recipe cooking or waste model. Bare reviewed vegetable names assume raw form. Not an Irish/UK supply-chain claim."}
    write_json(OUTPUT / "manifest.json", manifest)
    db.close()
    return manifest


def shadow():
    """Read 100 stored profiles in one read-only transaction; no inference or profiling."""
    from sqlalchemy import text
    from recipe_wrangler.utils.nutrition_postgres import _get_config, get_connection
    cfg = _get_config()
    schema, table = (str(cfg[key]).replace('"', '""') for key in ("schema", "profiles_table"))
    with get_connection() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        connection.execute(text("SET LOCAL statement_timeout = '60s'"))
        rows = connection.execute(text(f'''SELECT recipe_id, title, nutrition_source,
            total_sustainability, sustainability_profiling_details
            FROM "{schema}"."{table}"
            WHERE nutrition_source='eu' AND sustainability_profiling_details IS NOT NULL
            ORDER BY recipe_id LIMIT 100''')).mappings().all()
    if len(rows) != 100:
        raise ValueError("Require 100 distinct EU profiles for the shadow comparison")
    write_json(OUTPUT / "shadow-profile-snapshot.json", [dict(row) for row in rows])
    records = [doc["reference"] for doc in documents() if doc["calculation_eligible"]]
    by_key = {}
    for record in records:
        by_key.setdefault(food_key(record["name"]), []).append(record)
    comparisons, matched_positions, total_positions = [], 0, 0
    for row in rows:
        details = row["sustainability_profiling_details"]
        if isinstance(details, str):
            details = json.loads(details)
        if not isinstance(details, list) or not details:
            raise ValueError("Profile lacks ingredient details")
        names = [detail["ingredient"] for detail in details]
        weights = [float(detail["weight_g"]) for detail in details]
        if any(not __import__('math').isfinite(w) or w < 0 for w in weights):
            raise ValueError("Invalid baseline gram weights")
        selected = {name: record for name in names if (record := select_reference(name, by_key.get(food_key(name), []))) is not None}
        use_new = complete_recipe_selection(names, weights, selected)
        baseline = float(row["total_sustainability"])
        proposed = sum(w / 1000 * selected[n]["metrics"]["climate_change"]["value"] for n, w in zip(names, weights) if w) if use_new else baseline
        matched_positions += sum(name in selected for name in names)
        total_positions += len(names)
        comparisons.append({"recipe_id": row["recipe_id"], "title": row["title"],
            "baseline_kg_co2e": baseline, "proposed_kg_co2e": proposed, "delta_kg": proposed-baseline,
            "policy": POLICY_VERSION if use_new else "legacy_v1", "ingredient_count": len(names),
            "source_pinned_matches": len([n for n in names if n in selected]),
            "missing_source_pinned_names": [name for name in names if name not in selected],
            "references": [{"ingredient": name, "source": record["source"], "record": record["source_record_id"],
                             "factor": record["metrics"]["climate_change"]["value"]} for name, record in selected.items()]})
    report = {"recipes": len(rows), "ingredient_positions": total_positions, "source_pinned_positions": matched_positions,
        "complete_new_policy_recipes": sum(row["policy"] == POLICY_VERSION for row in comparisons),
        "changed_recipe_scores": sum(abs(row["delta_kg"]) > 1e-9 for row in comparisons),
        "mixed_source_recipes": 0, "cases": comparisons,
        "evaluation_status": "Exposed development shadow; mapping/policy regression evidence, not independent environmental accuracy gold."}
    write_json(OUTPUT / "shadow-report.json", report)
    return {key: value for key, value in report.items() if key != "cases"}


MAPPING = {"dynamic": "strict", "properties": {
    **{key: {"type": "keyword"} for key in ("reference_id", "release", "food_key", "source", "version")},
    "calculation_eligible": {"type": "boolean"}, "reference": {"type": "object", "enabled": False},
}}


def verify():
    manifest = json.loads((OUTPUT / "manifest.json").read_text())
    if digest(OUTPUT / "references.jsonl") != manifest["documents_sha256"]:
        raise ValueError("Release documents changed")
    count = request("POST", f"/{RELEASE}/_count", {"query": {"match_all": {}}})["count"]
    assert count == manifest["references"]
    checked = 0
    for batch in batches(documents()):
        result = request("POST", f"/{RELEASE}/_mget", {"ids": [doc["reference_id"] for doc in batch]})
        assert len(result["docs"]) == len(batch)
        for expected, actual in zip(batch, result["docs"]):
            assert actual.get("found") and actual["_source"] == expected
            checked += 1
    return {"documents_verified": checked, "unexpected_documents": count - checked, "model_calls": 0}


def import_release():
    manifest = json.loads((OUTPUT / "manifest.json").read_text())
    assert digest(OUTPUT / "references.jsonl") == manifest["documents_sha256"]
    legacy = legacy_state()
    write_json(OUTPUT / "legacy-before-import.json", legacy)
    existing = request("GET", f"/{RELEASE}", missing_ok=True)
    if existing is None:
        request("PUT", f"/{RELEASE}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "-1"}, "mappings": MAPPING})
    inserted, repeated = 0, 0
    for batch in batches(documents()):
        payload = "".join(json.dumps({"create": {"_index": RELEASE, "_id": doc["reference_id"]}}) + "\n" + json.dumps(doc) + "\n" for doc in batch).encode()
        result = request("POST", "/_bulk?timeout=60s", payload, raw=True)
        for doc, item in zip(batch, result["items"]):
            outcome = item["create"]
            if outcome["status"] == 201:
                inserted += 1
            elif outcome["status"] == 409:
                old = request("GET", f"/{RELEASE}/_doc/{doc['reference_id']}")
                if old["_source"] != doc:
                    raise ValueError("Existing release document conflicts; refusing overwrite")
                repeated += 1
            else:
                raise RuntimeError("Reference import failed: " + str(outcome.get("status")))
        time.sleep(0.03)
    request("PUT", f"/{RELEASE}/_settings", {"index": {"refresh_interval": "5s"}})
    request("POST", f"/{RELEASE}/_refresh")
    after = legacy_state()
    write_json(OUTPUT / "legacy-after-import.json", after)
    assert legacy == after, "Legacy collection changed during import; investigate before activation"
    return {"inserted": inserted, "identical_repeats": repeated, "legacy_count_and_garlic_sample_unchanged": True, **verify()}


def legacy_state():
    index = os.getenv("ELASTIC_VECTOR_INDEX", "ingredient_vectors")
    result = request("POST", f"/{index}/_search", {
        "size": 20, "timeout": "60s", "track_total_hits": True,
        "sort": [{"source_id": "asc"}],
        "_source": ["collection", "source_id", "document", "metadata"],
        "query": {"bool": {"filter": [{"term": {"collection": "sustainability_ingredients"}}],
                            "must": [{"match_phrase": {"document": "garlic"}}]}},
    })
    count = request("POST", f"/{index}/_search", {
        "size": 0, "timeout": "60s", "track_total_hits": True,
        "query": {"term": {"collection": "sustainability_ingredients"}},
    })
    return {"index": index, "count": count["hits"]["total"], "garlic_total": result["hits"]["total"],
            "garlic_sample": [{"index": hit["_index"], "id": hit["_id"], "source": hit["_source"]} for hit in result["hits"]["hits"]]}


def activate():
    shadow_report = json.loads((OUTPUT / "shadow-report.json").read_text())
    assert shadow_report["recipes"] == 100 and shadow_report["mixed_source_recipes"] == 0
    # Numerical changes must be inspected before activation, not accepted by an arbitrary tolerance.
    assert shadow_report["changed_recipe_scores"] == 0, "Review changed recipe scores before activation"
    verified = verify()
    previous = request("GET", f"/_alias/{REFERENCE_ALIAS}", missing_ok=True) or {}
    write_json(OUTPUT / "previous-alias.json", previous)
    actions = [{"remove": {"index": index, "alias": REFERENCE_ALIAS}} for index in previous]
    actions.append({"add": {"index": RELEASE, "alias": REFERENCE_ALIAS}})
    request("PUT", f"/{RELEASE}/_settings", {"index": {"blocks.write": True}})
    response = request("POST", "/_aliases", {"actions": actions})
    assert response.get("acknowledged")
    return {"active_alias": REFERENCE_ALIAS, "index": RELEASE, "previous_alias": previous, **verified}


def smoke():
    from recipe_wrangler.repositories.sustainability_references import lookup_references
    from recipe_wrangler.tools import sustainability_calculator as calculator
    selected, status = lookup_references(["garlic", "garlic powder", "garlic salt", "garlic in oil", "cooked garlic"])
    assert status == "available"
    assert selected["garlic"]["source_record_id"] == "11000"
    assert selected["garlic powder"]["source_record_id"] == "11023"
    assert not {"garlic salt", "garlic in oil", "cooked garlic"}.intersection(selected)

    def forbid_legacy(*args, **kwargs):
        raise AssertionError("Source-complete live smoke must not call legacy/model matching")

    calculator.best_sustainability_match = forbid_legacy
    result = calculator.sustainability_tool_vector.invoke({
        "title": "Source-aware garlic conformance probe (not a real recipe)",
        "ingredient_names": ["garlic"], "weights": [10], "serves": 1,
    })
    assert abs(result["total_sustainability"] - 0.00383) < 1e-12
    assert result["source_sustainability"] == "AGRIBALYSE"
    alias = request("GET", f"/_alias/{REFERENCE_ALIAS}")
    assert list(alias) == [RELEASE]
    settings = request("GET", f"/{RELEASE}/_settings")
    assert settings[RELEASE]["settings"]["index"]["blocks"]["write"] == "true"
    assert legacy_state() == json.loads((OUTPUT / "legacy-before-import.json").read_text())
    return {"active_alias": REFERENCE_ALIAS, "write_blocked": True, "legacy_count_and_sample_unchanged": True,
            "garlic_factor": selected["garlic"]["metrics"]["climate_change"]["value"],
            "powder_record_distinct": True, "compound_negative_controls": "passed",
            "model_calls": 0, "workspace_calculator_result": result,
            "deployment_boundary": "Running API is a pinned Docker image without a source mount. Workspace integration verified; that image needs a separate code rollout. Stored recipe profiles were not rewritten."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "shadow", "import", "verify", "activate", "smoke"])
    args = parser.parse_args()
    report_path = OUTPUT / (args.phase + "-receipt.json")
    if report_path.exists():
        raise FileExistsError("Preserving prior receipt: " + str(report_path))
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    resource.setrlimit(resource.RLIMIT_CPU, (900, 900))
    group = Path("/sys/fs/cgroup") / Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1].lstrip("/")
    if (group / "memory.max").read_text().strip() == "max" or (group / "memory.swap.max").read_text().strip() != "0":
        raise RuntimeError("Run in private bounded RAM/no-swap cgroup")
    telemetry = {"min_host_available_bytes": 2**63, "peak_cgroup_bytes": 0}
    stop = threading.Event()

    def monitor():
        while not stop.is_set():
            available = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:"))) * 1024
            telemetry["min_host_available_bytes"] = min(available, telemetry["min_host_available_bytes"])
            telemetry["peak_cgroup_bytes"] = max(int((group / "memory.current").read_text()), telemetry["peak_cgroup_bytes"])
            telemetry["host_memory_pressure"] = Path("/proc/pressure/memory").read_text().strip()
            telemetry["job_swap_bytes"] = int((group / "memory.swap.current").read_text())
            if available <= 10 * 1024**3 or telemetry["job_swap_bytes"]:
                os._exit(75)
            stop.wait(1)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    start = time.monotonic()
    try:
        result = {"prepare": prepare, "shadow": shadow, "import": import_release, "verify": verify, "activate": activate, "smoke": smoke}[args.phase]()
    finally:
        stop.set()
        thread.join(timeout=2)
    telemetry.update(elapsed_seconds=time.monotonic()-start, peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, exit_status=0)
    write_json(report_path, {"phase": args.phase, "result": result, "telemetry": telemetry})
    print(json.dumps({"phase": args.phase, "result": result, "telemetry": telemetry}, default=str))


if __name__ == "__main__":
    main()
