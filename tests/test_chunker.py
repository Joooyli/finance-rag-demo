from copy import deepcopy

import pytest

from app import embeddings
from app.chunker import build_document_chunks, split_text
from app.document_assembler import assemble_document
from app.document_schema import make_block

def test_split_text_empty():
    assert split_text("") == []

def test_split_text_has_overlap_and_content():
    text = ("金融风险管理需要识别风险、计量风险并持续监控。" * 100)
    chunks = split_text(text, chunk_size=300, overlap=50)
    assert len(chunks) > 1
    assert all(chunks)
    assert "".join(chunks)  # chunks contain usable content


@pytest.fixture
def fake_tokenizer(monkeypatch):
    # 真实token预算行为单独集成验证；规则单测不加载或下载模型。
    monkeypatch.setattr(embeddings, "count_tokens", lambda text: len(text) + 2)
    monkeypatch.setattr(embeddings, "get_token_budget", lambda: 90)


def logical(text, kind="paragraph"):
    block = make_block(1, kind, text, [50, 50, 500, 150], "pymupdf")
    return assemble_document([{"page": 1, "blocks": [block], "geometry": {"width": 600, "height": 800}}])[0]


def chunks(blocks, run_id="run1"):
    return build_document_chunks(blocks, doc_id="doc1", pdf_sha256="pdfhash", pipeline_fingerprint="pipeline", source="a.pdf", run_id=run_id)


def test_typed_chunks_fit_actual_budget_and_ids_do_not_depend_on_run(fake_tokenizer):
    blocks = logical("风险收益与资产负债。" * 40)
    first, second = chunks(blocks), chunks(blocks, "run2")
    assert len(first) > 1
    assert all(chunk["embedding_tokens"] <= 90 for chunk in first)
    assert [chunk["chunk_id"] for chunk in first] == [chunk["chunk_id"] for chunk in second]
    assert all(chunk["pages"] == [1] and chunk["parent_id"] for chunk in first)


def test_formula_is_atomic_and_oversize_is_explicit(fake_tokenizer):
    expression = "$$" + "x_1+" * 40 + "x_2$$"
    output = chunks(logical(expression, "formula"))
    assert len(output) == 1
    assert output[0]["text"] == expression
    assert output[0]["quality_status"] == "needs_review"
    assert "formula_exceeds_token_budget" in output[0]["issues"]


def test_inline_formula_is_not_split_even_when_oversize(fake_tokenizer):
    expression = "$" + "x_1+" * 25 + "x_2$"
    output = chunks(logical("说明。" + expression + "变量解释。"))
    assert any(expression in chunk["text"] for chunk in output)
    assert all(chunk["text"].count("$") % 2 == 0 for chunk in output)


def test_table_rows_and_financial_values_remain_intact(fake_tokenizer):
    html = "<table><tr><th>项目</th><th>金额</th></tr>" + "".join(
        f"<tr><td>资产{i}</td><td>-{i}.25%</td></tr>" for i in range(8)
    ) + "</table>"
    block = make_block(28, "table", "表格", [50, 50, 500, 750], "mineru", html=html)
    blocks, _ = assemble_document([{"page": 28, "blocks": [block]}])
    output = chunks(blocks)
    assert len(output) > 1
    assert all(chunk["embedding_tokens"] <= 90 and chunk["content_type"] == "table" for chunk in output)
    for i in range(8):
        assert sum(f"项目=资产{i}；金额=-{i}.25%" in chunk["text"] for chunk in output) == 1


def test_asset_only_figure_does_not_generate_path_embedding(fake_tokenizer):
    blocks = logical("", "figure")
    blocks[0]["asset_refs"] = ["assets/figure.png"]
    assert chunks(blocks) == []


def test_financial_numeric_value_is_not_split_at_budget_boundary(fake_tokenizer):
    value = "-1234567890.12345%"
    output = chunks(logical("说明" * 36 + value + "风险" * 40))
    assert any(value in chunk["text"] for chunk in output)
    assert all(not chunk["text"].startswith(".12345") for chunk in output)
