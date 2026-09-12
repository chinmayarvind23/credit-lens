# AWS demo configuration boundaries

The optional Terraform configuration uses one Fargate task, ECR, an ALB,
CloudFront and CloudWatch Logs. The container runs public synthetic fixtures,
uses ephemeral SQLite and has no application AWS task role. Task replacement
discards local audit data, and the one-task rollout permits interruption.

CloudFront supplies its default HTTPS hostname. The origin hop to the ALB uses
HTTP, restricted by security groups and a private header. Use this configuration
only for the synthetic demo, not confidential lending documents.

Initial IAM and CloudFront bootstrap belongs to a trusted administrator. Routine
deployer permissions are scoped to the execution role and existing distribution.
State files contain the origin header and must remain private.

See [setup and teardown](README.md) for required configuration and commands.
