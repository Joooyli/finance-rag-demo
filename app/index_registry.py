"""应用自己的文档/索引登记表；绝不编辑Chroma内部SQLite。"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from app import config

_writer_lock = threading.RLock()


@contextmanager
def _connect():
    path = Path(config.INDEX_REGISTRY_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS documents(
            doc_id TEXT PRIMARY KEY, source_key TEXT UNIQUE NOT NULL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS indexes(
            index_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, run_id TEXT NOT NULL,
            collection TEXT NOT NULL, signature TEXT NOT NULL, ids_json TEXT NOT NULL,
            state TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS active(
            doc_id TEXT NOT NULL, collection TEXT NOT NULL, index_id TEXT NOT NULL,
            PRIMARY KEY(doc_id, collection));
    """)
    try:
        with db:
            yield db
    finally:
        db.close()


def register_document(source_key: str, source: str, doc_id: str | None = None) -> str:
    with _connect() as db:
        if doc_id:
            row = db.execute("SELECT doc_id FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
            if row is None:
                raise ValueError("指定doc_id不存在；新增上传请省略doc_id")
            return row[0]
        row = db.execute("SELECT doc_id FROM documents WHERE source_key=?", (source_key,)).fetchone()
        if row:
            return row[0]
        identity = uuid.uuid4().hex
        db.execute("INSERT INTO documents VALUES(?,?,?)", (identity, source_key, source))
        return identity


@contextmanager
def index_writer_lock():
    """线程与CLI进程共用文件锁；激活前串行写入，查询仍可读取旧快照。"""
    with _writer_lock:
        lock_path = Path(str(config.INDEX_REGISTRY_PATH) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def stage_index(index_id: str, doc_id: str, run_id: str, signature: str, ids: list[str]) -> None:
    with _connect() as db:
        existing = db.execute("SELECT * FROM indexes WHERE index_id=?", (index_id,)).fetchone()
        if existing and existing["state"] == "active":
            if json.loads(existing["ids_json"]) != ids:
                raise ValueError("同一索引ID的成员清单发生变化")
            return
        db.execute("INSERT OR REPLACE INTO indexes(index_id,doc_id,run_id,collection,signature,ids_json,state) VALUES(?,?,?,?,?,?,?)",
                   (index_id, doc_id, run_id, config.COLLECTION_NAME, signature, json.dumps(ids), "staged"))


def activate_index(index_id: str) -> None:
    # SQLite事务只切换活动映射；所有向量必须已在外部完成读回核验。
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        record = db.execute("SELECT * FROM indexes WHERE index_id=?", (index_id,)).fetchone()
        if not record:
            raise ValueError("索引快照不存在")
        signatures = db.execute("SELECT DISTINCT i.signature FROM active a JOIN indexes i ON a.index_id=i.index_id WHERE a.collection=?", (record["collection"],)).fetchall()
        if any(row[0] != record["signature"] for row in signatures):
            raise ValueError("Embedding身份改变，须在新集合完整重建，不能混合激活")
        old = db.execute("SELECT index_id FROM active WHERE doc_id=? AND collection=?", (record["doc_id"], record["collection"])).fetchone()
        if old and old[0] != index_id:
            db.execute("UPDATE indexes SET state='superseded' WHERE index_id=?", (old[0],))
        db.execute("INSERT OR REPLACE INTO active VALUES(?,?,?)", (record["doc_id"], record["collection"], index_id))
        db.execute("UPDATE indexes SET state='active' WHERE index_id=?", (index_id,))


def get_active_snapshot(collection: str | None = None) -> list[dict]:
    if not Path(config.INDEX_REGISTRY_PATH).exists():
        return []
    with _connect() as db:
        rows = db.execute("SELECT i.* FROM active a JOIN indexes i ON a.index_id=i.index_id WHERE a.collection=?", (collection or config.COLLECTION_NAME,)).fetchall()
        return [dict(row) for row in rows]


def referenced_runs() -> set[str]:
    if not Path(config.INDEX_REGISTRY_PATH).exists():
        return set()
    with _connect() as db:
        # 保留快照亦可回滚，因此清日志时保护所有已登记的引用。
        return {row[0] for row in db.execute("SELECT run_id FROM indexes")}


def remove_index_references(collection: str, *, source: str | None = None, doc_id: str | None = None,
                            index_id: str | None = None) -> None:
    with _connect() as db:
        clause, params = "collection=?", [collection]
        if index_id:
            clause += " AND index_id=?"
            params.append(index_id)
        elif doc_id:
            clause += " AND doc_id=?"
            params.append(doc_id)
        elif source:
            clause += " AND doc_id IN (SELECT doc_id FROM documents WHERE source=?)"
            params.append(source)
        ids = [row[0] for row in db.execute("SELECT index_id FROM indexes WHERE " + clause, params)]
        for identity in ids:
            db.execute("DELETE FROM active WHERE index_id=?", (identity,))
            db.execute("DELETE FROM indexes WHERE index_id=?", (identity,))
