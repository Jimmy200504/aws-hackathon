"""Embedding client for hybrid retrieval.

Wraps Amazon Bedrock Titan Text Embeddings V2 for both index-time and
query-time vector generation. Falls back gracefully when Bedrock is not
configured or temporarily unavailable.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

LOGGER = logging.getLogger(__name__)

# Amazon Titan Text Embeddings V2 produces 1024-dimensional vectors.
EMBEDDING_DIM = 1024
DEFAULT_MODEL_ID = "amazon.titan-embed-text-v2:0"


class BedrockEmbeddingClient:
    """Generate text embeddings using Amazon Bedrock Titan Text Embeddings V2.

    The boto3 import and client construction are lazy so the local demo
    continues to work without Bedrock configured.
    """

    def __init__(
        self,
        model_id: str | None = None,
        *,
        region: str | None = None,
        client: Any = None,
    ) -> None:
        self.model_id = (model_id or "").strip() or None
        self.region = region or os.getenv("AWS_REGION", "us-east-1")
        self._client = client
        self._client_lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> BedrockEmbeddingClient:
        return cls(
            os.getenv("BEDROCK_EMBEDDING_MODEL_ID", DEFAULT_MODEL_ID),
        )

    @property
    def enabled(self) -> bool:
        return self.model_id is not None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                import boto3
                from botocore.config import Config

                self._client = boto3.client(
                    "bedrock-runtime",
                    region_name=self.region,
                    config=Config(
                        connect_timeout=1.0,
                        read_timeout=5.0,
                        retries={"max_attempts": 2, "mode": "standard"},
                    ),
                )
        return self._client

    def embed(self, text: str) -> list[float] | None:
        """Generate an embedding vector for the given text.

        Returns None if embedding is not available or fails.
        """
        if not self.enabled:
            return None
        if not text or not text.strip():
            return None
        # Titan V2 accepts up to 8192 tokens; truncate input text as safety.
        truncated = text[:2048]
        try:
            response = self._get_client().invoke_model(
                modelId=self.model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(
                    {
                        "inputText": truncated,
                        "dimensions": EMBEDDING_DIM,
                        "normalize": True,
                    }
                ),
            )
            result = json.loads(response["body"].read())
            embedding = result.get("embedding")
            if isinstance(embedding, list) and len(embedding) == EMBEDDING_DIM:
                return embedding
            LOGGER.warning("Unexpected embedding response shape")
            return None
        except Exception as exc:
            LOGGER.warning("Bedrock embedding failed: %s", type(exc).__name__)
            return None

    def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        """Generate embeddings for a batch of texts.

        Titan V2 does not support native batching, so this is sequential.
        For index-time bulk usage, consider parallelization at the caller.
        """
        return [self.embed(text) for text in texts]


def job_embedding_text(job: dict[str, Any]) -> str:
    """Compose the text to embed for a job document.

    Combines title + categories + first 200 chars of description for a
    concise, high-signal embedding input.
    """
    title = str(job.get("title") or job.get("職務名稱", "")).strip()
    categories = job.get("categories", [])
    if isinstance(categories, list):
        cat_text = " ".join(categories)
    else:
        cat_text = " ".join(
            str(job.get(k, ""))
            for k in ["職務大類", "職務中類", "職務小類"]
            if job.get(k) and job.get(k) != "NULL"
        )
    description = str(
        job.get("description") or job.get("職務內容", "")
    ).strip()[:200]
    parts = [p for p in [title, cat_text, description] if p]
    return " ".join(parts)
