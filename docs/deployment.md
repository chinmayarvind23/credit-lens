# Deployment

The [free interactive Hugging Face build](../infra/huggingface/browser/DEPLOYMENT.md) packages the workbench, Python browser runtime and fictional fixtures. It works independently of an owner workstation after publication. Use the browser build for the public demonstration.

For a local server, follow the [README](../README.md#setup). Optional PostgreSQL, Redis, search, queue and monitoring setup lives under [infra](../infra). Configure current identity and catalog authority before admitting protected data.

[Optional AWS setup](../infra/aws/README.md) supplies Terraform and operator instructions. Operators choose their own account, credentials and infrastructure costs. The public demo requires no AWS resources.
