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
from app.parse_log import list_runs, latest_run_id
from app.rag_pipeline import index_parsed_run


def main() -> int:
    parser = argparse.ArgumentParser(description="把已解析的切块写入 Chroma（不重新解析 PDF）")
    parser.add_argument("--run-id", help="解析记录 ID；省略时使用最近一次")
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
                f"pages={counts.get('pages_total')}  chunks={counts.get('chunks')}"
            )
        return 0

    run_id = args.run_id or latest_run_id()
    if not run_id:
        print(f"没有可用解析记录，请先执行：python -m scripts.parse_pdf  （目录：{config.PARSE_LOG_DIR}）")
        return 1

    if not config.ENABLE_INDEXING:
        print("提示：.env 中 ENABLE_INDEXING=false；本次是你显式执行入库，仍会写入。")

    print(f"使用解析记录：{run_id}")
    print(f"目标集合    ：{config.COLLECTION_NAME}")
    result = index_parsed_run(run_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
