"""Run one research script inside an already bounded private systemd cgroup."""

import argparse
import json
import os
from pathlib import Path
import resource
import runpy
import subprocess
import sys
import threading
import time

from recifine_probe import host_memory, swap


def swap_activity(previous, current, baseline, elapsed, allowed_swapin_rate):
    rate = (current['pswpin'] - previous['pswpin']) / max(elapsed, 0.001)
    return rate, current['pswpout'] != baseline['pswpout'] or rate > allowed_swapin_rate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--memory-gib", type=int, default=1)
    parser.add_argument("--model", action="store_true")
    parser.add_argument("--threads", type=int, choices=[1, 2], default=2)
    parser.add_argument("--cpu-core", type=int)
    parser.add_argument("--max-host-swapin-pages-per-second", type=int, default=0,
                        help="Allow bounded reads of old swapped pages; new swap writes always stop")
    parser.add_argument("script")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not 0 <= args.max_host_swapin_pages_per_second <= 256:
        raise ValueError("Host swap-in allowance must be between zero and 256 pages/second")
    if args.receipt.exists():
        raise FileExistsError(args.receipt)
    threads = str(args.threads)
    os.environ.update(CUDA_VISIBLE_DEVICES="", OMP_NUM_THREADS=threads, MKL_NUM_THREADS=threads,
                      OPENBLAS_NUM_THREADS=threads, NUMEXPR_NUM_THREADS=threads, TOKENIZERS_PARALLELISM="false")
    os.environ.setdefault('PGOPTIONS', '-c statement_timeout=60000')
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    if args.cpu_core is not None:
        assert args.cpu_core in os.sched_getaffinity(0)
        os.sched_setaffinity(0, [args.cpu_core])
    resource.setrlimit(resource.RLIMIT_CPU, (900, 900))
    group = Path("/sys/fs/cgroup") / Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1].lstrip("/")
    memory, initial_swap = host_memory(), swap()
    maximum = (group / "memory.max").read_text().strip()
    quota, period = (group / "cpu.max").read_text().split()
    ceiling = min(args.memory_gib * 1024**3, ((memory["MemAvailable"] - 10 * 1024**3) // 1024**3) * 1024**3,
                  memory["MemTotal"] // 2)
    if ceiling <= 0 or maximum == "max" or int(maximum) > ceiling or (group / "memory.swap.max").read_text().strip() != "0" or quota == "max" or int(quota) > 2 * int(period):
        raise RuntimeError("Private two-CPU/RAM/no-swap cgroup required")
    data = {"host_preflight": memory, "cpu_affinity": sorted(os.sched_getaffinity(0)),
            "numerical_threads": args.threads, "cgroup_cpu_limit": int(quota) / int(period),
            "loadavg": Path("/proc/loadavg").read_text().strip(), "ram_limit_bytes": int(maximum),
            "host_swap_baseline": initial_swap, "min_host_available_bytes": memory["MemAvailable"],
            "peak_cgroup_bytes": 0, "job_swap_bytes": 0, "gpu_used": False}
    data["max_host_swapin_pages_per_second"] = args.max_host_swapin_pages_per_second
    if args.model:
        data["gpu_preflight"] = subprocess.check_output(["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw", "--format=csv"], text=True)
    start, stop = time.monotonic(), threading.Event()

    def receipt(status):
        data.update(exit_status=status, elapsed_seconds=time.monotonic() - start,
                    peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        with args.receipt.open("x") as stream:
            json.dump(data, stream, indent=2)

    def monitor():
        last_swap, last_time = initial_swap, start
        while not stop.is_set():
            available = host_memory()["MemAvailable"]
            data["min_host_available_bytes"] = min(available, data["min_host_available_bytes"])
            data["peak_cgroup_bytes"] = max(data["peak_cgroup_bytes"], int((group / "memory.current").read_text()))
            data["job_swap_bytes"] = int((group / "memory.swap.current").read_text())
            data["host_swap_final"] = swap()
            now = time.monotonic()
            swapin_rate, swapping = swap_activity(last_swap, data['host_swap_final'], initial_swap,
                                                  now - last_time, args.max_host_swapin_pages_per_second)
            data["peak_host_swapin_pages_per_second"] = max(data.get("peak_host_swapin_pages_per_second", 0), swapin_rate)
            last_swap, last_time = data["host_swap_final"], now
            data["host_pressure"] = Path("/proc/pressure/memory").read_text().strip()
            pressure = float(data["host_pressure"].splitlines()[1].split()[1].split("=")[1])
            if available <= 10 * 1024**3 or data["job_swap_bytes"] or swapping or pressure > 1.0 or time.monotonic() - start > 890:
                data["stop_reason"] = "host memory/swap/pressure/time safeguard"
                receipt(75)
                os._exit(75)
            stop.wait(1)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    status = 1
    try:
        sys.argv = [args.script, *args.arguments]
        runpy.run_path(args.script, run_name="__main__")
        status = 0
    except SystemExit as error:
        status = error.code if isinstance(error.code, int) else 1
        raise
    finally:
        stop.set()
        thread.join(timeout=2)
        receipt(status)


if __name__ == "__main__":
    main()
