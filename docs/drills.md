# Failure drills

Break things on purpose, watch the signals, write down what happened. Each drill lists what to trigger,
what you should see, and what to record. Keep the results in this file; they are the best interview
material this repo produces.

Drills delete pods and drain nodes. Run the raw `kubectl` commands below only in a shell where
`KUBECONFIG` points at the lab cluster's own file (`export KUBECONFIG=~/.kube/gpu-lab.yaml` or
`~/.kube/eks-gpu-inference.yaml`), never with `~/.kube/config` and its work contexts.

Run a steady background load during drills, in a second terminal:

```bash
make loadtest   # or edit loadtest/job.yaml: STAGES="16", STAGE_DURATION="900"
```

Drills 2 and 3 run the load generator from your workstation through `make api` (port-forward).
That is fine for watching signals, not for benchmark numbers (see docs/benchmarks.md).

**Rehearse for free first.** On the kind dev cluster (`make dev-up`) the fake vLLM behaves like a
saturating server, so drills 1, 2, 3 and 7 and the two below work without a GPU. Only the GPU drills
(4-6, 8) need real hardware.

| Drill on the dev cluster | Trigger | Expect |
|---|---|---|
| Availability SLO burn | `make fault ERROR_RATE=0.3` under load | `LLMAvailabilityBudgetFastBurn` pending after ~1 min, firing 2 min later; 5xx on the HTTP panel |
| Latency SLO burn | `make fault ERROR_RATE=0 TTFT=3` under load | TTFT p95 above 2.5 s, `LLMLatencyBudgetFastBurn` |
| Recovery | `make fault ERROR_RATE=0 TTFT=0.15` | the short 5m window clears first, so the page resolves within minutes |

## 1. Kill a replica under load

```bash
POD=$(kubectl -n llm get pods -l app.kubernetes.io/name=vllm -o name | head -1)
kubectl -n llm delete "$POD" --wait=false
```

Expect: in-flight streams on that pod abort (`finished_reason="abort"`, maybe 5xx), the other replica's
TTFT jumps, the replacement pod goes through startup again.

Record: requests lost, TTFT peak, time until the new pod is Ready. Repeat with `modelCache.type=pvc` and
`emptyDir` and compare the cold start.

## 2. Exhaust the KV cache

```bash
python3 loadtest/llm_load.py --base-url http://localhost:8000 --stages 32,64 --stage-duration 120 \
  --prompt-tokens 3000 --max-tokens 1024
```

Expect: *KV cache usage* near 100%, *Preemptions / s* above zero, queue time dominating *Where time goes*,
`VLLMKVCacheSaturated` after 10 minutes, KEDA going to max replicas.

Record: the concurrency where preemptions start. Then lower `--max-num-seqs` and run again.

## 3. Prefix caching on a shared system prompt

```bash
python3 loadtest/llm_load.py --stages 16 --stage-duration 120 --shared-prefix-tokens 0
python3 loadtest/llm_load.py --stages 16 --stage-duration 120 --shared-prefix-tokens 2000
```

Expect: with the shared prefix, *Prefix cache hit ratio* goes up and TTFT p95 drops even though prompts
are longer.

## 4. Drain the GPU node

```bash
kubectl drain <gpu-node> --ignore-daemonsets --delete-emptydir-data
```

Expect, single-node k3s: every replica evicted (the PDB allows one at a time, then waits), a full outage
until `kubectl uncordon`. `VLLMNoHealthyReplicas` fires after 10 minutes. EKS with two GPU nodes: pods move,
the new node pulls a 10 GB image first.

Record: eviction order, time to recover, what the PDB actually protected.

## 5. Take away the device plugin

```bash
kubectl label node <gpu-node> nvidia.com/gpu.deploy.device-plugin=false --overwrite
# undo: kubectl label node <gpu-node> nvidia.com/gpu.deploy.device-plugin-
```

Expect: the operator removes the device plugin from that node, allocatable `nvidia.com/gpu` drops,
running pods keep running, new GPU pods stay Pending, `GPUNodeNotAdvertisingGPUs` fires after 10 minutes.

Variant: `nvidia.com/gpu.deploy.dcgm-exporter=false` → `GPUTelemetryMissing`.

## 6. Change GPU sharing live

```bash
kubectl label node <gpu-node> nvidia.com/device-plugin.config=exclusive --overwrite
```

Expect: allocatable goes from 3 to 1. The running replicas survive; once one restarts, only one can be
scheduled and KEDA's desired 2 stays Pending. A rollout now also hangs: the surge pod has no slot.
Switch back to `time-sliced-3`.

## 7. Bad rollout

```bash
helm -n llm upgrade vllm charts/vllm --reuse-values --set model.name=does-not/exist
```

Expect: the new pod crash-loops on startup; with `maxUnavailable: 0` the old replica keeps serving.
Recover with `helm -n llm rollback vllm`. Record: did users notice anything?

## 8. Spot interruption (EKS)

```bash
aws ec2 terminate-instances --instance-ids <gpu-node-instance-id>
```

Expect: node NotReady, pods Pending, the managed node group launches a replacement, then a full cold start
(node boot + image pull + model download + load).

Record: total time to recover. That number is the argument for pre-pulled images, a model cache that
outlives the node, and keeping one on-demand GPU under the Spot ones.
