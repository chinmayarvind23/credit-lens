# ADR-002: Snowflake Cortex Search as Primary Governed Retrieval

## Status

Accepted.

## Decision

Use Snowflake Cortex Search as the primary production retrieval path and Snowpark/SQL for governed structured transformations.

## Alternatives

Fully self-managed vector DB: more control, more operational surface.

All-Snowflake with no shadow lab: simpler, but weaker first-principles retrieval and HNSW learning.

## Tradeoff

Managed primary search plus shadow retrieval lab.

## Switching condition

Switch primary retrieval if measured quality, cost, latency, portability, or governance materially favor another system.
