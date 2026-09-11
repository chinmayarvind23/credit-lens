# CreditLens Deployment Plan

## Current deployment

The [interactive HF Space](https://huggingface.co/spaces/chinmayarvind/creditlens)
embeds the synthetic FastAPI/TypeScript app running on the owner's computer through
a free Cloudflare Quick Tunnel. See the [live release procedure](../infra/huggingface/live/DEPLOYMENT.md).
Computer, Docker and tunnel availability determine demo availability. HF hosts the
entry page with its Static SDK; each question executes against the live backend.
The former recorded preview has been removed from the published Space.

AWS Terraform is validated locally and remains unapplied under the user's
no-spending restriction. Live Cognito and Snowflake integration remain unfinished.
The following cloud architecture is a plan, not a list of deployed services.

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
