from __future__ import annotations

import json
import os
from typing import Any, Iterable
from urllib.parse import quote
from urllib.request import Request, urlopen


class OpenSearchRetriever:
    """Retrieve full-corpus job candidates from an IAM-protected OpenSearch index.

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

    @classmethod
    def from_environment(cls) -> OpenSearchRetriever | None:
        endpoint = os.getenv("OPENSEARCH_ENDPOINT", "").strip()
        if not endpoint:
            return None
        return cls(
            endpoint,
            os.getenv("OPENSEARCH_INDEX", "skillweave-jobs-v1"),
            timeout_seconds=float(os.getenv("OPENSEARCH_TIMEOUT_SECONDS", "2.0")),
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
        """Retrieve candidates for one query.

        `location_names` / `duty_names` come from the caller's explicit `c0` /
        `d0` codes and become filters, because the ranker penalises a mismatch
        by -16 and would otherwise discard most of an unfiltered BM25 page.
        Anything inferred by the query normalizer stays a boost: an inferred
        constraint must never be able to empty the result set.
        """
        locations = self._clean_names(location_names)
        duties = self._clean_names(duty_names)
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
        body = {
            "size": max(1, min(int(limit), 500)),
            "track_total_hits": False,
            "_source": self.SOURCE_FIELDS,
            "query": {
                "bool": {
                    "should": should,
                    "minimum_should_match": 1,
                    **({"filter": filters} if filters else {}),
                }
            },
            "sort": [{"_score": "desc"}, {"_id": "asc"}],
        }
        path = f"/{quote(self.index, safe='-_.')}/_search"
        result = self._signed_request("POST", path, body)
        hits = result.get("hits", {}).get("hits", [])
        if filters and isinstance(hits, list) and not hits:
            # A city filter that matches nothing usually means the caller's code
            # table and the indexed city string disagree. Losing the constraint
            # beats returning an empty page to the judge.
            body["query"]["bool"].pop("filter")
            result = self._signed_request("POST", path, body)
            hits = result.get("hits", {}).get("hits", [])
        if not isinstance(hits, list):
            raise RuntimeError("OpenSearch hits are malformed")
        candidates: list[dict[str, Any]] = []
        for hit in hits:
            source = hit.get("_source", {})
            if not isinstance(source, dict):
                continue
            job = dict(source)
            job["id"] = str(job.get("id") or hit.get("_id") or "")
            if not job["id"]:
                continue
            job["_retrieval_score"] = float(hit.get("_score") or 0.0)
            candidates.append(job)
        return candidates
