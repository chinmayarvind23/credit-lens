# Optional AWS setup reference

AWS is optional and outside the current delivery scope. The free interactive Hugging Face demo needs no AWS account. These instructions are for operators choosing their own server deployment. No AWS deployment has been performed. The user's no-out-of-pocket-spend constraint
prohibits applying this reference until account plan status and an eligible no-charge
path are verified. List-price resources are billable; neither a zero current bill nor
unused Free Tier offers proves that later usage cannot charge the account. Never
upgrade an account plan to make this deployment work.

The reference uses one ECS Fargate task, ECR, an ALB, CloudFront and CloudWatch Logs in
`us-east-1`. The container is the existing synthetic demo image. It runs as UID 1000,
has no AWS task role, accepts no real documents and uses ephemeral SQLite. A task
restart loses the demo audit history. Production lender data must not use this stack.
The production architecture remains separate from this demonstration; see [the scope
decision](ADR-synthetic-demo.md).

```text
Browser HTTPS → CloudFront default hostname (cache disabled)
             → ALB HTTP (CloudFront IP prefix + private origin header)
             → one Fargate task, port 7860 → synthetic fixture + local SQLite
                                            ↘ bounded CloudWatch logs
```

CloudFront's default certificate supplies HTTPS without buying a domain. The origin
hop is HTTP, restricted by security groups and an origin header. That is an explicit
synthetic-only compromise, not end-to-end encryption for regulated information.
[AWS origin restriction guidance](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/restrict-access-to-load-balancer.html).
The AWS-managed CachingDisabled and AllViewer policy IDs are pinned from the
[cache policy](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-cache-policies.html)
and [origin policy](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-origin-request-policies.html)
references. No CDN error or response caching is enabled.

## Local verification

Terraform >=1.13.5 is required. Providers and Windows/Linux checksums are locked.
These commands validate and run **mocked plans**, with no AWS credentials or resources:

```powershell
terraform -chdir=infra/aws init -backend=false
terraform -chdir=infra/aws fmt -check -recursive
terraform -chdir=infra/aws validate
terraform -chdir=infra/aws test
python scripts/render_aws_policies.py --account 111122223333 --output ../resources/credit_lens/aws-policy-review-new
```

All test providers are mocked and every test run uses `command = plan`. Tests check
the one-task budget, non-root runtime, absent task role, disabled cache, source access,
fixed role, immutable image and default reference guard. A passing mock plan is not an
account-backed plan, a permission simulation or a deployed service.

`reference_plan_enabled` defaults to false. Setting it permits planning the reference;
it does not authorize spending or override the user's no-spend constraint. The example
account and all-zero image digest are fixtures. Never apply them.

## Bootstrap and routine permissions

The three `deployer-*.json` templates are candidate least-privilege **routine** policies,
split to stay within managed-policy size limits. Render them locally for a verified
account. The renderer performs zero AWS calls. They scope regional resource names,
ARNs and ownership tags, pass only `creditlens-demo-execution` to ECS tasks, and grant
no IAM role-policy editing, account-plan upgrade or Organizations actions.

A trusted account administrator must perform initial bootstrap separately: create
the execution role from `execution-trust.json` and `execution-policy.json`, ensure the
ECS/ELB service-linked roles exist, and perform initial CloudFront creation/tagging.
Routine deployers cannot create CloudFront distributions or add/change ownership tags.
After bootstrap, render with `--distribution-id EXISTING_ID` to pin management to that
one distribution ARN. Without that argument the generated policies grant no CloudFront
operations. A bootstrap administrator can run the initial Terraform deployment and
then hand off its private state; independently created resources must be imported
before routine Terraform operation. **No bootstrap action has been performed.**

CloudFront creation needs permission before its ARN exists. Giving routine users
account-wide creation/tagging would let them claim other distributions, so this
reference keeps that permission with trusted bootstrap. The initial policy draft was
tightened before review; only final rendered policies should be considered. Policy
actions were checked against AWS's service authorization metadata. An account-backed
IAM simulation and initial plan remain unperformed; organization policies, quotas and
service-linked-role prerequisites can still deny operations.

The task execution role only pulls this ECR repository and writes this log stream
prefix. It is distinct from an application task role, which is absent. AWS requires
wildcard resources for selected discovery APIs and ECR authorization tokens; those
specific actions are listed explicitly instead of granting service-wide `*` actions.

## Future release sequence, currently prohibited

Once root verifies the user's constraints permit it, first review the exact account,
region, provider lock, image build manifest and resource plan. Bootstrap the empty ECR
repository before publishing the reviewed image, then use its immutable digest in
the complete plan. This two-stage ordering avoids deploying a nonexistent image.
The initial administrator handles bootstrap and CloudFront; routine updates use the
scoped user afterward. The complete plan must show only this reference's resources.
CloudFront propagation and ECS health checks must finish before claiming deployment.

Terraform state and saved plans include the private origin header. Initialize a local
backend with an absolute private state path outside the repository, and protect that
directory with owner-only OS permissions. Never commit state, plans, tfvars, credentials
or the origin header. A remote state backend is separate reviewed infrastructure, not
silently provisioned here. Terraform outputs alone do not prove that `/ready`, a valid
query, denied evidence access and the browser interface work.

## Cost and teardown

At 730 hours/month, list prices yield roughly $9.01 for 0.25 vCPU/0.5 GiB Fargate,
$16.43 ALB fixed charges and $10.95 for at least three public IPv4 addresses.
That is **$36.39/month before usage-based costs**, taxes and account-specific pricing.
ALB LCU, CloudFront traffic/requests, logs, image storage and transfers add cost. This
is an estimate, not a cap, and assumes no credits or allowances. [Fargate
pricing](https://aws.amazon.com/fargate/pricing/), [ALB
pricing](https://aws.amazon.com/elasticloadbalancing/pricing/), [IPv4
pricing](https://aws.amazon.com/vpc/pricing/).

No paid database, cache, OpenSearch, NAT gateway or autoscaling fleet is provisioned.
The one-task rollout allows downtime to avoid doubling compute during deployment.
Stopping the task alone does not stop ALB/IP charges. Full teardown requires reviewing
a destroy plan, deleting this demonstration's ECR images explicitly (repository
`force_delete` is false), and destroying the Terraform resources. CloudFront deletion
can take several minutes. Verify that the service, ALB, ENIs, log group and repository
are gone before concluding charges have stopped. Preserve or revoke the bootstrap
execution role and routine policies deliberately; Terraform does not own those roles.
