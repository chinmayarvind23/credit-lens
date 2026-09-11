resource "aws_ecr_repository" "demo" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  encryption_configuration { encryption_type = "AES256" }
  image_scanning_configuration { scan_on_push = true }
  depends_on = [terraform_data.no_spend_guard]
}

resource "aws_cloudwatch_log_group" "demo" {
  name              = "/ecs/${local.name}"
  retention_in_days = 7
}

resource "aws_ecs_cluster" "demo" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_ecs_task_definition" "demo" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = var.execution_role_arn
  # There is deliberately no task_role_arn: synthetic runtime needs no AWS API permissions.
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name            = "demo"
    image           = "${aws_ecr_repository.demo.repository_url}@${var.image_digest}"
    essential       = true
    user            = "1000:1000"
    portMappings    = [{ containerPort = 7860, hostPort = 7860, protocol = "tcp" }]
    linuxParameters = { capabilities = { drop = ["ALL"] }, initProcessEnabled = true }
    healthCheck = {
      command  = ["CMD-SHELL", "/app/.venv/bin/python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/ready',timeout=3).read()\""]
      interval = 30, timeout = 5, retries = 3, startPeriod = 60
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.demo.name
        awslogs-region        = "us-east-1"
        awslogs-stream-prefix = "demo"
      }
    }
  }])
}

resource "aws_lb" "demo" {
  name                       = local.name
  internal                   = false
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = aws_subnet.public[*].id
  drop_invalid_header_fields = true
  idle_timeout               = 30
  enable_deletion_protection = false
}

resource "aws_lb_target_group" "demo" {
  name                 = local.name
  port                 = 7860
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.demo.id
  deregistration_delay = 15
  health_check {
    path                = "/ready"
    matcher             = "200"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
    timeout             = 5
  }
}

resource "random_password" "origin" {
  length  = 48
  special = false
}

resource "aws_lb_listener" "demo" {
  load_balancer_arn = aws_lb.demo.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "Forbidden"
      status_code  = "403"
    }
  }
}

resource "aws_lb_listener_rule" "cloudfront" {
  listener_arn = aws_lb_listener.demo.arn
  priority     = 1
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.demo.arn
  }
  condition {
    http_header {
      http_header_name = "X-CreditLens-Origin"
      values           = [random_password.origin.result]
    }
  }
}

resource "aws_ecs_service" "demo" {
  name                               = local.name
  cluster                            = aws_ecs_cluster.demo.id
  task_definition                    = aws_ecs_task_definition.demo.arn
  desired_count                      = 1
  launch_type                        = "FARGATE"
  platform_version                   = "1.4.0"
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  health_check_grace_period_seconds  = 90
  wait_for_steady_state              = true
  enable_execute_command             = false
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.demo.arn
    container_name   = "demo"
    container_port   = 7860
  }
  depends_on = [aws_lb_listener_rule.cloudfront, aws_route.internet]
}
