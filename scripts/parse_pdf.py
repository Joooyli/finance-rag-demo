"""Parse PDFs into chunks and write the intermediate result to data/parse_log.

This script never touches Chroma and never calls the cloud LLM: it is the
"inspect first" step. Run ``python -m scripts.index_parsed`` afterwards to embed
and store what you have verified.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows consoles default to a legacy code page; keep Chinese output readable.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from app import config
from app.logging_config import configure_logging
from app.parse_log import load_run
from app.rag_pipeline import parse_document


def collect_targets(arguments: list[str]) -> list[Path]:
    if arguments:
        return [Path(item) for item in arguments]
    if not config.PDF_DIR.exists():
        return []
    return sorted(config.PDF_DIR.glob("*.pdf"))


def main() -> int:
    parser = argparse.ArgumentParser(description="只解析 PDF 并落盘日志，不入库")
    parser.add_argument("pdf", nargs="*", help="PDF 路径；省略时解析 data/source 下全部 PDF")
    parser.add_argument("--no-log", action="store_true", help="不写 data/parse_log（仅打印统计）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    configure_logging()
    targets = collect_targets(args.pdf)
    if not targets:
        print(f"没有找到 PDF：{config.PDF_DIR}")
        return 1

    results = []
    failed = 0
    needs_review = 0
    for target in targets:
        if not args.json:
            print(f"\n=== 解析 {target} ===")
        try:
            result = parse_document(target, write_log=not args.no_log)
        except Exception as exc:
            failed += 1
            print(f"失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            results.append({"file": str(target), "error": f"{type(exc).__name__}: {exc}"})
            continue

        counts = result.counts
        meta = load_run(result.run_id) if result.run_id else {}
        meta = meta or {}
        quality = meta.get("quality_status", counts.get("quality_status", "unknown"))
        if quality != "pass":
            needs_review += 1
        payload = {
            "file": target.name,
            "run_id": result.run_id,
            "run_dir": result.run_dir,
            "pages_total": counts.get("pages_total", 0),
            "pages_with_text": counts.get("pages_with_text", 0),
            "pages_by_mode": counts.get("pages_by_mode", {}),
            "ocr_pages": counts.get("ocr_pages", []),
            "header_blocks": counts.get("header_blocks", 0),
            "footer_blocks": counts.get("footer_blocks", 0),
            "chunks": counts.get("chunks", 0),
            "chars": counts.get("chars", 0),
            "parse_status": meta.get("parse_status", "diagnostic"),
            "quality_status": quality,
            "review_status": meta.get("review_status", "not_available"),
            "issues": meta.get("issues", counts.get("issues", [])),
        }
        results.append(payload)

        if not args.json:
            print(f"总页数      : {payload['pages_total']}")
            print(f"有文字页数  : {payload['pages_with_text']}")
            print(f"解析方式    : {payload['pages_by_mode']}")
            # 分类计数与是否删除分开，不能把发现页眉误报成已经剔除。
            print(f"页眉/页脚块 : {payload['header_blocks']} / {payload['footer_blocks']}；删除配置={config.HEADER_REMOVE}/{config.FOOTER_REMOVE}")
            print(f"切块数      : {payload['chunks']}")
            print(f"切块总字符  : {payload['chars']}")
            print(f"质量/审阅   : {payload['quality_status']} / {payload['review_status']}")
            if payload["issues"]:
                print(f"待核对问题  : {payload['issues']}")
            if payload["run_id"]:
                print(f"日志目录    : {result.run_dir}")

            empty_pages = [
                page["page"] for page in result.pages if not page.get("text")
            ]
            if empty_pages:
                print(f"注意：以下页没有可切块文字（纯图片或空白）：{empty_pages}")

            ocr_pages = [
                page["page"] for page in result.pages if page.get("ocr_used")
            ]
            if ocr_pages:
                print(f"提示：以下页使用了 OCR，建议人工核对：{ocr_pages}")

            if payload["pages_with_text"] == 0:
                print("警告：整份文档没有提取到文字，请检查 OCR 环境（TESSDATA_PREFIX / PATH）。")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print("\n先检查产物并确认：python -m scripts.review_parsed --run-id <run_id> --reviewer <姓名> --approve")
        print("确认后执行入库：python -m scripts.index_parsed --run-id <run_id>")
        if args.no_log:
            print("本次--no-log仅供诊断，无密封审阅产物，不能入库。")

    # 0=质量通过且待人工审阅，1=解析失败，2=完成但仍有质量问题/无审阅产物。
    return 1 if failed else (2 if needs_review else 0)


if __name__ == "__main__":
    raise SystemExit(main())
