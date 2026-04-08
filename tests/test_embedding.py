"""
Tests for embedding.py — uses mocked Bedrock client.
"""
import json
import pytest
from unittest.mock import MagicMock, patch
from io import BytesIO

from embedding import EmbeddingClient


def _make_embed_response(dims=1024):
    """Create a mock Bedrock InvokeModel response."""
    embedding = [0.1] * dims
    body = json.dumps({"embedding": embedding}).encode()
    return {"body": BytesIO(body)}


class TestEmbeddingClient:
    def setup_method(self):
        self.client = EmbeddingClient()
        self.client._client = MagicMock()

    def test_embed_text_success(self):
        """embed_text returns a 1024-dim vector on success."""
        self.client._client.invoke_model.return_value = _make_embed_response(1024)
        result = self.client.embed_text("hello world")
        assert result is not None
        assert len(result) == 1024
        assert all(isinstance(v, float) for v in result)

    def test_embed_text_empty(self):
        """Empty text returns None."""
        result = self.client.embed_text("")
        assert result is None
        result = self.client.embed_text("   ")
        assert result is None

    def test_embed_text_truncation(self):
        """Long text is truncated to 30000 chars."""
        self.client._client.invoke_model.return_value = _make_embed_response()
        long_text = "x" * 50000
        result = self.client.embed_text(long_text)
        assert result is not None
        # Verify the call was made with truncated text
        call_body = json.loads(
            self.client._client.invoke_model.call_args.kwargs.get("body")
            or self.client._client.invoke_model.call_args[1].get("body")
        )
        assert len(call_body["inputText"]) == 30000

    def test_embed_text_retry_on_throttle(self):
        """Retries on ThrottlingException."""
        from botocore.exceptions import ClientError

        throttle_error = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "InvokeModel",
        )
        self.client._client.invoke_model.side_effect = [
            throttle_error,
            _make_embed_response(),
        ]

        with patch("time.sleep"):  # Don't actually sleep
            result = self.client.embed_text("test", max_retries=2)

        assert result is not None
        assert len(result) == 1024
        assert self.client._client.invoke_model.call_count == 2

    def test_embed_text_permanent_failure(self):
        """Returns None on non-retryable error."""
        from botocore.exceptions import ClientError

        error = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "No access"}},
            "InvokeModel",
        )
        self.client._client.invoke_model.side_effect = error
        result = self.client.embed_text("test")
        assert result is None

    def test_embed_batch(self):
        """embed_batch returns a list matching input length."""
        self.client._client.invoke_model.side_effect = [
            _make_embed_response() for _ in range(3)
        ]
        results = self.client.embed_batch(["a", "b", "c"])
        assert len(results) == 3
        assert all(r is not None and len(r) == 1024 for r in results)

    def test_cosine_similarity(self):
        """cosine_similarity computes correctly."""
        vec = [1.0, 0.0, 0.0]
        assert abs(self.client.cosine_similarity(vec, vec) - 1.0) < 0.001

        vec_a = [1.0, 0.0]
        vec_b = [0.0, 1.0]
        assert abs(self.client.cosine_similarity(vec_a, vec_b)) < 0.001

    def test_cosine_similarity_zero_vector(self):
        """Zero vector returns 0."""
        assert self.client.cosine_similarity([0, 0], [1, 1]) == 0.0
