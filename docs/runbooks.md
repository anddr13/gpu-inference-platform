# Runbooks

One section per alert; `runbook_url` in every alert links here. Commands assume the default namespaces
(`llm`, `gpu-operator`, `monitoring`) and a shell where `KUBECONFIG` points at the lab cluster's own file
(`~/.kube/gpu-lab.yaml` or `~/.kube/eks-gpu-inference.yaml`), not `~/.kube/config`.

Common first look:

```bash
make gpu-status                                   # nodes, allocatable GPUs, operator pods
kubectl -n llm get pods -o wide
kubectl -n llm logs deploy/vllm --tail=200
kubectl -n llm describe pod -l app.kubernetes.io/name=vllm | sed -n '/Events/,$p'
```

---

## LLMAvailabilityBudgetBurn

`LLMAvailabilityBudgetFastBurn` (page) and `LLMAvailabilityBudgetSlowBurn` (ticket).

**Meaning.** The share of `/v1/*` calls answered with 5xx is burning the 99.5% budget 14.4x (or 6x) faster
than sustainable.

**Diagnose.**
1. Dashboard *LLM Serving*, panel *HTTP responses by status*: one pod or all of them?
2. `kubectl -n llm logs <pod> | grep -iE 'error|exception|EngineDead'`. `EngineDeadError` means the engine
   core crashed; liveness will restart the pod.
3. GPU side: *GPU Fleet* dashboard, *Last XID*. An XID at the same time points to the GPU, not the code.
4. Recent change? `helm -n llm history vllm`.

**Mitigate.** Roll back a bad release (`helm -n llm rollback vllm`). Delete a single broken pod. If every
replica fails on the same GPU node, cordon it (see GPUXidCritical).

## LLMLatencyBudgetBurn

`LLMLatencyBudgetFastBurn` (page) and `LLMLatencyBudgetSlowBurn` (ticket).

**Meaning.** Too many requests wait more than 2.5 s for their first token.

**Diagnose.** Dashboard *Where time goes (p95)*:
- **Queue time high**: the batch is full. Check *Running vs waiting*, *KV cache usage*, and whether KEDA is
  at max replicas (VLLMAutoscalerAtMaxReplicas).
- **Prefill time high, queue low**: prompts got longer. *Request size* panel. Long prompts are expensive;
  check the prefix cache hit ratio.
- **Only one pod slow**: thermal throttling or a noisy neighbour on a time-sliced GPU.

**Mitigate.** Add capacity (`maxReplicas`, GPU nodes). Short term: lower `--max-num-seqs` to protect
TTFT at the cost of throughput, or cap `max_tokens` at the gateway.

## VLLMNoHealthyReplicas

**Meaning.** No vLLM target answers scrapes for 10 minutes. The API is down.

**Diagnose.** `kubectl -n llm get pods`:
- `Pending`: no GPU to schedule on. `kubectl describe pod` shows `Insufficient nvidia.com/gpu` → see
  GPUNodeNotAdvertisingGPUs, or the GPU node is gone (Spot reclaim, scaled to 0).
- `CrashLoopBackOff`: read the previous logs (`kubectl logs --previous`). Usual causes: CUDA driver too old
  for the image (`CUDA driver version is insufficient`), out of GPU memory at startup (another process holds
  memory on a time-sliced GPU), model name typo or gated model without `HF_TOKEN`.
- `Running` but not Ready: still loading. A cold start on a fresh node takes several minutes.

**Mitigate.** Fix the cause above; on EKS `make gpu-nodes N=1` if the node group was scaled down.

## VLLMTargetDown

**Meaning.** One replica is not serving `/metrics` for 15 minutes; others may be fine.

**Diagnose / mitigate.** Same checks as VLLMNoHealthyReplicas, for one pod. If it is stuck in startup,
look for a slow model download (`kubectl logs` shows download progress) or an image pull on a new node
(`kubectl describe pod`, events).

## VLLMKVCacheSaturated

**Meaning.** A replica's KV cache is over 95% full for 10 minutes. New requests wait; running ones may be
preempted.

**Diagnose.** Is it many requests (high *running*) or a few very long ones (*request size* p95)?

**Mitigate.** More replicas. Lower `--max-model-len` if clients never need the full context. On a dedicated
GPU raise `--gpu-memory-utilization`. A smaller or quantised model frees memory for cache.

## VLLMPreemptions

**Meaning.** The scheduler evicts running sequences to free KV cache and recomputes them later. Wasted GPU
time and latency spikes for the affected requests.

**Mitigate.** Same as VLLMKVCacheSaturated. Lowering `--max-num-seqs` trades peak batch size for fewer
preemptions.

## VLLMRequestsQueued

**Meaning.** More than 10 requests have waited for a batch slot for 10 minutes.

**Diagnose.** If KEDA is scaling but the new replica is Pending, the cluster is out of GPUs. If KEDA is not
scaling: `kubectl -n llm describe scaledobject vllm` and `kubectl -n llm get hpa`.

**Mitigate.** Capacity, or raise `targetInFlightPerReplica` only if the load test says the replica can take it.

## VLLMAutoscalerAtMaxReplicas

**Meaning.** The HPA created by KEDA has been at `maxReplicas` for 15 minutes. There is no headroom left.

**Mitigate.** Raise `autoscaling.maxReplicas` and make sure GPUs exist for the extra replicas
(`make gpu-nodes N=2` on EKS). If this fires daily, it is a capacity planning ticket, not an incident.

## GPUXidCritical

**Meaning.** The GPU reported an XID that indicates hardware or driver trouble: 48 (double-bit ECC),
64 (row remap failure), 74 (NVLink), 79 (fallen off the bus), 94/95 (ECC containment), 119/120 (GSP).

**Diagnose.**
```bash
kubectl debug node/<node> -it --image=ubuntu -- chroot /host dmesg -T | grep -i xid
kubectl debug node/<node> -it --image=ubuntu -- chroot /host nvidia-smi -q | grep -iA3 -E 'retired|remap|ecc'
```

**Mitigate.**
1. `kubectl cordon <node>` so nothing new lands there, then `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`.
2. Reset or reboot. 79 and 119/120 usually need a reboot; on a cloud VM, replace the instance.
3. Repeated on the same card: hardware ticket / RMA. In the cloud, terminate and let the node group replace it.

## GPUXidErrorObserved

**Meaning.** A new XID showed up. 13, 31 and 43 are typically caused by the application (illegal memory
access, MMU fault, killed context), not the hardware.

**Diagnose.** Which pod used the GPU at that time? *GPU Fleet → Who uses the GPUs*, or
`DCGM_FI_DEV_XID_ERRORS` with the `exported_pod` label. Check that pod's logs for a CUDA error.

**Mitigate.** Application XIDs: fix or roll back the workload. If the same XID hits different workloads on
one GPU, treat it as GPUXidCritical.

## GPURowRemapFailure

**Meaning.** The GPU has no spare memory rows left to remap failing ones. Data corruption risk.

**Mitigate.** Cordon and drain immediately, take the GPU out of service, RMA.

## GPUNodeNotAdvertisingGPUs

**Meaning.** GPU Feature Discovery labelled the node `nvidia.com/gpu.present=true`, but it advertises no
allocatable `nvidia.com/gpu`. GPU pods stay Pending.

**Diagnose.**
```bash
kubectl -n gpu-operator get pods -o wide --field-selector spec.nodeName=<node>
kubectl -n gpu-operator logs ds/nvidia-device-plugin-daemonset --all-containers --tail=100
kubectl get node <node> --show-labels | tr ',' '\n' | grep nvidia.com/gpu.deploy
kubectl get clusterpolicy cluster-policy -o jsonpath='{.status.state}'
```
Usual causes: device plugin crash (bad sharing config), validator stuck because the driver is not loaded
(after a kernel upgrade without a driver rebuild), or someone set `nvidia.com/gpu.deploy.device-plugin=false`.

**Mitigate.** Fix the config or label; restart the device plugin pod on that node. Kernel / driver mismatch:
reinstall the driver for the running kernel and reboot (k3s), or replace the node (EKS).

## GPUTelemetryMissing

**Meaning.** A GPU node exports no DCGM metrics. Every GPU alert is blind there.

**Diagnose.** `kubectl -n gpu-operator get pods -l app=nvidia-dcgm-exporter -o wide`, then its logs. Check
the ServiceMonitor target in Prometheus (*Status → Targets*). An invalid field in the `dcgm-metrics`
ConfigMap makes the exporter fail at startup.

## GPUOperatorComponentUnavailable

**Meaning.** A GPU Operator DaemonSet has unavailable pods for 15 minutes.

**Diagnose.** `kubectl -n gpu-operator get ds,pods -o wide`; the failing operand's logs; and the operator's
own logs (`kubectl -n gpu-operator logs deploy/gpu-operator`). The validator pods show which layer is broken
(driver, toolkit, cuda, plugin).

## GPUHighTemperature

Covers `GPUHighTemperature` and `GPUThermalThrottling`.

**Meaning.** Above 85 C for 10 minutes, or clocks reduced for heat more than 20% of the time. Tokens/s per
GPU drops even though utilisation looks normal.

**Mitigate.** Cloud: rare; replace the instance if one card is consistently hotter. On-prem / home lab:
airflow, fan curve, power limit (`nvidia-smi -pl`).

## GPUAllocatedButIdle

**Meaning.** A pod has held a GPU for two hours at under 5% average utilisation. Money, not an incident.

**Mitigate.** Scale the workload down, move it to a time-sliced node, or stop it. On EKS,
`make gpu-nodes N=0` between lab sessions.
