# PDF 混合解析使用说明

更新日期：2026-10-26。

当前链路：固定PDF原件 → PyMuPDF预检 → 普通文字/PyMuPDF、复杂内容/MinerU、普通扫描文字/Tesseract → 区域融合/OCR完成 → 统一块清洗（页眉广告） → 跨页逻辑块 → 类型感知切块 → 质量检查 → 人工确认 → 本地向量入库。

## 1. 环境和配置

项目环境使用Python 3.12和`pymupdf==2.0`。MinerU继续使用已有独立环境，主环境不要再次安装MinerU：

```text
解释器：E:\learn_space\mineru\env\.venv\Scripts\python.exe
模型：  E:\learn_space\mineru\model
版本：  MinerU 4.0.12
档位：  standard
```

实际配置文件为项目根目录`.env`，程序读取该文件；`.env.example`仅是参考模板。保留已有`.env`中的密钥和设置，按需补齐：

```dotenv
MINERU_ENABLED=true
MINERU_PYTHON=E:\learn_space\mineru\env\.venv\Scripts\python.exe
MINERU_MODEL_BASE_DIR=E:\learn_space\mineru\model
MINERU_TIMEOUT_SECONDS=1800
HYBRID_COLLECTION_NAME=finance_pdf_chunks_v3
ENABLE_INDEXING=false
LAYOUT_ENABLED=true
ENABLE_OCR=true
OCR_LANG=chi_sim+eng
OCR_DPI=300
HEADER_REMOVE=true
FOOTER_REMOVE=false
HEADER_FILTER_TOP_RATIO=0.12
HEADER_REPEAT_MIN_PAGES=3
HEADER_REPEAT_MIN_RATIO=0.6
HEADER_REPEAT_POSITION_TOLERANCE=0.02
HEADER_AD_PATTERNS='["高顿学术中心独家研发学习资料[，,]未经许可[，,]不得私自传播[。.]?"]'
```

代码为MinerU子进程显式设置`MINERU_MODEL_SOURCE=local`、ONNX小模型和llama-cpp本地后端，并清空继承的远程VLM地址。`standard`、MinerU并发1、schema v3及人工确认门禁是当前固定约束。模型路径检查通过只表示必需路径存在，不证明权重内容hash、推理准确率或性能合格。

Tesseract需要实际安装和`chi_sim/eng`语言包；按本机位置填写`TESSERACT_CMD`。只有已确认的普通文字区域才交Tesseract；复杂表格/公式失败不会自动改成普通OCR并通过。

本地Embedding默认只读取已缓存模型。`EMBEDDING_MODEL`可填本地模型目录，或已缓存的`BAAI/bge-small-zh-v1.5`；找不到模型时会报错，不自动联网下载。文本预算按真实tokenizer和模型上限检查，`CHUNK_SIZE=700`不是700个token。

## 2. 解析：先生成审阅产物

在项目根目录执行：

```powershell
# 解析单个文件；不入库。
.\.venv\Scripts\python.exe -m scripts.parse_pdf data\source\frm_notes.pdf

# 省略文件时，解析data/source中的PDF。
.\.venv\Scripts\python.exe -m scripts.parse_pdf
```

记录输出的`run_id`。原PDF会固定到`data/documents/<doc_id>/<pdf_sha256>/source.pdf`，同名文件更新不覆盖历史原件；审阅定位始终引用对应版本。

主要解析产物：

```text
data/parse_log/<run_id>/
├── run.json               # parse/quality/review/index分别记录
├── manifest.json          # 密封产物清单及hash
├── pages.jsonl            # 原PDF每个物理页，含空白和失败
├── blocks.jsonl           # 原生/识别候选及最终选用区域
├── logical_blocks.jsonl   # 跨页段落、父表及结构
├── merges.jsonl           # 续接候选、规则、证据和决策
├── chunks.jsonl           # 最终正文、embedding_text与精确来源
├── events.jsonl           # 当前解析任务的结构化页面事件
├── raw/pymupdf/...        # 原生及layout证据
├── raw/mineru/...         # ZIP、结构化JSON及stdout/stderr
└── assets/...             # 原页区域裁图
```

同一PDF的后台任务会等待MinerU子进程返回，再校验产物、融合区域并完成必要OCR。全部页混合结果齐备后，调用`clean_document_blocks()`统一清洗一次页眉广告，随后生成页面正文、拼接跨页逻辑块和切块。MinerU CLI按原PDF的页码选页，未先拆成重新编号的小PDF；例如原第28–29页始终记录为28、29。页内没有可靠分区时整体采用MinerU结果，不把原生整页文字再追加一遍。

顶部12%仅为候选范围，不是固定裁切。明确header分类、已知广告整行规则或达到默认3页/60%且位置一致的短重复文本用于判断；同块正文只有具备准确行文本和bbox时才拆分，否则标记待核对。原文和被排除块保留，`pages.text`、逻辑块和`embedding_text`来自清洗后的选中块。详细方法、配置与日志见[页眉广告统一块清洗说明](页眉广告统一块清洗说明.md)。

旧run和向量不会自动清洗，需要重新解析、人工审阅和入库，成功后替换同doc_id的活动版本。API更新已有文档须显式传原doc_id。

`--no-log`仅用于诊断，不能生成可人工确认并正式入库的依据。

## 3. MinerU、Tesseract页面打印日志

控制台显示简洁日志，`data/logs/app.log`保存结构化运行日志，当前任务`events.jsonl`保存带run/页码等字段的解析事件。日志页码均为原PDF的一基物理页码。

MinerU示例形态：

```text
MinerU 页面=28 page_started batch_pages=[28, 29] tier=standard
MinerU 页面=29 page_started batch_pages=[28, 29] tier=standard
... 独立子进程执行，主解析任务等待 ...
MinerU 页面=28 page_completed ...
MinerU 页面=29 page_completed ...
```

`page_started`表示本批次已接收该页的解析请求，可能仍在等待并发槽或模型预检；不是模型已经完成该页。当前CLI整批返回后，适配器逐页校验并打印`page_completed`，不能据此推断逐页推理完成时间。命中缓存会标明缓存信息；失败打印`page_failed`及错误/诊断文件。stdout/stderr保留原始子进程进度，不作为正文解析来源。

Tesseract示例形态：

```text
[tesseract] page_started page=3 region=<block_id> reason=ordinary_text_fallback ...
[tesseract] page_completed page=3 region=<block_id> ... chars=123 duration_ms=456
```

这里的开始/完成是具体普通文字区域的OCR调用；同一页多个区域会有多组记录。失败打印`page_failed`、原页码、区域ID、耗时和错误，并将问题留在质量记录中。所有引擎完成后，`page_finalized`记录该页最终路由、字符数和质量状态。

`header_excluded`记录页眉广告排除规则、原页码和块ID；`cleaning_completed`记录每页清洗统计。最终页面日志和`page_finalized`均在全文混合解析与统一清洗完成后生成，原始引擎事件仍实时追加。

## 4. 检查并人工确认

先查看完整入库文本：

```powershell
.\.venv\Scripts\python.exe -m scripts.review_parsed --run-id <run_id> --show-all
```

重点对照原PDF检查表格数字、负号、小数点、单位、公式、续表关系、页码和区域位置。图表只恢复可识别内容；没有可靠提取的图意/曲线数值不作为事实入库。HTML表格、公式与图片可在UI中检查。

只有`quality_status=pass`的完整密封产物可以确认。确认命令由实际检查者执行：

```powershell
.\.venv\Scripts\python.exe -m scripts.review_parsed --run-id <run_id> `
  --reviewer "实际检查者" --approve --notes "已核对原PDF、表格公式及跨页来源"
```

不添加`--approve/--reject`时仅预览。`--reject`用于记录拒绝决定。程序不会把质量自动通过当成人工确认，也不会因为调用入库脚本就自动批准。

若存在`needs_review/failed`，先检查事件与原始产物，修复识别、配置或拼接问题，再重新解析并确认新产物。不能直接编辑`run.json`或手写`review.json`绕过问题；正文、资产或切块变化会使既有manifest/确认记录失效。

## 5. 显式入库

```powershell
.\.venv\Scripts\python.exe -m scripts.index_parsed --list
.\.venv\Scripts\python.exe -m scripts.index_parsed --run-id <run_id>
```

脚本再次校验schema、质量、完整文件、hash、人工确认及模型身份，基于审阅过的`embedding_text`生成向量。新数据先暂存、读回核验，再激活对应文档索引；失败保留旧活动版本。`ENABLE_INDEXING=true`与上传的`index=true`均不能绕过人工确认。

v3使用独立`HYBRID_COLLECTION_NAME`，旧`COLLECTION_NAME`对应的数据不会自动迁移、清空或混入新集合。旧日志缺少新结构与来源，需重新解析、人工确认后进入v3。

## 6. API与界面

首版使用本机单服务进程、单worker队列，启动命令不要增加多个uvicorn worker：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.api:app --host 127.0.0.1 --port 8000 --workers 1
```

| 地址/方法 | 用途 |
|---|---|
| `GET /ui` | 活动知识库、后台任务和解析概览 |
| `GET /ui/runs` | 解析记录 |
| `GET /ui/runs/<run_id>` | 逐页结果、问题、合并证据、图片和人工确认入口 |
| `GET /ui/chunks` | 已激活切块与页范围 |
| `POST /documents/index` | 上传并提交后台解析，返回202及job_id；更新已有文档显式传doc_id |
| `GET /jobs/<job_id>` | 后台任务状态与完成后的run_id |
| `POST /jobs/<job_id>/cancel` | 取消排队/运行中的解析；已开始入库不能声称可安全取消 |
| `POST /runs/<run_id>/review` | JSON包含reviewer、decision、notes，确认绑定当前manifest |
| `POST /runs/<run_id>/index` | 已确认产物提交后台入库，返回202及job_id |
| `POST /retrieval/search` | 本地向量检索 |
| `GET /docs` | 实际API参数与交互说明 |

上传请求立即返回，后台任务在等MinerU时API仍可响应状态查询。PDF B会在PDF A解析完成后开始，不需要等A被人工确认。服务重启时未完成任务标为中断，不自动宣称成功或重复执行。

列表、详情、来源统计、健康统计与检索均读取活动索引，暂存/旧版本不会作为当前知识库显示。来源支持跨页范围和精确页面，不只引用第一页。

## 7. 测试与当前验证边界

```powershell
# 默认运行不需要真实模型的测试。
.\.venv\Scripts\python.exe -m pytest -q

# 显式选择集成类别时，先确认使用独立测试数据/集合。
.\.venv\Scripts\python.exe -m pytest -m integration
```

默认排除`integration/mineru`标记，避免测试自动启动大型模型或写正式索引。已使用小型PDF验证PyMuPDF 2.0布局接口，并用模拟子进程和固定结构化样本验证调用、页码、路由、日志和失败处理。本次尚未完成真实财务PDF的全量MinerU准确率、冷启动耗时和资源性能验收，不能把单元测试通过当作金融数据正确性验收。

实施验证中另完成真实独立MinerU对原第5页的调用与产物校验，以及真实Tesseract对生成文字图片的识别；具体结果见[改动记录](PDF混合解析改动记录.md)。这些局部调用不替代整份文档的人工准确率验收。
