#!/usr/bin/env python3
"""Render the Grafana dashboards into charts/llm-observability/dashboards/.

Dashboards are code: edit this file, run `make dashboards`, commit both. CI fails when the
committed JSON is stale. Standard library only.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "charts" / "llm-observability" / "dashboards"
DS = {"type": "prometheus", "uid": "${datasource}"}


class Dashboard:
    """Lays panels out left to right on Grafana's 24-column grid."""

    def __init__(self, uid: str, title: str, tags: list[str], description: str):
        self.uid, self.title, self.tags, self.description = uid, title, tags, description
        self.panels: list[dict] = []
        self.variables: list[dict] = [
            {"name": "datasource", "label": "Data source", "type": "datasource", "query": "prometheus", "hide": 0}
        ]
        self._x = self._y = self._row_h = 0
        self._id = 0

    def _place(self, w: int, h: int) -> dict:
        if self._x + w > 24:
            self._x, self._y = 0, self._y + self._row_h
            self._row_h = 0
        pos = {"x": self._x, "y": self._y, "w": w, "h": h}
        self._x += w
        self._row_h = max(self._row_h, h)
        return pos

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def row(self, title: str) -> None:
        if self._x:
            self._x, self._y = 0, self._y + self._row_h
        self.panels.append(
            {"type": "row", "id": self._next_id(), "title": title, "collapsed": False, "panels": [],
             "gridPos": {"x": 0, "y": self._y, "w": 24, "h": 1}}
        )
        self._y += 1
        self._row_h = 0

    def query_var(self, name: str, label: str, query: str, multi: bool = True) -> None:
        self.variables.append(
            {"name": name, "label": label, "type": "query", "datasource": DS, "query": query,
             "definition": query, "refresh": 2, "sort": 1, "multi": multi, "includeAll": multi,
             "allValue": ".*", "current": {"selected": True, "text": ["All"], "value": ["$__all"]} if multi else {}}
        )

    def textbox_var(self, name: str, label: str, default: str) -> None:
        self.variables.append(
            {"name": name, "label": label, "type": "textbox", "query": default,
             "current": {"text": default, "value": default}, "hide": 0}
        )

    def stat(self, title: str, expr: str, unit: str = "short", w: int = 4, h: int = 4, decimals: int | None = None,
             thresholds: list[tuple[float | None, str]] | None = None, description: str = "") -> None:
        steps = [{"value": v, "color": c} for v, c in (thresholds or [(None, "green")])]
        defaults: dict = {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}, "color": {"mode": "thresholds"}}
        if decimals is not None:
            defaults["decimals"] = decimals
        self.panels.append(
            {"type": "stat", "id": self._next_id(), "title": title, "description": description, "datasource": DS,
             "gridPos": self._place(w, h),
             "targets": [{"refId": "A", "datasource": DS, "expr": expr, "instant": True, "range": False}],
             "fieldConfig": {"defaults": defaults, "overrides": []},
             "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                         "colorMode": "value", "graphMode": "none", "textMode": "value", "justifyMode": "auto"}}
        )

    def timeseries(self, title: str, targets: list[tuple[str, str]], unit: str = "short", w: int = 12, h: int = 8,
                   stack: bool = False, min_: float | None = None, max_: float | None = None, description: str = "",
                   thresholds: list[tuple[float | None, str]] | None = None) -> None:
        defaults: dict = {
            "unit": unit,
            "custom": {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 25 if stack else 8, "showPoints": "never",
                       "spanNulls": False, "stacking": {"mode": "normal" if stack else "none", "group": "A"},
                       "thresholdsStyle": {"mode": "dashed" if thresholds else "off"}},
            "color": {"mode": "palette-classic"},
        }
        if thresholds:
            defaults["thresholds"] = {"mode": "absolute",
                                      "steps": [{"value": v, "color": c} for v, c in thresholds]}
        if min_ is not None:
            defaults["min"] = min_
        if max_ is not None:
            defaults["max"] = max_
        self.panels.append(
            {"type": "timeseries", "id": self._next_id(), "title": title, "description": description,
             "datasource": DS, "gridPos": self._place(w, h),
             "targets": [{"refId": chr(ord("A") + i), "datasource": DS, "expr": expr, "legendFormat": legend,
                          "range": True}
                         for i, (expr, legend) in enumerate(targets)],
             "fieldConfig": {"defaults": defaults, "overrides": []},
             "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max"]},
                         "tooltip": {"mode": "multi", "sort": "desc"}}}
        )

    def render(self) -> dict:
        return {
            "uid": self.uid, "title": self.title, "tags": self.tags, "description": self.description,
            "editable": True, "graphTooltip": 1, "timezone": "browser", "refresh": "30s",
            "time": {"from": "now-1h", "to": "now"}, "schemaVersion": 39, "version": 1,
            "templating": {"list": self.variables},
            "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                                      "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                                      "name": "Annotations & Alerts", "type": "dashboard"}]},
            "panels": self.panels,
        }


def quantiles(metric: str, sel: str, qs=(0.5, 0.95, 0.99)) -> list[tuple[str, str]]:
    return [(f"histogram_quantile({q}, sum by (le) (rate({metric}_bucket{{{sel}}}[$__rate_interval])))",
             f"p{int(q * 100)}") for q in qs]


def llm_serving() -> Dashboard:
    d = Dashboard("llm-serving", "LLM Serving - vLLM", ["llm", "vllm", "slo"],
                  "Golden signals, SLOs, scheduler state and unit economics for vLLM.")
    d.query_var("namespace", "Namespace", "label_values(vllm:num_requests_running, namespace)")
    d.query_var("model", "Model", 'label_values(vllm:num_requests_running{namespace=~"$namespace"}, model_name)')
    d.textbox_var("gpu_hour_usd", "GPU $/hour", "0.80")
    s = 'namespace=~"$namespace", model_name=~"$model"'
    h = 'job="vllm", namespace=~"$namespace", handler=~"/v1/.*"'

    d.row("Overview")
    d.stat("Requests / s", f"sum(rate(vllm:request_success_total{{{s}}}[$__rate_interval]))", "reqps", decimals=2)
    d.stat("TTFT p95", f"histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket{{{s}}}[$__rate_interval])))",
           "s", decimals=2, thresholds=[(None, "green"), (1, "orange"), (2.5, "red")],
           description="Time to first token. SLO: 99% under 2.5s.")
    d.stat("Inter-token latency p95", f"histogram_quantile(0.95, sum by (le) (rate(vllm:inter_token_latency_seconds_bucket{{{s}}}[$__rate_interval])))",
           "s", decimals=3, thresholds=[(None, "green"), (0.05, "orange"), (0.1, "red")],
           description="Gap between streamed tokens. 50 ms = 20 tokens/s per user.")
    # The status="5xx" series only exists after the first 5xx: "or vector(0)" turns "never failed" into 0.
    d.stat("5xx ratio", f'(sum(rate(http_requests_total{{{h}, status="5xx"}}[$__rate_interval])) or vector(0)) / sum(rate(http_requests_total{{{h}}}[$__rate_interval]))',
           "percentunit", decimals=2, thresholds=[(None, "green"), (0.001, "orange"), (0.005, "red")])
    d.stat("KV cache usage (max)", f"max(vllm:kv_cache_usage_perc{{{s}}})", "percentunit", decimals=0,
           thresholds=[(None, "green"), (0.8, "orange"), (0.95, "red")])
    d.stat("Serving pods", 'count(up{job="vllm", namespace=~"$namespace"} == 1)', "short",
           thresholds=[(None, "red"), (1, "green")], description="Pods answering /metrics.")

    d.row("SLO (over the selected time range)")
    d.stat("Availability", f'1 - ((sum(increase(http_requests_total{{{h}, status="5xx"}}[$__range])) or vector(0)) / sum(increase(http_requests_total{{{h}}}[$__range])))',
           "percentunit", w=6, decimals=3, thresholds=[(None, "red"), (0.995, "green")], description="SLO 99.5% non-5xx.")
    d.stat("Requests with TTFT <= 2.5s", f'sum(increase(vllm:time_to_first_token_seconds_bucket{{{s}, le="2.5"}}[$__range])) / sum(increase(vllm:time_to_first_token_seconds_count{{{s}}}[$__range]))',
           "percentunit", w=6, decimals=3, thresholds=[(None, "red"), (0.99, "green")], description="SLO 99%.")
    # Burn rates from raw series, not the recording rules, so they also show history recorded before the rules existed.
    d.stat("Availability burn rate (1h)", f'((sum(rate(http_requests_total{{{h}, status="5xx"}}[1h])) or vector(0)) / sum(rate(http_requests_total{{{h}}}[1h]))) / 0.005', "short", w=6,
           decimals=1, thresholds=[(None, "green"), (6, "orange"), (14.4, "red")],
           description="1 = spending the budget exactly over 30 days. 14.4 pages.")
    d.stat("Latency burn rate (1h)", f'(1 - (sum(rate(vllm:time_to_first_token_seconds_bucket{{{s}, le="2.5"}}[1h])) / sum(rate(vllm:time_to_first_token_seconds_count{{{s}}}[1h])))) / 0.01', "short", w=6, decimals=1,
           thresholds=[(None, "green"), (6, "orange"), (14.4, "red")])

    d.row("Latency")
    d.timeseries("Time to first token", quantiles("vllm:time_to_first_token_seconds", s), "s",
                 thresholds=[(None, "transparent"), (2.5, "red")],
                 description="Dominated by queueing + prefill. Grows with prompt length and load.")
    d.timeseries("Inter-token latency", quantiles("vllm:inter_token_latency_seconds", s), "s",
                 description="Decode speed per user. Grows with batch size (concurrency).")
    d.timeseries("End-to-end request latency", quantiles("vllm:e2e_request_latency_seconds", s), "s")
    d.timeseries("Where time goes (p95)", [
        (f"histogram_quantile(0.95, sum by (le) (rate(vllm:request_queue_time_seconds_bucket{{{s}}}[$__rate_interval])))", "queue"),
        (f"histogram_quantile(0.95, sum by (le) (rate(vllm:request_prefill_time_seconds_bucket{{{s}}}[$__rate_interval])))", "prefill"),
        (f"histogram_quantile(0.95, sum by (le) (rate(vllm:request_decode_time_seconds_bucket{{{s}}}[$__rate_interval])))", "decode"),
    ], "s", description="Queue time rising first means you need more replicas, not a faster GPU.")

    d.row("Throughput")
    d.timeseries("Tokens / s", [
        (f"sum(rate(vllm:prompt_tokens_total{{{s}}}[$__rate_interval]))", "prompt (prefill)"),
        (f"sum(rate(vllm:generation_tokens_total{{{s}}}[$__rate_interval]))", "generated (decode)"),
    ], "short")
    d.timeseries("Finished requests by reason", [
        (f"sum by (finished_reason) (rate(vllm:request_success_total{{{s}}}[$__rate_interval]))", "{{finished_reason}}"),
    ], "reqps", stack=True, description="stop = natural end, length = hit max_tokens, abort = client went away.")
    d.timeseries("HTTP responses by status", [
        (f"sum by (status) (rate(http_requests_total{{{h}}}[$__rate_interval]))", "{{status}}"),
    ], "reqps", stack=True, description="A stream that fails after headers are sent still counts as 2xx.")
    d.timeseries("Request size (p50 / p95 tokens)", [
        (f"histogram_quantile(0.5, sum by (le) (rate(vllm:request_prompt_tokens_bucket{{{s}}}[$__rate_interval])))", "prompt p50"),
        (f"histogram_quantile(0.95, sum by (le) (rate(vllm:request_prompt_tokens_bucket{{{s}}}[$__rate_interval])))", "prompt p95"),
        (f"histogram_quantile(0.5, sum by (le) (rate(vllm:request_generation_tokens_bucket{{{s}}}[$__rate_interval])))", "output p50"),
        (f"histogram_quantile(0.95, sum by (le) (rate(vllm:request_generation_tokens_bucket{{{s}}}[$__rate_interval])))", "output p95"),
    ], "short")

    d.row("Scheduler and KV cache")
    d.timeseries("Running vs waiting requests", [
        (f"sum by (pod) (vllm:num_requests_running{{{s}}})", "running {{pod}}"),
        (f"sum by (pod) (vllm:num_requests_waiting{{{s}}})", "waiting {{pod}}"),
    ], "short", description="Waiting > 0 for long = batch is full (max-num-seqs or KV cache).")
    d.timeseries("KV cache usage", [(f"max by (pod) (vllm:kv_cache_usage_perc{{{s}}})", "{{pod}}")],
                 "percentunit", min_=0, max_=1, thresholds=[(None, "transparent"), (0.95, "red")])
    d.timeseries("Preemptions / s", [(f"sum by (pod) (rate(vllm:num_preemptions_total{{{s}}}[$__rate_interval]))", "{{pod}}")],
                 "short", description="Sequences evicted from KV cache and recomputed later. Should be ~0.")
    d.timeseries("Prefix cache hit ratio", [
        (f"sum by (pod) (rate(vllm:prefix_cache_hits_total{{{s}}}[$__rate_interval])) / sum by (pod) (rate(vllm:prefix_cache_queries_total{{{s}}}[$__rate_interval]))", "{{pod}}"),
    ], "percentunit", min_=0, max_=1, description="Shared system prompts make prefill nearly free when this is high.")

    d.row("Autoscaling, GPU and unit economics")
    d.timeseries("Replicas (KEDA / HPA)", [
        ('max by (horizontalpodautoscaler) (kube_horizontalpodautoscaler_status_desired_replicas{namespace=~"$namespace"})', "desired"),
        ('max by (horizontalpodautoscaler) (kube_horizontalpodautoscaler_status_current_replicas{namespace=~"$namespace"})', "current"),
        ('max by (horizontalpodautoscaler) (kube_horizontalpodautoscaler_spec_max_replicas{namespace=~"$namespace"})', "max"),
    ], "short", min_=0)
    d.timeseries("In-flight requests per replica", [
        (f"(sum(vllm:num_requests_running{{{s}}}) + sum(vllm:num_requests_waiting{{{s}}})) / count(up{{job=\"vllm\", namespace=~\"$namespace\"}} == 1)", "in-flight / replica"),
    ], "short", description="The KEDA scaling signal. Compare with targetInFlightPerReplica.")
    d.timeseries("GPU utilisation of serving pods", [
        ('max by (exported_pod, gpu) (DCGM_FI_DEV_GPU_UTIL{exported_namespace=~"$namespace"})', "{{exported_pod}} gpu{{gpu}}"),
    ], "percent", min_=0, max_=100,
       description="Near 100% at any batch size: a poor autoscaling signal, a fine saturation hint.")
    d.stat("Generated tokens / s / GPU",
           f"sum(rate(vllm:generation_tokens_total{{{s}}}[$__rate_interval])) / count(count by (node, UUID) (DCGM_FI_DEV_GPU_UTIL))",
           "short", w=6, h=8, decimals=0)
    d.stat("Cost per 1M generated tokens",
           f"($gpu_hour_usd * count(count by (node, UUID) (DCGM_FI_DEV_GPU_UTIL))) / (sum(rate(vllm:generation_tokens_total{{{s}}}[$__rate_interval])) * 3600 / 1e6 > 0)",
           "currencyUSD", w=6, h=8, decimals=2,
           description="Physical GPUs x $/hour / generated tokens per hour, at the current load. Set the price in the GPU $/hour box.")
    return d


def gpu_fleet() -> Dashboard:
    d = Dashboard("gpu-fleet", "GPU Fleet - DCGM", ["gpu", "dcgm", "nvidia"],
                  "Utilisation, memory, thermals, health and allocation of every NVIDIA GPU in the cluster.")
    d.query_var("node", "Node", "label_values(DCGM_FI_DEV_GPU_UTIL, node)")
    d.textbox_var("gpu_hour_usd", "GPU $/hour", "0.80")
    n = 'node=~"$node"'
    leg = "{{node}} gpu{{gpu}}"

    d.row("Overview")
    d.stat("Physical GPUs", f"count(count by (node, UUID) (DCGM_FI_DEV_GPU_UTIL{{{n}}}))", w=3)
    d.stat("Allocatable", 'sum(kube_node_status_allocatable{resource="nvidia_com_gpu"})', w=3,
           description="Allocatable nvidia.com/gpu. Exceeds physical GPUs when time-slicing advertises replicas.")
    d.stat("Requested",
           'sum(kube_pod_container_resource_requests{resource="nvidia_com_gpu"} * on (namespace, pod) group_left () (kube_pod_status_phase{phase="Running"} == 1))',
           w=3)
    d.stat("Avg utilisation", f"avg(DCGM_FI_DEV_GPU_UTIL{{{n}}})", "percent", w=3, decimals=0)
    d.stat("Power draw", f"sum(max by (node, UUID) (DCGM_FI_DEV_POWER_USAGE{{{n}}}))", "watt", w=3, decimals=0)
    d.stat("Hottest GPU", f"max(DCGM_FI_DEV_GPU_TEMP{{{n}}})", "celsius", w=3,
           thresholds=[(None, "green"), (80, "orange"), (85, "red")])
    # DCGM exports the XID field only after the first XID, so "none yet" would read as "no data".
    d.stat("Last XID", f"max(DCGM_FI_DEV_XID_ERRORS{{{n}}}) or (max(DCGM_FI_DEV_GPU_UTIL{{{n}}}) * 0)", w=3,
           thresholds=[(None, "green"), (1, "red")], description="0 = no XID since the driver loaded.")
    d.stat("Fleet cost / hour", f"$gpu_hour_usd * count(count by (node, UUID) (DCGM_FI_DEV_GPU_UTIL{{{n}}}))",
           "currencyUSD", w=3, decimals=2)

    d.row("Utilisation")
    d.timeseries("GPU utilisation", [(f"max by (node, gpu) (DCGM_FI_DEV_GPU_UTIL{{{n}}})", leg)], "percent", min_=0, max_=100,
                 description="Share of time any kernel ran. Says nothing about how much of the chip was busy.")
    d.timeseries("SM active / tensor pipe active (DCP)", [
        (f"max by (node, gpu) (DCGM_FI_PROF_SM_ACTIVE{{{n}}})", "SM active " + leg),
        (f"max by (node, gpu) (DCGM_FI_PROF_PIPE_TENSOR_ACTIVE{{{n}}})", "tensor " + leg),
    ], "percentunit", min_=0, max_=1, description="Real chip occupancy. Datacenter GPUs only; empty on GeForce cards.")
    d.timeseries("Memory bandwidth (DRAM active, DCP)", [(f"max by (node, gpu) (DCGM_FI_PROF_DRAM_ACTIVE{{{n}}})", leg)],
                 "percentunit", min_=0, max_=1, description="LLM decode is memory-bandwidth bound: this saturates first.")
    d.timeseries("PCIe throughput (DCP)", [
        (f"max by (node, gpu) (DCGM_FI_PROF_PCIE_RX_BYTES{{{n}}})", "rx " + leg),
        (f"max by (node, gpu) (DCGM_FI_PROF_PCIE_TX_BYTES{{{n}}})", "tx " + leg),
    ], "Bps", description="Spikes during model loading; steady traffic hints at CPU offload or host-device copies.")

    d.row("Memory")
    d.timeseries("Framebuffer used", [
        (f"max by (node, gpu) (DCGM_FI_DEV_FB_USED{{{n}}}) / max by (node, gpu) (DCGM_FI_DEV_FB_USED{{{n}}} + DCGM_FI_DEV_FB_FREE{{{n}}} + DCGM_FI_DEV_FB_RESERVED{{{n}}})", leg),
    ], "percentunit", min_=0, max_=1, description="~90% with vLLM is expected: it pre-allocates the KV cache.")
    d.timeseries("Framebuffer used (MiB)", [(f"max by (node, gpu) (DCGM_FI_DEV_FB_USED{{{n}}})", leg)], "decmbytes", min_=0)

    d.row("Thermals, power and clocks")
    d.timeseries("Temperature", [
        (f"max by (node, gpu) (DCGM_FI_DEV_GPU_TEMP{{{n}}})", "core " + leg),
        (f"max by (node, gpu) (DCGM_FI_DEV_MEMORY_TEMP{{{n}}})", "memory " + leg),
    ], "celsius", thresholds=[(None, "transparent"), (85, "red")])
    d.timeseries("Power draw", [(f"max by (node, gpu) (DCGM_FI_DEV_POWER_USAGE{{{n}}})", leg)], "watt", min_=0)
    d.timeseries("SM clock (MHz)", [(f"max by (node, gpu) (DCGM_FI_DEV_SM_CLOCK{{{n}}})", leg)], "short",
                 description="A drop under load means power or thermal throttling.")
    d.timeseries("Time throttled", [
        (f"max by (node, gpu) (rate(DCGM_FI_DEV_POWER_VIOLATION{{{n}}}[$__rate_interval])) / 1e9", "power " + leg),
        (f"max by (node, gpu) (rate(DCGM_FI_DEV_THERMAL_VIOLATION{{{n}}}[$__rate_interval])) / 1e9", "thermal " + leg),
    ], "percentunit", min_=0, max_=1)

    d.row("Health")
    d.timeseries("Last XID error code", [(f"max by (node, gpu) (DCGM_FI_DEV_XID_ERRORS{{{n}}}) or max by (node, gpu) (DCGM_FI_DEV_GPU_UTIL{{{n}}}) * 0", leg)], "short", min_=0,
                 description="13/31/43: usually the application. 48/64/74/79/94/95/119/120: hardware or driver, drain the node.")
    d.timeseries("PCIe replays / s", [(f"max by (node, gpu) (rate(DCGM_FI_DEV_PCIE_REPLAY_COUNTER{{{n}}}[$__rate_interval]))", leg)], "short", min_=0)
    d.timeseries("Remapped memory rows", [
        (f"max by (node, gpu) (DCGM_FI_DEV_CORRECTABLE_REMAPPED_ROWS{{{n}}})", "correctable " + leg),
        (f"max by (node, gpu) (DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS{{{n}}})", "uncorrectable " + leg),
    ], "short", min_=0)
    d.timeseries("GPU Operator pods unavailable", [
        ('max by (daemonset) (kube_daemonset_status_number_unavailable{namespace="gpu-operator"})', "{{daemonset}}"),
    ], "short", min_=0)

    d.row("Who uses the GPUs")
    d.timeseries("Utilisation by workload", [
        (f'max by (exported_namespace, exported_pod) (DCGM_FI_DEV_GPU_UTIL{{{n}, exported_pod!=""}})', "{{exported_namespace}}/{{exported_pod}}"),
    ], "percent", w=24, min_=0, max_=100,
       description="dcgm-exporter attributes GPUs to pods; Prometheus prefixes those labels with exported_.")
    return d


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for build in (llm_serving, gpu_fleet):
        dash = build()
        path = OUT_DIR / f"{dash.uid}.json"
        # newline="\n": identical bytes on Windows and Linux, so CI's stale-dashboard check is stable.
        path.write_text(json.dumps(dash.render(), indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(OUT_DIR.parent.parent.parent).as_posix()} ({len(dash.panels)} panels)")


if __name__ == "__main__":
    main()
