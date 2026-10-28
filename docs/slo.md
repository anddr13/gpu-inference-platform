# Service level objectives

Three SLOs for the OpenAI-compatible API, over a rolling 30 days. Rules live in
`charts/llm-observability/rules/llm-slo.rules.yaml`, with unit tests in `observability/tests/`.

| SLO | SLI | Target | Error budget |
|---|---|---|---|
| Availability | Share of `/v1/*` calls that do not return 5xx (`http_requests_total`, job `vllm`) | 99.5% | 0.5% of calls (~3.6 h of total outage per 30 days) |
| Latency | Share of requests whose first token arrives within 2.5 s (`vllm:time_to_first_token_seconds`) | 99% | 1% of requests |
| Decode speed | Share of streamed tokens that arrive within 50 ms of the previous one (`vllm:inter_token_latency_seconds`) | 95% | 5% of tokens |

## Why time to first token, and then decode speed

End-to-end latency depends on how many tokens the client asked for, so it cannot be promised. TTFT is what
users perceive as "it is answering", and for large models it is the first thing to degrade under load
(queueing + prefill).

The first A10G benchmark (docs/benchmarks.md) showed the other side: with a 1.5B model, TTFT p95 never got
past 2.5 s up to 96 concurrent requests, while inter-token latency tripled (8 ms → 34 ms). For small models
decode speed is what users notice first, so it became the third SLO. 50 ms per token is 20 tokens/s per user,
roughly fast reading. The measured runs stayed inside it: ITL p95 was 38 ms at the worst point.

2.5 s and 50 ms are bucket boundaries of vLLM's histograms, so both SLIs are exact rather than interpolated.

## Burn-rate alerts

Multi-window, multi-burn-rate, from the Google SRE Workbook. Burn rate 1 spends the budget in exactly 30 days.

| Alert | Long window | Short window | Burn rate | Budget spent before it fires | Severity |
|---|---|---|---|---|---|
| `*BudgetFastBurn` | 1 h | 5 m | 14.4 | 2% | critical (page) |
| `*BudgetSlowBurn` | 6 h | 30 m | 6 | 5% | warning (ticket) |

The short window makes the alert resolve quickly once the problem stops. For the 99.5% availability SLO,
a fast burn means more than 7.2% of calls failing over the last hour; for the 95% decode SLO, more than
72% of tokens slower than 50 ms.

## Blind spots

- A stream that fails after the 200 response headers counts as a success in `http_requests_total`. Watch
  `vllm:request_success_total{finished_reason="abort"}` on the dashboard.
- Requests that never reach vLLM (DNS, network policy, a dead Service) are invisible here. A synthetic probe
  from outside the cluster (blackbox exporter) closes that gap; it is on the roadmap.
- Lab Prometheus keeps 3 days, so "30 days" is the budget definition, not a window you can query.
