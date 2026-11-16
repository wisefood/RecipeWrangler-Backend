"""Bounded offline RecipeBERT probe, using installed Transformers without ReciFine.

Run acquire/smoke/evaluate in a private CPU-only systemd cgroup. No installation,
remote model code, pickle loading, database writes or production integration.
"""

import argparse
from collections import Counter
from importlib.metadata import version
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import threading
import time
from urllib.parse import urlparse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
BASE = Path(os.getenv("RECIFINE_PROBE_OUTPUT", str(ROOT / "evaluation/instructions/recifine-traditional-v1")))
MODEL_ID = "nuhuibrahim/recifinegold-recipebert-trad"
REVISION = "7686d9b1fda9123a768e2e80c70cb6a438b8d370"
UPSTREAM = "9c22c310cb07c4193aee85ce26e9897f3920b80c"
TYPES = ["ACTION BY CHEF", "ACTION BY FOOD", "ACTION BY TOOL", "DISCONTINUOUS ACTION",
         "DURATION", "FOOD", "FOOD STATE", "QUANTITY", "TOOL", "TOOL STATE"]
LABELS = ["O"] + sorted(f"{prefix}-{kind}" for kind in TYPES for prefix in ("B", "I"))


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def save(path, data):
    with path.open("x") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def fetch(url, path, ceiling):
    """One streamed request at a time, bounded bytes and socket timeout."""
    total = 0
    with urllib.request.urlopen(url, timeout=60) as response, path.open("xb") as stream:
        resolved = response.url
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > ceiling:
                raise ValueError("Download size ceiling exceeded")
            stream.write(chunk)
    # Do not store temporary signed CDN URLs or their query-string credentials.
    return {"url": url, "resolved_host": urlparse(resolved).hostname,
            "bytes": total, "sha256": digest(path)}


def acquire():
    model_dir = BASE / "model"
    model_dir.mkdir(exist_ok=False)
    source_dir = BASE / "upstream"
    source_dir.mkdir()
    downloads = {}
    api_url = f"https://huggingface.co/api/models/{MODEL_ID}/revision/{REVISION}?blobs=true"
    downloads["hub-metadata.json"] = fetch(api_url, BASE / "hub-metadata.json", 1024**2)
    metadata = json.loads((BASE / "hub-metadata.json").read_text())
    if metadata["sha"] != REVISION:
        raise ValueError("Model revision mismatch")
    files = {row["rfilename"]: row for row in metadata["siblings"]}
    for name in ["config.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.txt",
                 "README.md", "model.safetensors", "bert_merge_test_predictions.jsonl", "bert_merge_test_results.txt"]:
        target = model_dir / name
        url = f"https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/{name}?download=true"
        downloads[name] = fetch(url, target, 500 * 1024**2 if name == "model.safetensors" else 5 * 1024**2)
        expected = files[name].get("lfs", {}).get("sha256")
        if expected and downloads[name]["sha256"] != expected:
            raise ValueError("Hub LFS checksum mismatch: " + name)
        print("downloaded", name, downloads[name]["bytes"], flush=True)
    for relative in ["data/labels.py", "data/features.py", "data/kaner.py", "data/transform.py",
                     "configs/datasets/recifinegold/paper.yaml", "models/hf_wrappers.py", "inferencing/inference.py",
                     "training/dataset.py"]:
        target = source_dir / relative.replace("/", "__")
        url = f"https://raw.githubusercontent.com/nuhu-ibrahim/ReciFine/{UPSTREAM}/src/recifine/{relative}"
        downloads[relative] = fetch(url, target, 1024**2)
    config = json.loads((model_dir / "config.json").read_text())
    yaml = (source_dir / "configs__datasets__recifinegold__paper.yaml").read_text()
    types = re.findall(r'^    type: ["\']?([^"\'\n]+)', yaml, flags=re.M)
    if sorted(t.upper() for t in types) != sorted(TYPES) or len(config["id2label"]) != len(LABELS):
        raise ValueError("Upstream entity schema disagrees with output label count")
    manifest = {"model_id": MODEL_ID, "revision": REVISION, "upstream_revision": UPSTREAM,
                "license": metadata.get("cardData", {}).get("license"),
                "labels": LABELS, "label_order": "O then sorted BIO labels, upstream data/labels.py",
                "downloads": downloads, "packages": {p: version(p) for p in ["torch", "transformers", "safetensors", "tokenizers"]}}
    save(BASE / "acquisition.json", manifest)
    freeze_cases()
    return {"files": len(downloads), "bytes": sum(v["bytes"] for v in downloads.values()), "revision": REVISION}


PROBES = [
    ("Simmer the beans in a saucepan for 20 minutes.", [("FOOD", "beans"), ("TOOL", "saucepan"), ("DURATION", "20 minutes")]),
    ("Chop the onions and fry them for 5 minutes.", [("FOOD", "onions"), ("DURATION", "5 minutes")]),
    ("Preheat the oven to 180 degrees C.", [("TOOL", "oven"), ("TOOL STATE", "180 degrees C")]),
    ("Drain and rinse the canned beans.", [("FOOD", "beans")]),
    ("Soak the dried beans overnight.", [("FOOD", "beans"), ("DURATION", "overnight")]),
    ("Stir the sauce continuously for 2 minutes.", [("FOOD", "sauce"), ("DURATION", "2 minutes")]),
    ("Cook the rice for 15 to 20 minutes, until tender.", [("FOOD", "rice"), ("DURATION", "15 to 20 minutes"), ("FOOD STATE", "tender")]),
    ("Meanwhile, chop the carrots.", [("FOOD", "carrots")]),
    ("Do not boil the sauce.", [("FOOD", "sauce")]),
    ("Allow the cake to cool before slicing.", [("FOOD", "cake")]),
    ("Bake in two batches for 12 minutes per batch.", [("DURATION", "12 minutes")]),
    ("Refrigerate leftovers within 2 hours and use within 3 days.", [("DURATION", "2 hours"), ("DURATION", "3 days")]),
]


def freeze_cases():
    cases = [{"id": f"probe-{i:02d}", "kind": "authored_diagnostic", "text": text,
              "expected_mentions": [{"type": kind, "text": mention} for kind, mention in expected]}
             for i, (text, expected) in enumerate(PROBES, 1)]
    sources = {}
    for filename, field, title_field in [("data/FoodHero/foodhero_recipes_clean.json", "instructions", "title"),
                                         ("data/SafeFood_web/safefood_dinner_recipes.json", "method", "name")]:
        path = ROOT / filename
        sources[filename] = {"sha256": digest(path), "sampling": "first three recipes in stored order, first eight steps each"}
        data = json.loads(path.read_text())
        rows = list(data.values()) if isinstance(data, dict) else data
        for ri, row in enumerate(rows[:3], 1):
            for si, text in enumerate(row.get(field, [])[:8], 1):
                if isinstance(text, str) and text.strip():
                    cases.append({"id": f"{path.parent.name}-{ri}-{si}", "kind": "local_recipe_step",
                                  "source_file": filename, "title": row.get(title_field), "url": row.get("url"),
                                  "step": si, "text": text})
    published = BASE / "model/bert_merge_test_predictions.jsonl"
    with published.open() as stream:
        for i, line in enumerate(stream):
            if i == 20:
                break
            row = json.loads(line)
            cases.append({"id": f"author-{i+1}", "kind": "author_prediction_parity", "text": row["text"],
                          "words": row["input_words"], "published_labels": row["predicted_label"]})
    save(BASE / "inputs.json", {"sources": sources, "cases": cases,
        "scope": "Purposive development probe; expected mentions are incomplete checks, not full gold annotations. No independence from pretraining is claimed."})


def decode_spans(text, offsets, labels):
    """Match upstream BIO handling; retain exact original-text offsets."""
    spans, current = [], None
    for (start, end), label in zip(offsets, labels):
        prefix, _, kind = label.partition("-")
        if prefix == "I" and current and current["type"] == kind:
            current["end"] = end
            current["text"] = text[current["start"]:end]
            continue
        if current:
            spans.append(current)
        current = {"type": kind, "start": start, "end": end, "text": text[start:end]} if prefix == "B" else None
    if current:
        spans.append(current)
    return spans


def normalize_mention(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


def infer(phase):
    os.environ.update(CUDA_VISIBLE_DEVICES="", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      OMP_NUM_THREADS="2", MKL_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2",
                      TOKENIZERS_PARALLELISM="false", HF_HUB_DISABLE_PROGRESS_BARS="1")
    manifest = json.loads((BASE / "acquisition.json").read_text())
    for name in ["config.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.txt", "model.safetensors"]:
        if digest(BASE / "model" / name) != manifest["downloads"][name]["sha256"]:
            raise ValueError("Checkpoint changed: " + name)
    import torch
    from transformers import BertForTokenClassification, BertTokenizer
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    torch.manual_seed(42)
    started = time.monotonic()
    model = BertForTokenClassification.from_pretrained(BASE / "model", local_files_only=True,
                use_safetensors=True, trust_remote_code=False).to("cpu").eval()
    tokenizers = {False: BertTokenizer.from_pretrained(BASE / "model", local_files_only=True, do_lower_case=False)}
    if phase == "casecheck":
        tokenizers[True] = BertTokenizer.from_pretrained(BASE / "model", local_files_only=True, do_lower_case=True)
    if model.config.num_labels != len(LABELS):
        raise ValueError("Wrong classifier shape")
    cases = json.loads((BASE / "inputs.json").read_text())["cases"]
    if phase == "smoke":
        cases = cases[:2]
    if phase == "casecheck":
        cases = [{**case, "id": case["id"] + ("-lower" if lower else "-preserved"), "lowercase": lower}
                 for case in cases if case["kind"] == "author_prediction_parity" for lower in [False, True]]
    results = []
    for case in cases:
        tokenizer = tokenizers[case.get("lowercase", False)]
        text = case["text"]
        matches = list(re.finditer(r"\S+", text))
        words = [match.group() for match in matches]
        if "words" in case and words != case["words"]:
            raise ValueError("Published word alignment mismatch")
        tokens, positions, offsets = [], [], []
        for match in matches:
            pieces = tokenizer.tokenize(match.group())
            if not pieces:
                raise ValueError("Empty word tokenization")
            positions.append(len(tokens) + 1)
            offsets.append(match.span())
            tokens.extend(pieces)
        # Refuse silent truncation rather than dropping later instructions.
        if len(tokens) > 254:
            results.append({**case, "status": "too_long", "wordpieces": len(tokens)})
            continue
        ids = tokenizer.convert_tokens_to_ids([tokenizer.cls_token] + tokens + [tokenizer.sep_token])
        real = len(ids)
        padding = 256 - real
        encoded = {"input_ids": torch.tensor([ids + [tokenizer.pad_token_id] * padding]),
                   "attention_mask": torch.tensor([[1] * real + [0] * padding]),
                   "token_type_ids": torch.zeros((1, 256), dtype=torch.long)}
        with torch.inference_mode():
            logits = model(**encoded).logits[0]
        if not torch.isfinite(logits).all():
            raise ValueError("Nonfinite model output")
        tags = [LABELS[int(logits[pos].argmax())] for pos in positions]
        spans = decode_spans(text, offsets, tags)
        result = {**case, "status": "predicted", "words": words, "labels": tags, "entities": spans}
        if "published_labels" in case:
            result["published_token_agreements"] = sum(a == b for a, b in zip(tags, case["published_labels"]))
            result["published_token_count"] = len(tags)
            result["published_exact_match"] = tags == case["published_labels"]
        if "expected_mentions" in case:
            result["mention_checks"] = [{**expected, "found": any(
                span["type"] == expected["type"] and normalize_mention(span["text"]) == normalize_mention(expected["text"])
                for span in spans)} for expected in case["expected_mentions"]]
        results.append(result)
        print(case["id"], json.dumps(spans, ensure_ascii=False), flush=True)
    save(BASE / f"{phase}-predictions.json", results)
    checked = [check for row in results for check in row.get("mention_checks", [])]
    author = [row for row in results if row["kind"] == "author_prediction_parity" and row["status"] == "predicted"]
    summary = {"cases": len(results), "groups": dict(Counter(row["kind"] for row in results)),
        "predicted": sum(row["status"] == "predicted" for row in results),
        "too_long": sum(row["status"] == "too_long" for row in results),
        "targeted_mentions_found": sum(c["found"] for c in checked), "targeted_mentions_checked": len(checked),
        "author_exact_sequences": sum(row["published_exact_match"] for row in author),
        "author_sequences": len(author), "author_token_agreements": sum(row["published_token_agreements"] for row in author),
        "author_tokens": sum(row["published_token_count"] for row in author),
        "load_and_inference_seconds": time.monotonic() - started,
        "preprocessing": "Upstream whitespace words, case preserved, first WordPiece per word, 256 positions, all segments 0; no silent truncation",
        "scope": "Checkpoint compatibility/parity and exploratory extraction only; no complete accuracy, relation extraction, time estimation or scheduling claim",
        "inputs_sha256": digest(BASE / "inputs.json"), "runner_sha256": digest(Path(__file__)),
        "model_revision": REVISION, "labels": LABELS}
    if phase == "casecheck":
        summary["casing_comparison"] = {str(lower): {
            "sequences": sum(row.get("lowercase") == lower for row in author),
            "exact_sequences": sum(row["published_exact_match"] for row in author if row.get("lowercase") == lower),
            "token_agreements": sum(row["published_token_agreements"] for row in author if row.get("lowercase") == lower),
            "tokens": sum(row["published_token_count"] for row in author if row.get("lowercase") == lower),
        } for lower in [False, True]}
        summary["preprocessing"] = "Fixed published inputs; compare case-preserving and lowercase tokenizers, all other settings unchanged"
    if phase == "smoke" and (summary["predicted"] != 2 or not any(row["entities"] for row in results)):
        raise ValueError("Smoke failed to produce entities")
    save(BASE / f"{phase}-summary.json", summary)
    return summary


def host_memory():
    return {line.split(":")[0]: int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith(("MemAvailable:", "MemTotal:"))}


def swap():
    return {line.split()[0]: int(line.split()[1]) for line in Path("/proc/vmstat").read_text().splitlines()
            if line.startswith(("pswpin ", "pswpout "))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["acquire", "smoke", "evaluate", "casecheck"])
    args = parser.parse_args()
    BASE.mkdir(parents=True, exist_ok=True)
    receipt = BASE / f"{args.phase}-receipt.json"
    if receipt.exists():
        raise FileExistsError(receipt)
    if args.phase in {"evaluate", "casecheck"} and json.loads((BASE / "smoke-receipt.json").read_text())["telemetry"]["exit_status"] != 0:
        raise ValueError("Successful smoke required")
    group = Path("/sys/fs/cgroup") / Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1].lstrip("/")
    memory = host_memory()
    ceiling = min(4 * 1024**3, ((memory["MemAvailable"] - 10 * 1024**3) // 1024**3) * 1024**3, memory["MemTotal"] // 2)
    maximum = (group / "memory.max").read_text().strip()
    quota, period = (group / "cpu.max").read_text().split()
    if ceiling <= 0 or maximum == "max" or int(maximum) > ceiling or (group / "memory.swap.max").read_text().strip() != "0" or quota == "max" or int(quota) / int(period) > 2:
        raise RuntimeError("Require private <=4 GiB/no-swap/two-CPU cgroup and host headroom")
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    resource.setrlimit(resource.RLIMIT_CPU, (900, 900))
    gpu = subprocess.check_output(["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw", "--format=csv"], text=True)
    baseline_swap = swap()
    telemetry = {"host_preflight": memory, "loadavg": Path("/proc/loadavg").read_text().strip(), "gpu_preflight": gpu,
                 "cpu_affinity": sorted(os.sched_getaffinity(0)), "ram_limit_bytes": int(maximum),
                 "min_host_available_bytes": memory["MemAvailable"], "peak_cgroup_bytes": 0,
                 "host_swap_baseline": baseline_swap, "job_swap_bytes": 0}
    started = time.monotonic()
    stop = threading.Event()

    def monitor():
        while not stop.is_set():
            available = host_memory()["MemAvailable"]
            telemetry["min_host_available_bytes"] = min(available, telemetry["min_host_available_bytes"])
            telemetry["peak_cgroup_bytes"] = max(telemetry["peak_cgroup_bytes"], int((group / "memory.current").read_text()))
            telemetry["job_swap_bytes"] = int((group / "memory.swap.current").read_text())
            telemetry["host_memory_pressure"] = Path("/proc/pressure/memory").read_text().strip()
            telemetry["host_swap_final"] = swap()
            pressure = float(telemetry["host_memory_pressure"].splitlines()[1].split()[1].split("=")[1])
            if available <= 10 * 1024**3 or telemetry["job_swap_bytes"] or telemetry["host_swap_final"] != baseline_swap or pressure > 1.0 or time.monotonic() - started > 890:
                telemetry.update(exit_status=75, stop_reason="host memory/swap/pressure/time safeguard",
                                 elapsed_seconds=time.monotonic() - started, peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
                save(receipt, {"phase": args.phase, "telemetry": telemetry})
                os._exit(75)
            stop.wait(1)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    result, status = None, 1
    try:
        result = acquire() if args.phase == "acquire" else infer(args.phase)
        status = 0
    finally:
        stop.set()
        thread.join(timeout=2)
        telemetry.update(elapsed_seconds=time.monotonic() - started,
                         peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, exit_status=status)
        save(receipt, {"phase": args.phase, "result": result, "telemetry": telemetry})
    print(json.dumps({"phase": args.phase, "result": result, "telemetry": telemetry}), flush=True)


if __name__ == "__main__":
    main()
