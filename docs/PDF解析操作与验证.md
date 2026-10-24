# PDF 解析操作与验证

本文档对应 `app/pdf_loader.py`（PyMuPDF 2.x：layout + Markdown）与 `app/parse_log.py`（schema v2）的实际行为。
目标是：**先解析、看日志、确认无误，再入库**。

---

## 0. 适用范围

| 项 | 说明 |
|---|---|
| 解析器 | `pymupdf>=2` 的 `doc.to_markdown()`，内置版面分析（layout） |
| 页眉/页脚 | 解析阶段直接剔除（`HEADER_REMOVE` / `FOOTER_REMOVE`） |
| 产物 | `data/parse_log/<run_id>/{run.json, pages.jsonl, chunks.jsonl}` |
| 入库 | 默认关闭（`ENABLE_INDEXING=false`），确认后手动执行 |

---

## 1. 前置条件与自检

### 1.1 依赖版本

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip show pymupdf onnxruntime opencv-python 2>$null |
  Select-String -Pattern '^(Name|Version):'
```

期望看到：

```text
Name: pymupdf        Version: 2.0.x
Name: onnxruntime    Version: 1.x
Name: opencv-python  Version: 4.x / 5.x
```

### 1.2 解析能力自检

```powershell
$env:PYTHONIOENCODING = "utf-8"
.\.venv\Scripts\python.exe -c "import pymupdf; d=pymupdf.open('data/source/frm_notes.pdf'); print('version', pymupdf.__version__); print('to_markdown', hasattr(d,'to_markdown')); print('pages', d.page_count)"
```

期望：`to_markdown True`，页数与 PDF 一致。

### 1.3 OCR 自检

```powershell
$env:PYTHONIOENCODING = "utf-8"
.\.venv\Scripts\python.exe -c "from app.pdf_loader import configure_ocr_environment; configure_ocr_environment(); import subprocess,os; print('tesseract on PATH:', bool(__import__('shutil').which('tesseract'))); print('TESSDATA_PREFIX:', os.environ.get('TESSDATA_PREFIX'))"
```

期望：`tesseract on PATH: True` 且 `TESSDATA_PREFIX` 指向 `...\tessdata`。
若为 `False`，检查 `.env` 的 `TESSERACT_CMD` 是否指向真实存在的 `tesseract.exe`。

> OCR 只有在「PyMuPDF Layout 可用 + `ENABLE_OCR=true` + Tesseract 可发现 + `opencv-python` 已安装」四个条件同时满足时才会触发。

---

## 2. 操作步骤

### 2.1 放置 PDF

```powershell
Copy-Item "C:\path\to\your.pdf" data\source\
```

### 2.2 检查关键配置（`.env`）

| 配置 | 建议值 | 作用 |
|---|---|---|
| `ENABLE_INDEXING` | `false` | 解析阶段绝不写向量库 |
| `PARSE_LOG_ENABLED` | `true` | 写中间产物 |
| `HEADER_REMOVE` / `FOOTER_REMOVE` | `true` / `true` | 剔除广告页眉、页码页脚 |
| `LAYOUT_ENABLED` | `true` | 版面分析；关闭后 `elements` 为空 |
| `TABLE_STRATEGY` | `lines_strict` | 无框线表格可改 `lines` |
| `OCR_LANG` | `chi_sim+eng` | 必须显式包含中文 |
| `OCR_DPI` | `300` | OCR 渲染精度 |
| `TESSERACT_CMD` | 实际路径 | 供自动配置 PATH / TESSDATA_PREFIX |
| `PARSE_LOG_ELEMENT_TEXT_MAX` | `200` | `elements[].text` 预览长度 |

### 2.3 小样预览（不写日志、不入库）

先用 3 页确认解析效果，避免全量白跑：

```powershell
$env:PYTHONIOENCODING = "utf-8"
@'
import pymupdf
from app.pdf_loader import configure_ocr_environment
from app import config

configure_ocr_environment()
doc = pymupdf.open("data/source/frm_notes.pdf")
for chunk in doc.to_markdown(
    page_chunks=True,
    pages=[0, 2, 16],                 # 0 基页码：第 1、3、17 页
    header=not config.HEADER_REMOVE,
    footer=not config.FOOTER_REMOVE,
    ocr_language=config.OCR_LANG,
):
    text = chunk["text"]
    print(f"第 {chunk['metadata']['page_number']} 页 | {len(text)} 字符 | 版面块 {len(chunk.get('page_boxes') or [])}")
    print("  ", repr(text[:70]))
doc.close()
'@ | .\.venv\Scripts\python.exe -
```

判断标准：

- 纯图片页应为 `ocr` 效果（文本非空，可能带 `<!-- Start of picture text -->` 标记）；
- 有页眉的页，开头不应是重复的广告语；
- 正文首行应是真实内容。

### 2.4 全量解析（只写日志，不入库）

```powershell
$env:PYTHONIOENCODING = "utf-8"
.\.venv\Scripts\python.exe -m scripts.parse_pdf
```

指定单个文件 / 只统计不写日志：

```powershell
.\.venv\Scripts\python.exe -m scripts.parse_pdf data\source\frm_notes.pdf
.\.venv\Scripts\python.exe -m scripts.parse_pdf --no-log
.\.venv\Scripts\python.exe -m scripts.parse_pdf --json
```

控制台会输出：

```text
总页数      : 46
有文字页数  : 45
解析方式    : {'empty': 1, 'mixed': 23, 'native': 21, 'ocr': 1}
页眉/页脚块 : 44 / 46（已在解析阶段剔除）
切块数      : 57
切块总字符  : 27565
日志目录    : ...\data\parse_log\20261022-175526__frm_notes__9b722fcd
提示：以下页使用了 OCR，建议人工核对：[1, 3, 5, ...]
```

### 2.5 查看产物

```text
data/parse_log/<run_id>/
├── run.json      # 状态、解析器版本、页眉/OCR/版面参数、统计
├── pages.jsonl   # 每页一行（schema v2）
└── chunks.jsonl  # 每个切块一行
```

`pages.jsonl` 关键字段：

| 字段 | 含义 | 校验用途 |
|---|---|---|
| `schema_version` | 固定 `2` | 判断是否为新日志 |
| `extractor` / `extractor_version` | `pymupdf.to_markdown` / `2.0` | 复现 |
| `page` / `page_width` / `page_height` / `rotation` | 页码与几何 | 旋转页核对 |
| `mode` | `native` / `ocr` / `mixed` / `image_only` / `empty` | 决定要不要人工看 |
| `elements` | `index` / `class` / `bbox` / `pos` / `text`（前 200 字） | 页眉、表格、图片定位 |
| `raw_text` | 删页眉后的 Markdown 原文 | 与 `text` 对比 |
| `text` | 规范化后的正文，切块用它 | 入库内容 |
| `raw_chars` / `chars` / `text_sha1` | 长度与指纹 | 完整性 |

⚠️ `elements[].pos` 指向 `raw_text`，**不要**用它去索引 `text`。

### 2.6 入库（确认后）

```powershell
.\.venv\Scripts\python.exe -m scripts.index_parsed                 # 最近一次解析
.\.venv\Scripts\python.exe -m scripts.index_parsed --run-id <id>   # 指定某次
```

> 文本已从纯文本变为 Markdown，`chunk_id` 会全部变化。入库前确认集合是空的（或先 `python -m scripts.reset_db --delete-source <文件名> --yes`）。

---

## 3. 验证步骤

以下命令都在项目根目录执行，先设置：

```powershell
$env:PYTHONIOENCODING = "utf-8"
```

### V0：文件与 schema 完整性（必做）

```powershell
@'
from app.parse_log import latest_run_id, load_run, load_run_pages, load_run_chunks

run_id = latest_run_id()
meta = load_run(run_id)
pages = load_run_pages(run_id)
chunks = load_run_chunks(run_id)

print("run_id :", run_id)
print("schema :", meta.get("schema_version"), meta.get("extractor"), meta.get("extractor_version"))
print("pages  :", len(pages), " chunks:", len(chunks))

required = {"schema_version","extractor","extractor_version","page","page_width",
            "page_height","rotation","elements","raw_text","text"}
print("缺字段页:", [p["page"] for p in pages if not required <= set(p)])

print("含代理字符页:", [p["page"] for p in pages
      if any(0xD800 <= ord(c) <= 0xDFFF for c in (p.get("raw_text") or "") + (p.get("text") or ""))])
'@ | .\.venv\Scripts\python.exe -
```

**通过标准**

- `schema = 2`，`extractor = pymupdf.to_markdown`；
- `pages` 等于 PDF 总页数（含空页记录）；
- `缺字段页`、`含代理字符页` 均为 `[]`；
- `chunks` 与 `run.json` 的 `counts.chunks` 一致。

### V1：页眉/页脚是否真的剔除（必做）

```powershell
@'
from app.parse_log import latest_run_id, load_run_pages

pages = load_run_pages(latest_run_id())
MODES = {}
for p in pages:
    MODES[p.get("mode")] = MODES.get(p.get("mode"), 0) + 1
print("模式分布:", dict(sorted(MODES.items())))
print("页眉块:", sum(p.get("header_blocks") or 0 for p in pages),
      "页脚块:", sum(p.get("footer_blocks") or 0 for p in pages))

AD = "高顿学术中心"          # 换成你文档里的页眉/广告语关键字
print("仍含页眉文字的页:", [p["page"] for p in pages if AD in (p.get("text") or "")])

cleared = [p["page"] for p in pages
           if any(e.get("class") == "page-header" and e.get("pos") == [0, 0]
                  for e in (p.get("elements") or []))]
print("页眉块已清空的页数:", len(cleared))

for p in pages[:3]:
    print(f"\n第 {p['page']} 页 mode={p['mode']} raw={p['raw_chars']} text={p['chars']}")
    print("  开头:", repr((p.get("text") or "")[:60]))
'@ | .\.venv\Scripts\python.exe -
```

**通过标准**

- 关键字命中的页为 `[]`（或仅剩确实属于正文的页）；
- `页眉块` 数量接近页数，且这些块 `pos=[0,0]`、`text=""`；
- 抽查页开头是正文，不是重复广告语。

**不通过怎么办**：`HEADER_REMOVE` 是否被改回 `false`；或该页页眉被 layout 识别成了 `section-header`（属正文标题，方法正确但也需人工确认）。

### V2：正文完整性（必做）

```powershell
@'
from app.parse_log import latest_run_id, load_run_pages

pages = load_run_pages(latest_run_id())
no_text = [p["page"] for p in pages if not (p.get("text") or "").strip()]
print("无正文页:", no_text)
for p in pages:
    if p["page"] in no_text:
        print(f"  第 {p['page']} 页 mode={p['mode']} 版面块={len(p.get('elements') or [])}")

ratios = [(p["page"], round(p["chars"] / p["raw_chars"], 2)) for p in pages if p.get("raw_chars")]
low = [item for item in ratios if item[1] < 0.5]
print("清理后不足 50% 的页:", low)
print("字符合计: raw=%s text=%s" % (sum(p.get("raw_chars") or 0 for p in pages),
                                    sum(p.get("chars") or 0 for p in pages)))
'@ | .\.venv\Scripts\python.exe -
```

**通过标准**

- `无正文页` 只包含纯图片/空白广告页，逐页点开确认「应该是空的」；
- 比例过低的页（< 0.5）需查看是否被规范化删多了（正常只删图片占位符与多余空行）；
- `text` 合计不应显著小于 `raw`（本 PDF 实测 raw 27k+ / text 27k+ 量级）。

### V3：OCR 页抽查（必做）

```powershell
@'
from app.parse_log import latest_run_id, load_run, load_run_pages

meta = load_run(latest_run_id())
ocr_pages = (meta.get("counts") or {}).get("ocr_pages") or []
print("OCR 页:", ocr_pages)

pages = {p["page"]: p for p in load_run_pages(latest_run_id())}
for number in ocr_pages[:3]:
    p = pages.get(number) or {}
    print(f"\n=== 第 {number} 页 mode={p.get('mode')} ===")
    print((p.get("text") or "")[:400])
'@ | .\.venv\Scripts\python.exe -
```

**通过标准**：文字可读、数字和小数点无明显错乱。金融数字必须回看 PDF 原页，OCR 永远不能当作事实来源。

### V4：表格与公式（抽检）

```powershell
@'
from collections import Counter
from app.parse_log import latest_run_id, load_run_pages

pages = load_run_pages(latest_run_id())
classes = Counter()
for p in pages:
    for element in p.get("elements") or []:
        classes[element.get("class")] += 1
print("版面块分类:", dict(classes))

tables = [(p["page"], e["bbox"]) for p in pages for e in (p.get("elements") or [])
          if e.get("class") == "table"]
print("表格块数量:", len(tables), "示例:", tables[:5])
'@ | .\.venv\Scripts\python.exe -
```

**通过标准**：`table` 数量与文档中实际表格大致相符；随机挑 2–3 页，确认 Markdown 表格列没有错位。若表格识别不足，把 `TABLE_STRATEGY` 改为 `lines` 后重跑。

### V5：入库后检索验证（可选）

```powershell
.\.venv\Scripts\python.exe -m scripts.test_retrieval "期权价值会受到什么因素影响" --top-k 5
```

**通过标准**：返回的 `source` / `page` 与预期一致，`text` 是你刚解析出的正文（Markdown 形式），而不是旧的纯文本或页眉广告。

---

## 4. 验收清单

| 编号 | 检查项 | 通过标准 | 结果 |
|---|---|---|---|
| V0-1 | 日志 schema | `schema_version = 2` | ☐ |
| V0-2 | 页数完整 | `pages` = PDF 页数，无缺页 | ☐ |
| V0-3 | 字段完整 | 无缺字段页 | ☐ |
| V0-4 | 字符安全 | 无代理字符页（JSON 可正常读取） | ☐ |
| V1-1 | 页眉剔除 | 广告语在 `text` 中 0 命中 | ☐ |
| V1-2 | 审计留痕 | `page-header` 块存在且 `pos=[0,0]` | ☐ |
| V2-1 | 空页 | 空页清单均为纯图/空白页 | ☐ |
| V2-2 | 文本量 | `chars` 与 `raw_chars` 比例正常 | ☐ |
| V2-3 | 切块 | `chunks.jsonl` 行数 = `counts.chunks` | ☐ |
| V3-1 | OCR 可读 | 抽取 OCR 页文字可读 | ☐ |
| V3-2 | 数字核对 | 关键数字与 PDF 原页一致 | ☐ |
| V4-1 | 表格 | 表格块数量、列结构无明显错误 | ☐ |
| V5-1 | 检索 | Top-5 命中预期页码 | ☐ |

---

## 5. 常见问题排查

| 症状 | 原因 | 处理 |
|---|---|---|
| 纯图片页 `mode=image_only`、`text` 为空 | OCR 未触发 | 确认 `opencv-python` 已装、`ENABLE_OCR=true`、`TESSDATA_PREFIX` 指向 tessdata |
| 页眉还在正文开头 | `HEADER_REMOVE=false`，或该行被识别为 `section-header` | 检查 `.env`；用 V1 脚本看 `elements` 里该块的 `class` |
| `elements` 全为空 | layout 不可用（缺 `onnxruntime`）或 `LAYOUT_ENABLED=false` | 安装 `onnxruntime` 并确认 `python -c "import pymupdf; pymupdf.use_layout"` |
| 写日志报 `UnicodeEncodeError` | 出现孤立代理字符 | 代码已清洗；若自行脚本写 JSON，需同样用 `app.text_utils.sanitize_text` |
| `attempt to write a readonly database` | Chroma 数据库被占用或无写权限 | 停掉 uvicorn、确认 `data/chroma_db` 可写 |
| `RapidOCR is missing` 提示 | 未装 RapidOCR（可选） | 忽略，或 `pip install rapidocr` 提升精度 |
| 表格错位 | 无框线表格 | 改 `TABLE_STRATEGY=lines` 重跑 |

---

## 6. 回滚与重跑

```powershell
# 只删解析日志
.\.venv\Scripts\python.exe -m scripts.reset_db --purge-parse-logs --yes

# 重新解析（不入库）
.\.venv\Scripts\python.exe -m scripts.parse_pdf data\source\frm_notes.pdf

# 参数对比实验：改 HEADER_REMOVE / TABLE_STRATEGY 后重跑，比较 pages.jsonl 的 raw_chars 与 chunks.jsonl 行数
```
