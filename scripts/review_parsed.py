"""先展示解析证据，再记录绑定manifest的实际人工决定。"""
from __future__ import annotations

import argparse
import json
import sys

from app.logging_config import configure_logging
from app.parse_log import load_run, read_jsonl, resolve_run_dir
from app.parse_review import approve_run


def main() -> int:
    parser = argparse.ArgumentParser(description="查看v3解析产物并显式人工确认；默认仅预览")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--reviewer", help="实际检查并作出决定的人员")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--approve", action="store_true", help="声明已检查完整产物并批准当前manifest")
    action.add_argument("--reject", action="store_true", help="拒绝当前产物")
    parser.add_argument("--notes", default="", help="审核备注/已核对限制")
    parser.add_argument("--show-all", action="store_true", help="打印每个最终embedding_text全文；默认显示预览")
    args = parser.parse_args()
    configure_logging()
    try:
        root = resolve_run_dir(args.run_id)
        meta = load_run(args.run_id)
        if not meta:
            raise ValueError("解析记录不存在")
        print(f"目录：{root}")
        print(f"解析={meta.get('parse_status')}  质量={meta.get('quality_status')}  审阅={meta.get('review_status')}")
        print(f"问题：{json.dumps(meta.get('issues', []), ensure_ascii=False)}")
        records = list(read_jsonl(root / "chunks.jsonl", strict=True))
        for record in records:
            text = record.get("embedding_text", record.get("text", ""))
            preview = text if args.show_all else text[:500] + ("\n[预览截断，请回看完整产物或使用--show-all]" if len(text) > 500 else "")
            print(f"\n切块 {record.get('chunk_index')} / {record.get('content_type')} / 页码{record.get('pages')} / tokens={record.get('embedding_tokens')}")
            print(preview)
            if record.get("issues"):
                print(f"问题：{record['issues']}")
        if not args.approve and not args.reject:
            print("\n仅预览，未记录人工确认。检查原PDF、结构化产物和完整入库文本后，使用--reviewer <姓名> --approve或--reject。")
            return 0
        if not args.reviewer or not args.reviewer.strip():
            raise ValueError("批准/拒绝必须提供--reviewer")
        # 显式--approve才代表人的决定，脚本执行本身不能自动转成approved。
        decision = "approved" if args.approve else "rejected"
        result = approve_run(args.run_id, args.reviewer, decision=decision, notes=args.notes)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"审阅未保存：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
