#!/usr/bin/env python3
"""Closed-loop load generator for OpenAI-compatible LLM servers (vLLM, Triton's OpenAI frontend, ...).

Runs one stage per concurrency level. Each virtual user sends streaming chat completions back
to back, so offered load equals concurrency. Per request it records:

  TTFT  time to first streamed token: what a user perceives as "it started answering"
  ITL   mean gap between streamed tokens after the first one: reading speed
  E2E   whole request

The summary marks the stages that meet the TTFT SLO. The highest passing concurrency, divided by
the replica count, is the value for autoscaling.targetInFlightPerReplica.

  pip install httpx==0.28.1
  python llm_load.py --base-url http://localhost:8000 --model qwen2.5-1.5b --stages 1,8,16,32,64
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import time
from dataclasses import asdict, dataclass

import httpx

WORDS = [
    "cluster", "node", "pod", "gpu", "kernel", "driver", "latency", "throughput", "batch", "token", "cache",
    "prefill", "decode", "replica", "scheduler", "queue", "memory", "bandwidth", "tensor", "model", "weights",
    "request", "response", "stream", "budget", "error", "alert", "dashboard", "metric", "label", "rollout",
    "upgrade", "drain", "spot", "capacity", "cost", "region", "zone", "network", "storage", "volume",
]


@dataclass
class Result:
    ok: bool
    ttft: float | None = None
    itl: float | None = None
    e2e: float = 0.0
    output_tokens: int = 0
    error: str = ""


@dataclass
class StageSummary:
    concurrency: int
    requests: int
    errors: int
    duration_s: float
    rps: float
    output_tokens_per_s: float
    ttft_p50: float | None
    ttft_p95: float | None
    ttft_p99: float | None
    itl_p50: float | None
    itl_p95: float | None
    e2e_p50: float | None
    e2e_p95: float | None
    meets_slo: bool


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile: the smallest value with at least q of the samples at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[rank]


def make_prompt(rng: random.Random, approx_tokens: int, shared_prefix: str) -> str:
    body = " ".join(rng.choice(WORDS) for _ in range(max(1, int(approx_tokens * 0.75))))
    return f"{shared_prefix}Summarise these operations notes in three short bullet points:\n{body}"


async def one_request(client: httpx.AsyncClient, args: argparse.Namespace, prompt: str) -> Result:
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": args.max_tokens,
        "temperature": 0.7,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if args.ignore_eos:
        # vLLM extension: always generate max_tokens, so every request costs the same.
        payload["ignore_eos"] = True

    start = time.perf_counter()
    first = last = None
    chunks = 0
    usage_tokens = None
    try:
        async with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode(errors="replace")[:200]
                return Result(ok=False, e2e=time.perf_counter() - start, error=f"HTTP {resp.status_code}: {body}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("usage"):
                    usage_tokens = chunk["usage"].get("completion_tokens")
                for choice in chunk.get("choices") or []:
                    if (choice.get("delta") or {}).get("content"):
                        now = time.perf_counter()
                        first = first or now
                        last = now
                        chunks += 1
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        return Result(ok=False, e2e=time.perf_counter() - start, error=f"{type(exc).__name__}: {exc}"[:200])

    end = time.perf_counter()
    if first is None:
        return Result(ok=False, e2e=end - start, error="stream ended without tokens")
    return Result(
        ok=True,
        ttft=first - start,
        itl=(last - first) / (chunks - 1) if chunks > 1 else None,
        e2e=end - start,
        output_tokens=usage_tokens if usage_tokens is not None else chunks,
    )


def client_for(args: argparse.Namespace, connections: int) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    return httpx.AsyncClient(
        base_url=args.base_url,
        headers=headers,
        timeout=httpx.Timeout(args.timeout, connect=10.0),
        limits=httpx.Limits(max_connections=connections, max_keepalive_connections=connections),
    )


async def run_stage(args: argparse.Namespace, concurrency: int, shared_prefix: str) -> StageSummary:
    results: list[Result] = []
    deadline = time.perf_counter() + args.stage_duration

    async with client_for(args, concurrency + 4) as client:
        async def user(uid: int) -> None:
            rng = random.Random(args.seed * 1000 + uid)
            while time.perf_counter() < deadline:
                results.append(await one_request(client, args, make_prompt(rng, args.prompt_tokens, shared_prefix)))

        started = time.perf_counter()
        await asyncio.gather(*(user(i) for i in range(concurrency)))
        elapsed = time.perf_counter() - started

    ok = [r for r in results if r.ok]
    ttfts = [r.ttft for r in ok if r.ttft is not None]
    itls = [r.itl for r in ok if r.itl is not None]
    e2es = [r.e2e for r in ok]
    ttft_p95 = percentile(ttfts, 0.95)
    errors = len(results) - len(ok)
    for r in results:
        if not r.ok:
            print(f"  error: {r.error}", file=sys.stderr)
            break
    return StageSummary(
        concurrency=concurrency,
        requests=len(results),
        errors=errors,
        duration_s=round(elapsed, 2),
        rps=round(len(ok) / elapsed, 3) if elapsed else 0.0,
        output_tokens_per_s=round(sum(r.output_tokens for r in ok) / elapsed, 1) if elapsed else 0.0,
        ttft_p50=percentile(ttfts, 0.5),
        ttft_p95=ttft_p95,
        ttft_p99=percentile(ttfts, 0.99),
        itl_p50=percentile(itls, 0.5),
        itl_p95=percentile(itls, 0.95),
        e2e_p50=percentile(e2es, 0.5),
        e2e_p95=percentile(e2es, 0.95),
        meets_slo=bool(ok) and ttft_p95 is not None and ttft_p95 <= args.slo_ttft_p95
        and errors / max(1, len(results)) <= args.max_error_rate,
    )


def fmt(v: float | None, scale: float = 1.0, digits: int = 2) -> str:
    return "-" if v is None else f"{v * scale:.{digits}f}"


def print_table(rows: list[StageSummary], slo: float) -> None:
    header = (f"{'conc':>5} {'reqs':>6} {'err':>4} {'req/s':>7} {'out tok/s':>10} "
              f"{'TTFT p50':>9} {'p95':>7} {'p99':>7} {'ITL p50 ms':>11} {'p95':>6} {'E2E p95':>8}  SLO")
    print(header)
    print("-" * len(header))
    for r in rows:
        print(f"{r.concurrency:>5} {r.requests:>6} {r.errors:>4} {r.rps:>7.2f} {r.output_tokens_per_s:>10.1f} "
              f"{fmt(r.ttft_p50):>9} {fmt(r.ttft_p95):>7} {fmt(r.ttft_p99):>7} "
              f"{fmt(r.itl_p50, 1000, 1):>11} {fmt(r.itl_p95, 1000, 1):>6} {fmt(r.e2e_p95):>8}  "
              f"{'ok' if r.meets_slo else 'MISS'}")
    passing = [r.concurrency for r in rows if r.meets_slo]
    print()
    if passing:
        print(f"Highest concurrency meeting TTFT p95 <= {slo}s: {max(passing)}")
    else:
        print(f"No stage met TTFT p95 <= {slo}s")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--model", default="qwen2.5-1.5b", help="served model name")
    p.add_argument("--api-key", default="", help="sent as a Bearer token when set")
    p.add_argument("--stages", default="1,4,8,16,32", help="comma-separated concurrency levels")
    p.add_argument("--stage-duration", type=float, default=60.0, help="seconds per stage")
    p.add_argument("--prompt-tokens", type=int, default=512, help="approximate prompt length")
    p.add_argument("--shared-prefix-tokens", type=int, default=0,
                   help="identical prefix on every prompt, to exercise prefix caching")
    p.add_argument("--max-tokens", type=int, default=128)
    p.add_argument("--ignore-eos", action=argparse.BooleanOptionalAction, default=True,
                   help="vLLM extension that forces exactly max-tokens output")
    p.add_argument("--warmup-requests", type=int, default=2)
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--slo-ttft-p95", type=float, default=2.5, help="seconds")
    p.add_argument("--max-error-rate", type=float, default=0.01)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--json", dest="json_path", help="write per-stage results to this file")
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> list[StageSummary] | None:
    stages = [int(s) for s in args.stages.split(",") if s.strip()]
    rng = random.Random(args.seed)
    shared_prefix = ""
    if args.shared_prefix_tokens:
        shared_prefix = make_prompt(rng, args.shared_prefix_tokens, "") + "\n\n"

    print(f"target {args.base_url} model={args.model} prompt~{args.prompt_tokens} tok "
          f"max_tokens={args.max_tokens} stages={stages} x {args.stage_duration:.0f}s")
    async with client_for(args, 2) as client:
        for _ in range(args.warmup_requests):
            warm = await one_request(client, args, make_prompt(rng, args.prompt_tokens, shared_prefix))
            if not warm.ok:
                print(f"warm-up failed: {warm.error}", file=sys.stderr)
                return None

    rows = []
    for concurrency in stages:
        print(f"stage: concurrency={concurrency} ...", flush=True)
        rows.append(await run_stage(args, concurrency, shared_prefix))
    return rows


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows = asyncio.run(run(args))
    if rows is None:
        return 2
    print()
    print_table(rows, args.slo_ttft_p95)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as f:
            json.dump({"args": vars(args), "stages": [asdict(r) for r in rows]}, f, indent=2)
        print(f"results written to {args.json_path}")

    total = sum(r.requests for r in rows)
    failed = sum(r.errors for r in rows)
    return 1 if total == 0 or failed / total > args.max_error_rate else 0


if __name__ == "__main__":
    sys.exit(main())
