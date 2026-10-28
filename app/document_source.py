"""输入版本固定后再解析，防止同名上传破坏历史审阅证据。"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from app import config
from app.document_schema import DocumentSource, file_hash
from app.index_registry import register_document


def prepare_source(pdf_path: Path, *, doc_id: str | None = None,
                   source_key: str | None = None, source: str | None = None) -> DocumentSource:
    path = Path(pdf_path).resolve()
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError("请提供存在的PDF文件")
    source = source or path.name
    source_key = source_key or os.path.normcase(str(path))
    doc_id = register_document(source_key, source, doc_id)
    # 先快照再计算hash，避免原文件在hash与复制之间改变。
    root = Path(config.DOCUMENT_DIR) / doc_id
    root.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(suffix=".pdf", dir=root)
    os.close(fd)
    tmp = Path(temporary)
    try:
        shutil.copyfile(path, tmp)
        sha = file_hash(tmp)
        destination = root / sha / "source.pdf"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if file_hash(destination) != sha:
                raise ValueError("已保存的PDF原件校验失败")
        else:
            tmp.replace(destination)
        return DocumentSource(doc_id, source, source_key, sha, str(destination))
    finally:
        tmp.unlink(missing_ok=True)
