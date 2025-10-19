#!/usr/bin/env python3
"""
stress_probe.py

Stress test the probe endpoint by issuing concurrent requests for multiple targets.
Now supports --targets-file to load targets from a text file.

Example:
    python stress_probe.py \
      --url "http://127.0.0.1:9642/probe" \
      --targets-file targets.txt \
      --concurrency 50 \
      --count 5 --burst 3
"""

import argparse
import asyncio
import time
from typing import List, Tuple
import aiohttp
import random
import statistics
import sys
from pathlib import Path

# ---------------- Helper ----------------
def percentile(sorted_list: List[float], p: float) -> float:
    if not sorted_list:
        return 0.0
    k = (len(sorted_list)-1) * (p/100.0)
    f = int(k)
    c = f + 1
    if c >= len(sorted_list):
        return sorted_list[-1]
    d0 = sorted_list[f] * (c - k)
    d1 = sorted_list[c] * (k - f)
    return d0 + d1

# ---------------- Worker ----------------
async def probe_once(session: aiohttp.ClientSession, base_url: str, target: str,
                     params: dict, timeout: int, retries: int) -> Tuple[bool, float, int, str]:
    """Perform a single HTTP GET to the probe URL with retries."""
    attempt = 0
    while attempt <= retries:
        attempt += 1
        start = time.monotonic()
        try:
            async with session.get(base_url, params={**params, "target": target}, timeout=timeout) as resp:
                text = await resp.text()
                elapsed = time.monotonic() - start
                success = 200 <= resp.status < 400
                return (success, elapsed, resp.status, (text[:200] if not success else ""))
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            err = "timeout"
        except Exception as e:
            elapsed = time.monotonic() - start
            err = str(e)
        if attempt <= retries:
            await asyncio.sleep(0.1 * (2 ** (attempt - 1)) + random.random() * 0.05)
        else:
            return (False, elapsed, 0, err)

# ---------------- Main orchestrator ----------------
async def run_stress_test(base_url: str, targets: List[str],
                          concurrency: int, timeout: int,
                          retries: int) -> None:
    connector = aiohttp.TCPConnector(limit_per_host=concurrency, limit=0)
    timeout_obj = aiohttp.ClientTimeout(total=None)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout_obj) as session:
        params = {}
        print(f"Launching {len(targets)} concurrent probe requests...")
        start_all = time.monotonic()
        tasks = [probe_once(session, base_url, t, params, timeout, retries) for t in targets]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        total_time = time.monotonic() - start_all

    # summary
    latencies = [r[1] for r in results if r[0]]
    failures = [r for r in results if not r[0]]
    success_count = len(results) - len(failures)
    print("\n--- Results ---")
    print(f"Total requests: {len(results)}")
    print(f"Successes: {success_count}")
    print(f"Failures: {len(failures)}")
    print(f"Total elapsed time: {total_time:.3f}s")
    if latencies:
        lat_sorted = sorted(latencies)
        print(f"Avg latency: {statistics.mean(lat_sorted):.4f}s")
        print(f"Min: {lat_sorted[0]:.4f}s, Max: {lat_sorted[-1]:.4f}s")
        print(f"P50: {percentile(lat_sorted,50):.4f}s, "
              f"P90: {percentile(lat_sorted,90):.4f}s, "
              f"P99: {percentile(lat_sorted,99):.4f}s")
    if failures:
        print("\nSample failures (up to 10):")
        for ok, elapsed, status, err in failures[:10]:
            print(f"  status={status}, elapsed={elapsed:.3f}s, err={err}")

# ---------------- CLI parsing ----------------
def parse_args():
    p = argparse.ArgumentParser(description="Stress test probe endpoint.")
    p.add_argument("--url", default="http://127.0.0.1:9642/probe",
                   help="Probe endpoint base URL (default: http://127.0.0.1:9642/probe)")
    p.add_argument("--base-target", default="google.com",
                   help="Base target (used only if no targets file given).")
    p.add_argument("--targets-file", type=str,
                   help="Path to a file containing targets (one per line).")
    p.add_argument("--concurrency", "-c", type=int, default=50,
                   help="Concurrent request count (default 50).")
    p.add_argument("--timeout", type=int, default=10)
    p.add_argument("--retries", type=int, default=1)
    return p.parse_args()

def load_targets_from_file(path: str) -> List[str]:
    path_obj = Path(path)
    if not path_obj.exists():
        print(f"Error: file '{path}' not found.", file=sys.stderr)
        sys.exit(1)
    with open(path_obj, "r", encoding="utf-8") as f:
        targets = [line.strip() for line in f if line.strip()]
    if not targets:
        print(f"Error: no targets found in {path}", file=sys.stderr)
        sys.exit(1)
    return targets

# ---------------- Entrypoint ----------------
def main():
    args = parse_args()
    if args.targets_file:
        targets = load_targets_from_file(args.targets_file)
    else:
        targets = [args.base_target] * args.concurrency

    try:
        asyncio.run(run_stress_test(args.url, targets,
                                    concurrency=args.concurrency,
                                    timeout=args.timeout, retries=args.retries))
    except KeyboardInterrupt:
        print("\nInterrupted by user.")

if __name__ == "__main__":
    main()
