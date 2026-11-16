"""Build an offline sustainability vector bundle; never publish or select factors.

Run smoke/build inside a private systemd cgroup (4 GiB RAM, no swap, two CPUs).
The shared matcher currently embeds source names without an instruction prefix.
"""

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "evaluation/sustainability/data/sustainability-references-20261103-v1"
MODEL = "BAAI/bge-small-en-v1.5"
REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
DIMENSIONS = 384
COLLECTION = "sustainability_references_prepared_20261104_v1"


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def load_documents(source):
    manifest = json.loads((source / "manifest.json").read_text())
    if digest(source / "references.jsonl") != manifest["documents_sha256"]:
        raise ValueError("Reference export checksum mismatch")
    with (source / "references.jsonl").open() as stream:
        documents = [json.loads(line) for line in stream]
    ids = [doc["reference_id"] for doc in documents]
    if len(documents) != manifest["references"] or len(set(ids)) != len(ids):
        raise ValueError("Reference count or identity mismatch")
    for doc in documents:
        record = doc["reference"]
        if record["reference_id"] != doc["reference_id"] or not record["name"].strip():
            raise ValueError("Invalid reference identity or name")
    return documents, manifest


def validate_vectors(vectors, count):
    if len(vectors) != count:
        raise ValueError("Embedding batch count mismatch")
    for vector in vectors:
        if len(vector) != DIMENSIONS or any(not math.isfinite(v) for v in vector):
            raise ValueError("Invalid embedding dimension or non-finite value")
        if sum(v * v for v in vector) <= 0:
            raise ValueError("Zero embedding")


def vector_document(doc, vector):
    """Retain full provenance; catalogue eligibility is not match acceptance."""
    return {"_id": doc["reference_id"], "_source": {
        "collection": COLLECTION, "source_id": doc["reference_id"],
        "document": doc["reference"]["name"],
        "metadata": {"reference_release": doc["release"],
                     "calculation_eligible": doc["calculation_eligible"],
                     "reference": doc["reference"],
                     "facet_annotation_status": "pending_shared_matcher",
                     "candidate_only": True},
        "embedding_model": MODEL, "embedding_revision": REVISION,
        "vector_space": "cosine", "embedding": vector,
    }}


def embed(phase, output):
    # Override .env batch/device settings before importing the shared helper.
    os.environ.update(EMBED_MODEL_NAME=MODEL, EMBED_MODEL_REVISION=REVISION,
        EMBED_DEVICE="cpu", EMBED_BATCH_SIZE="8", EMBED_WARM_ON_IMPORT="0",
        CUDA_VISIBLE_DEVICES="", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
        OMP_NUM_THREADS="2", MKL_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2",
        NUMEXPR_NUM_THREADS="2", TOKENIZERS_PARALLELISM="false")
    sys.path.insert(0, str(ROOT / "src"))
    import torch
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    from recipe_wrangler.utils.get_embeddings import get_embeddings_batch

    documents, manifest = load_documents(SOURCE)
    names = sorted({doc["reference"]["name"] for doc in documents})
    if phase == "smoke":
        names = ["Garlic, fresh", "Garlic powder, dried", "garlic salt", "Chicken stock"]
    vectors = {}
    start = time.monotonic()
    for offset in range(0, len(names), 8):
        batch = names[offset:offset + 8]
        values = get_embeddings_batch(batch)
        validate_vectors(values, len(batch))
        vectors.update(zip(batch, values))
        if offset % 800 == 0:
            print(f"embedded={min(offset + 8, len(names))}/{len(names)}", flush=True)
    result = {"references": len(documents), "embedded_unique_names": len(names),
              "embedding_seconds": time.monotonic() - start,
              "embedding_model": MODEL, "embedding_revision": REVISION,
              "dimensions": DIMENSIONS, "embedding_input": "source_name_verbatim_v1",
              "normalization": "shared HuggingFaceEmbeddings defaults; cosine at retrieval",
              "source_manifest_sha256": digest(SOURCE / "manifest.json"),
              "source_documents_sha256": manifest["documents_sha256"],
              "candidate_only": True, "index_published": False, "gpu_used": False}
    if phase == "smoke":
        if vectors[names[0]] == vectors[names[1]]:
            raise ValueError("Distinct food descriptions produced identical vectors")
        return result
    path = output / "vectors.jsonl"
    with path.open("x") as stream:
        for doc in documents:
            row = vector_document(doc, vectors[doc["reference"]["name"]])
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
    # Re-read the artifact: verify every reference, provenance payload and vector.
    with path.open() as stream:
        for doc in documents:
            row = json.loads(next(stream))
            if row != vector_document(doc, vectors[doc["reference"]["name"]]):
                raise ValueError("Vector export verification failed")
            validate_vectors([row["_source"]["embedding"]], 1)
        if next(stream, None) is not None:
            raise ValueError("Unexpected exported records")
    result.update(collection=COLLECTION, sources=dict(Counter(doc["source"] for doc in documents)),
                  vectors_sha256=digest(path), vectors_bytes=path.stat().st_size,
                  verified_references=len(documents),
                  integration="Pending shared matcher facets/eligibility gates; no aliases or recipe scores changed")
    write_json(output / "manifest.json", result)
    return result


def host_memory():
    return {line.split(":")[0]: int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith(("MemAvailable:", "MemTotal:"))}


def swap_counters():
    return {line.split()[0]: int(line.split()[1])
            for line in Path("/proc/vmstat").read_text().splitlines()
            if line.startswith(("pswpin ", "pswpout "))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["smoke", "build"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    receipt = args.output / f"{args.phase}-receipt.json"
    if receipt.exists() or (args.phase == "build" and (args.output / "vectors.jsonl").exists()):
        raise FileExistsError("Preserving previous run artifacts")
    if args.phase == "build":
        smoke = json.loads((args.output / "smoke-receipt.json").read_text())
        if smoke["telemetry"]["exit_status"] != 0:
            raise ValueError("Successful monitored smoke required before build")
    group = Path("/sys/fs/cgroup") / Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1].lstrip("/")
    memory = host_memory()
    ceiling = min(4 * 1024**3, ((memory["MemAvailable"] - 10 * 1024**3) // 1024**3) * 1024**3,
                  memory["MemTotal"] // 2)
    quota, period = (group / "cpu.max").read_text().split()
    if (ceiling <= 0 or (group / "memory.max").read_text().strip() == "max"
            or int((group / "memory.max").read_text()) > ceiling
            or (group / "memory.swap.max").read_text().strip() != "0"
            or quota == "max" or int(quota) / int(period) > 2):
        raise RuntimeError("Require bounded two-CPU, <=4 GiB RAM/no-swap private cgroup and host headroom")
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    resource.setrlimit(resource.RLIMIT_CPU, (900, 900))
    gpu = subprocess.check_output(["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw", "--format=csv"], text=True)
    baseline_swap = swap_counters()
    telemetry = {"host_preflight": memory, "loadavg": Path("/proc/loadavg").read_text().strip(),
                 "gpu_preflight": gpu, "cpu_affinity": sorted(os.sched_getaffinity(0)),
                 "ram_limit_bytes": int((group / "memory.max").read_text()),
                 "min_host_available_bytes": memory["MemAvailable"], "peak_cgroup_bytes": 0,
                 "host_swap_baseline": baseline_swap, "job_swap_bytes": 0}
    stop = threading.Event()
    started = time.monotonic()

    def monitor():
        while not stop.is_set():
            available = host_memory()["MemAvailable"]
            telemetry["min_host_available_bytes"] = min(available, telemetry["min_host_available_bytes"])
            telemetry["peak_cgroup_bytes"] = max(telemetry["peak_cgroup_bytes"], int((group / "memory.current").read_text()))
            telemetry["job_swap_bytes"] = int((group / "memory.swap.current").read_text())
            telemetry["host_memory_pressure"] = Path("/proc/pressure/memory").read_text().strip()
            telemetry["host_swap_final"] = swap_counters()
            pressure = float(telemetry["host_memory_pressure"].splitlines()[1].split()[1].split("=")[1])
            if (available <= 10 * 1024**3 or telemetry["job_swap_bytes"]
                    or telemetry["host_swap_final"] != baseline_swap or pressure > 1.0
                    or time.monotonic() - started > 890):
                telemetry.update(exit_status=75, stop_reason="memory/swap/pressure/time safeguard",
                                 elapsed_seconds=time.monotonic() - started,
                                 peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
                write_json(receipt, {"phase": args.phase, "telemetry": telemetry})
                os._exit(75)
            stop.wait(1)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    result, status = None, 1
    try:
        result = embed(args.phase, args.output)
        status = 0
    finally:
        stop.set()
        thread.join(timeout=2)
        telemetry.update(elapsed_seconds=time.monotonic() - started,
                         peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, exit_status=status)
        write_json(receipt, {"phase": args.phase, "result": result, "telemetry": telemetry})
    print(json.dumps({"phase": args.phase, "result": result, "telemetry": telemetry}), flush=True)


if __name__ == "__main__":
    main()
