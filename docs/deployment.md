# CreditLens Deployment Plan

## Local development

Use:

- `uv` for Python,
- Bun for TypeScript/frontend,
- Docker Compose for local supporting services where useful.

Local support may include Postgres, Redis, OpenSearch, Weaviate, OTel Collector, Prometheus, and Grafana.

Snowflake remains external.

## Walking skeleton

Deploy in Chunk 2:

- Vercel frontend,
- FastAPI on AWS,
- managed identity connection,
- Snowflake connectivity.

Solve environment, CORS, secrets, and connectivity early.

## Primary cloud

AWS + Snowflake.

Expected AWS roles:

- S3 source documents,
- Cognito identity,
- ECS/EC2 compute,
- RDS/Postgres,
- ElastiCache/Redis,
- OpenSearch,
- SQS,
- IAM,
- CloudWatch,
- Terraform-managed infrastructure.

## Secondary required paths

Outside hot path:

- Lightsail: low-cost staging/deployment comparison,
- Supabase: sanitized demo or feedback/session metadata only,
- HF Spaces: sanitized AI demo,
- Vercel: frontend/public demo.

## Infrastructure as code

Terraform covers primary AWS resources used by the deployment.

## CI/CD

GitHub Actions should run lint, format check, strict type check, deterministic tests, security scans, smoke evals, infrastructure validation, and build.
