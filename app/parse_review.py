"""人工批准绑定密封内容；任何入库入口都执行相同校验。"""
from __future__ import annotations

import json
import math
from pathlib import Path

from app.document_schema import SCHEMA_VERSION, file_hash, stable_hash, text_hash
from app.parse_log import (_write_json_atomic, load_run, now_iso, read_jsonl, resolve_run_dir)

MUTABLE_FILES = {"run.json", "review.json", "index.json", "events.jsonl", "manifest.json"}
REQUIRED_FILES = ("pages.jsonl", "blocks.jsonl", "logical_blocks.jsonl", "merges.jsonl", "chunks.jsonl")


def _content_files(root: Path) -> dict[str, Path]:
    return {p.relative_to(root).as_posix(): p for p in root.rglob("*")
            if p.is_file() and not (p.parent == root and p.name in MUTABLE_FILES)}


def _validate_records(root: Path, manifest: dict) -> list[dict]:
    data = {name: list(read_jsonl(root / name, strict=True)) for name in REQUIRED_FILES}
    pages = data["pages.jsonl"]
    expected_pages = list(range(1, manifest["counts"]["pages_total"] + 1))
    if sorted(p.get("page", -1) for p in pages) != expected_pages:
        raise ValueError("物理页面集合不完整或存在重复")
    blocks = {b["block_id"]: b for b in data["blocks.jsonl"]}
    logical = {b["logical_block_id"]: b for b in data["logical_blocks.jsonl"]}
    if len(blocks) != len(data["blocks.jsonl"]) or len(logical) != len(data["logical_blocks.jsonl"]):
        raise ValueError("块ID重复")
    merges = {m.get("merge_id") for m in data["merges.jsonl"]}
    page_lookup = {p["page"]: p for p in pages}

    def validate_bbox(bbox, page_number):
        if not isinstance(bbox, list) or len(bbox) != 4 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in bbox
        ) or bbox[2] <= bbox[0] or bbox[3] <= bbox[1] or min(bbox[:2]) < -1:
            raise ValueError("来源区域bbox无效")
        geometry = page_lookup[page_number].get("geometry") or {}
        if geometry and (bbox[2] > geometry["width"] + 1 or bbox[3] > geometry["height"] + 1):
            raise ValueError("来源区域bbox超出页面")

    for block in blocks.values():
        if block.get("page") not in page_lookup:
            raise ValueError("来源块物理页无效")
        if block.get("bbox") is not None:
            validate_bbox(block["bbox"], block["page"])
    records = data["chunks.jsonl"]
    if not records or len(records) != manifest["counts"]["chunks"]:
        raise ValueError("没有可入库切块或切块数不一致")
    if len({r["chunk_id"] for r in records}) != len(records):
        raise ValueError("chunk_id重复")
    for item in logical.values():
        if not set(item.get("source_block_ids", [])).issubset(blocks):
            raise ValueError("逻辑块引用了不存在的来源块")
    for item in records:
        if item.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("切块schema不受支持")
        if item.get("doc_id") != manifest["doc_id"] or item.get("pdf_sha256") != manifest["pdf_sha256"]:
            raise ValueError("切块文档身份不匹配")
        text = item.get("embedding_text", "")
        if not text or text_hash(text) != item.get("embedding_text_sha256"):
            raise ValueError("切块文本hash不匹配")
        item_pages = sorted(set(item.get("pages", [])))
        if not item_pages or not set(item_pages).issubset(expected_pages):
            raise ValueError("切块来源页无效")
        if item.get("page_start") != item_pages[0] or item.get("page_end") != item_pages[-1]:
            raise ValueError("切块页范围不一致")
        if not item.get("source_spans") or {s["page"] for s in item["source_spans"]} != set(item_pages):
            raise ValueError("切块精确来源不完整")
        for span in item["source_spans"]:
            if span.get("block_id") not in blocks:
                raise ValueError("来源区域引用了不存在的块")
            if blocks[span["block_id"]]["page"] != span["page"]:
                raise ValueError("来源区域与来源块的物理页不一致")
            validate_bbox(span.get("bbox"), span["page"])
        if not set(item.get("block_ids", [])).issubset(blocks) or not set(item.get("merge_ids", [])).issubset(merges):
            raise ValueError("切块引用不存在的来源或合并决策")
        if item.get("parent_id") and item["parent_id"] not in logical:
            raise ValueError("切块父逻辑块不存在")
    return records


def seal_run(run_dir: Path, meta: dict) -> dict:
    root = Path(run_dir)
    for name in REQUIRED_FILES:
        if not (root / name).is_file():
            raise ValueError(f"缺少必需产物: {name}")
    files = {name: {"sha256": file_hash(path), "bytes": path.stat().st_size}
             for name, path in sorted(_content_files(root).items())}
    manifest = {"schema_version": SCHEMA_VERSION, "run_id": meta["run_id"],
                "doc_id": meta["doc_id"], "source": meta["source"],
                "pdf_path": meta["pdf_path"], "pdf_sha256": meta["pdf_sha256"],
                "pipeline_fingerprint": meta["pipeline_fingerprint"],
                "embedding": meta["embedding"], "config": meta["config"],
                "quality_status": meta["quality_status"], "counts": meta["counts"], "files": files}
    _validate_records(root, manifest)
    # 不把manifest自己或可更新的review/index/events放进根hash，避免循环依赖。
    manifest["manifest_sha256"] = stable_hash(manifest)
    _write_json_atomic(root / "manifest.json", manifest)
    return manifest


def validate_indexable_run(run_id: str, base_dir: Path | None = None,
                           require_review: bool = True) -> tuple[dict, list[dict], dict]:
    root = resolve_run_dir(run_id, base_dir)
    meta = load_run(run_id, base_dir)
    if not meta or meta.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("仅支持v3解析产物入库，历史记录须重新解析")
    if meta.get("parse_status") != "completed" or meta.get("quality_status") != "pass":
        raise ValueError("解析未完成或质量问题尚未解决")
    path = root / "manifest.json"
    if not path.is_file():
        raise ValueError("解析产物尚未密封")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expected = manifest.pop("manifest_sha256", None)
    if not expected or stable_hash(manifest) != expected:
        raise ValueError("manifest校验失败")
    manifest["manifest_sha256"] = expected
    if manifest["quality_status"] != "pass":
        raise ValueError("密封产物质量未通过")
    for key in ("doc_id", "pdf_sha256", "pipeline_fingerprint", "run_id"):
        if meta.get(key) != manifest.get(key):
            raise ValueError(f"运行记录与密封产物不一致: {key}")
    actual = _content_files(root)
    if set(actual) != set(manifest["files"]):
        raise ValueError("密封产物文件集合已改变")
    for name, info in manifest["files"].items():
        target = (root / name).resolve()
        if not target.is_relative_to(root) or not target.is_file() or file_hash(target) != info["sha256"]:
            raise ValueError(f"解析产物缺失或被修改: {name}")
    original = Path(manifest["pdf_path"])
    if not original.is_file() or file_hash(original) != manifest["pdf_sha256"]:
        raise ValueError("不可变PDF原件缺失或内容已改变")
    records = _validate_records(root, manifest)
    if any(r.get("quality_status") != "pass" for r in records):
        raise ValueError("存在未通过质量检查的切块")
    if require_review:
        review_path = root / "review.json"
        review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
        if review.get("decision") != "approved" or review.get("manifest_sha256") != expected or not review.get("reviewer"):
            raise ValueError("尚未人工确认当前密封产物")
    return meta, records, manifest


def approve_run(run_id: str, reviewer: str, decision: str = "approved", notes: str = "",
                base_dir: Path | None = None) -> dict:
    if not reviewer.strip() or decision not in {"approved", "rejected"}:
        raise ValueError("请填写确认者，并选择approved/rejected")
    # 批准必须满足质量门禁；拒绝可以记录失败产物，绝不因此获得入库资格。
    if decision == "approved":
        meta, _, manifest = validate_indexable_run(run_id, base_dir, require_review=False)
        manifest_hash = manifest["manifest_sha256"]
    else:
        meta = load_run(run_id, base_dir)
        if not meta or meta.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("解析记录不存在或不是v3记录")
        manifest_path = resolve_run_dir(run_id, base_dir) / "manifest.json"
        manifest_hash = file_hash(manifest_path) if manifest_path.is_file() else None
    review = {"schema_version": SCHEMA_VERSION, "reviewer": reviewer.strip(),
              "reviewed_at": now_iso(), "decision": decision, "notes": notes,
              "manifest_sha256": manifest_hash}
    root = resolve_run_dir(run_id, base_dir)
    _write_json_atomic(root / "review.json", review)
    meta.pop("run_dir", None)
    meta["review_status"] = decision
    _write_json_atomic(root / "run.json", meta)
    return review
