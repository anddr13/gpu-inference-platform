SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

ENV       ?= k3s
CLUSTER   ?= gpu-inference
REGION    ?= eu-north-1
NS        ?= llm
MODEL     ?= qwen2.5-1.5b
N         ?= 1
KIND_CLUSTER ?= gpu-lab-dev
# The dev cluster gets its own kubeconfig, so dev targets never touch your work contexts.
DEV_KUBECONFIG ?= $(HOME)/.kube/kind-$(KIND_CLUSTER).yaml
DEV_ENV := KUBECONFIG=$(DEV_KUBECONFIG) GRAFANA_ADMIN_PASSWORD=$${GRAFANA_ADMIN_PASSWORD:-admin}
EKS_KUBECONFIG ?= $(HOME)/.kube/eks-$(CLUSTER).yaml
K3S_KUBECONFIG ?= $(HOME)/.kube/gpu-lab.yaml
# Every target reaches the lab cluster of $(ENV) through that cluster's own kubeconfig file,
# never ~/.kube/config and its work contexts. One-off override: make <target> KUBECONFIG=<file>
LAB_KUBECONFIG_k3s = $(K3S_KUBECONFIG)
LAB_KUBECONFIG_eks = $(EKS_KUBECONFIG)
LAB_KUBECONFIG_dev = $(DEV_KUBECONFIG)
KUBECONFIG = $(LAB_KUBECONFIG_$(ENV))
export KUBECONFIG
ERROR_RATE   ?= 0.0
TTFT         ?= 0.15
# AWS targets always use this CLI profile, never the default one (which may hold work credentials).
# Sign in once per day: aws login --remote --profile gpu-lab --region eu-north-1
AWS_PROFILE ?= gpu-lab
export AWS_PROFILE
# Terraform uses a credential_process profile that asks the CLI for fresh credentials whenever the
# old ones expire. `aws login` credentials live 15 minutes and an EKS apply takes longer, so exporting
# them once would fail mid-apply. Works for any login method (aws login, SSO, access keys).
TF_PROFILE := $(AWS_PROFILE)-terraform
TF := AWS_PROFILE=$(TF_PROFILE) terraform -chdir=infra/aws-eks

PROM_IMAGE := prom/prometheus:v3.15.1
PROMTOOL   ?= $(shell command -v promtool 2>/dev/null || echo "docker run --rm -v $(CURDIR):/repo -w /repo --entrypoint promtool $(PROM_IMAGE)")
KUBECONFORM_SCHEMAS := -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

##@ Platform

# An empty or missing KUBECONFIG would make kubectl fall back to ~/.kube/config, so refuse instead.
.PHONY: require-kubeconfig
require-kubeconfig:
	@if [ -z "$(KUBECONFIG)" ] || [ ! -f "$(KUBECONFIG)" ]; then \
	  echo "No kubeconfig for ENV=$(ENV): '$(KUBECONFIG)'. make eks-up / make dev-up create one; k3s: docs/setup.md." >&2; \
	  exit 1; \
	fi

.PHONY: confirm-context
confirm-context: require-kubeconfig
	@ctx=$$(kubectl config current-context); echo "kubeconfig: $(KUBECONFIG)"; echo "kubectl context: $$ctx"; \
	if [ "$(YES)" != "1" ]; then read -r -p "Deploy ENV=$(ENV) to this context? [y/N] " ok; [ "$$ok" = "y" ]; fi

.PHONY: platform
platform: confirm-context ## Install / upgrade everything with helmfile (ENV=k3s|eks, needs GRAFANA_ADMIN_PASSWORD)
	@# On a fresh cluster helm-diff cannot render ServiceMonitors before kube-prometheus-stack installs the CRDs.
	helmfile -e $(ENV) apply --skip-diff-on-install

.PHONY: diff
diff: require-kubeconfig ## Show what `make platform` would change
	helmfile -e $(ENV) diff

.PHONY: platform-down
platform-down: confirm-context ## Uninstall all releases (the model cache PVC is kept)
	GRAFANA_ADMIN_PASSWORD=$${GRAFANA_ADMIN_PASSWORD:-unused} helmfile -e $(ENV) destroy

##@ No-GPU dev cluster (kind + fake vLLM)

.PHONY: dev-up
dev-up: ## kind cluster with the whole platform; a fake vLLM stands in for the GPU (needs Docker, kind)
	kind get clusters | grep -qx $(KIND_CLUSTER) || \
	  kind create cluster --config infra/kind/cluster.yaml --kubeconfig $(DEV_KUBECONFIG)
	kind export kubeconfig --name $(KIND_CLUSTER) --kubeconfig $(DEV_KUBECONFIG)
	$(DEV_ENV) helmfile -e dev sync
	@echo; echo "Ready. export KUBECONFIG=$(DEV_KUBECONFIG)   then: make grafana / make e2e"

.PHONY: dev-down
dev-down: ## Delete the kind cluster
	kind delete cluster --name $(KIND_CLUSTER) --kubeconfig $(DEV_KUBECONFIG)

.PHONY: fault
fault: ## Inject faults into every fake vLLM pod: make fault ERROR_RATE=0.3 TTFT=3
	@export KUBECONFIG=$(DEV_KUBECONFIG); \
	for pod in $$(kubectl -n $(NS) get pods -l app.kubernetes.io/name=vllm -o name); do \
	  kubectl -n $(NS) exec $$pod -- python -c "import sys, urllib.request as u; print(u.urlopen(u.Request('http://127.0.0.1:8000/admin', data=sys.argv[1].encode(), method='POST')).read().decode())" \
	    '{"error_rate": $(ERROR_RATE), "ttft": $(TTFT)}'; \
	done

.PHONY: e2e
e2e: ## End-to-end check on the dev cluster: scraping, rules, dashboards, KEDA, burn-rate alert
	$(DEV_ENV) python3 tests/e2e/e2e.py

##@ GPU checks

.PHONY: gpu-status
gpu-status: require-kubeconfig ## GPU nodes, advertised GPUs and GPU Operator pods
	kubectl get nodes -L nvidia.com/gpu.product,nvidia.com/gpu.count,nvidia.com/gpu.replicas,nvidia.com/device-plugin.config
	@echo
	kubectl get nodes -o custom-columns='NODE:.metadata.name,ALLOCATABLE_GPU:.status.allocatable.nvidia\.com/gpu'
	@echo
	kubectl -n gpu-operator get pods -o wide

.PHONY: gpu-test
gpu-test: require-kubeconfig ## Run a CUDA vector-add pod on a GPU node
	kubectl apply -f platform/gpu-operator/smoke-test.yaml
	kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/cuda-vectoradd --timeout=300s
	kubectl logs pod/cuda-vectoradd
	kubectl delete -f platform/gpu-operator/smoke-test.yaml

##@ Serving

.PHONY: smoke
smoke: require-kubeconfig ## One chat completion from inside the cluster
	kubectl -n $(NS) run curl-smoke --rm -i --restart=Never --image=curlimages/curl:8.16.0@sha256:463eaf6072688fe96ac64fa623fe73e1dbe25d8ad6c34404a669ad3ce1f104b6 -- \
	  curl -sS --max-time 120 http://vllm.$(NS).svc:8000/v1/chat/completions \
	  -H 'Content-Type: application/json' \
	  -d '{"model":"$(MODEL)","messages":[{"role":"user","content":"Explain a Kubernetes PodDisruptionBudget in one sentence."}],"max_tokens":80}'

.PHONY: loadtest
loadtest: require-kubeconfig ## Run the staged load test as a Job in the cluster and follow its output
	kubectl -n $(NS) create configmap llm-load-script --from-file=loadtest/llm_load.py --dry-run=client -o yaml | kubectl apply -f -
	kubectl -n $(NS) delete job llm-load --ignore-not-found
	kubectl apply -f loadtest/job.yaml
	kubectl -n $(NS) wait --for=condition=Ready pod -l job-name=llm-load --timeout=180s
	kubectl -n $(NS) logs -f job/llm-load

.PHONY: loadtest-local
loadtest-local: ## Load test through `make api` (ARGS="--stages 1,8 --stage-duration 30")
	python3 loadtest/llm_load.py --base-url http://localhost:8000 --model $(MODEL) $(ARGS)

.PHONY: fake
fake: ## Fake vLLM (OpenAI API + vLLM metrics) on :8001, no cluster needed
	python3 loadtest/fake_vllm.py --port 8001

##@ Access

.PHONY: grafana
grafana: require-kubeconfig ## Grafana on http://localhost:3000 (admin / $GRAFANA_ADMIN_PASSWORD)
	kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80

.PHONY: prometheus
prometheus: require-kubeconfig ## Prometheus on http://localhost:9090
	kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090

.PHONY: api
api: require-kubeconfig ## vLLM OpenAI API on http://localhost:8000
	kubectl -n $(NS) port-forward svc/vllm 8000:8000

##@ Quality gates (same as CI)

.PHONY: lint
lint: lint-charts test-rules lint-python check-dashboards ## Everything CI runs, except Terraform

.PHONY: lint-charts
lint-charts: ## helm lint + kubeconform on rendered manifests
	helm lint charts/vllm -f serving/vllm/values.yaml -f serving/vllm/values-$(ENV).yaml
	helm lint charts/llm-observability
	helm template vllm charts/vllm -n $(NS) -f serving/vllm/values.yaml -f serving/vllm/values-$(ENV).yaml \
	  | kubeconform -strict -summary $(KUBECONFORM_SCHEMAS)
	helm template llm-observability charts/llm-observability -n monitoring \
	  | kubeconform -strict -summary $(KUBECONFORM_SCHEMAS)

.PHONY: test-rules
test-rules: ## promtool check + unit tests for alerts and recording rules
	$(PROMTOOL) check rules charts/llm-observability/rules/*.yaml
	$(PROMTOOL) test rules observability/tests/*.test.yaml

.PHONY: lint-python
lint-python: ## ruff on the load test, fake vLLM, e2e test and dashboard generator
	ruff check loadtest observability tests

.PHONY: dashboards
dashboards: ## Regenerate Grafana JSON from observability/build_dashboards.py
	python3 observability/build_dashboards.py

.PHONY: check-dashboards
check-dashboards: dashboards ## Fail if committed dashboards are stale
	git diff --exit-code -- charts/llm-observability/dashboards

##@ AWS EKS

.PHONY: eks-preflight
eks-preflight: ## Read-only checks: account, GPU types, Spot quota, Spot price, tfvars, leftovers
	REGION=$(REGION) bash infra/aws-eks/preflight.sh

.PHONY: aws-tf-profile
aws-tf-profile:
	@# Refreshing aws-login credentials needs a region; without one the refresh fails after 15 minutes.
	@aws configure set profile.$(AWS_PROFILE).region $(REGION)
	@aws configure set profile.$(TF_PROFILE).credential_process \
	  "aws configure export-credentials --profile $(AWS_PROFILE) --region $(REGION) --format process"
	@aws configure set profile.$(TF_PROFILE).region $(REGION)

.PHONY: eks-up
eks-up: eks-preflight aws-tf-profile ## preflight + terraform apply + a separate kubeconfig for the lab cluster
	$(TF) init -input=false
	$(TF) apply
	aws eks update-kubeconfig --region $(REGION) --name $(CLUSTER) --kubeconfig $(EKS_KUBECONFIG) --profile $(AWS_PROFILE)
	@echo; echo "Cluster ready. export KUBECONFIG=$(EKS_KUBECONFIG)   then: make platform ENV=eks"

.PHONY: gpu-nodes
gpu-nodes: ## Scale the EKS GPU node group: make gpu-nodes N=0 to stop GPU spend
	@ng=$$(aws eks list-nodegroups --cluster-name $(CLUSTER) --region $(REGION) \
	  --query "nodegroups[?starts_with(@, 'gpu')] | [0]" --output text); \
	echo "scaling $$ng to $(N)"; \
	aws eks update-nodegroup-config --cluster-name $(CLUSTER) --region $(REGION) --nodegroup-name $$ng \
	  --scaling-config minSize=0,maxSize=2,desiredSize=$(N)

.PHONY: eks-down
eks-down: aws-tf-profile ## Uninstall releases first (they own cloud resources), terraform destroy, check for leftovers
	@# The helmfile template requires the Grafana password even though destroy never uses it.
	-KUBECONFIG=$(EKS_KUBECONFIG) GRAFANA_ADMIN_PASSWORD=$${GRAFANA_ADMIN_PASSWORD:-unused} helmfile -e eks destroy
	$(TF) destroy $(if $(filter 1,$(YES)),-auto-approve)
	REGION=$(REGION) bash infra/aws-eks/preflight.sh leftovers

##@ Help

.PHONY: help
help:
	@awk 'BEGIN {FS = ":.*##"} /^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)} \
	  /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
