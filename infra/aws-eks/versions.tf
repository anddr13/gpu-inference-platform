terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }

  # Remote state in S3 with native locking (no DynamoDB). Partial config: `make state-up` creates the
  # bucket (infra/state) and writes backend.hcl, which `make eks-up` passes to `terraform init`.
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "gpu-inference-platform"
      ManagedBy = "terraform"
    }
  }
}
