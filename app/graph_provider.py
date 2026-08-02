"""Online alias resolution and one-hop Neptune Analytics graph features."""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.retrieval import OpenSearchRetriever

LOGGER = logging.getLogger(__name__)
_SPACE = re.compile(r"\s+")
_ASCII_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.+#")


def _normalize_surface(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower().strip()
    normalized = normalized.replace("臺", "台")
    return _SPACE.sub(" ", normalized)


def _surface_in_query(query: str, surface: str) -> bool:
    if not surface:
        return False
    start = 0
    while (index := query.find(surface, start)) >= 0:
        end = index + len(surface)
        if surface.isascii():
            left = query[index - 1] if index else ""
            right = query[end] if end < len(query) else ""
            if (left and left in _ASCII_TOKEN_CHARS) or (
                right and right in _ASCII_TOKEN_CHARS
            ):
                start = index + 1
                continue
        return True
    return False


@dataclass
class GraphExpansion:
    canonical_ids: tuple[str, ...] = ()
    relations: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    backend: str = "disabled"
    version: str = ""
    degraded_components: list[str] = field(default_factory=list)


class AliasIndexResolver(OpenSearchRetriever):
    def resolve(self, query: str, limit: int = 8) -> tuple[str, ...]:
        normalized_query = _normalize_surface(query)
        result = self._signed_request(
            "POST",
            f"/{self.index}/_search",
            {
                "size": 50,
                "_source": [
                    "canonical_ids",
                    "canonical_id",
                    "normalized_surface",
                    "blocked_phrases",
                ],
                "query": {
                    "match": {
                        "normalized_surface": {
                            "query": normalized_query,
                            "operator": "or",
                        }
                    }
                }
            },
        )
        candidates: list[tuple[int, str]] = []
        for hit in result.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            surface = _normalize_surface(str(source.get("normalized_surface", "")))
            blocked = [
                _normalize_surface(str(phrase))
                for phrase in source.get("blocked_phrases", [])
            ]
            if (
                not _surface_in_query(normalized_query, surface)
                or any(phrase in normalized_query for phrase in blocked if phrase)
            ):
                continue
            values = source.get("canonical_ids", source.get("canonical_id", []))
            if isinstance(values, str):
                values = [values]
            for value in values if isinstance(values, list) else []:
                if value:
                    candidates.append((len(surface), str(value)))
        resolved: list[str] = []
        for _, canonical_id in sorted(candidates, reverse=True):
            if canonical_id not in resolved:
                resolved.append(canonical_id)
        return tuple(resolved[:limit])


class GraphFeatureProvider:
    QUERY = """
    MATCH (source) WHERE id(source) IN $skill_ids
    MATCH (source)-[edge:RELATED_TO]-(target)
    RETURN id(source) AS source_id, source.label AS source_label,
           id(target) AS target_id, target.label AS target_label,
           id(edge) AS edge_id,
           type(edge) AS relation_type, edge.weight AS weight,
           edge.confidence AS confidence, edge.support_jobs AS support_jobs,
           edge.support_companies AS support_companies, edge.evidence AS evidence,
           edge.rules_version AS rules_version, edge.corpus_hash AS corpus_hash,
           edge.provenance AS provenance
    ORDER BY source_id, weight DESC, target_id LIMIT 160
    """.strip()

    def __init__(
        self,
        graph_id: str,
        *,
        region: str = "ap-northeast-1",
        graph_version: str = "",
        alias_resolver: AliasIndexResolver | None = None,
        client: Any = None,
        timeout_ms: int = 150,
    ) -> None:
        self.graph_id = graph_id
        self.region = region
        self.graph_version = graph_version
        self.alias_resolver = alias_resolver
        self.timeout_ms = max(1, int(timeout_ms))
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "neptune-graph",
                region_name=region,
                config=Config(
                    connect_timeout=self.timeout_ms / 1000,
                    read_timeout=self.timeout_ms / 1000,
                    retries={"max_attempts": 0},
                ),
            )
        self.client = client

    @classmethod
    def from_environment(cls) -> "GraphFeatureProvider | None":
        graph_id = os.getenv("NEPTUNE_GRAPH_ID", "").strip()
        if not graph_id:
            return None
        endpoint = os.getenv("OPENSEARCH_ENDPOINT", "").strip()
        alias_index = os.getenv("SKILL_ALIAS_INDEX", "").strip()
        aws_region = os.getenv("AWS_REGION", "ap-northeast-1")
        graph_region = os.getenv("NEPTUNE_GRAPH_REGION", aws_region)
        timeout_ms = int(os.getenv("GRAPH_QUERY_TIMEOUT_MS", "150"))
        alias_resolver = None
        if endpoint and alias_index:
            alias_resolver = AliasIndexResolver(
                endpoint,
                alias_index,
                region=aws_region,
                timeout_seconds=timeout_ms / 1000,
            )
        return cls(
            graph_id,
            region=graph_region,
            graph_version=os.getenv("GRAPH_VERSION", ""),
            alias_resolver=alias_resolver,
            timeout_ms=timeout_ms,
        )

    @staticmethod
    def _rows(response: dict[str, Any]) -> list[dict[str, Any]]:
        payload = response.get("payload", response.get("results", []))
        if hasattr(payload, "read"):
            payload = payload.read()
        if isinstance(payload, bytes):
            payload = payload.decode()
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(payload, dict):
            if "results" not in payload and "data" not in payload:
                raise RuntimeError("Neptune returned malformed rows")
            payload = payload.get("results", payload.get("data"))
        if not isinstance(payload, list):
            raise RuntimeError("Neptune returned malformed rows")
        return [row for row in payload if isinstance(row, dict)]

    @staticmethod
    def _decode_json_property(value: Any, fallback: Any) -> Any:
        if not isinstance(value, str):
            return value if value is not None else fallback
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value

    def expand(self, query: str, fallback_ids: Iterable[str] = ()) -> GraphExpansion:
        degraded: list[str] = []
        canonical: tuple[str, ...] = ()
        if self.alias_resolver:
            try:
                canonical = self.alias_resolver.resolve(query)
            except Exception as exc:
                LOGGER.warning(
                    "Skill alias lookup failed safely: %s", type(exc).__name__
                )
                degraded.append("skill_alias_index")
        try:
            ids = tuple(dict.fromkeys((*canonical, *fallback_ids)))[:8]
            if not ids:
                return GraphExpansion(
                    backend="neptune_analytics",
                    version=self.graph_version,
                    degraded_components=degraded,
                )
            response = self.client.execute_query(
                graphIdentifier=self.graph_id,
                queryString=self.QUERY,
                language="OPEN_CYPHER",
                parameters={"skill_ids": list(ids)},
                planCache="ENABLED",
            )
            relations: dict[str, dict[str, dict[str, Any]]] = {}
            for row in self._rows(response):
                source = str(row.get("source_id", ""))
                target = str(row.get("target_id", ""))
                if not source or not target:
                    continue
                relations.setdefault(source, {})[target] = {
                    "source_label": str(row.get("source_label", "")),
                    "target_label": str(row.get("target_label", "")),
                    "edge_id": str(row.get("edge_id", "")),
                    "relation_type": str(row.get("relation_type", "RELATED_TO")),
                    "weight": float(row.get("weight", 0)),
                    "confidence": float(row.get("confidence", 0)),
                    "support_jobs": int(row.get("support_jobs", 0)),
                    "support_companies": int(row.get("support_companies", 0)),
                    "evidence": self._decode_json_property(
                        row.get("evidence"), []
                    ),
                    "rules_version": str(row.get("rules_version", "")),
                    "corpus_hash": str(row.get("corpus_hash", "")),
                    "provenance": self._decode_json_property(
                        row.get("provenance"), {}
                    ),
                }
            return GraphExpansion(
                canonical_ids=ids,
                relations=relations,
                backend="neptune_analytics",
                version=self.graph_version,
                degraded_components=degraded,
            )
        except Exception as exc:
            LOGGER.warning(
                "Neptune graph expansion failed safely: %s",
                type(exc).__name__,
            )
            return GraphExpansion(
                canonical_ids=tuple(fallback_ids),
                backend="neptune_analytics",
                version=self.graph_version,
                degraded_components=[*degraded, "neptune"],
            )
