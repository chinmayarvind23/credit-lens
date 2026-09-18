# Documentation

- [System design](system-design.md): request flow, ingestion and deployment boundaries.
- [Runtime contracts](LLD.md): modules, authority and failure behavior.
- [Security](security.md): identity, scope, citations and public-demo constraints.
- [Commands](commands.md): local startup and development checks.
- [Evaluation usage](../evals/README.md): offline harness and semantic runners.
- [Observability](observability.md): tracing, operational signals and dashboards.
- [Recovery](recovery.md): restoring canonical state and current permissions.
- [Deployment](deployment.md): browser hosting and optional operator infrastructure.
- [Weaviate vector search](weaviate.md): governed production configuration, synchronization and recovery.
- [Grounded synthesis](generation.md): pinned Ollama generation, supporting quotes and operator configuration.
- [Governed ingestion](ingestion.md): staged PDFs, catalog-bound jobs and explicit vector synchronization.
- [GraphQL inspection](graphql-admin.md) and [Cortex contract](cortex-index-contract.md).

Integration setup lives with its implementation in [infra](../infra). The generated API reference is available at `/docs` on the running server.
