"""Read-only coverage gate for prepared-form time adaptation; loads no models."""

import argparse
from collections import Counter, defaultdict
import gzip
import json
import os
from pathlib import Path
import re
import resource
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from scripts.instructions.recifine_probe import digest, host_memory, swap

QUERY = """MATCH (r:Recipe)
CALL (r) {
 OPTIONAL MATCH (r)-[:HAS_INGREDIENT_ORIGINAL]->(i:Ingredients_original)
 RETURN collect(coalesce(i.original_text,i.name)) AS originals
}
CALL (r) {
 OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
 RETURN collect(i.name) AS parsed_names
}
RETURN coalesce(r.recipe_id,r.id) AS recipe_id, r.source AS source,
 r.title AS title, r.instructions AS instructions, r.url AS url,
 r.duration AS duration, r.duration_minutes AS duration_minutes,
 originals, parsed_names
"""

PULSE = re.compile(r"\b(?:beans?|chickpeas?|garbanzo(?:s| beans?)?|lentils?)\b", re.I)
EXCLUDE_PULSE = re.compile(r"\b(?:green|runner|string|coffee|vanilla|cocoa|cacao|jelly)\s+beans?\b|\bbean\s+(?:sprouts?|paste|sauce)\b", re.I)
PREPARED = re.compile(r"\b(?:canned|tinned|cans?|tins?|precooked|pre[- ]cooked|cooked|boiled|steamed|roasted|baked|ready[- ]to[- ](?:eat|use)|frozen)\b", re.I)
DRIED = re.compile(r"\b(?:dry|dried|uncooked)\b", re.I)
GRAIN = re.compile(r"\b(?:rice|quinoa|barley|bulgur)\b", re.I)
VEGETABLE = re.compile(r"\b(?:onions?|carrots?|peppers?|broccoli|cauliflower)\b", re.I)
COOK = re.compile(r"\b(?:boil|simmer|cook|steam)(?:s|ed|ing)?\b", re.I)
SOAK = re.compile(r"\bsoak(?:s|ed|ing)?\b", re.I)
CHOP = re.compile(r"\b(?:chop|dice|mince|slice|peel)(?:s|d|ed|ing)?\b", re.I)
MINUTES = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:[-–]|to)?\s*(\d+(?:\.\d+)?)?\s*(minutes?|mins?|hours?|hrs?)\b", re.I)


def strings(value):
    if isinstance(value, str):
        return [value]
    return [str(x) for x in value or [] if x is not None]


def long_cooking(text):
    if not COOK.search(text):
        return False
    for match in MINUTES.finditer(text):
        upper = float(match[2] or match[1]) * (60 if match[3].lower().startswith(("h",)) else 1)
        if upper >= 30:
            return True
    return bool(re.search(r"\b(?:one|two|three|four)\s+hours?\b", text, re.I))


def classify(row):
    lines = strings(row.get("originals")) or strings(row.get("parsed_names"))
    steps = strings(row.get("instructions"))
    pulses = [line for line in lines if PULSE.search(line) and not EXCLUDE_PULSE.search(line)]
    dry = [line for line in pulses if DRIED.search(line) and not PREPARED.search(line)]
    unspecified = [line for line in pulses if not PREPARED.search(line)]
    soak_steps = [step for step in steps if SOAK.search(step)]
    long_steps = [step for step in steps if long_cooking(step)]
    pulse_steps = [step for step in soak_steps + long_steps if PULSE.search(step)]
    grain_lines = [line for line in lines if GRAIN.search(line) and not PREPARED.search(line)
                   and not re.search(r"\b(?:flour|noodles?|wine|vinegar|syrup|milk|paper|cereal|flakes?)\b", line, re.I)]
    grain_steps = [step for step in steps if GRAIN.search(step) and COOK.search(step)]
    veg_lines = [line for line in lines if VEGETABLE.search(line) and not PREPARED.search(line)
                 and not re.search(r"\b(?:powder|flakes?|salt|sauce|paste|pickled|dried|dry)\b", line, re.I)]
    veg_steps = [step for step in steps if VEGETABLE.search(step) and CHOP.search(step)]
    flags = {
        "pulses_any": bool(pulses),
        "pulses_explicit_dried": bool(dry),
        "pulses_explicit_dried_and_soak_or_long_cook": bool(dry and (soak_steps or long_steps)),
        "pulses_unprepared_or_unspecified_and_soak_or_long_cook": bool(unspecified and (soak_steps or long_steps)),
        "pulses_explicit_dried_with_named_pulse_step": bool(dry and pulse_steps),
        "grains_unprepared_or_unspecified_with_named_cook_step": bool(grain_lines and grain_steps),
        "vegetables_unprepared_or_unspecified_with_named_cut_step": bool(veg_lines and veg_steps),
    }
    evidence = {"pulse_lines": pulses, "explicit_dried_lines": dry, "pulse_steps": pulse_steps,
                "grain_lines": grain_lines, "grain_steps": grain_steps, "vegetable_lines": veg_lines, "vegetable_steps": veg_steps}
    return flags, evidence


def live_rows(query):
    from recipe_wrangler.utils.env_loader import load_runtime_env
    load_runtime_env()
    from neo4j import GraphDatabase, READ_ACCESS, Query
    auth = os.getenv("NEO4J_AUTH")
    pair = tuple(auth.split("/", 1)) if auth else (os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD"))
    with GraphDatabase.driver(os.getenv("NEO4J_URI", "bolt://localhost:7687"), auth=pair,
                             connection_timeout=5, max_transaction_retry_time=0) as driver:
        with driver.session(default_access_mode=READ_ACCESS, fetch_size=100) as session:
            for record in session.run(Query(query, timeout=60)):
                yield record.data()


def snapshot_rows(path, smoke):
    with gzip.open(path, "rt") as stream:
        for index, line in enumerate(stream):
            if smoke and index >= 100:
                break
            record = json.loads(line)
            yield {"recipe_id": record["recipe_id"], "source": record["source"], "title": record["title"],
                   "url": record.get("url"), "instructions": record.get("instructions"),
                   "duration": record.get("duration"),
                   "originals": [row["text"] for row in record.get("originals", []) if row.get("text")],
                   "parsed_names": [row["name"] for row in record.get("ingredients", []) if row.get("name")]}


def run(output, smoke, snapshot=None):
    query = QUERY + (" LIMIT 100" if smoke else "")
    (output / "query.cypher").write_text(query)
    counts, seen, examples = defaultdict(Counter), set(), []
    rows = snapshot_rows(snapshot, smoke) if snapshot else live_rows(query)
    with (output / "recipes.jsonl").open("x") as capture:
        for row in rows:
            identity = (row["source"], row["recipe_id"])
            if identity in seen or row["recipe_id"] is None:
                raise ValueError("Duplicate/missing recipe identity")
            seen.add(identity)
            capture.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            flags, evidence = classify(row)
            counts[row["source"]]["recipes"] += 1
            counts[row["source"]]["instructions_present"] += bool(row["instructions"])
            counts[row["source"]]["original_ingredients_present"] += bool(row["originals"])
            counts[row["source"]].update({key: int(value) for key, value in flags.items()})
            if any(flags[key] for key in ["pulses_explicit_dried_and_soak_or_long_cook",
                "grains_unprepared_or_unspecified_with_named_cook_step", "vegetables_unprepared_or_unspecified_with_named_cut_step"]):
                examples.append({"recipe_id": row["recipe_id"], "source": row["source"], "title": row["title"],
                                 "url": row["url"], "flags": flags, "evidence": evidence})
    data = {"sources": dict(sorted(counts.items())), "total": dict(sum(counts.values(), Counter())),
            "query": query, "query_timeout_seconds": 60, "fetch_size": 100,
            "scope": "Lexical candidate screen, not verified adaptable cases. Missing state is kept separate; recipe-wide co-occurrence does not prove action attachment.",
            "source_snapshot": str(snapshot) if snapshot else None,
            "source_snapshot_sha256": digest(snapshot) if snapshot else None,
            "query_executed_this_run": snapshot is None,
            "snapshot_sha256": digest(output / "recipes.jsonl"), "script_sha256": digest(Path(__file__)),
            "production_writes": 0, "model_calls": 0}
    for name, value in [("coverage.json", data), ("candidates.json", examples)]:
        with (output / name).open("x") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    resource.setrlimit(resource.RLIMIT_CPU, (900, 900))
    group = Path("/sys/fs/cgroup") / Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1].lstrip("/")
    memory, baseline_swap = host_memory(), swap()
    cap = min(1024**3, ((memory["MemAvailable"] - 10 * 1024**3) // 1024**3) * 1024**3, memory["MemTotal"] // 2)
    quota, period = (group / "cpu.max").read_text().split()
    if cap <= 0 or (group / "memory.max").read_text().strip() == "max" or int((group / "memory.max").read_text()) > cap or (group / "memory.swap.max").read_text().strip() != "0" or quota == "max" or int(quota) > 2 * int(period):
        raise RuntimeError("Require private <=1 GiB/no-swap/two-CPU scope and host headroom")
    telemetry = {"host_preflight": memory, "loadavg": Path("/proc/loadavg").read_text().strip(),
                 "min_host_available_bytes": memory["MemAvailable"], "peak_cgroup_bytes": 0,
                 "host_swap_baseline": baseline_swap, "job_swap_bytes": 0}
    stop, started = threading.Event(), time.monotonic()

    def monitor():
        while not stop.is_set():
            available = host_memory()["MemAvailable"]
            telemetry["min_host_available_bytes"] = min(available, telemetry["min_host_available_bytes"])
            telemetry["peak_cgroup_bytes"] = max(telemetry["peak_cgroup_bytes"], int((group / "memory.current").read_text()))
            telemetry["job_swap_bytes"] = int((group / "memory.swap.current").read_text())
            telemetry["host_swap_final"] = swap()
            telemetry["host_pressure"] = Path("/proc/pressure/memory").read_text().strip()
            pressure = float(telemetry["host_pressure"].splitlines()[1].split()[1].split("=")[1])
            if available <= 10 * 1024**3 or telemetry["job_swap_bytes"] or telemetry["host_swap_final"] != baseline_swap or pressure > 1.0 or time.monotonic() - started > 890:
                telemetry.update(exit_status=75, elapsed_seconds=time.monotonic() - started,
                                 peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
                (args.output / "receipt.json").write_text(json.dumps(telemetry, indent=2))
                os._exit(75)
            stop.wait(1)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    status = 1
    try:
        result = run(args.output, args.smoke, args.snapshot)
        status = 0
        print(json.dumps(result), flush=True)
    finally:
        stop.set()
        thread.join(timeout=2)
        telemetry.update(exit_status=status, elapsed_seconds=time.monotonic() - started,
                         peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        (args.output / "receipt.json").write_text(json.dumps(telemetry, indent=2))


if __name__ == "__main__":
    main()
