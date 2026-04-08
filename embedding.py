"""
OpenClaw Memory System v2 — Bedrock Embedding
==============================================
Wraps Amazon Bedrock Titan Embed Text V2 for generating
1024-dim embeddings. Supports single and batch embedding.
"""

import json
import logging
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from config import (
    EMBED_MODEL_ID,
    EMBED_DIMENSIONS,
    BEDROCK_REGION,
)

logger = logging.getLogger(__name__)


class EmbeddingClient:
    """
    Generates text embeddings via Amazon Bedrock Titan Embed V2.
    Supports single and batch requests with retry logic.
    """

    def __init__(
        self,
        model_id: str = EMBED_MODEL_ID,
        dimensions: int = EMBED_DIMENSIONS,
        region: str = BEDROCK_REGION,
    ):
        self.model_id = model_id
        self.dimensions = dimensions
        self.region = region
        self._client = None

    @property
    def client(self):
        """Lazy-init Bedrock Runtime client."""
        if self._client is None:
            self._client = boto3.client(
                "bedrock-runtime",
                region_name=self.region,
            )
            logger.info("Bedrock Runtime client created (region=%s)", self.region)
        return self._client

    def embed_text(self, text: str, max_retries: int = 3) -> Optional[list[float]]:
        """
        Generate embedding for a single text.
        
        Args:
            text: Input text to embed.
            max_retries: Number of retries on transient failures.
        
        Returns:
            List of floats (1024-dim vector), or None on failure.
        """
        if not text or not text.strip():
            logger.warning("Empty text passed to embed_text, returning None")
            return None

        # Titan Embed V2 has a 8192 token limit; truncate long text
        # Rough estimate: 1 token ≈ 4 chars for English, 1-2 for CJK
        # Use 30000 chars as safe limit
        if len(text) > 30000:
            text = text[:30000]
            logger.warning("Text truncated to 30000 chars for embedding")

        for attempt in range(max_retries):
            try:
                body = json.dumps({
                    "inputText": text,
                    "dimensions": self.dimensions,
                    "normalize": True,
                })

                resp = self.client.invoke_model(
                    modelId=self.model_id,
                    body=body,
                    contentType="application/json",
                    accept="application/json",
                )

                result = json.loads(resp["body"].read())
                embedding = result.get("embedding")

                if embedding and len(embedding) == self.dimensions:
                    return embedding
                else:
                    logger.error(
                        "Unexpected embedding shape: got %d, expected %d",
                        len(embedding) if embedding else 0,
                        self.dimensions,
                    )
                    return None

            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                if error_code in ("ThrottlingException", "ServiceUnavailableException"):
                    delay = (2 ** attempt) * 1.0
                    logger.warning(
                        "Bedrock embed throttled (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1, max_retries, delay, e,
                    )
                    time.sleep(delay)
                    continue
                else:
                    logger.error("Bedrock embed failed with %s: %s", error_code, e)
                    return None
            except Exception as e:
                logger.error("Unexpected error in embed_text: %s", e)
                return None

        logger.error("embed_text failed after %d retries", max_retries)
        return None

    def embed_batch(
        self,
        texts: list[str],
        max_retries: int = 3,
    ) -> list[Optional[list[float]]]:
        """
        Generate embeddings for a batch of texts.
        
        Titan Embed V2 doesn't support native batch API, so we call
        sequentially but could be parallelized if needed.
        
        Args:
            texts: List of input texts.
            max_retries: Retries per text.
        
        Returns:
            List of embeddings (or None for failures), same order as input.
        """
        results: list[Optional[list[float]]] = []
        for i, text in enumerate(texts):
            embedding = self.embed_text(text, max_retries=max_retries)
            results.append(embedding)
            if embedding is None:
                logger.warning("Failed to embed text %d/%d", i + 1, len(texts))
        return results

    def cosine_similarity(self, vec_a: list[float], vec_b: list[float]) -> float:
        """
        Compute cosine similarity between two vectors.
        Useful for MMR and dedup checks.
        """
        if len(vec_a) != len(vec_b):
            raise ValueError(
                f"Vector dimension mismatch: {len(vec_a)} vs {len(vec_b)}"
            )
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = sum(a * a for a in vec_a) ** 0.5
        norm_b = sum(b * b for b in vec_b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
