#!/usr/bin/env python3
"""End-to-end check of the platform wiring on the no-GPU dev cluster (`make dev-up`). Stdlib only.

Proves on a running cluster what unit tests cannot:
  1. Prometheus scrapes the serving pods through the ServiceMonitor, as job="vllm"
  2. every rule group from the PrometheusRules is loaded
  3. Grafana provisioned both dashboards from their ConfigMaps
  4. KEDA scales the Deployment out under load, and the recording rules see the traffic
  5. an injected 5xx burst drives the availability burn-rate alert

  python3 tests/e2e/e2e.py      # kubectl must point at kind-gpu-lab-dev (E2E_CONTEXT overrides)
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NS = "llm"
EXPECTED_CONTEXT = os.environ.get("E2E_CONTEXT", "kind-gpu-lab-dev")
GRAFANA_AUTH = "admin:" + os.environ.get("GRAFANA_ADMIN_PASSWORD", "admin")
RULE_GROUPS = {"llm-serving.recording", "llm-serving.alerts", "llm-slo.recording", "llm-slo.alerts",
               "gpu.recording", "gpu.alerts"}
DASHBOARDS = {"LLM Serving - vLLM", "GPU Fleet - DCGM"}


def kubectl(*args: str, stdin: str | None = None, check: bool = True) -> str:
    result = subprocess.run(["kubectl", *args], input=stdin, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"kubectl {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def wait_until(probe: Callable[[], object], timeout: float, what: str, interval: float = 5):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if result := probe():
                return result
        except Exception as exc:  # keep polling; the last error is reported on timeout
            last_error = exc
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}"
                         + (f" (last error: {last_error})" if last_error else ""))


def http_json(url: str, auth: str | None = None):
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


@contextlib.contextmanager
def port_forward(namespace: str, target: str, remote_port: int) -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(["kubectl", "-n", namespace, "port-forward", target, f"{port}:{remote_port}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def listening() -> bool:
        with socket.socket() as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    try:
        wait_until(listening, 30, f"port-forward to {target}", interval=1)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def prom_query(base: str, query: str) -> list:
    return http_json(f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": query}))["data"]["result"]


def hpa_desired_replicas() -> int:
    out = kubectl("-n", NS, "get", "hpa", "keda-hpa-vllm", "-o", "jsonpath={.status.desiredReplicas}", check=False)
    return int(out or 0)


def start_load_job(stages: str, duration: int) -> None:
    script = kubectl("-n", NS, "create", "configmap", "llm-load-script",
                     f"--from-file={ROOT / 'loadtest' / 'llm_load.py'}", "--dry-run=client", "-o", "yaml")
    kubectl("apply", "-f", "-", stdin=script)
    kubectl("-n", NS, "delete", "job", "llm-load", "--ignore-not-found", "--wait=true")
    job = json.loads(kubectl("create", "-f", str(ROOT / "loadtest" / "job.yaml"), "--dry-run=client", "-o", "json"))
    overrides = {"STAGES": stages, "STAGE_DURATION": str(duration), "PROMPT_TOKENS": "256", "MAX_TOKENS": "64"}
    for env in job["spec"]["template"]["spec"]["containers"][0]["env"]:
        env["value"] = overrides.get(env["name"], env["value"])
    kubectl("apply", "-f", "-", stdin=json.dumps(job))


def set_faults(**faults: float) -> None:
    """POST /admin on every running fake vLLM pod."""
    code = ("import sys, urllib.request as u; "
            "u.urlopen(u.Request('http://127.0.0.1:8000/admin', data=sys.argv[1].encode(), method='POST'), timeout=5)")
    pods = kubectl("-n", NS, "get", "pods", "-l", "app.kubernetes.io/name=vllm",
                   "--field-selector=status.phase=Running", "-o", "name").split()
    for pod in pods:
        kubectl("-n", NS, "exec", pod, "--", "python", "-c", code, json.dumps(faults), check=False)
    print(f"  faults {faults} set on {len(pods)} pod(s)")


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


def main() -> int:
    context = kubectl("config", "current-context").strip()
    if context != EXPECTED_CONTEXT:
        print(f"refusing to run: kubectl context is {context!r}, expected {EXPECTED_CONTEXT!r}", file=sys.stderr)
        return 2

    step("serving Deployment is ready")
    kubectl("-n", NS, "rollout", "status", "deploy/vllm", "--timeout=300s")

    with port_forward("monitoring", "svc/kube-prometheus-stack-prometheus", 9090) as prom, \
            port_forward("monitoring", "svc/kube-prometheus-stack-grafana", 80) as grafana:
        step("Prometheus scrapes the serving pods as job=\"vllm\"")
        wait_until(lambda: prom_query(prom, f'up{{job="vllm", namespace="{NS}"}} == 1'), 180, 'up{job="vllm"} == 1')

        step("all rule groups are loaded")
        wait_until(lambda: {g["name"] for g in http_json(f"{prom}/api/v1/rules")["data"]["groups"]} >= RULE_GROUPS,
                   180, f"rule groups {sorted(RULE_GROUPS)}")

        step("Grafana provisioned the dashboards")
        wait_until(lambda: {d["title"] for d in http_json(f"{grafana}/api/search?type=dash-db", GRAFANA_AUTH)} >= DASHBOARDS,
                   180, f"dashboards {sorted(DASHBOARDS)}")

        step("load drives KEDA to scale out")
        start_load_job(stages="24", duration=360)
        wait_until(lambda: hpa_desired_replicas() >= 2, 300, "KEDA HPA desiredReplicas >= 2")
        print(f"  desired replicas: {hpa_desired_replicas()}")
        wait_until(lambda: prom_query(prom, f'llm:requests:rate5m{{namespace="{NS}"}} > 0'), 180,
                   "recording rule llm:requests:rate5m > 0")
        kubectl("-n", NS, "rollout", "status", "deploy/vllm", "--timeout=180s")

        step("a 5xx burst fires the availability burn-rate alert")
        set_faults(error_rate=0.5)
        try:
            alerts = wait_until(lambda: prom_query(prom, 'ALERTS{alertname="LLMAvailabilityBudgetFastBurn"}'),
                                300, "LLMAvailabilityBudgetFastBurn pending or firing")
            print("  alert state: " + ", ".join(sorted({a["metric"]["alertstate"] for a in alerts})))
        finally:
            set_faults(error_rate=0.0)
            kubectl("-n", NS, "delete", "job", "llm-load", "--ignore-not-found", "--wait=false")

    print("\nE2E PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
