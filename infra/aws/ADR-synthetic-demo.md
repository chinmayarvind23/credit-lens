# ADR: Separate the AWS synthetic demonstration from production lending infrastructure

Status: accepted as an unapplied reference; live deployment is constrained by the
user's no-out-of-pocket-spend requirement and unverified account eligibility.

The existing application can demonstrate scoped evidence and deterministic packet
preparation using public synthetic fixtures. It does not yet have a shared production
catalog, durable cross-replica audit store or live governed retrieval. Provisioning a
full RDS, ElastiCache and managed OpenSearch fleet would create charges while leaving
those application boundaries unfinished.

Use one small Fargate task with its immutable reviewed demo image, ALB, CloudFront,
ECR and bounded CloudWatch logs. The supplied CloudFront hostname gives the browser
HTTPS without a user domain. The public demo remains visibly synthetic, uses local
SQLite and grants no application AWS role. It loses local audit data on task replacement
and permits deployment downtime. The CloudFront-to-ALB HTTP hop is restricted but does
not satisfy an end-to-end encrypted production design.

The deployer cannot edit IAM policies or claim arbitrary CloudFront distributions.
Trusted initial bootstrap is separate; routine management is pinned to the existing
distribution ARN. The account, role, image digest and no-spend eligibility require
verification before any deployment. Local mocked tests are configuration evidence only.

A single Lightsail VM may cost less but adds patching and HTTPS operations. App Runner
offers a managed domain but changes the accepted ECS path. A VPC-only CloudFront origin
could remove public ALB IPv4 costs and strengthen the origin boundary, but would add
service-role and networking setup not validated in this reference. Those are explicit
alternatives for a future funded or verified no-charge deployment.

Production promotion requires durable SQL canonical authority/audits, managed OIDC,
shared revocation/cache consistency, encrypted private service paths, live Cortex
integration, measured provider quality/cost/latency and failure/load tests. A public
demo URL alone does not establish those requirements.
