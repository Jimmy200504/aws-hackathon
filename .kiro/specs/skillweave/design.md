# Design

## Decision 1: LLM before ranking, not after ranking

Bedrock converts unstructured JD text into evidence-grounded graph proposals offline. It does not write unconstrained recommendation explanations online. This makes GenAI measurable through ablation and keeps latency predictable.

## Decision 2: dual candidate evidence

OpenSearch supplies BM25/vector candidates. Neptune supplies bounded one-hop graph features. A graph neighbor alone cannot enter the final candidate set without lexical or direct canonical-skill evidence in the local safety ranker.

## Decision 3: temporal quarantine

Post-cutoff jobs are available to lexical cold-start retrieval but have no JD-derived graph edges. This makes the failure visible and testable.

## Decision 4: behavior is biased evidence

View/apply labels are graded but exposure-biased. The production trainer uses clipped IPS weights. The evaluator metrics remain unweighted and are reported separately.

## Interfaces

- `POST /api/v1/jobs/search`
- `POST /api/v1/graph/trace`
- `GET /api/v1/meta`
- `GET /health`

## Release gates

- Contract tests green
- Leakage tests green
- Bedrock gold-set extraction precision gate
- Validation improvement
- Fresh holdout NDCG target
- AWS smoke/load test
