import pytest

from app import embeddings


class FakeTokenizer:
    def __call__(self, text, **kwargs):
        assert kwargs == {"add_special_tokens": True, "truncation": False}
        return {"input_ids": list(range(len(text) + 2))}


class FakeModel:
    tokenizer = FakeTokenizer()
    max_seq_length = 8

    def encode(self, *args, **kwargs):
        raise AssertionError("超限文本不得进入encode")


def test_embedding_preflight_rejects_truncation_before_encode(monkeypatch):
    monkeypatch.setattr(embeddings, "get_model", lambda: FakeModel())
    monkeypatch.setattr(embeddings.config, "EMBEDDING_MAX_TOKENS", 8, raising=False)
    assert embeddings.count_tokens("abc") == 5
    assert embeddings.get_token_budget() == 8
    with pytest.raises(ValueError, match="tokens=11"):
        embeddings.embed_texts(["123456789"])
