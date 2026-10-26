{{- define "vllm.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "vllm.fullname" -}}
{{- if contains .Chart.Name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "vllm.selectorLabels" -}}
app.kubernetes.io/name: {{ include "vllm.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "vllm.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{ include "vllm.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: inference-server
{{- end }}

{{/* PromQL selector for this release's metrics; the ServiceMonitor adds namespace and service labels. */}}
{{- define "vllm.promSelector" -}}
namespace="{{ .Release.Namespace }}",service="{{ include "vllm.fullname" . }}"
{{- end }}
