import argparse
import os
from typing import Optional

from neo4j import GraphDatabase

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None


# Maps lowercased Recipe.source values to their canonical collection URN.
SOURCE_TO_URN = {
    "recipe1m": "urn:rcollection:recipe1m",
    "healthyfoods": "urn:rcollection:healthyfood",
    "foodhero": "urn:rcollection:foodhero",
    "irish_safefood": "urn:rcollection:rcsi-recipes",
    "myplate": "urn:rcollection:myplate",
}


def _connect(uri: str, username: str, password: Optional[str], no_auth: bool):
    if no_auth:
        return GraphDatabase.driver(uri, auth=None)
    if not password:
        raise RuntimeError("Neo4j password missing. Set NEO4J_PASSWORD or use --no-auth.")
    return GraphDatabase.driver(uri, auth=(username, password))


def plan(driver) -> dict:
    with driver.session() as s:
        rows = s.run(
            "MATCH (r:Recipe) "
            "RETURN coalesce(r.source, '<null>') AS source, count(*) AS n "
            "ORDER BY n DESC"
        ).data()
    buckets = {"mapped": {}, "unmapped": {}, "null_source": 0}
    for row in rows:
        src = row["source"]
        n = row["n"]
        if src == "<null>":
            buckets["null_source"] = n
            continue
        urn = SOURCE_TO_URN.get(src.lower())
        if urn:
            buckets["mapped"].setdefault(urn, {"total": 0, "sources": {}})
            buckets["mapped"][urn]["total"] += n
            buckets["mapped"][urn]["sources"][src] = n
        else:
            buckets["unmapped"][src] = n
    return buckets


def apply(driver) -> dict:
    results = {}
    with driver.session() as s:
        for key, urn in SOURCE_TO_URN.items():
            # Case-insensitive match on r.source.
            res = s.run(
                "MATCH (r:Recipe) WHERE toLower(r.source) = $key "
                "SET r.source_id = $urn "
                "RETURN count(r) AS n",
                key=key,
                urn=urn,
            ).single()
            results[urn] = res["n"] if res else 0
    return results


def verify(driver) -> list:
    with driver.session() as s:
        return s.run(
            "MATCH (r:Recipe) "
            "RETURN coalesce(r.source_id, '<null>') AS source_id, count(*) AS n "
            "ORDER BY n DESC"
        ).data()


def main():
    p = argparse.ArgumentParser(description="Backfill Recipe.source_id from Recipe.source.")
    p.add_argument("--apply", action="store_true", help="Actually write. Default is dry-run.")
    p.add_argument("--uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    p.add_argument("--user", default=os.getenv("NEO4J_USERNAME", os.getenv("NEO4J_USER", "neo4j")))
    p.add_argument("--password", default=os.getenv("NEO4J_PASSWORD"))
    p.add_argument("--no-auth", action="store_true", default=os.getenv("NEO4J_NO_AUTH") == "1")
    args = p.parse_args()

    if load_dotenv:
        load_dotenv()
        # Re-read after dotenv in case env was empty.
        args.password = args.password or os.getenv("NEO4J_PASSWORD")

    driver = _connect(args.uri, args.user, args.password, args.no_auth)
    try:
        print(f"Connected to {args.uri}")
        print()

        buckets = plan(driver)
        print("=== Plan (dry-run view) ===")
        print("Will set source_id on:")
        total_mapped = 0
        for urn, info in buckets["mapped"].items():
            print(f"  {urn:<35s} <- {info['total']:>7d} rows  (sources: {info['sources']})")
            total_mapped += info["total"]
        print(f"  {'TOTAL':<35s}    {total_mapped:>7d} rows")
        print()
        if buckets["unmapped"]:
            print("Leaving UNTOUCHED (source not in mapping):")
            for src, n in buckets["unmapped"].items():
                print(f"  {src!r}: {n} rows")
        if buckets["null_source"]:
            print(f"Leaving UNTOUCHED (r.source IS NULL): {buckets['null_source']} rows")
        print()

        if not args.apply:
            print("Dry-run only. Re-run with --apply to write.")
            return

        print("=== Applying ===")
        res = apply(driver)
        for urn, n in res.items():
            print(f"  {urn:<35s} wrote {n:>7d} rows")
        print()

        print("=== Verification (source_id distribution) ===")
        for row in verify(driver):
            print(f"  {row['source_id']!r}: {row['n']}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
