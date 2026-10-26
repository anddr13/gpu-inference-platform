# ADR 0004: Autoscale vLLM on in-flight requests per replica (KEDA)

Status: accepted

## Context

An LLM replica is saturated when its batch is full: new requests wait, time to first token climbs, and
the KV cache starts preempting running sequences. The autoscaler needs a signal that rises with load
before users notice.

## Signals considered

| Signal | Verdict |
|---|---|
| CPU / memory | Unrelated to GPU work. Useless. |
| GPU utilisation (`DCGM_FI_DEV_GPU_UTIL`) | Share of time *any* kernel runs. Near 100% with one request or with fifty. Says "busy", not "full". |
| KV cache usage | Good saturation indicator, but per pod, jumpy with long prompts, and only meaningful close to 1.0. Used for alerting. |
| Waiting requests | Zero until the batch is full, then explodes. Too late as the only input. |
| **Running + waiting (in-flight)** | Proportional to offered load, works like Knative/KServe concurrency targets. **Chosen.** |

## Decision

A KEDA `ScaledObject` with a Prometheus trigger:

```promql
sum(vllm:num_requests_running{namespace="llm",service="vllm"})
  + sum(vllm:num_requests_waiting{namespace="llm",service="vllm"})
```

`metricType: AverageValue`, so desired replicas = ceil(in-flight / `targetInFlightPerReplica`).
The target is not a guess: it is the highest concurrency per replica at which the load test still meets
the TTFT SLO (p95 <= 2.5s), with some headroom taken off.

Behaviour: scale up one pod per minute with no stabilisation window; scale down one pod per five minutes
after ten minutes of stability. `minReplicaCount: 1`. If Prometheus is unreachable, the fallback holds the
minimum.

KEDA over prometheus-adapter: one CR instead of adapter rule config, fallback support, and a path to
scale-to-zero later. It still drives a standard HPA.

## Consequences

- A new replica needs minutes to become Ready (image pull, weights, CUDA graphs). Autoscaling absorbs
  trends, not spikes. The headroom between the target and the saturation point is the price of that.
- No scale-to-zero: the first request after idle would wait for a full cold start.
- With time-slicing the second replica shares the GPU, so scaling out adds batch slots but not compute.
  With exclusive GPUs the second replica needs a second GPU node, and nothing provisions one automatically
  yet: the pod stays Pending until `make gpu-nodes N=2`. Karpenter closes that gap (roadmap).
- Next step: route by load, not round-robin. The Gateway API Inference Extension picks a replica by queue
  depth and KV cache state, which matters as much as replica count once there are several replicas.
