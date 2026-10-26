# ADR 0001: Manage the GPU software stack with the NVIDIA GPU Operator

Status: accepted

## Context

A GPU node needs five things before a pod can use the card: a kernel driver, a container runtime
hook, a device plugin that advertises `nvidia.com/gpu` to the kubelet, telemetry, and node labels that
say which GPU it is. Installed by hand, every node becomes a snowflake, and driver / CUDA / toolkit
versions drift apart. Driver upgrades on a live fleet need drain-and-reboot orchestration.

## Decision

Use the GPU Operator (chart v26.7.2) with one `ClusterPolicy` describing the whole stack.

In this lab the driver and container toolkit are **pre-installed** and the operator runs with
`driver.enabled=false`, `toolkit.enabled=false`:

- EKS: the `AL2023_x86_64_NVIDIA` AMI ships both, tested together by AWS.
- k3s: `infra/k3s/bootstrap.sh` installs both from NVIDIA's repos and makes `nvidia` the default runtime.

The operator owns everything above the driver: device plugin, GPU Feature Discovery, DCGM exporter,
MIG manager, the validator, and the GPU sharing config (ADR 0003).

## Alternatives

| Option | Why not here |
|---|---|
| Operator-managed driver (driver container) | The right choice when you own the node OS (bare metal, kubeadm on Ubuntu): the upgrade controller rolls driver versions node by node with cordon/drain. On EKS it fights the AMI; on k3s the toolkit needs k3s-specific containerd paths. A good next exercise on a bare Ubuntu box. |
| Standalone `nvidia-device-plugin` + `dcgm-exporter` charts | Works, but two charts to keep compatible, no validation, no node-level operand control. |
| Dynamic Resource Allocation (DRA) | Kubernetes DRA is GA since 1.34 and the operator has an experimental `GPUCluster` CR with the NVIDIA DRA driver. Too early for a stable lab; it is where GPU scheduling is heading. |

## Consequences

- One CR to read when debugging, and per-node switches through labels
  (`nvidia.com/gpu.deploy.<operand>=false`, `nvidia.com/device-plugin.config=<profile>`).
- CDI is off in this mode, so devices are injected through `NVIDIA_VISIBLE_DEVICES`. Because `nvidia` is the
  default runtime, any image that sets `NVIDIA_VISIBLE_DEVICES=all` in its own env (CUDA base images do) can
  see every GPU on the node without requesting one. In production prefer CDI with the operator-managed
  toolkit, or the device plugin's volume-mounts strategy.
- Driver upgrades are an AMI rollout (EKS) or an apt upgrade + reboot (k3s), not an operator action.
