#!/usr/bin/env python3
"""Link recipe photos that were bulk-uploaded to the `images` bucket to their recipes.

On 2026-09-04, 249 photos were put straight into the bucket with `mc`,
bypassing the gateway's upload endpoint. That endpoint stores an object as a
bare ``uuid4`` key and the gateway serves only keys that parse as a UUID
(``GET /api/v1/images/{image_id}`` rejects anything else), so a key such as
``ESSRG_12-172d4c95ba.jpg`` or ``<recipe-uuid>-<hash>.jpg`` could be neither
served nor referenced, and the recipes it was meant for kept ``image_url: null``.

The key does say which recipe it belongs to: everything before the ten-hex
suffix is the recipe id (``ESSRG_12`` for the curated Hungarian set, a UUID for
the sets that use UUID ids). For each such object this script

1. looks the recipe up in RecipeWrangler (``GET /api/v1/recipes/{id}``) and
   skips ids that do not exist, or that already carry a real image unless
   ``--force``. An ``image_url`` under ``--relink-prefix`` (default
   ``/static/``) does not count as real: the bulk import wrote
   ``/static/data/generated_recipe_images/grokified/<key>``, a path the
   recipe wrangler would serve from a data directory its image does not ship,
   so on the public site it returns the SPA's HTML page instead of a photo;
2. copies the object server-side to a fresh UUID key with ``mc cp``, so it is
   servable by the gateway exactly like an uploaded image (content type comes
   along with the copy);
3. patches the recipe's ``image_url`` to the gateway URL for that key
   (``PATCH /api/v1/recipes/{id}``), which re-projects it to Elasticsearch.

Dry-run by default. Nothing is deleted: the originals stay in the bucket, and
the report lists them so they can be removed with ``mc rm`` once the links are
verified. Re-running is safe: a recipe that already has an image is skipped.

Usage
-----
RecipeWrangler is ClusterIP-only, so reach it through a port-forward::

    kubectl --context k8s-w -n wf-prod port-forward svc/recipewrangler 18001:8001

    # Report what would happen (default)
    python scripts/maintenance/link_bulk_images.py

    # Try three, then everything
    python scripts/maintenance/link_bulk_images.py --apply --limit 3
    python scripts/maintenance/link_bulk_images.py --apply

    # One batch only, e.g. the Hungarian set
    python scripts/maintenance/link_bulk_images.py --apply --only ESSRG_

Requires the ``mc`` client with the ``wisefood`` alias configured. Only the
standard library is used, so any Python 3.10+ runs it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

#: ``<recipe id>-<ten hex>.<ext>`` — the shape of every bulk-uploaded key.
#: Gateway uploads are bare UUIDs and never match, so they are left alone.
BULK_KEY = re.compile(
    r"^(?P<recipe_id>.+)-(?P<digest>[0-9a-f]{10})\.(?P<ext>jpe?g|png|webp)$",
    re.IGNORECASE,
)


@dataclass
class Plan:
    key: str
    recipe_id: str
    status: str  # linked | would-link | skipped-missing | skipped-has-image | failed
    detail: str = ""
    new_key: str = ""
    image_url: str = ""
    previous_url: str = ""


@dataclass
class Report:
    generated_at: str
    api: str
    bucket: str
    applied: bool
    plans: list[Plan] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for plan in self.plans:
            out[plan.status] = out.get(plan.status, 0) + 1
        return out


# --------------------------------------------------------------------------- #
# mc                                                                           #
# --------------------------------------------------------------------------- #

def mc(*args: str) -> str:
    result = subprocess.run(["mc", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"mc {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def list_bulk_objects(target: str, since: str | None) -> list[tuple[str, str]]:
    """``(key, last_modified)`` for every object whose key has the bulk shape."""
    objects: list[tuple[str, str]] = []
    for line in mc("ls", "--json", "--recursive", target).splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        key = str(entry.get("key") or "")
        modified = str(entry.get("lastModified") or "")
        if not BULK_KEY.match(key):
            continue
        if since and modified[:10] < since:
            continue
        objects.append((key, modified))
    return objects


def copy_to_uuid_key(target: str, key: str) -> str:
    new_key = str(uuid.uuid4())
    # Server-side copy inside the bucket; metadata (Content-Type) travels with it.
    mc("cp", "--quiet", f"{target}/{key}", f"{target}/{new_key}")
    return new_key


# --------------------------------------------------------------------------- #
# RecipeWrangler                                                               #
# --------------------------------------------------------------------------- #

def http_json(method: str, url: str, payload: dict | None = None, timeout: float = 30.0) -> tuple[int, dict | None]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return response.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            parsed = json.loads(body) if body else None
        except json.JSONDecodeError:
            parsed = {"detail": body.decode(errors="replace")[:200]}
        return exc.code, parsed


def current_image_url(details: dict | None) -> str | None:
    """The recipe's image URL wherever the details payload keeps it."""
    if not isinstance(details, dict):
        return None
    for holder in (details, details.get("recipe"), details.get("result"), details.get("data")):
        if isinstance(holder, dict):
            value = holder.get("image_url")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:18001/api/v1", help="RecipeWrangler API base (default: a local port-forward)")
    parser.add_argument("--target", default="wisefood/images", help="mc alias/bucket holding the photos")
    parser.add_argument("--image-base", default="https://demo.wisefood-project.eu/rest/api/v1/images",
                        help="Public gateway URL that serves an image id; this is what gets stored on the recipe")
    parser.add_argument("--since", default="2026-09-04", help="Only objects modified on or after this date (YYYY-MM-DD); '' for all")
    parser.add_argument("--only", default="", help="Only keys starting with this prefix, e.g. ESSRG_")
    parser.add_argument("--limit", type=int, default=0, help="Stop after this many link attempts (0 = no limit)")
    parser.add_argument("--relink-prefix", action="append", default=None,
                        help="Treat an existing image_url starting with this as unlinked (repeatable; default: /static/)")
    parser.add_argument("--force", action="store_true", help="Also relink recipes that already have a real image_url")
    parser.add_argument("--apply", action="store_true", help="Copy objects and patch recipes; without it, report only")
    parser.add_argument("--report", default="link_bulk_images_report.json", help="Where to write the JSON report")
    args = parser.parse_args()
    relink_prefixes = tuple(args.relink_prefix or ["/static/"])

    report = Report(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        api=args.api, bucket=args.target, applied=args.apply,
    )

    status, _ = http_json("GET", f"{args.api.rstrip('/')}/recipes/count")
    if status >= 400:
        print(f"RecipeWrangler not reachable at {args.api} (HTTP {status}). Is the port-forward up?", file=sys.stderr)
        return 2

    objects = list_bulk_objects(args.target, args.since or None)
    if args.only:
        objects = [(k, m) for k, m in objects if k.startswith(args.only)]
    print(f"{len(objects)} bulk-uploaded objects to consider in {args.target}")

    attempts = 0
    for key, _modified in sorted(objects):
        match = BULK_KEY.match(key)
        assert match is not None
        recipe_id = match.group("recipe_id")

        status, details = http_json("GET", f"{args.api.rstrip('/')}/recipes/{recipe_id}")
        if status == 404:
            report.plans.append(Plan(key, recipe_id, "skipped-missing", "no such recipe"))
            continue
        if status >= 400:
            report.plans.append(Plan(key, recipe_id, "failed", f"GET returned HTTP {status}"))
            continue

        existing = current_image_url(details)
        placeholder = bool(existing) and existing.startswith(relink_prefixes)
        if existing and not placeholder and not args.force:
            report.plans.append(Plan(key, recipe_id, "skipped-has-image", existing))
            continue
        previous = existing or ""

        if args.limit and attempts >= args.limit:
            report.plans.append(Plan(key, recipe_id, "would-link", "beyond --limit"))
            continue
        attempts += 1

        if not args.apply:
            report.plans.append(Plan(key, recipe_id, "would-link", "dry run", previous_url=previous))
            continue

        try:
            new_key = copy_to_uuid_key(args.target, key)
        except RuntimeError as exc:
            report.plans.append(Plan(key, recipe_id, "failed", str(exc)))
            continue

        image_url = f"{args.image_base.rstrip('/')}/{new_key}"
        status, body = http_json("PATCH", f"{args.api.rstrip('/')}/recipes/{recipe_id}", {"image_url": image_url})
        if status >= 400:
            detail = (body or {}).get("detail") if isinstance(body, dict) else None
            report.plans.append(Plan(key, recipe_id, "failed", f"PATCH HTTP {status}: {detail}", new_key, image_url, previous))
            continue
        report.plans.append(Plan(key, recipe_id, "linked", "", new_key, image_url, previous))
        print(f"linked {recipe_id} <- {key} as {new_key}")

    Path(args.report).write_text(json.dumps({**asdict(report), "counts": report.counts()}, indent=2) + "\n")

    counts = report.counts()
    print()
    for status_name in ("linked", "would-link", "skipped-has-image", "skipped-missing", "failed"):
        if counts.get(status_name):
            print(f"{status_name:>18}: {counts[status_name]}")
    missing = [p for p in report.plans if p.status == "skipped-missing"]
    if missing:
        print("\nno recipe for:", ", ".join(p.recipe_id for p in missing[:20]), "..." if len(missing) > 20 else "")
    print(f"\nreport: {args.report}")
    if not args.apply:
        print("dry run — re-run with --apply to copy the objects and patch the recipes")
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
