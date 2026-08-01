from __future__ import annotations

import hashlib
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
        "employment_type",
        "shifts",
        "education",
        "experience",
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
        service: str | None = None,
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
        # Signing with the wrong SigV4 service name yields a 403 that looks
        # exactly like a policy problem, so let the deployment state it
        # explicitly instead of inferring it from the endpoint hostname.
        if service in {"aoss", "es"}:
            self.service = service
        else:
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
            service=os.getenv("OPENSEARCH_SERVICE"),
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
        if self.service == "aoss":
            headers["x-amz-content-sha256"] = hashlib.sha256(payload).hexdigest()
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

    @staticmethod
    def _intent_boosts(intent: Any) -> list[dict[str, Any]]:
        """Turn a normalizer-inferred intent into recall-safe scoring clauses.

        Attribute queries (現領/兼職/晚班) dominate the head of the search log but
        barely occur in job free text; their answer lives in the structured
        `employment_type` / `shifts` fields instead.
        """
        if intent is None:
            return []
        clauses: list[dict[str, Any]] = []
        for name in getattr(intent, "duty_categories", ()) or ():
            clauses.append(
                {"match_phrase": {"categories": {"query": name, "boost": 6.0}}}
            )
        for name in getattr(intent, "employment_types", ()) or ():
            clauses.append({"term": {"employment_type": {"value": name, "boost": 2.5}}})
        for name in getattr(intent, "shifts", ()) or ():
            clauses.append({"term": {"shifts": {"value": name, "boost": 2.0}}})
        for name in getattr(intent, "locations", ()) or ():
            clauses.append({"term": {"city.keyword": {"value": name, "boost": 3.0}}})
        salary_type = getattr(intent, "salary_type", None)
        if salary_type:
            clauses.append({"term": {"salary_type": {"value": salary_type, "boost": 1.5}}})
        company = getattr(intent, "company", None)
        if company:
            clauses.append(
                {"match_phrase": {"description": {"query": company, "boost": 3.0}}}
            )
            clauses.append({"match_phrase": {"title": {"query": company, "boost": 4.0}}})
        return clauses

    def _build_bm25_query(
        self,
        query: str,
        *,
        locations: list[str],
        duties: list[str],
        wants_remote: bool,
        salary_intent: dict[str, Any] | None = None,
        intent: Any = None,
    ) -> dict[str, Any]:
        """Build a BM25 query with explicit filters and inferred-intent boosts."""
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
            # A remote-intent query (遠端/在家工作/WFH) must be able to pull
            # in an is_remote job even when the query text has no lexical
            # overlap with the title/description (e.g. 遠端客服 vs a job
            # titled "夜間客服人員" that only mentions "可遠端" in the body).
            # This is a `should` clause, not a `must` filter: it widens
            # recall without excluding otherwise-relevant lexical matches.
            should.append({"term": {"is_remote": {"value": True, "boost": 6.0}}})
        if salary_intent is not None:
            # Mirror the local ranker's salary-range match: a job whose
            # salary_min/salary_max range covers the requested figure is
            # relevant even if its title never prints that exact number
            # (e.g. 168+ jobs with a covering range but no literal "210" in
            # the title -- the recall gap fixed in app/ranker.py). Gate on
            # salary_type so an hourly query does not match a monthly job.
            target = float(salary_intent.get("target", 0.0))
            salary_type = salary_intent.get("salary_type")
            if salary_type and target > 0:
                should.append(
                    {
                        "bool": {
                            "filter": [{"term": {"salary_type": salary_type}}],
                            "should": [
                                # salary_max > 0 means an explicit upper bound
                                # is set; the job's range covers the target
                                # when that ceiling reaches at least target.
                                {
                                    "bool": {
                                        "filter": [
                                            {"range": {"salary_max": {"gt": 0}}},
                                            {"range": {"salary_max": {"gte": target}}},
                                        ]
                                    }
                                },
                                # salary_max == 0 means no upper bound was
                                # parsed (e.g. "面議40000‧" or open-ended pay);
                                # fall back to comparing salary_min alone.
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
        should.extend(self._intent_boosts(intent))
        filters = [{"terms": {"city.keyword": locations}}] if locations else []
        return {
            "bool": {
                "should": should,
                "minimum_should_match": 1,
                **({"filter": filters} if filters else {}),
            }
        }

    def _search_bm25(
        self,
        query: str,
        *,
        limit: int,
        locations: list[str],
        duties: list[str],
        wants_remote: bool,
        salary_intent: dict[str, Any] | None,
        intent: Any = None,
    ) -> list[dict[str, Any]]:
        """Execute BM25 keyword search with a recall-safe filter fallback."""
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
                intent=intent,
            ),
            "sort": [{"_score": "desc"}, {"_id": "asc"}],
        }
        result = self._signed_request(
            "POST",
            f"/{quote(self.index, safe='-_.')}/_search",
            body,
        )
        hits = result.get("hits", {}).get("hits", [])
        query_body = body["query"]
        if (
            locations
            and isinstance(hits, list)
            and not hits
            and isinstance(query_body, dict)
        ):
            bool_query = query_body.get("bool", {})
            if isinstance(bool_query, dict):
                bool_query.pop("filter", None)
            result = self._signed_request(
                "POST",
                f"/{quote(self.index, safe='-_.')}/_search",
                body,
            )
            hits = result.get("hits", {}).get("hits", [])
        if not isinstance(hits, list):
            raise RuntimeError("OpenSearch hits are malformed")
        return hits

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
        intent: Any = None,
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
            intent=intent,
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
