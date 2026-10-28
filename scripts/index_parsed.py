"""Embed chunks from an existing parse log run and store them in Chroma.

Use this after ``python -m scripts.parse_pdf`` and after you have inspected the
written chunks. The PDF is **not** parsed again, so what you reviewed is exactly
what gets indexed.
"""

from __future__ import annotations

import argparse
import json
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from app import config
from app.logging_config import configure_logging
from app.parse_log import list_runs
from app.parse_review import validate_indexable_run
from app.rag_pipeline import index_parsed_run


def main() -> int:
    parser = argparse.ArgumentParser(description="把已解析的切块写入 Chroma（不重新解析 PDF）")
    parser.add_argument("--run-id", help="解析记录 ID；省略时使用最近一次质量通过且已人工确认的记录")
    parser.add_argument("--list", action="store_true", help="列出可用解析记录")
    args = parser.parse_args()

    configure_logging()

    if args.list:
        runs = list_runs()
        if not runs:
            print(f"没有解析记录：{config.PARSE_LOG_DIR}")
            return 1
        for run in runs:
            counts = run.get("counts") or {}
            print(
                f"{run['run_id']}  status={run.get('status')}  "
                f"quality={run.get('quality_status', 'legacy')}  review={run.get('review_status', 'unknown')}  "
                f"pages={counts.get('pages_total')}  chunks={counts.get('chunks')}"
            )
        return 0

    run_id = args.run_id
    if not run_id:
        # 最近的失败/待审核运行不能覆盖用户已经确认的可入库结果。
        for candidate in list_runs():
            if candidate.get("schema_version") != 3 or candidate.get("quality_status") != "pass" or candidate.get("review_status") != "approved":
                continue
            try:
                validate_indexable_run(candidate["run_id"])
            except (ValueError, OSError, KeyError) as exc:
                print(f"跳过不可入库记录 {candidate['run_id']}: {exc}", file=sys.stderr)
                continue
            run_id = candidate["run_id"]
            break
    if not run_id:
        print(f"没有质量通过且已人工确认的v3记录；请先解析并执行scripts.review_parsed（目录：{config.PARSE_LOG_DIR}）")
        return 1

    if not config.ENABLE_INDEXING:
        print("本次为显式入库请求；ENABLE_INDEXING=false不会阻止命令，但质量/人工确认门禁仍必须通过。")

    print(f"使用解析记录：{run_id}")
    print(f"目标集合    ：{config.COLLECTION_NAME}")
    try:
        result = index_parsed_run(run_id)
    except (ValueError, OSError) as exc:
        print(f"入库被拒绝：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
