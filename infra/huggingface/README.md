---
title: CreditLens Synthetic Underwriting Demo
emoji: 📑
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# CreditLens synthetic underwriting demo

CreditLens assembles cited borrower evidence, deterministic financial metrics, missing-document checks, conflicts, and next actions for human review.

This Space runs the **synthetic local extractive demonstration**. Borrowers and policy pages are fictional. The local financial disposition assesses **DSCR only**. It does not approve or reject loans or complete a full policy assessment.

Select a borrower, set the policy date, and ask about debt service coverage, missing documents, or conflicts. Open a citation to inspect its page text and provenance. Expand the execution trace to see the actual provider mode and request timing.

## Boundaries

- No real borrower files, credentials, source corpus artifacts, evaluation outputs, or private planning documents are included in the uploaded package.
- The runtime creates the small synthetic corpus from source code and stores demo grants and audit events in a local SQLite database.
- Demo identity is fixed server-side and limited to its authorized synthetic borrowers.
- The container runs as UID 1000 on port 7860. It accepts no production mode or external database override through the demo entrypoint.
- No authentication token is needed for this public synthetic demo. Do not enter confidential borrower details or credentials.
- Runtime storage is ephemeral. Restarting the Space can reset demo audit data. This deployment is not a durable production system.

## Container verification

The repository includes a multi-stage Dockerfile, locked Bun and Python dependencies, and an upload staging script that checks an explicit file allowlist and content hashes. A generated `deployment-manifest.json` identifies the staged source snapshot. Build and deployment status must be checked from the actual deployment evidence; this card does not itself certify a successful build or deployment.
