# Every cloud provider is mocked. These plans create no AWS resources and use no credentials.
mock_provider "aws" {
  override_during = plan
  mock_resource "aws_ecr_repository" {
    defaults = { repository_url = "111122223333.dkr.ecr.us-east-1.amazonaws.com/creditlens-demo" }
  }
  mock_resource "aws_security_group" {
    defaults = { id = "sg-0123456789abcdef0" }
  }
}
mock_provider "random" { override_during = plan }

override_resource {
  target          = aws_security_group.alb
  override_during = plan
  values          = { id = "sg-0123456789abcdef0" }
}

variables {
  account_id             = "111122223333"
  execution_role_arn     = "arn:aws:iam::111122223333:role/creditlens-demo-execution"
  image_digest           = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  reference_plan_enabled = true
}

run "bounded_synthetic_demo" {
  command = plan
  assert {
    condition     = aws_ecs_service.demo.desired_count == 1 && aws_ecs_task_definition.demo.cpu == "256" && aws_ecs_task_definition.demo.memory == "512"
    error_message = "The reference must stay at one minimal Fargate task."
  }
  assert {
    condition     = aws_ecs_task_definition.demo.task_role_arn == null
    error_message = "Synthetic application runtime must not receive AWS credentials."
  }
  assert {
    condition     = jsondecode(aws_ecs_task_definition.demo.container_definitions)[0].user == "1000:1000"
    error_message = "The reviewed image must run as the unprivileged demo user."
  }
  assert {
    condition     = aws_ecs_service.demo.deployment_maximum_percent == 100 && aws_ecs_service.demo.deployment_minimum_healthy_percent == 0
    error_message = "Reference deployment must not silently double the running task budget."
  }
  assert {
    condition     = aws_cloudfront_distribution.demo.default_cache_behavior[0].cache_policy_id == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    error_message = "All responses must use the disabled CDN cache policy."
  }
  assert {
    condition     = aws_cloudfront_distribution.demo.viewer_certificate[0].cloudfront_default_certificate
    error_message = "A public HTTPS hostname must not depend on the user purchasing a domain."
  }
  assert {
    condition     = aws_lb_listener.demo.default_action[0].type == "fixed-response"
    error_message = "The ALB must deny requests lacking the origin header."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.task_from_alb.from_port == 7860 && aws_vpc_security_group_ingress_rule.task_from_alb.referenced_security_group_id == aws_security_group.alb.id
    error_message = "Only the dedicated ALB may reach the application port."
  }
  assert {
    condition     = aws_cloudwatch_log_group.demo.retention_in_days == 7 && !aws_ecr_repository.demo.force_delete
    error_message = "Bound logs and retain explicit image-deletion review."
  }
}

run "reference_disabled_by_default" {
  command = plan
  variables { reference_plan_enabled = false }
  expect_failures = [terraform_data.no_spend_guard]
}

run "mutable_image_rejected" {
  command = plan
  variables { image_digest = "latest" }
  expect_failures = [var.image_digest]
}

run "other_role_rejected" {
  command = plan
  variables { execution_role_arn = "arn:aws:iam::111122223333:role/Administrator" }
  expect_failures = [var.execution_role_arn]
}

run "other_region_rejected" {
  command = plan
  variables { availability_zones = ["us-west-2a", "us-west-2b"] }
  expect_failures = [var.availability_zones]
}
