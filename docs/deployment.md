# Deployment

The [interactive CreditLens Space](https://huggingface.co/spaces/chinmayarvind/creditlens)
runs Python in the visitor's browser on Hugging Face free Static hosting. HF hosts
all assets, including the pinned runtime. It requires no owner computer, API server,
temporary tunnel or paid compute. New questions and source inspection were verified
with browser networking disabled after startup.

Follow the [browser release guide](../infra/huggingface/browser/DEPLOYMENT.md) to
build, inspect and publish a clean source snapshot. It records exact file hashes,
checks free Static mode and uses a parent-bound remote commit. The historical
[local-container/tunnel procedure](../infra/huggingface/live/DEPLOYMENT.md) remains
for reference and is no longer the public deployment path.

The browser uses lexical retrieval and quoted evidence. Optional hybrid models run
in the server deployment. Only public fictional documents are distributed; browser
scope filtering is not an access boundary for confidential data.

AWS deployment is optional and has not been performed. The [AWS reference](../infra/aws/README.md)
provides operator setup, IAM bootstrap, image publication, Terraform validation,
health checks and teardown in us-east-1. Its resources can incur charges. No AWS
credentials or resources are needed for the free Space or local development.

Use the repository README for the FastAPI server, optional PostgreSQL/Redis/queue
services and local OTel/Prometheus configuration. Hosted GitHub Actions remains
manual-dispatch only; no paid pipeline or managed cloud resource is enabled by default.
