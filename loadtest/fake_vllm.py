#!/usr/bin/env python3
"""Fake vLLM: an OpenAI-compatible streaming server that exports vLLM's Prometheus metrics. Stdlib only.

It imitates what the platform depends on, so the whole loop (scraping, recording rules, alerts, KEDA,
Grafana) runs on a laptop without a GPU (`make dev-up`):

  POST /v1/chat/completions   SSE streaming, stream_options.include_usage, ignore_eos
  GET  /health, /v1/models
  GET  /metrics               vLLM v0.32 metric names, labels and histogram buckets
  GET  /admin                 current fault settings
  POST /admin                 fault injection for drills, e.g. {"error_rate": 0.2, "ttft": 3.0}

Latency model: at most --capacity requests decode at once (the batch) and the rest wait, so TTFT grows
with load. Prefill time grows with prompt length, inter-token latency with batch size. KV cache usage is
the tokens held by running requests divided by --kv-cache-tokens.

  python fake_vllm.py --port 8001 --capacity 8
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import time
import uuid
from dataclasses import dataclass

# Same bucket boundaries as vllm/v1/metrics/buckets.py (v0.32.0), so SLO rules using le="2.5" work.
TTFT_BUCKETS = (0.001, 0.005, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5,
                10.0, 20.0, 40.0, 80.0, 160.0, 640.0, 2560.0)
ITL_BUCKETS = (0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0,
               20.0, 40.0, 80.0)
LATENCY_BUCKETS = (0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 2.5, 5.0, 10.0, 15.0, 20.0, 30.0, 40.0, 50.0, 60.0,
                   120.0, 240.0, 480.0, 960.0, 1920.0, 7680.0)
TOKEN_BUCKETS = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000)

REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}
# A prompt this long shares its first part with earlier prompts often enough to count as a prefix hit.
PREFIX_CHARS = 2000


class Histogram:
    def __init__(self, buckets: tuple[float, ...]):
        self.buckets = buckets
        self.counts = [0] * len(buckets)  # cumulative, like Prometheus buckets
        self.count = 0
        self.sum = 0.0

    def observe(self, value: float) -> None:
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                self.counts[i] += 1
        self.count += 1
        self.sum += value

    def render(self, name: str, labels: str) -> list[str]:
        lines = [f'{name}_bucket{{{labels},le="{float(b)!r}"}} {c}' for b, c in zip(self.buckets, self.counts, strict=True)]
        lines.append(f'{name}_bucket{{{labels},le="+Inf"}} {self.count}')
        lines.append(f"{name}_count{{{labels}}} {self.count}")
        lines.append(f"{name}_sum{{{labels}}} {self.sum}")
        return lines


@dataclass
class Faults:
    error_rate: float
    ttft: float
    itl: float


class FakeVLLM:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.faults = Faults(args.error_rate, args.ttft, args.itl)
        self.batch = asyncio.Semaphore(args.capacity)
        self.rng = random.Random(args.seed)
        self.running = self.waiting = self.kv_tokens = 0
        self.seen_prefixes: set[int] = set()
        self.labels = f'model_name="{args.model_name}",engine="0"'
        self.counters = {"prompt_tokens": 0, "generation_tokens": 0, "num_preemptions": 0,
                         "prefix_cache_queries": 0, "prefix_cache_hits": 0}
        self.finished = {"stop": 0, "length": 0, "abort": 0}
        self.http: dict[tuple[str, str, str], int] = {}
        self.hist = {
            "vllm:time_to_first_token_seconds": Histogram(TTFT_BUCKETS),
            "vllm:inter_token_latency_seconds": Histogram(ITL_BUCKETS),
            "vllm:e2e_request_latency_seconds": Histogram(LATENCY_BUCKETS),
            "vllm:request_queue_time_seconds": Histogram(LATENCY_BUCKETS),
            "vllm:request_prefill_time_seconds": Histogram(LATENCY_BUCKETS),
            "vllm:request_decode_time_seconds": Histogram(LATENCY_BUCKETS),
            "vllm:request_prompt_tokens": Histogram(TOKEN_BUCKETS),
            "vllm:request_generation_tokens": Histogram(TOKEN_BUCKETS),
        }

    # --- HTTP plumbing -------------------------------------------------------------------------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            method, target, _ = request_line.decode("latin-1").split(" ", 2)
            path = target.split("?", 1)[0]
            headers = {}
            while (line := await reader.readline()) not in (b"\r\n", b"\n", b""):
                key, _, value = line.decode("latin-1").partition(":")
                headers[key.strip().lower()] = value.strip()
            body = await reader.readexactly(int(headers.get("content-length", "0")))

            if method == "GET" and path == "/health":
                await self.respond(writer, 200, b"", "text/plain")
            elif method == "GET" and path == "/metrics":
                await self.respond(writer, 200, self.render_metrics().encode(), "text/plain; version=0.0.4; charset=utf-8")
            elif path == "/admin":
                await self.admin(writer, method, body)
            elif method == "GET" and path == "/v1/models":
                models = {"object": "list", "data": [{"id": self.args.model_name, "object": "model"}]}
                await self.respond(writer, 200, json.dumps(models).encode())
                self.count_http(path, method, 200)
            elif method == "POST" and path == "/v1/chat/completions":
                await self.chat(writer, body)
            else:
                await self.respond(writer, 404, b'{"error": "not found"}')
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def respond(writer: asyncio.StreamWriter, status: int, body: bytes, ctype: str = "application/json") -> None:
        writer.write(f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}\r\nContent-Type: {ctype}\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()

    def count_http(self, handler: str, method: str, status: int) -> None:
        key = (handler, method, f"{status // 100}xx")  # grouped like prometheus-fastapi-instrumentator
        self.http[key] = self.http.get(key, 0) + 1

    async def admin(self, writer: asyncio.StreamWriter, method: str, body: bytes) -> None:
        if method == "POST":
            update = json.loads(body or b"{}")
            for field in ("error_rate", "ttft", "itl"):
                if field in update:
                    setattr(self.faults, field, float(update[field]))
            print(f"faults updated: {self.faults}", flush=True)
        await self.respond(writer, 200, json.dumps(self.faults.__dict__).encode())

    # --- the "engine" --------------------------------------------------------------------------

    async def chat(self, writer: asyncio.StreamWriter, body: bytes) -> None:
        handler = "/v1/chat/completions"
        try:
            req = json.loads(body or b"{}")
            prompt = "".join(str(m.get("content", "")) for m in req.get("messages", []))
        except (ValueError, AttributeError):
            await self.respond(writer, 400, b'{"error": "invalid JSON"}')
            self.count_http(handler, "POST", 400)
            return

        if self.rng.random() < self.faults.error_rate:
            error = {"error": {"message": "injected fault", "type": "InternalServerError", "code": 500}}
            await self.respond(writer, 500, json.dumps(error).encode())
            self.count_http(handler, "POST", 500)
            return

        max_tokens = int(req.get("max_tokens") or 16)
        n_out = max_tokens if req.get("ignore_eos") else self.rng.randint(max(1, max_tokens // 4), max_tokens)
        n_prompt = max(1, len(prompt) // 4)
        rid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n"
                     b"Connection: close\r\n\r\n")
        self.count_http(handler, "POST", 200)

        def event(payload: dict) -> bytes:
            return f"data: {json.dumps(payload)}\n\n".encode()

        arrival = time.perf_counter()
        self.waiting += 1
        generated = 0
        reason = "abort"
        preempted = False
        async with self.batch:
            self.waiting -= 1
            self.running += 1
            self.kv_tokens += n_prompt
            admitted = time.perf_counter()
            try:
                self.prefix_cache(prompt, n_prompt)
                await asyncio.sleep(self.faults.ttft + n_prompt * self.args.prefill_seconds_per_token)
                first = last = time.perf_counter()
                for i in range(n_out):
                    now = time.perf_counter()
                    if i:
                        self.hist["vllm:inter_token_latency_seconds"].observe(now - last)
                    last = now
                    writer.write(event({"id": rid, "object": "chat.completion.chunk", "created": int(time.time()),
                                        "model": self.args.model_name,
                                        "choices": [{"index": 0, "delta": {"content": f" tok{i}"}, "finish_reason": None}]}))
                    await writer.drain()
                    generated += 1
                    self.kv_tokens += 1
                    if self.kv_tokens > self.args.kv_cache_tokens and not preempted:
                        # vLLM would evict this sequence and recompute it later; count it once.
                        self.counters["num_preemptions"] += 1
                        preempted = True
                    # Decode slows down as the batch fills up.
                    await asyncio.sleep(self.faults.itl * (1 + self.running / self.args.capacity))
                reason = "length" if n_out == max_tokens else "stop"
                if (req.get("stream_options") or {}).get("include_usage"):
                    writer.write(event({"id": rid, "object": "chat.completion.chunk", "model": self.args.model_name,
                                        "choices": [], "usage": {"prompt_tokens": n_prompt, "completion_tokens": n_out,
                                                                 "total_tokens": n_prompt + n_out}}))
                writer.write(b"data: [DONE]\n\n")
                await writer.drain()
            finally:
                end = time.perf_counter()
                self.running -= 1
                self.kv_tokens -= n_prompt + generated
                self.finished[reason] += 1
                self.counters["prompt_tokens"] += n_prompt
                self.counters["generation_tokens"] += generated
                self.hist["vllm:request_queue_time_seconds"].observe(admitted - arrival)
                self.hist["vllm:e2e_request_latency_seconds"].observe(end - arrival)
                self.hist["vllm:request_prompt_tokens"].observe(n_prompt)
                self.hist["vllm:request_generation_tokens"].observe(generated)
                if generated:
                    self.hist["vllm:time_to_first_token_seconds"].observe(first - arrival)
                    self.hist["vllm:request_prefill_time_seconds"].observe(first - admitted)
                    self.hist["vllm:request_decode_time_seconds"].observe(end - first)

    def prefix_cache(self, prompt: str, n_prompt: int) -> None:
        self.counters["prefix_cache_queries"] += n_prompt
        if len(prompt) >= PREFIX_CHARS:
            key = hash(prompt[:PREFIX_CHARS])
            if key in self.seen_prefixes:
                self.counters["prefix_cache_hits"] += PREFIX_CHARS // 4
            self.seen_prefixes.add(key)

    # --- /metrics ------------------------------------------------------------------------------

    def render_metrics(self) -> str:
        lb = self.labels
        out: list[str] = []

        def family(name: str, kind: str, help_text: str, samples: list[str]) -> None:
            out.extend([f"# HELP {name} {help_text}", f"# TYPE {name} {kind}", *samples])

        family("vllm:num_requests_running", "gauge", "Number of requests in model execution batches.",
               [f"vllm:num_requests_running{{{lb}}} {self.running}"])
        family("vllm:num_requests_waiting", "gauge", "Number of requests waiting to be processed.",
               [f"vllm:num_requests_waiting{{{lb}}} {self.waiting}"])
        family("vllm:kv_cache_usage_perc", "gauge", "KV-cache usage. 1 means 100 percent usage.",
               [f"vllm:kv_cache_usage_perc{{{lb}}} {min(1.0, self.kv_tokens / self.args.kv_cache_tokens)}"])
        for key, help_text in (("prompt_tokens", "Number of prefill tokens processed."),
                               ("generation_tokens", "Number of generation tokens processed."),
                               ("num_preemptions", "Cumulative number of preemption from the engine."),
                               ("prefix_cache_queries", "Prefix cache queries, in terms of number of queried tokens."),
                               ("prefix_cache_hits", "Prefix cache hits, in terms of number of cached tokens.")):
            family(f"vllm:{key}_total", "counter", help_text, [f"vllm:{key}_total{{{lb}}} {self.counters[key]}"])
        family("vllm:request_success_total", "counter", "Count of successfully processed requests.",
               [f'vllm:request_success_total{{{lb},finished_reason="{r}"}} {n}' for r, n in self.finished.items()])
        for name, hist in self.hist.items():
            family(name, "histogram", name.removeprefix("vllm:").replace("_", " ") + ".", hist.render(name, lb))
        family("http_requests_total", "counter", "Total number of requests by method, status and handler.",
               [f'http_requests_total{{handler="{h}",method="{m}",status="{s}"}} {n}' for (h, m, s), n in self.http.items()])
        return "\n".join(out) + "\n"


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8001)
    p.add_argument("--model-name", default="qwen2.5-1.5b", help="served model name, used as the model_name label")
    p.add_argument("--capacity", type=int, default=8, help="requests decoded concurrently (max-num-seqs)")
    p.add_argument("--ttft", type=float, default=0.15, help="base prefill time in seconds")
    p.add_argument("--prefill-seconds-per-token", type=float, default=0.00005)
    p.add_argument("--itl", type=float, default=0.02, help="seconds per output token with an empty batch")
    p.add_argument("--kv-cache-tokens", type=int, default=100_000)
    p.add_argument("--error-rate", type=float, default=0.0, help="share of requests answered with HTTP 500")
    p.add_argument("--seed", type=int, default=None)
    args = p.parse_args()

    server = await asyncio.start_server(FakeVLLM(args).handle, args.host, args.port)
    print(f"fake vLLM on http://{args.host}:{args.port} model={args.model_name} capacity={args.capacity}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
