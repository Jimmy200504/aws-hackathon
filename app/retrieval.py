from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable
from urllib.parse import quote
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)


class OpenSearchRetriever:
    """Retrieve full-corpus job candidates from an IAM-protected OpenSearch index.

    Supports hybrid retrieval: BM25 keyword search + kNN vector search merged
    via Reciprocal Rank Fusion (RRF). Falls back to BM25-only when embedding
    is unavailable.

    Imports from botocore are intentionally lazy. The local 12,000-job demo
    remains dependency-free unless an OpenSearch endpoint is configured.
    """

    SOURCE_FIELDS = [
        "id",
        "title",
        "description",
        "salary",
        "salary_min",
        "salary_max",
        "salary_type",
        "is_remote",
        "city",
        "categories",
        "industry",
        "company_id",
        "modified_at",
        "post_cutoff_jd",
        "graph_eligible",
        "skills",
        "skill_evidence",
        "skill_confidence",
        "skill_provenance",
        "freshness",
        "view_count",
        "apply_count",
    ]

    def __init__(
        self,
        endpoint: str,
        index: str,
        *,
        region: str | None = None,
        timeout_seconds: float = 2.0,
        embedding_client: Any = None,
    ) -> None:
        endpoint = endpoint.strip().rstrip("/")
        if not endpoint:
            raise ValueError("OpenSearch endpoint must not be empty")
        if not endpoint.startswith("https://") and not endpoint.startswith(
            ("http://127.0.0.1", "http://localhost")
        ):
            raise ValueError("OpenSearch endpoint must use HTTPS")
        if not index or any(character in index for character in "/?#"):
            raise ValueError("invalid OpenSearch index name")
        self.endpoint = endpoint
        self.index = index
        self.region = region or os.getenv("AWS_REGION", "us-east-1")
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.service = "aoss" if ".aoss." in endpoint else "es"
        self.sign_requests = not endpoint.startswith(
            ("http://127.0.0.1", "http://localhost")
        )
        self.embedding_client = embedding_client

    @classmethod
    def from_environment(cls) -> OpenSearchRetriever | None:
        endpoint = os.getenv("OPENSEARCH_ENDPOINT", "").strip()
        if not endpoint:
            return None
        # Lazy import to keep dependency-free when not using embeddings
        embedding_client = None
        if os.getenv("BEDROCK_EMBEDDING_MODEL_ID") or os.getenv("HYBRID_RETRIEVAL", ""):
            from app.embeddings import BedrockEmbeddingClient
            embedding_client = BedrockEmbeddingClient.from_environment()
        return cls(
            endpoint,
            os.getenv("OPENSEARCH_INDEX", "skillweave-jobs-v1"),
            timeout_seconds=float(os.getenv("OPENSEARCH_TIMEOUT_SECONDS", "2.0")),
            embedding_client=embedding_client,
        )

    @staticmethod
    def _clean_names(values: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    def _signed_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        url = self.endpoint + path
        headers = {"content-type": "application/json"}
        if self.sign_requests:
            try:
                from botocore.auth import SigV4Auth
                from botocore.awsrequest import AWSRequest
                from botocore.session import get_session
            except ImportError as exc:
                raise RuntimeError(
                    "botocore is required for AWS OpenSearch endpoints"
                ) from exc
            credentials = get_session().get_credentials()
            if credentials is None:
                raise RuntimeError("AWS credentials are unavailable for OpenSearch")
            aws_request = AWSRequest(
                method=method,
                url=url,
                data=payload,
                headers=headers,
            )
            SigV4Auth(
                credentials.get_frozen_credentials(),
                self.service,
                self.region,
            ).add_auth(aws_request)
            headers = dict(aws_request.headers.items())
        request = Request(
            url,
            data=payload,
            method=method,
            headers=headers,
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            decoded = json.loads(response.read())
        if not isinstance(decoded, dict):
            raise RuntimeError("OpenSearch returned a non-object response")
        return decoded

    def _build_bm25_query(
        self,
        query: str,
        *,
        locations: list[str],
        duties: list[str],
        wants_remote: bool,
        salary_intent: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build the BM25 keyword query (same as original logic)."""
        should: list[dict[str, Any]] = [
            {
                "match_phrase": {
                    "title": {
                        "query": query,
                        "boost": 8.0,
                    }
                }
            },
            {
                "multi_match": {
                    "query": query,
                    "fields": ["title^7", "categories^4", "skill_labels^4", "description"],
                    "type": "best_fields",
                    "operator": "or",
                    "minimum_should_match": "50%",
                }
            },
        ]
        should.extend(
            {"term": {"city.keyword": {"value": name, "boost": 4.0}}}
            for name in locations
        )
        should.extend(
            {
                "match_phrase": {
                    "categories": {
                        "query": name,
                        "boost": 3.5,
                    }
                }
            }
            for name in duties
        )
        if wants_remote:
            should.append({"term": {"is_remote": {"value": True, "boost": 6.0}}})
        if salary_intent is not None:
            target = float(salary_intent.get("target", 0.0))
            salary_type = salary_intent.get("salary_type")
            if salary_type and target > 0:
                should.append(
                    {
                        "bool": {
                            "filter": [{"term": {"salary_type": salary_type}}],
                            "should": [
                                {
                                    "bool": {
                                        "filter": [
                                            {"range": {"salary_max": {"gt": 0}}},
                                            {"range": {"salary_max": {"gte": target}}},
                                        ]
                                    }
                                },
                                {
                                    "bool": {
                                        "filter": [
                                            {"term": {"salary_max": 0}},
                                            {"range": {"salary_min": {"gte": target}}},
                                        ]
                                    }
                                },
                            ],
                            "minimum_should_match": 1,
                            "boost": 6.0,
                        }
                    }
                )
        return {"bool": {"should": should, "minimum_should_match": 1}}

    def _search_bm25(
        self,
        query: str,
        *,
        limit: int,
        locations: list[str],
        duties: list[str],
        wants_remote: bool,
        salary_intent: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Execute BM25 keyword search."""
        body = {
            "size": limit,
            "track_total_hits": False,
            "_source": self.SOURCE_FIELDS,
            "query": self._build_bm25_query(
                query,
                locations=locations,
                duties=duties,
                wants_remote=wants_remote,
                salary_intent=salary_intent,
            ),
            "sort": [{"_score": "desc"}, {"_id": "asc"}],
        }
        result = self._signed_request(
            "POST",
            f"/{quote(self.index, safe='-_.')}/_search",
            body,
        )
        return result.get("hits", {}).get("hits", [])

    def _search_knn(
        self,
        query_embedding: list[float],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Execute kNN vector search."""
        body = {
            "size": limit,
            "track_total_hits": False,
            "_source": self.SOURCE_FIELDS,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": query_embedding,
                        "k": limit,
                    }
                }
            },
        }
        result = self._signed_request(
            "POST",
            f"/{quote(self.index, safe='-_.')}/_search",
            body,
        )
        return result.get("hits", {}).get("hits", [])

    @staticmethod
    def _rrf_merge(
        bm25_hits: list[dict[str, Any]],
        knn_hits: list[dict[str, Any]],
        *,
        k: int = 60,
        bm25_weight: float = 1.0,
        knn_weight: float = 0.4,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Merge BM25 and kNN results using weighted Reciprocal Rank Fusion.

        RRF score = sum(weight_i / (k + rank_i)) for each result list.
        k=60 is the standard constant that prevents top ranks from dominating.
        BM25 gets higher weight (1.0) vs kNN (0.4) since keyword precision
        is more important for job search; kNN contributes semantic diversity.
        """
        scores: dict[str, float] = {}
        hit_map: dict[str, dict[str, Any]] = {}

        for rank, hit in enumerate(bm25_hits, 1):
            doc_id = hit.get("_id", "")
            scores[doc_id] = scores.get(doc_id, 0.0) + bm25_weight / (k + rank)
            if doc_id not in hit_map:
                hit_map[doc_id] = hit

        for rank, hit in enumerate(knn_hits, 1):
            doc_id = hit.get("_id", "")
            scores[doc_id] = scores.get(doc_id, 0.0) + knn_weight / (k + rank)
            if doc_id not in hit_map:
                hit_map[doc_id] = hit

        ranked = sorted(scores.items(), key=lambda item: -item[1])[:limit]
        return [
            {**hit_map[doc_id], "_rrf_score": rrf_score}
            for doc_id, rrf_score in ranked
            if doc_id in hit_map
        ]

    @staticmethod
    def _bm25_plus_knn_expansion(
        bm25_hits: list[dict[str, Any]],
        knn_hits: list[dict[str, Any]],
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """BM25 results keep original ranking; kNN adds novel candidates at tail.

        This preserves BM25's precision for the top positions while using
        kNN to expand recall with semantically related jobs that BM25 missed.
        """
        result: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        # BM25 results first, in original order
        for hit in bm25_hits:
            doc_id = hit.get("_id", "")
            if doc_id and doc_id not in seen_ids:
                seen_ids.add(doc_id)
                result.append(hit)

        # kNN novel candidates appended at the tail
        for hit in knn_hits:
            if len(result) >= limit:
                break
            doc_id = hit.get("_id", "")
            if doc_id and doc_id not in seen_ids:
                seen_ids.add(doc_id)
                result.append(hit)

        return result[:limit]

    def retrieve(
        self,
        query: str,
        *,
        limit: int,
        location_names: Iterable[str] = (),
        duty_names: Iterable[str] = (),
        wants_remote: bool = False,
        salary_intent: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        locations = self._clean_names(location_names)
        duties = self._clean_names(duty_names)
        effective_limit = max(1, min(int(limit), 500))

        # Always run BM25 keyword search
        bm25_hits = self._search_bm25(
            query,
            limit=effective_limit,
            locations=locations,
            duties=duties,
            wants_remote=wants_remote,
            salary_intent=salary_intent,
        )

        # Attempt kNN vector search if embedding client is available
        knn_hits: list[dict[str, Any]] = []
        if self.embedding_client is not None:
            try:
                query_embedding = self.embedding_client.embed(query)
                if query_embedding is not None:
                    # kNN fetches fewer results (top-50) to contribute semantic
                    # diversity without overwhelming BM25's precise matches.
                    knn_limit = min(50, effective_limit)
                    knn_hits = self._search_knn(
                        query_embedding,
                        limit=knn_limit,
                    )
            except Exception as exc:
                LOGGER.warning(
                    "kNN search failed; using BM25-only: %s", type(exc).__name__
                )

        # Merge results: BM25 results keep original order, kNN adds novel
        # candidates at the tail (expansion-only, does not reorder BM25).
        if knn_hits:
            merged_hits = self._bm25_plus_knn_expansion(
                bm25_hits, knn_hits, limit=effective_limit
            )
        else:
            merged_hits = bm25_hits

        # Parse hits into candidate dicts
        candidates: list[dict[str, Any]] = []
        for hit in merged_hits:
            source = hit.get("_source", {})
            if not isinstance(source, dict):
                continue
            job = dict(source)
            job["id"] = str(job.get("id") or hit.get("_id") or "")
            if not job["id"]:
                continue
            job["_retrieval_score"] = float(
                hit.get("_rrf_score") or hit.get("_score") or 0.0
            )
            candidates.append(job)
        return candidates
