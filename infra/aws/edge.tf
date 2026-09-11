locals {
  # AWS-published managed IDs are stable and avoid extra account-wide discovery permissions.
  disabled_cache_policy_id = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
  all_viewer_policy_id     = "216adef6-5c7f-47e4-b989-5492eafa07d3"
}

resource "aws_cloudfront_distribution" "demo" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "CreditLens synthetic demonstration; no production lender data"
  price_class     = "PriceClass_100"
  http_version    = "http2and3"
  origin {
    domain_name = aws_lb.demo.dns_name
    origin_id   = "demo-alb"
    custom_header {
      name  = "X-CreditLens-Origin"
      value = random_password.origin.result
    }
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      origin_read_timeout    = 30
    }
  }
  default_cache_behavior {
    target_origin_id         = "demo-alb"
    allowed_methods          = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods           = ["GET", "HEAD"]
    viewer_protocol_policy   = "redirect-to-https"
    cache_policy_id          = local.disabled_cache_policy_id
    origin_request_policy_id = local.all_viewer_policy_id
    compress                 = true
  }
  dynamic "custom_error_response" {
    for_each = [400, 403, 404, 405, 414, 416, 500, 501, 502, 503, 504]
    content {
      error_code            = custom_error_response.value
      error_caching_min_ttl = 0
    }
  }
  restrictions {
    geo_restriction { restriction_type = "none" }
  }
  viewer_certificate { cloudfront_default_certificate = true }
  depends_on = [aws_lb_listener_rule.cloudfront]
}

output "demo_url" { value = "https://${aws_cloudfront_distribution.demo.domain_name}" }
output "repository_url" { value = aws_ecr_repository.demo.repository_url }
output "cluster_name" { value = aws_ecs_cluster.demo.name }
output "service_name" { value = aws_ecs_service.demo.name }
output "deployment_status" { value = "Reference only. A Terraform output is not evidence of a tested deployment." }
