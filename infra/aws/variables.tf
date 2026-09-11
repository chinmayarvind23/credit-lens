variable "aws_profile" {
  type        = string
  default     = "creditlens"
  description = "Named credentials profile; credentials never enter tfvars or state."
}

variable "account_id" {
  type        = string
  description = "Verified 12-digit AWS account, checked by the provider before operations."
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "A verified 12-digit account ID is required."
  }
}

variable "image_digest" {
  type        = string
  description = "Reviewed image's ECR sha256 digest. Tags cannot silently replace a running release."
  validation {
    condition     = can(regex("^sha256:[a-f0-9]{64}$", var.image_digest))
    error_message = "Use an immutable sha256 image digest."
  }
}

variable "availability_zones" {
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
  description = "Two account-available AZs for ALB; verify availability before any real plan."
  validation {
    condition = length(var.availability_zones) == 2 && length(distinct(var.availability_zones)) == 2 && alltrue([
      for zone in var.availability_zones : can(regex("^us-east-1[a-z]$", zone))
    ])
    error_message = "Provide two distinct us-east-1 availability zones."
  }
}

variable "execution_role_arn" {
  type        = string
  description = "Administrator-created pull/log role; Terraform cannot edit IAM role policies."
  validation {
    condition     = var.execution_role_arn == "arn:aws:iam::${var.account_id}:role/creditlens-demo-execution"
    error_message = "Use only the reviewed CreditLens execution role in the verified account."
  }
}

variable "reference_plan_enabled" {
  type        = bool
  default     = false
  description = "Explicitly enable an offline/reference plan. This does not authorize spending or apply."
}
