# ADR 0003: GPU sharing through time-slicing (lab only)

Status: accepted for the lab; production uses exclusive GPUs

## Context

The lab has one 24 GB GPU per node. Autoscaling to a second replica needs a second schedulable
`nvidia.com/gpu`, and a rolling update with `maxSurge: 1` needs one more on top of the current replicas.
A 1.5B model uses a fraction of the card.

## Options

| Mode | Memory isolation | Fault isolation | Latency predictability | Hardware |
|---|---|---|---|---|
| Exclusive (1 pod = 1 GPU) | yes | yes | best | any |
| Time-slicing | **no** | **no** | contention between processes | any |
| MPS | limits configurable | partial | better than time-slicing | Volta+ |
| MIG | yes (hardware partitions) | yes | good | A30, A100, H100, H200, B200 |
| vGPU | yes | yes | good | licensed, hypervisor |

L4 and A10G do not support MIG, which rules it out on the lab instance types.

## Decision

The device plugin config `time-sliced-3` advertises each physical GPU as three `nvidia.com/gpu`:
KEDA may run two serving replicas, and the third slot is surge capacity for rolling updates (N+1).
With two slots, a rollout while KEDA holds two replicas leaves the surge pod Pending forever
(`maxUnavailable: 0` never lets an old pod go first).
`failRequestsGreaterThanOne: true` rejects pods asking for two slices, which would only look like two GPUs.

Because there is no memory isolation, the replicas split memory themselves:
`--gpu-memory-utilization 0.30` each (about 7 GB of 24 GB), so all three slots fit with headroom.
A profile `exclusive` exists in the same ConfigMap; switch a node with
`kubectl label node <node> nvidia.com/device-plugin.config=exclusive --overwrite`.

## Consequences

- The scheduler believes there are three GPUs; nothing stops a third-party pod from taking the free slot
  and OOMing the card. Acceptable in a lab, not in a shared cluster.
- vLLM processes time-share the SMs, so inter-token latency jitters and an XID in one process can take
  down all of them.
- The surge slot holds no memory while idle, but its 30% is reserved by arithmetic: KV cache per replica is
  smaller than the card could give. That is the price of zero-downtime rollouts on one GPU.
- Measured on an A10G (docs/benchmarks.md, 2026-10-26): one exclusive replica at `0.90` delivered
  +36-63% tokens/s and 37-44% lower inter-token latency than two time-sliced replicas at `0.30`, at
  48-96 concurrent requests. Continuous batching inside one process beats processes taking turns on the
  same SMs. Time-slicing stays in the lab only for the rollout surge slot; scale-out means more GPUs.
- Production default: exclusive GPUs for latency-SLO serving; MIG on H100-class cards for many small models;
  time-slicing for dev and notebooks.
