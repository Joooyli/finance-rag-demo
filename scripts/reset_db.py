"""Inspect and delete local RAG data: Chroma collections/chunks and parse logs.

Destructive actions require ``--yes``. Without it the script only prints what it
would delete. Stop the API (uvicorn) first on Windows: the SQLite database may be
locked while the server is running.

Examples
--------
python -m scripts.reset_db --list
python -m scripts.reset_db --drop-collection finance_pdf_chunks --yes
python -m scripts.reset_db --delete-source frm_notes.pdf --yes
python -m scripts.reset_db --wipe --yes
python -m scripts.reset_db --purge-parse-logs --yes
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

import chromadb
from chromadb.config import Settings

from app import config
from app.index_registry import index_writer_lock, referenced_runs, remove_index_references
from app.parse_log import resolve_run_dir


def get_client() -> chromadb.PersistentClient:
    return chromadb.PersistentClient(
        path=str(config.CHROMA_DIR),
        settings=Settings(anonymized_telemetry=False),
    )


def collection_names(client) -> list[str]:
    """Chroma 0.6.x returns collection names (a str subclass), not Collection objects."""
    names: list[str] = []
    for item in client.list_collections():
        names.append(item if isinstance(item, str) else str(item))
    return names


def collection_info(client) -> list[tuple[str, int]]:
    infos: list[tuple[str, int]] = []
    for name in collection_names(client):
        try:
            count = client.get_collection(name).count()
        except Exception:
            count = -1
        infos.append((name, count))
    return sorted(infos)


def list_everything() -> int:
    print(f"数据库目录      : {config.CHROMA_DIR}")
    print(f"当前配置集合    : {config.COLLECTION_NAME}")
    print(f"解析日志目录    : {config.PARSE_LOG_DIR}")
    print(f"入库开关        : ENABLE_INDEXING={config.ENABLE_INDEXING}")
    print()

    if not config.CHROMA_DIR.exists():
        print("数据库目录不存在，尚未建库。")
    else:
        try:
            infos = collection_info(get_client())
        except Exception as exc:
            print(f"无法读取数据库（服务可能仍在占用）：{exc}")
            infos = []
        if not infos:
            print("数据库中没有集合。")
        else:
            print("集合：")
            for name, count in infos:
                marker = " <- 当前配置" if name == config.COLLECTION_NAME else ""
                print(f"  - {name}: {count} 个切块{marker}")

    runs = sorted(config.PARSE_LOG_DIR.glob("*/")) if config.PARSE_LOG_DIR.exists() else []
    print()
    print(f"解析记录：{len(runs)} 次")
    for run_dir in runs[:10]:
        print(f"  - {run_dir.name}")
    if len(runs) > 10:
        print(f"  ... 其余 {len(runs) - 10} 次")
    return 0


def confirm(action: str, details: list[str], assume_yes: bool) -> bool:
    print(f"\n计划执行：{action}")
    for line in details:
        print(f"  {line}")
    if assume_yes:
        return True
    print("\n未执行。确认无误后追加 --yes 重新运行。")
    return False


def drop_collection(name: str, assume_yes: bool) -> int:
    client = get_client()
    existing = set(collection_names(client))
    if name not in existing:
        print(f"集合不存在：{name}")
        return 1
    count = client.get_collection(name).count()
    if not confirm(
        f"删除整个集合 {name}",
        [f"集合 {name} 的 {count} 个切块将被删除", "PDF 原文件不会被删除"],
        assume_yes,
    ):
        return 0
    # 与入库共用锁；实际向量删除成功后才移除登记引用，失败时继续保护证据。
    with index_writer_lock():
        client.delete_collection(name)
        remove_index_references(name)
    print(f"已删除集合：{name}")
    return 0


def delete_source(source: str, collection_name: str, assume_yes: bool) -> int:
    client = get_client()
    try:
        collection = client.get_collection(collection_name)
    except Exception:
        print(f"集合不存在：{collection_name}")
        return 1

    before = collection.count()
    matches = collection.get(where={"source": source}, include=["metadatas"])["ids"]
    if not confirm(
        f"按来源删除 {source}",
        [
            f"集合               : {collection_name}",
            f"匹配切块数         : {len(matches)}",
            f"删除前集合总数     : {before}",
            f"删除后预计剩余     : {before - len(matches)}",
            "仅删除该文件名的切块，其他文件不受影响",
        ],
        assume_yes,
    ):
        return 0
    if not matches:
        print("没有匹配的切块，无需删除。")
        return 0
    with index_writer_lock():
        collection.delete(where={"source": source})
        remove_index_references(collection_name, source=source)
    print(f"已删除 {len(matches)} 个切块，集合现有 {collection.count()} 个。")
    return 0


def wipe_collection(collection_name: str, assume_yes: bool) -> int:
    client = get_client()
    try:
        collection = client.get_collection(collection_name)
    except Exception:
        print(f"集合不存在：{collection_name}")
        return 1

    total = collection.count()
    if not confirm(
        f"清空集合 {collection_name}",
        [f"集合 {collection_name} 的 {total} 个切块将被删除", "集合本身保留，PDF 原文件保留"],
        assume_yes,
    ):
        return 0

    removed = 0
    with index_writer_lock():
        while True:
            ids = collection.get(limit=1000, include=["metadatas"])["ids"]
            if not ids:
                break
            collection.delete(ids=ids)
            removed += len(ids)
        remove_index_references(collection_name)
    print(f"已清空集合 {collection_name}：删除 {removed} 个切块，剩余 {collection.count()} 个。")
    return 0


def purge_parse_logs(assume_yes: bool) -> int:
    root = config.PARSE_LOG_DIR
    if not root.exists():
        print(f"解析日志目录不存在：{root}")
        return 0
    protected = referenced_runs()
    runs = [path for path in root.iterdir() if path.is_dir() and path.name not in protected]
    if protected:
        print(f"保留已登记索引引用的解析记录：{sorted(protected)}")
    if not runs:
        print("没有解析记录可删除。")
        return 0
    if not confirm(
        "删除未被索引引用的解析日志",
        [f"目录：{root}", f"将删除 {len(runs)} 个运行目录（pages.jsonl / chunks.jsonl / run.json）"],
        assume_yes,
    ):
        return 0
    # 获取写锁后重新核对引用，防止预览后发生入库而删除了刚被激活的证据。
    with index_writer_lock():
        current_protected = referenced_runs()
        runs = [path for path in runs if path.name not in current_protected]
        for path in runs:
            _assert_run_target(path, root)
            shutil.rmtree(path)
    print(f"已删除 {len(runs)} 个解析记录目录。")
    return 0


def delete_log_run(run_id: str, assume_yes: bool) -> int:
    target = resolve_run_dir(run_id)
    if not target.exists():
        print(f"解析记录不存在：{run_id}")
        return 1
    if run_id in referenced_runs():
        print(f"拒绝删除：解析记录仍被已登记的索引版本引用：{run_id}")
        return 1
    if not confirm("删除单次解析日志", [f"目录：{target}"], assume_yes):
        return 0
    with index_writer_lock():
        if run_id in referenced_runs():
            print(f"拒绝删除：该记录刚被索引引用：{run_id}")
            return 1
        _assert_run_target(target, config.PARSE_LOG_DIR)
        shutil.rmtree(target)
    print(f"已删除解析记录：{run_id}")
    return 0


def _assert_run_target(path: Path, root: Path) -> None:
    """Windows递归删除前验证绝对路径，不接受越界链接或根目录本身。"""
    resolved_root = Path(root).resolve()
    resolved = Path(path).resolve()
    if resolved.parent != resolved_root:
        raise ValueError("拒绝删除解析目录以外的路径")


def delete_identity(identity: str, collection_name: str, assume_yes: bool, *, kind: str) -> int:
    if kind not in {"doc_id", "index_id"}:
        raise ValueError("仅允许按文档ID或索引ID清理")
    try:
        collection = get_client().get_collection(collection_name)
    except Exception:
        print(f"集合不存在：{collection_name}")
        return 1
    matches = collection.get(where={kind: identity}, include=["metadatas"])["ids"]
    if not confirm(f"按{kind}删除 {identity}", [f"集合：{collection_name}", f"匹配向量：{len(matches)}", "成功删除后移除对应活动/历史登记引用；原件和解析产物保留"], assume_yes):
        return 0
    with index_writer_lock():
        # 锁内重新读取成员，避免预览到删除之间的新入库被漏删。
        current = collection.get(where={kind: identity}, include=["metadatas"])["ids"]
        if current:
            collection.delete(ids=current)
        remove_index_references(collection_name, **{kind: identity})
    print(f"已删除{len(current)}个向量及对应索引引用。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="查看或删除本地 RAG 数据（Chroma 集合/切块、解析日志）")
    parser.add_argument("--list", action="store_true", help="列出集合、切块数与解析记录")
    parser.add_argument("--drop-collection", metavar="NAME", help="删除整个集合")
    parser.add_argument("--delete-source", metavar="FILE", help="按 source 文件名删除切块")
    parser.add_argument("--delete-doc", metavar="DOC_ID", help="删除逻辑文档的全部索引版本")
    parser.add_argument("--delete-index", metavar="INDEX_ID", help="删除指定索引快照")
    parser.add_argument("--wipe", action="store_true", help="清空当前配置集合的全部切块")
    parser.add_argument("--collection", help="配合 --delete-source/--wipe 指定集合，默认使用配置值")
    parser.add_argument("--purge-parse-logs", action="store_true", help="删除全部解析记录目录")
    parser.add_argument("--delete-log-run", metavar="RUN_ID", help="删除单次解析记录")
    parser.add_argument("--yes", action="store_true", help="确认执行破坏性操作")
    args = parser.parse_args()

    actions = [
        args.drop_collection is not None,
        args.delete_source is not None,
        args.delete_doc is not None,
        args.delete_index is not None,
        args.wipe,
        args.purge_parse_logs,
        args.delete_log_run is not None,
    ]
    if not any(actions):
        return list_everything()
    if sum(actions) > 1:
        parser.error("每次只能指定一种清理操作")

    collection_name = args.collection or config.COLLECTION_NAME
    if args.drop_collection is not None:
        return drop_collection(args.drop_collection, args.yes)
    if args.delete_source is not None:
        return delete_source(args.delete_source, collection_name, args.yes)
    if args.delete_doc is not None:
        return delete_identity(args.delete_doc, collection_name, args.yes, kind="doc_id")
    if args.delete_index is not None:
        return delete_identity(args.delete_index, collection_name, args.yes, kind="index_id")
    if args.wipe:
        return wipe_collection(collection_name, args.yes)
    if args.purge_parse_logs:
        return purge_parse_logs(args.yes)
    if args.delete_log_run is not None:
        return delete_log_run(args.delete_log_run, args.yes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
