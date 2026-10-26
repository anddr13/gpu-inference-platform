terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }

  # Lab default: local state. For a team, switch to S3 with native locking:
  # backend "s3" {
  #   bucket       = "my-tf-state"
  #   key          = "gpu-inference-platform/eks.tfstate"
  #   region       = "eu-north-1"
  #   use_lockfile = true
  # }
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
