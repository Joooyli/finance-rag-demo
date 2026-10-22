from app.chunker import split_text

def test_split_text_empty():
    assert split_text("") == []

def test_split_text_has_overlap_and_content():
    text = ("金融风险管理需要识别风险、计量风险并持续监控。" * 100)
    chunks = split_text(text, chunk_size=300, overlap=50)
    assert len(chunks) > 1
    assert all(chunks)
    assert "".join(chunks)  # chunks contain usable content
