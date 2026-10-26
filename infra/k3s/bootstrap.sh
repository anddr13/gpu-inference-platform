#!/usr/bin/env bash
# Single-node GPU lab on Ubuntu 22.04 / 24.04:
#   1. NVIDIA driver on the host (reboot once after install, then run again)
#   2. NVIDIA Container Toolkit on the host
#   3. k3s with "nvidia" as the default containerd runtime
# The GPU Operator then manages everything above the driver (platform/gpu-operator/values.yaml).
#
#   sudo bash infra/k3s/bootstrap.sh    # safe to re-run
set -euo pipefail

K3S_VERSION="${K3S_VERSION:-v1.36.5+k3s1}"
# The default vLLM image (CUDA 13) needs driver >= 580.
MIN_DRIVER_MAJOR=580

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "run with sudo"
# shellcheck source=/dev/null
. /etc/os-release
[[ "${ID}" == "ubuntu" ]] || die "tested on Ubuntu 22.04 / 24.04 only (found ${ID})"
command -v lspci >/dev/null || apt-get install -y pciutils
lspci | grep -qi 'nvidia' || die "no NVIDIA device on the PCI bus"

# --- 1. Driver ---------------------------------------------------------------
if ! nvidia-smi >/dev/null 2>&1; then
  log "Installing the recommended NVIDIA server driver"
  apt-get update
  apt-get install -y ubuntu-drivers-common
  ubuntu-drivers install --gpgpu
  log "Driver installed. Reboot, then run this script again."
  exit 0
fi

DRIVER_VERSION=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1)
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)
log "Found ${GPU_NAME}, driver ${DRIVER_VERSION}"
if (( ${DRIVER_VERSION%%.*} < MIN_DRIVER_MAJOR )); then
  echo "WARNING: driver < ${MIN_DRIVER_MAJOR}. Either upgrade it, or set image.tag: v0.32.0-cu129"
  echo "         in serving/vllm/values-k3s.yaml (CUDA 12.9 build)."
fi

# --- 2. Container toolkit ----------------------------------------------------
if ! command -v nvidia-ctk >/dev/null; then
  log "Installing NVIDIA Container Toolkit"
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update
  apt-get install -y nvidia-container-toolkit
fi
log "$(nvidia-ctk --version | head -n1)"

# --- 3. k3s ------------------------------------------------------------------
mkdir -p /etc/rancher/k3s
cat > /etc/rancher/k3s/config.yaml <<'EOF'
write-kubeconfig-mode: "0644"
# k3s finds nvidia-container-runtime at startup and registers it with containerd.
# As the default runtime, GPU Operator operands and GPU pods need no runtimeClassName.
default-runtime: nvidia
disable:
  - traefik
EOF

if ! systemctl is-active --quiet k3s; then
  log "Installing k3s ${K3S_VERSION}"
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="${K3S_VERSION}" sh -
else
  # k3s only looks for runtimes when it starts.
  log "Restarting k3s to pick up the NVIDIA runtime"
  systemctl restart k3s
fi

log "Waiting for the node to become Ready"
until k3s kubectl get nodes 2>/dev/null | grep -q ' Ready'; do sleep 5; done

grep -q nvidia /var/lib/rancher/k3s/agent/etc/containerd/config.toml \
  || die "k3s did not register the nvidia runtime; check 'journalctl -u k3s'"

log "Done. On your workstation:"
cat <<EOF
  scp ${SUDO_USER:-ubuntu}@<this-host>:/etc/rancher/k3s/k3s.yaml ~/.kube/gpu-lab.yaml
  sed -i 's/127.0.0.1/<this-host-ip>/' ~/.kube/gpu-lab.yaml
  export KUBECONFIG=~/.kube/gpu-lab.yaml
  make platform ENV=k3s
EOF
