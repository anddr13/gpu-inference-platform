# ADR 0002: vLLM as the serving engine

Status: accepted

## Context

The platform serves open-weight chat models behind an OpenAI-compatible API on one or a few 24 GB GPUs.
What matters operationally: throughput per GPU, latency under concurrency, how much work a model change
is, and how observable the engine is.

## Decision

vLLM (v0.32.0, `vllm/vllm-openai` image).

- Continuous batching and PagedAttention: high throughput per GPU without hand-tuned batch sizes.
- Automatic prefix caching on by default: shared system prompts make prefill nearly free.
- OpenAI-compatible server in the box, so clients and the load test need no adapter.
- A model change is a Helm value. No engine build step.
- First-class Prometheus metrics: TTFT, inter-token latency, queue/prefill/decode time, KV cache usage,
  preemptions, prefix cache hits. The dashboards and SLOs are built on these.

## Alternatives

| Option | Trade-off |
|---|---|
| Triton Inference Server + TensorRT-LLM | Highest raw performance on NVIDIA, in-flight batching, multi-model and ensembles, non-LLM models in the same server. The cost: a TensorRT engine is compiled per model, GPU type, precision and max shape, so every change is a build pipeline. Planned as phase 2 for a measured comparison on the same GPU. |
| SGLang | Strong on prefix-heavy and structured-output workloads (RadixAttention). Similar operational shape to vLLM; worth a benchmark later. |
| Hugging Face Transformers + FastAPI | No continuous batching: throughput collapses under concurrency. Fine for a demo, not for serving. |

## Consequences

- The vLLM image is about 10 GB and built for CUDA 13 (driver >= 580). Cold start = image pull + weight
  download + load + CUDA graph capture, measured in minutes. This drives: no scale-to-zero, a model cache
  volume on k3s, and conservative scale-down.
- vLLM pre-allocates `--gpu-memory-utilization` of the card at startup, so framebuffer usage says nothing
  about load; KV cache usage does.
- Fast release cadence (roughly every two weeks). The image tag is pinned and upgraded deliberately, with the
  load test as the regression check.
