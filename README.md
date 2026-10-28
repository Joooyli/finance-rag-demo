# 本地中文金融 PDF RAG 学习项目

PDF/图片 OCR → 文本切块 → 本地 BGE Embedding → 本地 Chroma → 本地检索 → 云端 OpenAI 兼容 Chat Completions API。

**解析与入库拆成两步**：先解析并把每个切块写入日志文件，人工检查确认后再写入向量库。默认只解析、不入库。

## 1. 环境要求

- Windows 10/11 x64，推荐 Python 3.12 x64。
- `chromadb==0.6.3` 固定依赖 `chroma-hnswlib==0.7.6`。该版本提供 Windows Python 3.12 x64 wheel，但没有 Windows Python 3.12/3.13 wheel。在 3.12/3.13 上 pip 会尝试源码编译，未安装 Microsoft C++ Build Tools 时报 `Microsoft Visual C++ 14.0 or greater is required`。
- 解析器使用 **PyMuPDF 2.x**（`pymupdf>=2`），它内置版面分析（layout）与 Markdown 转换；
- layout 依赖 `onnxruntime`，OCR 触发还依赖 `opencv-python`，两者都在 `requirements.txt` 里。
- OCR 需要 Tesseract，并安装 `chi_sim` 与 `eng` 语言包；PyMuPDF 通过 `PATH` 与 `TESSDATA_PREFIX` 找到它，代码会根据 `TESSERACT_CMD` 自动推导。
- 普通办公电脑可先用 CPU；首次加载 `BAAI/bge-small-zh-v1.5` 需要网络，缓存后可离线。
- 云端问答需要兼容 OpenAI `POST /chat/completions` 的地址、密钥与模型名。

## 2. 创建虚拟环境

在 PowerShell 进入项目目录：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -V                      # 此方案应显示 3.12.x；显示 3.12/3.13 说明用错了解释器
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

若 PowerShell 阻止激活脚本，先执行 `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` 再激活；也可以始终直接用 `.\.venv\Scripts\python.exe` 执行命令。

## 3. 配置

编辑 `.env`（不要提交到 Git）：

```dotenv
LLM_BASE_URL=https://服务商域名/v1
LLM_API_KEY=真实密钥
LLM_MODEL=模型名

# 解析/入库开关
ENABLE_INDEXING=false          # 检查完解析结果后改为 true，或用 scripts.index_parsed 显式入库
PARSE_LOG_ENABLED=true         # 是否把中间解析结果写入 data/parse_log
```

完整可配置项见 `.env.example`，常用项：

| 配置 | 默认值 | 作用 |
|---|---|---|
| `ENABLE_INDEXING` | `false` | 是否把切块写入 Chroma |
| `PARSE_LOG_ENABLED` | `true` | 是否写中间解析日志 |
| `PARSE_LOG_DIR` | `data/parse_log` | 解析日志目录 |
| `HEADER_REMOVE` / `FOOTER_REMOVE` | `true` / `true` | 解析阶段剔除页眉/页脚（广告语、页码） |
| `LAYOUT_ENABLED` | `true` | 是否启用版面分析；关闭后 `elements` 为空 |
| `TABLE_STRATEGY` | `lines_strict` | 表格识别策略，无框线表格可试 `lines` |
| `OCR_DPI` | `300` | OCR 渲染精度 |
| `PARSE_LOG_INCLUDE_ELEMENTS` / `PARSE_LOG_ELEMENT_TEXT_MAX` | `true` / `200` | 日志里是否写版面块及其文本预览长度 |
| `CHROMA_DIR` | `data/chroma_db` | 向量库目录 |
| `COLLECTION_NAME` | `finance_pdf_chunks` | 集合名 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `700` / `100` | 切块大小与重叠字符数 |
| `ENABLE_OCR` | `true` | 是否 OCR |
| `OCR_LANG` | `chi_sim+eng` | Tesseract 语言 |
| `TESSERACT_CMD` | 空 | Tesseract 可执行文件路径 |
| `OCR_LOW_TEXT_PAGES` / `MIN_NATIVE_TEXT_CHARS` | `true` / `80` | 低文本页整页 OCR |
| `LOG_LEVEL` / `LOG_TO_FILE` | `INFO` / `true` | 运行日志 |
| `INDEX_BATCH_SIZE` | `32` | 向量化批大小 |

## 4. 安装 OCR（可选但建议）

```dotenv
TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
OCR_LANG=chi_sim+eng
ENABLE_OCR=true
```

PyMuPDF 通过 MuPDF 调用 Tesseract，需要能找到 `tesseract.exe` 与 `tessdata`。程序会依据 `TESSERACT_CMD` 自动把所在目录加入 `PATH` 并设置 `TESSDATA_PREFIX`，一般无需手工配置环境变量。

验证 OCR 是否生效：解析后看 `pages.jsonl`，纯图片页的 `mode` 应为 `ocr` 且 `text` 非空；`run.json` 里 `ocr.engine` 应为 `tesseract`。若纯图片页仍是 `image_only`，说明 Tesseract 或 `opencv-python` 缺失。

不需要 OCR 时设 `ENABLE_OCR=false`。OCR 会误识别金融数字、负号、小数点与表格列，重要数值必须回看原页。

## 5. 第一步：解析并检查（不入库）

> 完整的操作流程、逐项验证脚本与验收清单见 [docs/PDF解析操作与验证.md](docs/PDF解析操作与验证.md)。

将 PDF 放入 `data/source/`，然后解析全部 PDF：

```powershell
python -m scripts.parse_pdf
```

也可以指定文件、或只打印统计而不写日志：

```powershell
python -m scripts.parse_pdf data\source\frm_notes.pdf
python -m scripts.parse_pdf --no-log
python -m scripts.parse_pdf --json
```

每次解析生成一个目录：

```text
data/parse_log/20250612-103015__frm_notes__a1b2c3d4/
├── run.json      # 状态、PDF 哈希、解析器版本、页眉/OCR/版面参数、统计
├── pages.jsonl   # 每页一行（schema v2）：版面块、原文、清洗后正文
└── chunks.jsonl  # 每个切块一行：chunk_id、页码、块序号、字符数、文本
```

`pages.jsonl` 每行的核心字段：

| 字段 | 含义 |
|---|---|
| `schema_version` / `extractor` / `extractor_version` | 日志版本与解析器（`pymupdf.to_markdown` / `2.0`） |
| `page` / `page_width` / `page_height` / `rotation` | 页码与页面几何（`page.rect`，已含旋转） |
| `mode` | `native` / `ocr` / `mixed` / `image_only` / `empty` / `failed` |
| `elements` | 版面块：`index` / `class` / `bbox` / `pos` / `text`（前 200 字） |
| `raw_text` | 剔除页眉后的 Markdown 原文（未做额外清洗） |
| `text` | 规范化后的正文，切块使用的就是它 |
| `raw_chars` / `chars` / `text_sha1` | 长度与内容指纹 |

注意：`elements[].pos` 指向 `raw_text`，**不要**用它去索引 `text`（规范化会改变长度）。

检查要点：

- 页眉/页脚已在解析阶段剔除（`HEADER_REMOVE=true`），`elements` 里仍保留 `class="page-header"` 的块（`pos` 为 `[0, 0]`），可据此确认删除范围；
- `mode` 为 `ocr` / `mixed` 的页，人工核对数字与表格；
- `mode` 为 `image_only` / `empty` 的页，确认是广告页还是解析失败；
- 页数是否与 PDF 一致；
- `chunks.jsonl` 每行一个切块，确认数字、表格、标题没有被切断。

也可以启动服务后在浏览器检查（见第 7 节）：`http://127.0.0.1:8000/ui/runs`。

## 6. 第二步：入库

确认解析结果无误后，两种方式任选：

```powershell
# 方式一：把 .env 中 ENABLE_INDEXING 改为 true，再重新解析（会重新解析 PDF）
python -m scripts.parse_pdf

# 方式二（推荐）：直接索引已检查过的解析日志，不重新解析 PDF
python -m scripts.index_parsed                # 使用最近一次解析记录
python -m scripts.index_parsed --list         # 查看可用记录
python -m scripts.index_parsed --run-id 20250612-103015__frm_notes__a1b2c3d4
```

方式二保证「你检查过的内容」就是「入库的内容」。

通过 API 上传时可用 `index` 参数临时覆盖开关：

```powershell
# 只解析不入库
curl.exe -X POST "http://127.0.0.1:8000/documents/index?index=false" -F "file=@data/source/frm_notes.pdf"

# 解析并入库
curl.exe -X POST "http://127.0.0.1:8000/documents/index?index=true" -F "file=@data/source/frm_notes.pdf"
```

首次入库会下载并加载 Embedding 模型，需要等待。

## 7. 可视化页面

启动服务：

```powershell
python -m uvicorn app.api:app --host 127.0.0.1 --port 8000
```

| 地址 | 内容 |
|---|---|
| `http://127.0.0.1:8000/ui` | 概览：集合、切块数、文件分布、最近解析记录 |
| `http://127.0.0.1:8000/ui/chunks` | 已入库切块表格：页码、块序号、字符数、文本；支持关键词/文件/页码筛选与分页 |
| `http://127.0.0.1:8000/ui/chunks/{chunk_id}` | 单个切块全文、元数据、同页其他切块 |
| `http://127.0.0.1:8000/ui/runs` | 全部解析记录 |
| `http://127.0.0.1:8000/ui/runs/{run_id}` | 单次解析：逐页的解析方式、原生/OCR 字符数、切块数、全文 |
| `http://127.0.0.1:8000/docs` | OpenAPI 交互文档 |

服务只绑定 `127.0.0.1`，仅本机可访问。页面内容包含 PDF 原文，注意敏感信息。

## 8. 删除或重置数据

独立脚本 `scripts/reset_db.py`，破坏性操作必须加 `--yes`，否则只打印将要删除的内容：

```powershell
python -m scripts.reset_db --list                        # 列出集合、切块数、解析记录
python -m scripts.reset_db --delete-source frm_notes.pdf --yes   # 删除某个文件的所有切块
python -m scripts.reset_db --wipe --yes                  # 清空当前集合（保留集合）
python -m scripts.reset_db --drop-collection finance_pdf_chunks --yes  # 删除整个集合
python -m scripts.reset_db --purge-parse-logs --yes      # 删除全部解析日志
python -m scripts.reset_db --delete-log-run <run_id> --yes
```
预览输出集合
python -c "from scripts.reset_db import drop_collection; drop_collection('finance_pdf_chunks', assume_yes=False)"
确认输出集合
python -c "from scripts.reset_db import drop_collection; drop_collection('finance_pdf_chunks', assume_yes=True)"


先停止 uvicorn 再执行，避免 Windows 下 SQLite 文件被占用。PDF 原文件不会被删除。

## 9. 检索与问答

```powershell
# 本地检索（不调用云端）
curl.exe -X POST "http://127.0.0.1:8000/retrieval/search" -H "Content-Type: application/json" -d '{\"question\":\"什么是市场风险？\",\"top_k\":5}'

# 问答（把问题和检索片段发给云端 LLM）
curl.exe -X POST "http://127.0.0.1:8000/chat" -H "Content-Type: application/json" -d '{\"question\":\"这份材料如何定义市场风险？\",\"top_k\":5}'

# 健康检查
curl.exe "http://127.0.0.1:8000/health"
```

不启动 API 的检索测试：

```powershell
python -m scripts.test_retrieval "期权价值会受到什么影响" --top-k 5
```

`/chat` 只上传问题与检索到的文本片段，不上传原始 PDF 和本地向量；服务商仍可能记录 API 请求。

单元测试：

```powershell
python -m pytest
```

## 10. 切块参数

默认 `CHUNK_SIZE=700`、`CHUNK_OVERLAP=100`，按字符切分，尽量在段落/句子边界结束，**按页独立切块**。它是教学基线，不是最优参数。可用 400/80、700/100、1000/150 分别解析入库，对比同一组问题的 Top-K 命中。

## 11. PDF 与表格解析边界

解析器是 PyMuPDF 2.x 的 layout + Markdown 转换：版面块会被分类为 `text` / `list-item` / `table` / `picture` / `formula` / `section-header` / `page-header` / `page-footer`，页眉页脚默认剔除。图片表格依旧可能丢失行列结构；跨页表格、多栏、复杂公式或低质量扫描件建议先用 MinerU 解析成 Markdown/JSON 再接入 `app/chunker.py`。

## 12. 重要限制

- 学习用 MVP，不是生产级金融风控系统。
- 向量相似度不是答案正确率；应建立人工问题集核验 Top-K 命中、来源页码与回答忠实度。
- 文件名作为来源标记；同名文件内容变化后旧 chunk 可能残留，用 `scripts.reset_db.py --delete-source` 清理后再入库。
- 不能把 OCR 数字直接当作金融事实。
- 不要把真实客户信息、未公开风险报告或受监管数据发送到未获批准的云 API。

## 13. 目录结构

```text
app/
├── config.py          # 环境变量与参数
├── logging_config.py  # 运行日志（控制台 + data/logs/app.log）
├── parse_log.py       # 解析日志：run.json / pages.jsonl / chunks.jsonl
├── text_utils.py      # 文本清洗（代理字符、图片标记、空行）
├── pdf_loader.py      # PyMuPDF 2.x 解析：markdown + 版面块 + 页眉剔除
├── chunker.py         # 文本切块
├── embeddings.py      # 本地向量
├── vector_store.py    # Chroma 读写
├── rag_pipeline.py    # 解析、入库、检索、问答
├── web_ui.py          # /ui 页面路由
├── templates/         # 页面模板
└── api.py             # FastAPI 入口
scripts/
├── parse_pdf.py       # 只解析、写日志
├── index_parsed.py    # 用解析日志入库
├── reset_db.py        # 删除集合/切块/解析日志
└── test_retrieval.py  # 本地检索测试
data/
├── source/            # PDF 原文件
├── chroma_db/         # 向量库
├── parse_log/         # 解析日志（每次一个目录）
└── logs/              # 运行日志
```
