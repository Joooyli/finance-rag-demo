"""独立 MinerU 4 环境适配器：主环境只读取产物，不 import MinerU 的依赖。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any

import pymupdf

from app import config
from app.page_geometry import normalized_bbox, visual_geometry

logger = logging.getLogger(__name__)
ADAPTER_VERSION = "mineru-middle-2.0-v1"
# 同一服务进程中的所有适配器共享限流，防止多个任务同时冷启动本地模型。
_MINERU_SLOT = threading.BoundedSemaphore(1)


class MinerUError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _hash_bytes(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _inline_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_inline_text(item) for item in value)
    if isinstance(value, dict):
        content = _inline_text(value.get("content", ""))
        return f"${content}$" if value.get("type") == "equation_inline" else content
    return ""


class _TableText(HTMLParser):
    """保留行/列分隔；完整 HTML 另外保存，不把丢失跨度的文本当作结构真值。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.images: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"td", "th"}:
            if self.parts and not self.parts[-1].endswith("\n"):
                self.parts.append(" | ")
        elif tag == "br":
            self.parts.append(" ")
        elif tag == "eq":
            self.parts.append("$")
        elif tag == "img":
            src = dict(attrs).get("src")
            if src:
                self.images.append(src)

    def handle_endtag(self, tag):
        if tag == "tr":
            self.parts.append("\n")
        elif tag == "eq":
            self.parts.append("$")

    def handle_data(self, data):
        self.parts.append(data)


def _all_nodes(value):
    if isinstance(value, dict):
        yield value
        yield from _all_nodes(value.get("content"))
    elif isinstance(value, list):
        for child in value:
            yield from _all_nodes(child)


def _list_text(value: Any) -> str:
    # 只在结构列表之间换行；不能遍历所有type=text节点，否则文本块和内部span会重复。
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _list_text(item)))
    if isinstance(value, dict) and value.get("type") in {"list", "index"}:
        return _list_text(value.get("content", []))
    return _inline_text(value).strip()


def _page_ranges(pages: list[int]) -> str:
    ranges: list[str] = []
    start = end = pages[0]
    for number in pages[1:]:
        if number == end + 1:
            end = number
        else:
            ranges.append(str(start) if start == end else f"{start}-{end}")
            start = end = number
    ranges.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(ranges)


def _emit(sink, event: str, page: int, **fields) -> None:
    payload = {"engine": "mineru", "stage": "parsing", "event": event,
               "page": page, **fields}
    level = logging.ERROR if event == "page_failed" else logging.INFO
    logger.log(level, "MinerU 页面=%s %s %s", page, event,
               " ".join(f"{key}={value}" for key, value in fields.items()), extra={"event_data": payload})
    if sink is not None:
        sink(payload)


class MinerUAdapter:
    def __init__(self, *, python_path=None, model_dir=None, cache_dir=None,
                 timeout_seconds=None, tier=None):
        self.python_path = Path(python_path or getattr(config, "MINERU_PYTHON",
            r"E:\learn_space\mineru\env\.venv\Scripts\python.exe"))
        self.model_dir = Path(model_dir or getattr(config, "MINERU_MODEL_BASE_DIR",
            r"E:\learn_space\mineru\model"))
        self.cache_dir = Path(cache_dir or getattr(config, "MINERU_CACHE_DIR",
            config.ROOT_DIR / "data/parser_cache/mineru"))
        self.timeout_seconds = float(timeout_seconds if timeout_seconds is not None else
                                     getattr(config, "MINERU_TIMEOUT_SECONDS", 1800))
        self.tier = tier or getattr(config, "MINERU_TIER", "standard")
        self._preflight_key: str | None = None

    def child_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        # 仅给子进程设置；清空远程地址，不能继承终端或用户配置中的远程 VLM 服务。
        env.update({"MINERU_MODEL_BASE_DIR": str(self.model_dir),
                    "MINERU_MODEL_SOURCE": "local", "MINERU_MODEL_SMALL_BACKEND": "onnx",
                    "MINERU_MODEL_VLM_ENGINE": "llama-cpp", "MINERU_MODEL_VLM_SERVER_URL": "",
                    "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        return env

    def _model_inventory(self) -> list[tuple[str, int, int]]:
        if not self.model_dir.is_dir():
            raise MinerUError("missing_models", f"MinerU 模型目录不存在: {self.model_dir}")
        # 清单指纹用于缓存失效，记录大小/mtime；它不宣称已经验证权重内容 checksum。
        return sorted((str(path.relative_to(self.model_dir)).replace("\\", "/"),
                       path.stat().st_size, path.stat().st_mtime_ns)
                      for path in self.model_dir.rglob("*") if path.is_file())

    def _installed_version(self) -> str:
        site_packages = self.python_path.parent.parent / "Lib/site-packages"
        for metadata in site_packages.glob("mineru-*.dist-info/METADATA"):
            found = re.search(r"(?m)^Version:\s*(\S+)", metadata.read_text(encoding="utf-8"))
            if found:
                return found.group(1)
        raise MinerUError("missing_environment", "独立环境中未找到 MinerU 包版本")

    @staticmethod
    def _stop_process_tree(process) -> None:
        if process.poll() is not None:
            return
        # 超时和取消均停止完整进程树，避免主CLI退出后模型worker继续占用资源。
        if os.name == "nt":
            stopped = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                     timeout=15, check=False)
            if stopped.returncode != 0 and process.poll() is None:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=15)

    def _run_process(self, argv, stdout_path: Path, stderr_path: Path, *,
                     cancel_event=None, timeout_seconds=None) -> None:
        kwargs = {"env": self.child_environment(), "shell": False,
                  "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0) |
                                       getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        else:
            kwargs["start_new_session"] = True
        started = time.monotonic()
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(argv, stdout=stdout, stderr=stderr, **kwargs)
            try:
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        raise MinerUError("cancelled", "MinerU任务已取消")
                    if time.monotonic() - started > timeout:
                        raise MinerUError("timeout", f"MinerU超过时限 {timeout}s")
                    time.sleep(0.1)
                if process.returncode != 0:
                    raise MinerUError("process_failed",
                                      f"MinerU退出码={process.returncode}，诊断: {stderr_path}")
            except BaseException:
                self._stop_process_tree(process)
                raise

    def preflight(self, output_dir: Path, *, cancel_event=None) -> dict:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        if not self.python_path.is_file():
            raise MinerUError("missing_environment", f"MinerU独立解释器不存在: {self.python_path}")
        if self.tier != "standard":
            raise MinerUError("unsupported_tier", "当前已确认并验证的MinerU档位为standard")
        version = self._installed_version()
        inventory = self._model_inventory()
        fingerprint = _hash_json({"python": str(self.python_path.resolve()),
                                  "version": version, "models": inventory, "tier": self.tier})
        if fingerprint != self._preflight_key:
            argv = [str(self.python_path), "-B", "-m", "mineru.kit.main", "models", "verify",
                    "--tier", self.tier, "--small-backend", "onnx", "--vlm-engine", "llama-cpp"]
            self._run_process(argv, output_dir / "preflight.stdout.log",
                              output_dir / "preflight.stderr.log", cancel_event=cancel_event,
                              timeout_seconds=min(self.timeout_seconds, 120))
            self._preflight_key = fingerprint
        return {"version": version, "model_inventory_fingerprint": _hash_json(inventory),
                "runtime_fingerprint": fingerprint, "tier": self.tier,
                "model_source": "local", "small_backend": "onnx", "vlm_engine": "llama-cpp"}

    @staticmethod
    def _extract_zip(zip_path: Path, destination: Path) -> dict:
        with zipfile.ZipFile(zip_path) as archive:
            if archive.testzip() is not None:
                raise MinerUError("invalid_zip", "MinerU ZIP校验失败")
            for item in archive.infolist():
                member = PurePosixPath(item.filename)
                if (member.is_absolute() or ".." in member.parts or "\\" in item.filename
                        or ":" in item.filename or (item.external_attr >> 16) & 0o170000 == 0o120000):
                    raise MinerUError("unsafe_asset_path", "MinerU ZIP含非法资产路径")
                target = destination.joinpath(*member.parts).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise MinerUError("unsafe_asset_path", "MinerU ZIP含越界资产路径")
            archive.extractall(destination)
        middle_path = destination / "middle_json.json"
        if not middle_path.is_file():
            raise MinerUError("missing_output", "MinerU ZIP缺少middle_json.json")
        payload = json.loads(middle_path.read_text(encoding="utf-8"))
        if payload.get("schema") != "docvortex.middle" or str(payload.get("schema_version")) != "2.0":
            raise MinerUError("unsupported_schema", "仅支持已验证的docvortex.middle schema 2.0")
        return payload

    @staticmethod
    def _relative_asset(path: str, extracted: Path, output_dir: Path) -> str:
        member = PurePosixPath(path)
        if member.is_absolute() or ".." in member.parts or "\\" in path or ":" in path:
            raise MinerUError("unsafe_asset_path", f"非法图片路径: {path}")
        local = extracted.joinpath(*member.parts).resolve()
        if not local.is_relative_to(extracted.resolve()) or not local.is_file():
            raise MinerUError("missing_asset", f"MinerU图片不存在: {path}")
        return local.relative_to(output_dir.resolve()).as_posix()

    def _adapt_block(self, raw: dict, page: int, order: int, geometry: dict,
                     extracted: Path, output_dir: Path, pdf_sha: str) -> dict:
        mapping = {"text": "paragraph", "ref_text": "paragraph", "paragraph_title": "heading",
                   "doc_title": "heading", "list": "list_item", "index": "list_item",
                   "equation": "formula", "image": "figure", "chart": "figure",
                   "table": "table", "page_footnote": "footnote", "aside_text": "footnote",
                   "header": "header", "footer": "footer", "page_number": "page_number",
                   "code": "paragraph"}
        raw_type = raw.get("type")
        if raw_type not in mapping:
            raise MinerUError("unsupported_block", f"未知MinerU页面块类型: {raw_type}")
        kind = mapping[raw_type]
        missing_bbox = raw.get("bbox") is None
        bbox = normalized_bbox(raw["bbox"], geometry) if not missing_bbox else [
            0.0, 0.0, geometry["width"], geometry["height"]]
        nodes = list(_all_nodes(raw))
        assets: list[str] = []
        for node in nodes:
            if node.get("image_path"):
                assets.append(self._relative_asset(node["image_path"], extracted, output_dir))
            # 本地CLI以ZIP sidecar为契约，远程URL或内嵌base64不静默混入正文/向量。
            if node.get("image_url") or node.get("image_base64"):
                raise MinerUError("unsupported_image_payload", "MinerU ZIP必须使用本地图片sidecar")
        html = ""
        captions = []
        if kind in {"table", "figure"}:
            bodies = [node for node in nodes if node.get("type") in
                      {"table_body", "image_body", "chart_body"}]
            body_text = []
            for node in bodies:
                content = _inline_text(node.get("content", ""))
                if node.get("type") == "table_body":
                    html = content
                    parser = _TableText()
                    parser.feed(html)
                    body_text.append("".join(parser.parts).strip())
                    for image in parser.images:
                        assets.append(self._relative_asset(image, extracted, output_dir))
                elif content:
                    body_text.append(content)
            captions = [_inline_text(node.get("content", "")) for node in nodes
                        if str(node.get("type", "")).endswith(("_caption", "_footnote"))]
            text = "\n".join(part for part in [*captions, *body_text] if part)
        elif kind == "list_item":
            text = _list_text(raw.get("content", []))
        else:
            text = _inline_text(raw.get("content", "")).strip()
        block_id = _hash_json([pdf_sha, page, kind, order, bbox])
        # 适配阶段只保留引擎分类，不删除页眉页脚；融合/OCR后由统一块清洗一次决定。
        selected = True
        status = "needs_review" if missing_bbox else "pass"
        if not text and kind not in {"figure", "header", "footer", "page_number"}:
            status = "needs_review"
        if kind == "figure" and not text and not assets:
            status = "needs_review"
        block = {"block_id": block_id, "page": page, "type": kind, "order": order,
                 "bbox": bbox, "engine": "mineru", "content_text": text,
                 "quality_status": status, "selected": selected, "asset_refs": sorted(set(assets)),
                 "provenance": {"raw_ref": (extracted / "middle_json.json").relative_to(output_dir).as_posix(),
                                "raw_index": raw.get("index"),
                                "raw_type": raw_type, "raw_bbox": raw.get("bbox"),
                                "coordinate_space": "normalized_visual_page", "adapter": ADAPTER_VERSION},
                 "continues_prev": bool(raw.get("continues_prev", False))}
        if "level" in raw:
            block["level"] = raw["level"]
        if html:
            html_file = extracted / f"table-{page}-{order}.html"
            html_file.write_text(html, encoding="utf-8")
            block.update(html=html, html_ref=html_file.relative_to(output_dir).as_posix())
        if kind == "formula":
            block["latex"] = text
            block["content_text"] = f"$$\n{text}\n$$" if text else ""
        if captions:
            block["caption"] = "\n".join(captions)
        return block

    def parse_pages(self, pdf_path, pages, output_dir, *, event_sink=None,
                    cancel_event=None) -> dict[int, list[dict]]:
        pdf_path, output_dir = Path(pdf_path).resolve(), Path(output_dir).resolve()
        requested = sorted(set(pages))
        if not requested:
            return {}
        if any(type(page) is not int or page < 1 for page in requested):
            raise ValueError("MinerU pages must contain positive one-based physical page numbers")
        output_dir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        for page in requested:
            _emit(event_sink, "page_started", page, batch_pages=requested, tier=self.tier)
        acquired = False
        try:
            # 等待限流槽时同样响应取消，不让排队任务永远阻塞。
            while not acquired:
                if cancel_event is not None and cancel_event.is_set():
                    raise MinerUError("cancelled", "MinerU排队任务已取消")
                acquired = _MINERU_SLOT.acquire(timeout=0.1)
            runtime = self.preflight(output_dir, cancel_event=cancel_event)
            pdf_sha = _hash_bytes(pdf_path)
            with pymupdf.open(pdf_path) as document:
                if requested[-1] > len(document):
                    raise MinerUError("invalid_pages", "请求页号超过原PDF物理页数")
                geometries = {page: visual_geometry(document[page - 1]) for page in requested}
            key = _hash_json({"pdf": pdf_sha, "pages": requested, "runtime": runtime,
                              "adapter": ADAPTER_VERSION, "ocr_mode": "auto", "image_analysis": True})
            zip_path, cache_hit = output_dir / "result.zip", False
            cache_zip = self.cache_dir / f"{key}.zip"
            cache_meta = self.cache_dir / f"{key}.json"
            if cache_zip.is_file() and cache_meta.is_file():
                try:
                    meta = json.loads(cache_meta.read_text(encoding="utf-8"))
                    cache_hit = meta.get("sha256") == _hash_bytes(cache_zip)
                except (OSError, ValueError):
                    cache_hit = False
            if cache_hit:
                shutil.copyfile(cache_zip, zip_path)
                for page in requested:
                    _emit(event_sink, "cache_hit", page, batch_pages=requested)
            else:
                argv = [str(self.python_path), "-B", "-m", "mineru.kit.main", "parse", str(pdf_path),
                        "--pages", _page_ranges(requested), "--tier", self.tier, "--format", "zip",
                        "--ocr-mode", "auto", "-o", str(zip_path)]
                self._run_process(argv, output_dir / "parse.stdout.log", output_dir / "parse.stderr.log",
                                  cancel_event=cancel_event)
            if not zip_path.is_file():
                raise MinerUError("missing_output", "MinerU成功退出但未生成ZIP")
            extracted = output_dir / "extracted"
            extracted.mkdir(exist_ok=True)
            middle = self._extract_zip(zip_path, extracted)
            middle_pages = middle.get("pages")
            if not isinstance(middle_pages, list) or any(not isinstance(item, dict) for item in middle_pages):
                raise MinerUError("invalid_output", "MinerU缺少合法pages列表")
            actual = [item.get("page_idx") for item in middle_pages]
            if any(type(number) is not int for number in actual) or sorted(actual) != [p - 1 for p in requested]:
                raise MinerUError("page_mismatch", f"MinerU原页号不匹配: expected={requested}, actual={actual}")
            results: dict[int, list[dict]] = {}
            for item in middle_pages:
                page = item["page_idx"] + 1
                if not isinstance(item.get("blocks"), list):
                    raise MinerUError("invalid_output", f"MinerU第{page}页blocks不合法")
                results[page] = [self._adapt_block(raw, page, order, geometries[page], extracted,
                                                 output_dir, pdf_sha)
                                 for order, raw in enumerate(item["blocks"])]
            if not cache_hit:
                # 只缓存完整校验通过的引擎原始ZIP；原子替换避免进程中断留下伪命中。
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                temp_zip, temp_meta = cache_zip.with_suffix(".zip.tmp"), cache_meta.with_suffix(".json.tmp")
                shutil.copyfile(zip_path, temp_zip)
                temp_meta.write_text(json.dumps({"sha256": _hash_bytes(zip_path), "runtime": runtime},
                                                ensure_ascii=False, indent=2), encoding="utf-8")
                temp_zip.replace(cache_zip)
                temp_meta.replace(cache_meta)
            for page in requested:
                # CLI一次返回整批；此处逐页报告已校验完成，绝不从进度条猜测页完成时刻。
                _emit(event_sink, "page_completed", page, batch_pages=requested,
                      batch_finished=True, blocks=len(results[page]), cache_hit=cache_hit,
                      duration_ms=round((time.monotonic() - started) * 1000))
            (output_dir / "runtime.json").write_text(json.dumps(runtime, ensure_ascii=False, indent=2),
                                                      encoding="utf-8")
            return results
        except BaseException as exc:
            for page in requested:
                _emit(event_sink, "page_failed", page, batch_pages=requested,
                      error_code=getattr(exc, "code", "mineru_failed"), message=str(exc))
            raise
        finally:
            if acquired:
                _MINERU_SLOT.release()
