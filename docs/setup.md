# Setup

Three ways to run the platform. Do most of the work on the free one, rent a GPU for the measurements.

| Path | GPU | What you get | Cost |
|---|---|---|---|
| 0. kind on a laptop | none (fake vLLM) | everything except GPU Operator and real inference: scraping, rules, alerts, KEDA, dashboards, e2e test | free |
| A. k3s on one GPU VM | real | the full stack, single node | ~$0.20-0.50 per GPU hour |
| B. AWS EKS, Spot GPU node group | real | the full stack on managed Kubernetes, Terraform | ~$0.50 per hour (Spot, eu-north-1) |

## Tools on your workstation

| Tool | Version | Why |
|---|---|---|
| kubectl | 1.34+ | |
| helm | 3.22+ | |
| helm-diff plugin | latest | `helmfile apply` needs it: `helm plugin install https://github.com/databus23/helm-diff` |
| helmfile | 1.8+ | installs all releases in order |
| python | 3.12+ | load test, dashboard generator, e2e test (`pip install httpx==0.28.1`) |
| Docker + kind | kind 0.33+ | path 0 |
| terraform, aws cli | 1.10+, v2 | path B |

On Windows, run the Makefile from WSL.

## Path 0: no GPU, kind on your laptop

Needs Docker with about 6 GB of memory.

```bash
make dev-up                          # ~5 minutes on first run
export KUBECONFIG=~/.kube/kind-gpu-lab-dev.yaml
make grafana                         # admin / admin
make e2e                             # end-to-end assertions
make fault ERROR_RATE=0.3            # then watch LLMAvailabilityBudgetFastBurn go pending → firing
make fault ERROR_RATE=0 TTFT=3       # latency SLO drill
make dev-down
```

## Path A: one GPU machine with k3s

Any Ubuntu 22.04 / 24.04 machine with an NVIDIA GPU and root access: a cloud GPU VM or a dedicated server.
Container-only GPU marketplaces (pods without systemd or kernel access) do not work: k3s and the
GPU Operator need a real OS.

| GPU memory | Change in `serving/vllm/values.yaml` |
|---|---|
| 24 GB (L4, A10G, RTX 4090, RTX PRO 4000) | none |
| 16 GB (T4, A4000) | `model.name: Qwen/Qwen2.5-0.5B-Instruct`, `servedName: qwen2.5-0.5b`, `revision: ""` |
| 8-12 GB | one replica: label the node `nvidia.com/device-plugin.config=exclusive`, `gpuMemoryUtilization: 0.85`, `autoscaling.enabled: false`, `strategy.rollingUpdate: {maxSurge: 0, maxUnavailable: 1}` |

Host RAM: 32 GB or more (up to three vLLM pods during a rollout, plus Prometheus).

On the GPU machine:

```bash
git clone https://github.com/anddr13/gpu-inference-platform.git && cd gpu-inference-platform
sudo bash infra/k3s/bootstrap.sh     # installs the driver, asks for a reboot
sudo reboot
sudo bash infra/k3s/bootstrap.sh     # toolkit + k3s
```

Copy the kubeconfig to your workstation as the script prints, then:

```bash
export KUBECONFIG=~/.kube/gpu-lab.yaml
export GRAFANA_ADMIN_PASSWORD='pick-one'
make platform ENV=k3s
```

## Path B: AWS EKS with Spot GPUs

What the GPU node runs (EKS AMI release v20261019, Kubernetes 1.36, `AL2023_x86_64_NVIDIA`): NVIDIA
driver 580.178.04, container toolkit 1.20.1, containerd 2.3.5. nodeadm makes `nvidia` the default
containerd runtime on these nodes, which is what `platform/gpu-operator/values.yaml` assumes, and
driver 580 runs the CUDA 13 vLLM image.

### Once, before the first session

1. **A personal AWS account**, never an employer's, with MFA on the root user and an IAM user with
   AdministratorAccess for daily work. The Makefile always uses the CLI profile `gpu-lab`; sign in with
   short-lived credentials, no access keys on disk:

   ```bash
   aws login --remote --profile gpu-lab --region eu-north-1   # prints a URL, paste the code back
   ```

   Access keys (`aws configure --profile gpu-lab`) or SSO (`aws configure sso`) work too.
2. **GPU quota.** Service Quotas → Amazon EC2 → region eu-north-1 → *All G and VT Spot Instance Requests*
   (`L-3819A6DF`) → request 16 vCPUs. The default is 0, and approval can take from hours to a couple of days.
   From the CLI:

   ```bash
   aws service-quotas request-service-quota-increase --profile gpu-lab --region eu-north-1 \
     --service-code ec2 --quota-code L-3819A6DF --desired-value 16
   ```

   **AWS Free plan accounts** (the plan with $100-200 of credits and "projects" in the console) only
   launch free-tier instance types and block Spot, so neither the GPU node nor the t3.large system nodes
   start. Upgrade to the paid plan first (Billing → Free plan → Upgrade plan); remaining credits are
   applied before your card. If Spot stays denied after the upgrade, set `gpu_capacity_type = "ON_DEMAND"`
   and request *Running On-Demand G and VT instances* (`L-DB2E81BA`) instead. The preflight checks the
   plan, the Spot SCP and the right quota. On-demand, the cluster costs ~$1.40/h in eu-north-1.
3. **Tools in WSL**: aws cli v2, terraform, kubectl, helm + helm-diff, helmfile.
4. **tfvars**: `cp infra/aws-eks/terraform.tfvars.example infra/aws-eks/terraform.tfvars`, then set your IP
   in `api_allowed_cidrs` and your address in `budget_alert_email`.
5. `make eks-preflight` until it passes. It checks the account, GPU types per zone, the quota, the current
   Spot price, the EKS version, the tfvars placeholders and leftovers from earlier sessions. It only reads.

### One session

| Step | Command | Time |
|---|---|---|
| Cluster | `make eks-up` (runs the preflight first) | ~15-20 min |
| Point kubectl at it | `export KUBECONFIG=~/.kube/eks-gpu-inference.yaml` | |
| Platform + model | `GRAFANA_ADMIN_PASSWORD=... make platform ENV=eks` | ~10 min (first vLLM image pull ~5 min) |
| Work | verify layers, `make loadtest`, drills, benchmarks | as long as you need |
| Teardown | `make eks-down` (destroy + leftovers check) | ~10-15 min |

Each session carries about $1 of overhead: cluster time while it is created and destroyed, plus ~13 GB of
image and model through the NAT gateway. Short pause on the same day: `make gpu-nodes N=0`. Longer
breaks: `make eks-down`, because the cluster without a GPU node still costs about $8 a day.

The lab kubeconfig is a separate file, so `kubectl` in other terminals stays on your usual contexts.

## Verify, layer by layer

```bash
make gpu-status   # node has nvidia.com/gpu.product, allocatable nvidia.com/gpu = 3 (time-sliced)
make gpu-test     # CUDA vector add: "Test PASSED"
kubectl -n llm rollout status deploy/vllm --timeout=20m
make smoke        # one chat completion
make grafana      # http://localhost:3000, folder "LLM Platform"
make loadtest     # staged load, prints the table and the highest concurrency that meets the SLO
```

If a layer fails, the runbooks cover the usual causes: GPUNodeNotAdvertisingGPUs for the device plugin,
VLLMNoHealthyReplicas for the server.

## What it costs

Prices as of October 2026, from public price trackers. Spot prices move daily, so check the console
before you start.

### EKS, eu-north-1, per hour while running

| Item | Spot GPU | On-demand GPU |
|---|---|---|
| EKS control plane | $0.10 | $0.10 |
| 2 x t3.large (system node group) | ~$0.17 | ~$0.17 |
| NAT gateway + public IPv4 | ~$0.05 | ~$0.05 |
| EBS (200 GB GPU root + system disks) | ~$0.03 | ~$0.03 |
| 1 x g6.2xlarge (L4 24 GB) | ~$0.16-0.30 | ~$1.04 |
| **Total** | **~$0.50-0.65** | **~$1.40** |

One-off per fresh GPU node: ~13 GB through the NAT gateway (vLLM image + model), about $0.60.
eu-north-1 is the default because a g6.2xlarge Spot instance cost ~$0.16/h there against ~$0.65/h in
eu-central-1 at the time of writing.

### Single GPU VM with k3s (path A), per GPU hour

| Option | GPU | Price |
|---|---|---|
| AWS EC2 g6.2xlarge Spot, eu-north-1 | L4 24 GB | ~$0.16-0.30 + disk |
| GCP g2-standard-8 Spot | L4 24 GB | ~$0.39-0.51 (on-demand ~$0.85) |
| Hetzner GEX45 dedicated | RTX PRO 4000 Blackwell 24 GB | ~€214 / month + setup fee; only pays off near full-time use |

### Budget for the whole project

Build and debug on path 0 (free), then rent a GPU only for the real measurements: about 30-50 GPU hours
for the benchmarks and drills.

| Plan | Estimate |
|---|---|
| Path A on a Spot VM, 40 h | ~$10-20 |
| Path B (EKS, Spot), 40 h | ~$20-30, plus ~$5 of NAT traffic |
| Path B with on-demand GPU, 40 h | ~$60 |

Set `budget_alert_email` in `terraform.tfvars`: Terraform then creates an AWS Budget ($30 by default)
that emails you at 80% of actual spend and when the forecast crosses 100%.
