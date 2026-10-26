data "aws_availability_zones" "available" {
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 3)
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

  name = var.name
  cidr = var.vpc_cidr
  azs  = local.azs

  private_subnets = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnets  = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 8, i + 48)]

  # One NAT gateway is a single point of failure, acceptable for a lab and ~3x cheaper.
  enable_nat_gateway = true
  single_nat_gateway = true

  public_subnet_tags  = { "kubernetes.io/role/elb" = 1 }
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = 1 }
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = var.name
  kubernetes_version = var.kubernetes_version

  # Nodes and in-cluster controllers use the private endpoint; the public one is only for your IP.
  # Restricting the public endpoint without private access would leave nodes unable to join.
  endpoint_private_access      = true
  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.api_allowed_cidrs

  # The identity running terraform becomes cluster admin through an access entry.
  enable_cluster_creator_admin_permissions = true

  # Module default is audit + api + authenticator kept 90 days. The api log is chatty and CloudWatch
  # bills ingestion per GB, so the lab keeps only the audit trail, for a week.
  enabled_log_types                      = var.control_plane_log_types
  cloudwatch_log_group_retention_in_days = 7

  # Every create/destroy cycle schedules the secrets-encryption key for deletion; keep that short.
  kms_key_deletion_window_in_days = 7

  # No workload here calls AWS APIs, so no IRSA OIDC provider. AWS accounts created through the console
  # "projects" flow deny iam:CreateOpenIDConnectProvider by SCP; pods that need AWS access later should
  # use EKS Pod Identity (the agent add-on is installed below).
  enable_irsa = false

  addons = {
    coredns = {}
    eks-pod-identity-agent = {
      before_compute = true
    }
    kube-proxy = {}
    vpc-cni = {
      before_compute = true
    }
  }

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  eks_managed_node_groups = {
    system = {
      ami_type       = "AL2023_x86_64_STANDARD"
      instance_types = var.system_instance_types
      min_size       = 2
      max_size       = 3
      desired_size   = 2

      labels = {
        "workload-type" = "system"
      }
    }

    gpu = {
      # NVIDIA driver and container toolkit are baked into this AMI, so the GPU Operator
      # runs with driver.enabled=false / toolkit.enabled=false (platform/gpu-operator/values.yaml).
      ami_type       = "AL2023_x86_64_NVIDIA"
      instance_types = var.gpu_instance_types
      capacity_type  = var.gpu_capacity_type

      min_size     = 0
      max_size     = var.gpu_max_size
      desired_size = var.gpu_desired_size

      # The vLLM image is ~10 GB compressed; models and compile caches need room too.
      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = 200
            volume_type           = "gp3"
            encrypted             = true
            delete_on_termination = true
          }
        }
      }

      labels = {
        "workload-type" = "gpu-inference"
      }

      # Only pods that tolerate this land on expensive GPU nodes.
      taints = {
        gpu = {
          key    = "nvidia.com/gpu"
          value  = "true"
          effect = "NO_SCHEDULE"
        }
      }
    }
  }
}
