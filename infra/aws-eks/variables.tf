variable "region" {
  description = <<-EOT
    AWS region. Spot prices differ a lot between regions: in October 2026 a g6.2xlarge Spot
    cost ~$0.16/h in eu-north-1 vs ~$0.65/h in eu-central-1. Check before you apply.
  EOT
  type        = string
  default     = "eu-north-1"
}

variable "name" {
  description = "Cluster and VPC name."
  type        = string
  default     = "gpu-inference"
}

variable "kubernetes_version" {
  description = "EKS Kubernetes version."
  type        = string
  default     = "1.36"
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}

variable "api_allowed_cidrs" {
  description = "CIDRs allowed to reach the public EKS API endpoint. Narrow this to your IP."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "system_instance_types" {
  description = "Instance types for Prometheus, Grafana, KEDA and the GPU Operator controller."
  type        = list(string)
  default     = ["t3.large"]
}

variable "gpu_instance_types" {
  description = <<-EOT
    Single-GPU (24 GB) types with 8 vCPU / 32 GiB: two vLLM replicas plus a rollout surge pod fit.
    Several types widen the Spot pool and reduce interruptions.
  EOT
  type        = list(string)
  default     = ["g6.2xlarge", "g5.2xlarge"]
}

variable "gpu_capacity_type" {
  description = "SPOT for the lab (~60% cheaper, can be reclaimed with 2 minutes notice) or ON_DEMAND."
  type        = string
  default     = "SPOT"

  validation {
    condition     = contains(["SPOT", "ON_DEMAND"], var.gpu_capacity_type)
    error_message = "gpu_capacity_type must be SPOT or ON_DEMAND."
  }
}

variable "gpu_desired_size" {
  description = <<-EOT
    GPU nodes at creation only: the EKS module ignores later desired_size changes so that
    autoscalers own it. Scale between sessions with `make gpu-nodes N=0` instead.
  EOT
  type        = number
  default     = 1
}

variable "gpu_max_size" {
  type    = number
  default = 2
}

variable "budget_alert_email" {
  description = "Email for AWS Budgets alerts (80% actual, 100% forecasted). Empty = no budget."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget for the whole account, in USD."
  type        = number
  default     = 30
}

variable "control_plane_log_types" {
  description = "EKS control plane logs sent to CloudWatch. [] turns them off; add \"api\" only while debugging."
  type        = list(string)
  default     = ["audit"]
}
