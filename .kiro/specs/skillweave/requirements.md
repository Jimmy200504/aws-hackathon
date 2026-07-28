# Requirements

## R1 — Judge-compatible search

WHEN a valid query-only payload is submitted, THE SYSTEM SHALL return a deterministic ranked job list with continuous ranks beginning at one.

WHEN location or duty codes are supplied, THE SYSTEM SHALL decode them or safely retain unknown codes without producing a server error.

WHEN the request uses the original brief aliases `ks/c0/d0`, THE SYSTEM SHALL map them to the workshop contract.

## R2 — Train-only skill graph

WHEN a job source timestamp is later than the graph cutoff, THE SYSTEM SHALL NOT create any JD-derived graph edge for that job.

WHEN an LLM proposes a `REQUIRES` edge, THE SYSTEM SHALL require an exact source evidence span and a valid field.

## R3 — Retrieval and ranking

THE SYSTEM SHALL combine lexical, semantic, graph, condition, behavior, freshness, and cold-start features.

THE SYSTEM SHALL bound related-edge contribution so graph density cannot dominate direct evidence.

## R4 — Verifiable GenAI necessity

THE REPOSITORY SHALL provide one command that executes graph-on and graph-off evaluation on identical query groups and reports NDCG@10, MRR, Hit@1, Hit@10, and Precision@10.

THE RELEASE SHALL NOT pass the theme gate unless held-out NDCG@10 degrades materially when the graph is removed.

## R5 — Operations

THE AWS DEPLOYMENT SHALL expose health checks, version metadata, structured latency telemetry, and a contract-safe fallback when graph or model services time out.
