terraform {
  required_version = ">= 1.13.5, < 2.0.0"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "6.61.0" }
    random = { source = "hashicorp/random", version = "3.9.0" }
  }
  # The caller supplies a private path during init; no state belongs in this repository.
  backend "local" {}
}

provider "aws" {
  region              = "us-east-1"
  profile             = var.aws_profile
  allowed_account_ids = [var.account_id]
  default_tags { tags = local.tags }
}

locals {
  name = "creditlens-demo"
  tags = { Project = "CreditLens", Environment = "synthetic-demo", ManagedBy = "Terraform" }
}
