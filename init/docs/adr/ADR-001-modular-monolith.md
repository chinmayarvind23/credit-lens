# ADR-001: Start with a Modular Monolith

## Status

Accepted.

## Context

Core query path is tightly coupled and modeled workload is low.

## Decision

Use one FastAPI deployment with clear internal modules.

## Alternative: Microservices

Pros: independent deployment/scaling.

Cons: network failures, service auth, distributed tracing, retry/version complexity, no current scale requirement.

## Tradeoff

Accept one deployment unit for simpler reasoning and faster iteration.

## Switching condition

Extract only when a component gains independent scale, availability, ownership, or deployment needs.
