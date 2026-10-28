# PDF 混合解析详细设计

日期：2026-10-26。状态：设计基线，后续已实施核心代码；实际行为、验证和当前边界见[改动记录](PDF混合解析改动记录.md)。本文件的“当前核实/本轮未执行”记录原设计阶段的审查，不代替后续实施验证。

配套方案：[PDF混合解析方案.md](PDF混合解析方案.md)。本文定义后续实施所需的数据契约、调用边界、路由、跨页策略、日志、入库门禁和代码改动点。示例代码与新配置仅用于设计说明，当前项目尚不支持。

## 1. 设计约束与事实来源

### 1.1 已确认约束

- 普通文字优先 PyMuPDF 2.0，复杂内容 MinerU standard，普通文字 OCR 兜底使用 Tesseract。
- MinerU 独立环境：`E:\learn_space\mineru\env\.venv`；本地模型：`E:\learn_space\mineru\model`。
- 提取实际可识别的内容，保留图片与原页位置；图表语义、曲线数值不作自动恢复承诺。
- 完整解析记录通过质量检查并经人工确认后才能入库；失败、存疑记录保留待核对。
- 继续沿用本地 Embedding 与 Chroma，问答接口保持现有整体方向。

### 1.2 本轮已核实与未核实

| 类别 | 结论 |
|---|---|
| 项目依赖 | `.venv` 为 Python 3.12.10，PyMuPDF 2.0；Chroma 依赖为 0.6.3 |
| PyMuPDF 接口 | `Document.to_markdown` 可用，`pymupdf.to_markdown` 不存在；可返回 `page_boxes` |
| MinerU 版本 | 独立环境安装 4.0.12；`python -B -m mineru.kit.main parse --help` 可用 |
| Standard | 当前版本映射 `effort="high"`，本机 ONNX 与 GGUF 模型路径检查通过 |
| 本地模型预检 | `models verify --tier standard --small-backend onnx --vlm-engine llama-cpp` 在显式 local 环境下退出码为 0；仅检查必需路径齐备，未校验权重内容hash或推理可用性 |
| 已有输出样本 | 只读检查 `E:\learn_space\mineru\resultmd\table-p28-p29.zip`，确认 JSON 结构、页号、表格 HTML、图片及续表标记 |
| 本轮未执行 | 新的 PDF 解析、模型推理、向量化、入库、依赖安装与配置修改 |
| 尚需实测 | 识别准确率、旋转页坐标映射、路由阈值、跨页拼接、性能和资源消耗 |

关键源码依据：

- 当前项目：`app/pdf_loader.py:35`、`app/rag_pipeline.py:44`、`app/parse_log.py:23`、`app/vector_store.py:17`。
- 项目环境：`.venv/Lib/site-packages/pymupdf/__init__.py` 的 `Document.to_markdown`；`chromadb/api/types.py:535` 的 metadata 校验。
- MinerU 独立环境 `Lib/site-packages`：`mineru/kit/commands/parse.py:29`、`mineru/kit/main.py:76`、`mineru/parser/tier.py:17`、`mineru/model/download.py:178`。
- MinerU 数据契约：`docvortex/schema.py:464` 的 bbox、`:709` 的表格属性、`:1040` 的 PageInfo；`docvortex/document/pdf/_document.py:500` 的视觉页面尺寸。

以上行号定位当前审查版本，未来升级应重新核对接口。

## 2. 模块边界与处理阶段

```text
parse_document
  ├─ 固定输入文件、文档身份、版本与配置快照
  ├─ inspect_pdf：保留所有物理页，提取页面几何、原生 spans 与 layout
  ├─ plan_routes：页面预检、区域分类、质量分流、邻页上下文扩展
  ├─ execute_routes
  │    ├─ PyMuPDF native
  │    ├─ Tesseract ordinary-text fallback
  │    └─ MinerU independent-process adapter
  ├─ normalize_and_fuse：统一坐标/类型/顺序、候选选择、去重
  ├─ assemble_document：跨页段落、列表、续表和关联关系
  ├─ build_typed_chunks：完整行/公式保护、token预算、精确来源
  └─ validate_and_seal：写产物、完整性校验、等待人工审阅

review_run
  └─ 针对密封产物 manifest 记录人工确认

index_parsed_run / index_pdf
  └─ 同一门禁 → 同一序列化器 → Embedding → 暂存索引版本 → 校验 → 激活
```

页面物理内容、逻辑内容、检索 chunk 分开保存。页面边界负责定位，逻辑块边界负责语义，chunk 边界负责检索预算。

`data/source` 作为输入收集位置；进入解析前按 PDF hash 固定不可变原件，例如 `data/documents/<doc_id>/<pdf_sha256>/source.pdf`，审阅、MinerU 与后续定位均引用该版本。不能继续由同名上传覆盖已审阅原件。API 更新已有文档时显式传 doc_id；未指定时创建新文档并返回身份，不因同名文件静默替换。长时间解析通过本机 worker 执行，上传接口返回任务/run 标识，UI 查询状态，避免直接阻塞当前 async handler。

## 3. MinerU 调用契约

### 3.1 采用独立 Python 子进程

不把 MinerU 加入主环境 `requirements.txt`，也不向主进程 `sys.path` 注入独立环境。主项目调用独立解释器并读取结构化产物，避免 Pydantic、OpenAI、NumPy 等依赖版本互相影响。

已核实的命令形态：

```powershell
# 以下仅展示调用形态，后续由适配器为子进程设置环境变量。
& 'E:\learn_space\mineru\env\.venv\Scripts\python.exe' -B -m mineru.kit.main parse `
  'E:\learn_space\finance-rag-learning\finance-rag-learning\data\source\frm_notes.pdf' `
  --pages '28-29' --tier standard --format zip `
  -o '<本次解析目录>\raw\mineru\job-0001.zip'
```

Windows 不需要激活环境；适配器用参数数组和 `shell=False`。生产实现使用可取消的 `Popen` 管理任务、超时与输出，以下示意的是参数和环境契约：

```python
# 设计示意，不是本轮代码修改。
argv = [
    str(mineru_python), "-B", "-m", "mineru.kit.main", "parse",
    str(original_pdf), "--pages", "28-29", "--tier", "standard",
    "--format", "zip", "--ocr-mode", "auto", "-o", str(output_zip),
]
child_env = os.environ.copy()
child_env.update({
    "MINERU_MODEL_BASE_DIR": str(model_dir),
    "MINERU_MODEL_SOURCE": "local",
    "MINERU_MODEL_SMALL_BACKEND": "onnx",
    "MINERU_MODEL_VLM_ENGINE": "llama-cpp",
    "MINERU_MODEL_VLM_SERVER_URL": "",
    "PYTHONUTF8": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
})
# 启动时 shell=False；stdout/stderr 写独立文件，结束后检查退出码和ZIP。
```

`local` 是模型源配置值，不是 `models download --source` 的合法下载源。不给命令添加远程解析参数，显式清空远程 VLM 地址。local 保证使用本地模型解析路径；是否存在其他外部访问应在离线验收中验证，不将路径校验当作网络审计。

### 3.2 任务计划、生命周期与缓存

1. 所有 CLI 页码使用原 PDF **一基物理页码**。不先拆 PDF，避免重新编号。
2. 将相邻复杂页合成一次任务；疑似跨页表格把相邻页加入同一任务。CLI 支持 `--pages "1-5,8"`，首版优先连续页组。
3. 首版以一个文档为任务边界，已选复杂页可优先合并一次调用，减少模型冷启动；大文档是否分批根据实测内存决定。
4. MinerU 默认并发 1，避免与本地 Embedding 同时争用大量资源。超时为配置项，初值暂定 1800 秒，必须根据冷启动和页数调整，不声称固定时限适合所有机器。
5. stdout/stderr 可能有进度或非 JSON 日志，只保存诊断，不据此解析正文。退出码 0 后仍需检查输出存在、ZIP 完整、schema、页集合及块字段。
6. 超时/取消须终止任务及其子进程树，保存已产生证据；任务未完全终止前不释放并发名额。不使用可能遗留 worker 的简单 `run(timeout=...)` 作为完整生命周期方案。
7. 缓存键包含 PDF SHA256、页集合、MinerU 版本、tier、ocr_mode、图像分析参数、模型文件清单指纹和适配器版本。命中时校验产物 hash。
8. 默认不盲目重试；明确瞬时资源错误可有限重试并记录 attempt。缺模型、schema 不支持、页集合错误应直接中止该路径。

CLI 没有区域 bbox 参数。首版的区域路由节省的是未选中页面的推理量；被选页面中的普通区域仍可能被 MinerU 计算，但最终只按融合规则采用结果。

### 3.3 当前输出的适配规则

ZIP 中已确认包含：

```text
markdown.md
middle_json.json
structured_content.json
model_output.json
images/*
```

以 `middle_json.json` 为主要适配输入，其当前结构示意：

```json
{
  "schema": "docvortex.middle",
  "schema_version": "2.0",
  "metadata": {
    "producer": {"name": "mineru", "version": "4.0.12"},
    "document": {"page_count": 46, "page_count_kind": "physical"}
  },
  "pages": [
    {"page_idx": 27, "blocks": []},
    {"page_idx": 28, "blocks": []}
  ],
  "is_full_document": false
}
```

- `page_idx + 1` 才是主项目物理页码；校验实际返回集合等于请求集合，不能按数组序号改编号。
- 此版本没有 `pdf_info`；PageInfo 不含宽、高、旋转，从原 PDF 补齐。
- `content` 可以是文本、嵌套块或行内 spans，按类型递归适配，不能统一 `str(content)`。
- `table` 父块下 `table_body.content` 为 HTML，保留 `rowspan/colspan/<eq>/<img>` 等信息；Markdown 只能作为预览。
- `continues_prev` 是跨页候选证据，不代表所有表格已合并；`cell_merge` 为可选字段，不能依赖其一定存在。
- `model_output.json.pages` 是二维原始块列表，另有 `page_index_map`；它不是 Middle 的 PageInfo。
- 图片正文不写 base64 入库；ZIP 提取的图片转为相对资产引用并保留 hash。提取时验证成员路径留在任务目录。
- 只接受已验证 schema 的版本适配器；未知 schema 明确 `unsupported_schema`，不能退回仅拼 Markdown 并标通过。

### 3.4 几何坐标

统一几何为：**原 PDF 物理页，视觉旋转后的可见页面，左上角原点，单位 point**。保存原 PDF 的 mediabox/cropbox/rotation、视觉宽高和正逆变换。

MinerU bbox 是严格 0–1 归一化坐标。对普通未裁剪、未旋转页可按视觉 `x*width、y*height` 映射；含 rotation/cropbox 时必须先验证引擎视口与 PyMuPDF 渲染视口一致，再做完整变换。PyMuPDF 原生文本坐标也先经过对应变换，不能直接与 MinerU bbox 算 IoU。

原始 bbox、原始坐标空间和变换版本都保留。旋转 90/180/270 度及非零 cropbox 原点须用叠框样本验收；对齐未通过时该页不得做区域融合，记录待核对。

## 4. 页面与区域质量路由

### 4.1 PyMuPDF 2.0 预检

- 用原 PDF 枚举全部页面，记录 blank/failed 状态，不丢页。
- 原生 spans 保存文字、字体、bbox；layout 提供 `text/table/formula/picture/section-header` 等区域候选和阅读顺序。
- `Document.to_markdown(..., page_chunks=True, use_ocr=False)` 显式禁止其隐式 OCR，避免先发生无法控制的混合识别。
- 页眉页脚先保留分类证据，再设置是否进入最终正文；当 REMOVE=true 时，API 对应参数是 `header=False/footer=False`。
- 结合 drawings/表格候选、数学符号及布局线索发现矢量表格/公式；`get_images()` 不是复杂内容检测器。
- PyMuPDF layout 的类型和 bbox 也是候选结果，不视为绝对真值；扫描页分类不明时进入 MinerU。

### 4.2 质量指标

| 指标 | 含义与用途 |
|---|---|
| native_chars / text_area_ratio | 文字数量及空间覆盖；低字数只作信号，标题页不能仅因少于 80 字触发 OCR |
| invalid_char_ratio | U+FFFD、代理字符、控制字符比例；长乱码同样可能失败 |
| duplicate_overlap_ratio | 高空间重叠且文本相近的重复文字候选 |
| image_area_ratio / visual_ink_ratio | 扫描图像与可见内容覆盖，识别“图像有内容但文本层缺失” |
| complex_area_ratio | 表格、公式、图形等复杂区域面积占比 |
| reading_order_flags | 多栏、交错区域、局部旋转等是否影响可靠排序 |
| structure_checks | 表格单元格/跨度是否合法、公式分隔是否闭合、输出是否异常重复 |
| source_geometry_valid | 页面/区域坐标是否有效且能映射回原页 |

规则输出 `quality_status=pass|needs_review|failed|blank`，外加 metrics、reason_codes 和 rule_version。不要把规则分数或 OCR 平均置信度当作“金融事实正确概率”。引擎不提供置信度时保留空缺，不填虚构分值。

暂定初始信号可用 invalid_char_ratio > 1%、complex_area_ratio > 35%；它们只触发复核/升级候选，需用样本标注校准，不单独决定识别成功。重复度检测同时使用空间重叠，避免误删不同位置相同的财务数字。

### 4.3 路由决策表

| 场景 | 解析选择 | 失败路径 |
|---|---|---|
| 普通原生文字，质量合格 | PyMuPDF | 有异常区域才进入后续候选 |
| 已确认普通扫描文字或普通文字原生提取失败 | Tesseract 区域 OCR，默认 300 dpi | 质量仍不合格则待核对；类型不明升级 MinerU |
| 表格、公式、图片/图表 | MinerU standard，保留结构与图像 | 不用 Tesseract 表格纯文字覆盖结构失败 |
| 多栏/复杂阅读顺序、复杂区域占比高、分区不可靠 | 整页 MinerU | 输出仍不合格则待核对 |
| 整页扫描且无法可靠区分正文/表格/公式 | 整页 MinerU | 不能假设是普通文字直接交 Tesseract 并通过 |
| 明确空白页 | 留空白页记录，不生成 chunk | 禁止跨越该页盲拼续段 |
| 引擎错误、缺模型、无有效结果 | 记录 failed/needs_review 和证据 | 阻断该完整解析记录入库 |

Tesseract 应优先识别已确定为普通文字的区域，可按区域版式选择 PSM；不继续对所有图片无差别 `--psm 6`。采用 `image_to_data` 时可保留词级坐标/置信度，坐标须由像素按 DPI 和裁剪变换回页面。

### 4.4 融合与所有权

每页在 route_plan 中明确每个区域最终由哪个引擎负责，并区分用于分析的候选结果与用于正文的最终结果。

- 普通原生区域采用 PyMuPDF；复杂区域根据 bbox 覆盖和语义类型匹配 MinerU 结果，替换原候选区域。
- 表格父块、表格单元格和图片中的 OCR 文字不能重复进入独立正文；子块作为结构证据保留。
- 用 bbox、类型、阅读顺序及文字相似性联合匹配；同一数字或短语在不同位置出现不能仅按文本删除。
- MinerU 块跨越多个原始区域、匹配歧义或重复消除不可靠时，先升级整页所有权，不强行拼接两套全页文本。
- 整页 MinerU 模式用其合格结果替换整页最终正文，原生结果留在 raw 证据中。
- 同页既采用 PyMuPDF 又采用 MinerU 的场景标记 `route="hybrid"`，不再用一个含糊的 `ocr/mixed` 表达引擎与质量。
- 标题、单位、图注、脚注和阅读顺序单独关联；处理方式标签只入 metadata，不拼进待向量化文本。

## 5. 文档级跨页组装

### 5.1 通用约束

输入是按物理页排序的**全部页面及最终区域**。只在物理相邻页建立自动续接关系；空白页、失败页、缺失页和明确章节边界阻断自动合并。

组装产出 logical blocks 与关系图，原始 blocks 不覆盖。每次决策写 `merges.jsonl`：来源块、规则版本、证据、接受/拒绝/待核对、人工修正。无标点、相似表头或 `continues_prev=true` 都不能单独作为合并充分条件。

### 5.2 段落与列表

联合判断前页末块/后页首块位置、字体/缩进、章节路径、文本类型、未闭合句法及是否出现新标题/新列表号。中文按语义续接，不加入任意空格；英文按明确断词规则恢复，所有文本变换留记录。列表内部续句可以跨页，新项目不能拼到旧项目中。

误拼风险高于保留分页时，保留两个块和候选关系，进入审阅。不以扩大 `CHUNK_OVERLAP` 代替跨页组装。

### 5.3 跨页表格

1. 保留每页 table fragment 的 HTML、表号/标题、单位、注释、bbox、原页号与列结构。
2. 使用相邻位置、表号/续表、表头、列数/边界和 MinerU continuation 提示形成候选。
3. 验证 rowspan/colspan 与列语义一致后归入同一父 `table_id`；`continues_prev` 仅作为证据之一。
4. 重复表头可在逻辑表删除，在检索子块重新附加；删除动作必须保留证据，不能误删普通数据行。
5. 每行/单元格保留原页来源；前页最后一行和后页第一行疑似同一行时，只有明确互补证据才合并。
6. 列数变化、单位冲突、缺少关键列或跨页单元格无法对应时，不自动拼接，进入待核对。
7. 金融数字、负号、括号、小数点、百分号、币种与量纲不做静默“纠错”；保存识别结果并回看原图确认。

### 5.4 公式、图像与注释

- 公式保存 LaTeX/MathML/原始图像引用；多个相邻公式不因位置接近而合成一个表达式。
- 跨页续式需要明确结构或人工确认，保留公式号、变量解释及来源。
- 图像保留资产、图号/标题、图注、已识别文字和 bbox。只有插图、无可检索正文的图片可以只保存资产，符合本期范围；本应恢复的表格或公式识别失败仍阻断入库。
- 图像中的趋势、曲线精确值等未被可靠提取时，不生成猜测描述。人工补充内容必须记录为人工转录及来源。
- 页脚中的正文脚注与装饰页码分开；删除页眉页脚不能丢失表格单位或重要注释。重复内容分类失误需在审阅中恢复。

## 6. 类型感知切块与 Embedding 文本

### 6.1 切块规则

| 类型 | chunk 规则 |
|---|---|
| 普通段落 | 在 logical block 上按段落/句子和 token 预算切；可涵盖多个相邻页 |
| 列表 | 优先保留完整项目与层级；过长项目按句子拆并携带父标题 |
| 表格 | 父表保留完整 HTML；子 chunk 按完整行组拆分，重复必要表头、单位、标题，保存父表和行范围 |
| 公式 | 表达式原子保存；合适时连同变量解释；不能从 LaTeX 中间截断 |
| 图像 | 可检索图注/识别文字与资产关联；不把图片路径、base64 当语义正文 |

先用实际 Embedding 模型的 tokenizer 和 `max_seq_length` 确定预算，并扣除标题、表头、单位和特殊 token 的占用。现有 `700` 字符仅可作初始软上限，不等于模型 token 上限。

超长表格单行可按列组拆分，但必须重复行键/列名且保持可解释对应；无法安全拆分的单行或超长公式标记待处理，不能依赖 `encode()` 静默截断。重叠只用于普通文字语境，不在表格中重复数据行；表头重复另行标记。

### 6.2 两种文本与引用一致性

- `text`：供人工检查的规范化可读原文，保留必要 Markdown/公式格式。
- `embedding_text`：确定性构造的检索文本，加入必要章节、表号、单位和表头；表格可采用“列名=值”的完整行表达，复杂合并单元格保留层级关系。
- 不使用 LLM 生成的业务总结替代原文，不把调试标签、处理方式、本地文件路径、HTML 标签和 base64 混入 Embedding。
- Chroma `documents` 保存 `embedding_text`，Embedding 也基于相同字符串；详情页可经 artifact_ref 查看 `text`、HTML、图像和来源。
- 检索返回的正文、进入 LLM 的正文、实际引用来源保持一致；若取父表补充上下文，要把补充块纳入引用及预算。
- 记录实际 token 数、`embedding_text_sha256` 和构造策略版本；审阅界面同时显示最终入库文本。

## 7. 统一数据契约与标识

### 7.1 文档、版本和 ID

| 标识 | 定义 |
|---|---|
| source | 仅用于显示的文件名，不作为唯一身份 |
| source_key | 来源身份：项目内规范相对路径；API 上传使用持久化文档标识，同名覆盖不能静默推断 |
| doc_id | source_key 对应的稳定逻辑文档 ID，由文档登记表维持；同一来源更新 PDF 后保持不变 |
| pdf_sha256 | 本次输入 PDF 字节内容指纹；同内容不同来源可共享解析缓存，但来源身份仍独立 |
| pipeline_fingerprint | schema、解析器/适配器、模型指纹、路由/清洗/组装/切块规则、相关配置及 tokenizer 身份的规范 JSON hash |
| block_id | PDF 版本、页码、类型、规范几何/顺序构造的稳定标识，不使用时间随机值 |
| chunk_id | hash(doc_id、PDF版本、pipeline指纹、有序来源块/子范围、embedding_text hash)；不包含时间型 run_id |
| parse_revision | 密封解析 manifest 的内容指纹，人工修改产物后会变化 |
| index_id | hash(doc_id、parse_revision、Embedding身份)，作为一次可激活的索引快照 |
| vector_record_id | hash(index_id、chunk_id)，Chroma 实际记录ID；chunk_id保留为逻辑切块ID |

几何参与 ID 前规定精度和序列化顺序，避免无意义浮点差异。模型输出确实变化时产生新 chunk 是合理行为，旧版本由索引快照机制管理。

### 7.2 页面与块

`PageRecord` 必有 `schema_version/run_id/doc_id/page/geometry/route/status/quality/block_ids`。即使空白或失败也有记录。`engines_used` 是数组；`ocr_used` 单独表达是否采用 OCR；`ocr_engines` 区分 Tesseract 与 MinerU 内部 OCR，不能用是否配置 Tesseract 路径推断引擎。

`BlockRecord` 必有：`block_id/page/type/order/bbox/engine/status/content_text/quality/provenance`。可选结构包括 `html_ref/latex/mathml/asset_refs/parent_id/section_path`。归一化类型包括 `paragraph/list_item/heading/table/formula/figure/caption/footnote/header/footer/page_number`。

原始内容、bbox、引擎标签、模型版本和原始产物位置留在 provenance 中。被最终正文排除的原生块、页眉和重复候选仍保留，标记 `selected=false` 及原因。

跨页组装另存 `LogicalBlockRecord`，包含 `logical_block_id/type/pages/source_block_ids/merge_ids/content_text/section_path/quality`，以及可选的父级关系、完整表格 HTML、数据行/单元格来源、公式及资产引用。它允许包含多个物理页，不复用单页 BlockRecord 的 page 字段。chunk 的 parent_id 指向 logical_block_id，例如跨页父表；source_spans.block_id 仍指向单页来源块。该父对象持久化到 `logical_blocks.jsonl`，纳入 manifest 和引用闭合检查。

### 7.3 来源范围

每个 chunk 保留 `pages/page_start/page_end/source_spans/block_ids/merge_ids`。其中 pages 是去重排序后的精确物理页集合；page_start/end 是范围索引，不能代替精确集合。

`source_spans` 每项包含物理页、规范 bbox、block_id、文本引用和映射类型。可选字符范围统一为 Unicode 字符串的零基半开区间 `[start,end)`：

- `block_char_range` 索引指定 `BlockRecord.content_text`。
- `chunk_char_range` 索引该 chunk 的 `embedding_text`。
- `mapping_kind=exact|block|synthetic_context`：精确转录、仅区域对应、重复标题/表头等上下文。
- HTML 转行文本、公式转换等无法逐字映射时只提供块级来源，不伪造字符偏移。
- PyMuPDF 的原始 `pos` 只指向其自身 raw Markdown；不得拿它索引清洗后 text、MinerU 文本或 embedding_text。

## 8. 日志与审阅产物 schema v3

### 8.1 文件布局

```text
data/parse_log/<run_id>/
├── run.json                # 可更新的运行概览、parse/review/index独立状态
├── manifest.json           # 不可变内容清单、hash、页/块/chunk数量、版本与配置
├── pages.jsonl             # 每物理页一条，含失败和空白
├── blocks.jsonl            # 原始候选与最终块、结构及来源
├── logical_blocks.jsonl    # 文档逻辑块与跨页父表、完整结构、行/单元格来源
├── merges.jsonl            # 跨页候选、规则、证据、决策
├── chunks.jsonl            # 最终 text / embedding_text / 来源与入库字段
├── review.json             # 人工审阅结果，绑定manifest hash
├── index.json              # 入库过程、目标集合、index_id、激活状态
├── events.jsonl            # 当前任务结构化事件
├── raw/mineru/job-0001.zip  # 引擎原始输出，不改写
├── raw/mineru/job-0001.stdout.log
├── raw/mineru/job-0001.stderr.log
├── raw/pymupdf/...
└── assets/...              # 图片、表格HTML、原页/区域图等
```

产物引用使用解析目录内相对路径，便于移动和 UI 展示；索引登记表负责把 run_id 解析到存储位置。已激活向量依赖的产物不能被普通“清日志”操作删除。

### 8.2 run 与事件

run 概览包含 PDF hash、引擎/模型版本、配置指纹、物理页数、各引擎采用块数、质量问题、跨页合并数量、token 异常、耗时、manifest hash 等。状态拆开：

```text
parse_status  = running | completed | completed_with_issues | failed
quality_status = pass | needs_review | failed
review_status = pending | approved | rejected
index_status  = not_indexed | indexing | staged | active | failed | superseded
```

完成解析不代表质量合格或已入库。人工 approved 也不能覆盖仍未解决的 failed；需修正产物或把符合已确认范围的插图限制明确归类后重新校验。

事件格式示例：

```json
{
  "schema_version": 3,
  "ts": "2026-10-26T10:00:00+08:00",
  "level": "INFO",
  "event": "route_selected",
  "run_id": "example-run",
  "doc_id": "doc-example",
  "page": 28,
  "block_id": "block-example",
  "stage": "routing",
  "engine": "mineru",
  "route": "region",
  "reason_codes": ["table_detected", "possible_continuation"],
  "attempt": 1,
  "duration_ms": 12
}
```

文件事件保留结构化字段，控制台保留简短文字。错误事件包含 `error_code/error_type/message/retryable` 与诊断引用，不再吞掉 OCR/MinerU 异常。运行日志不反复输出全文，正文存审阅产物。

### 8.3 chunk 记录示例

下例字段和值仅展示数据契约，hash/ID 均为占位，不是当前样本解析结论。

```json
{
  "schema_version": 3,
  "chunk_id": "chunk-example",
  "doc_id": "doc-example",
  "pdf_sha256": "<pdf-sha256>",
  "pipeline_fingerprint": "<pipeline-sha256>",
  "run_id": "example-run",
  "source": "frm_notes.pdf",
  "chunk_index": 17,
  "content_type": "table",
  "parent_id": "table-example",
  "pages": [28, 29],
  "page_start": 28,
  "page_end": 29,
  "block_ids": ["table-fragment-28", "table-fragment-29"],
  "merge_ids": ["merge-example"],
  "text": "<规范化表格原文>",
  "embedding_text": "<表号、单位、表头及完整数据行>",
  "embedding_tokens": 210,
  "embedding_text_sha256": "<text-sha256>",
  "source_spans": [
    {"page": 28, "block_id": "table-fragment-28", "bbox": [50, 80, 540, 760], "text_ref": "block:table-fragment-28#content_text", "mapping_kind": "block"},
    {"page": 29, "block_id": "table-fragment-29", "bbox": [50, 60, 540, 350], "text_ref": "block:table-fragment-29#content_text", "mapping_kind": "block"}
  ],
  "engine": "mineru",
  "quality_status": "pass",
  "artifact_ref": "chunks.jsonl#chunk-example"
}
```

`chunks.jsonl` 密封后不因审核/入库更新而修改；review/index 分文件记录，防止 hash 自引用。

### 8.4 完整性与写入失败

原始日志“写失败不影响解析”只能保留给辅助运行日志。**审阅产物是入库依据，写入失败必须阻断该记录通过**。

- 记录总页数，校验页面集合恰好覆盖 1..N，校验块/合并/chunk 引用闭合。
- JSONL 严格逐行校验；不能像现有 `read_jsonl()` 一样遇损坏行就跳过后继续入库。
- manifest 包含所有不可变内容产物的相对路径、字节数、SHA256，以及规范配置和版本信息；采用排序后的规范清单计算根 hash。清单排除 manifest 文件本身及根hash字段，避免自引用。
- mutable 的 run/review/index/events 不纳入不可变产物 hash，避免审阅或入库状态更新使自身失效。review 单独绑定 manifest hash。
- 临时文件完成后再原子替换/密封；失败产物保留诊断，但没有可入库的 sealed manifest。

## 9. Chroma 字段与检索契约

### 9.1 向量记录

| Chroma 部分 | 内容 |
|---|---|
| ids | vector_record_id，即 hash(index_id、chunk_id) |
| documents | 通过审核的 embedding_text |
| embeddings | 对相同 embedding_text 生成的本地向量 |
| metadatas | 下列可筛选标量和小型来源摘要 |

| metadata 字段 | 类型 | 用途 |
|---|---|---|
| schema_version | int | 新记录为 3 |
| doc_id / source / pdf_sha256 | str | 逻辑文档、显示名与内容版本 |
| run_id / parse_revision / index_id | str | 解析、审核产物及索引快照定位 |
| pipeline_fingerprint / embedding_signature | str | 处理链与向量模型身份 |
| chunk_id / chunk_index | str / int | 稳定标识与文档内全局顺序 |
| vector_record_id | str | Chroma 实际记录ID，与查询结果 ids 一致 |
| page_start / page_end | int | 页范围筛选与显示 |
| pages_json | str | 精确页列表，JSON字符串；不是可直接查询的数组 |
| page | int | 兼容字段，等于 page_start；新代码不能只据此引用 |
| content_type / parent_id | str | 段落/表格/公式/图注与逻辑父块；无父块时省略 |
| engine | str | pymupdf / tesseract / mineru / hybrid |
| quality_status / review_status | str | 入库均必须是 pass / approved |
| embedding_text_sha256 / embedding_tokens | str / int | 入库内容与预算核对 |
| artifact_ref | str | run_id 内的审阅产物定位 |

复杂 source_spans、HTML、资产、合并证据保存在产物中；小型来源集合可按需增加 `source_spans_json`，不把整个结构化文档塞进 metadata。metadata 缺失值省略，禁止写入 list/dict/None；这一限制已按当前 Chroma 0.6.3 核实。

### 9.2 Embedding 身份

collection/索引 manifest 保存模型 ID、固定 revision 或本地文件指纹、维数、归一化方式、tokenizer、max_seq_length、文本构造版本。不能只记录一个可变的模型名称，也不能把不同模型/维数向量混入同一集合。

继续保留当前本地 BGE 路径，首版不更换模型。加载实际模型后读取预算，逐条验证 token 长度再 encode；任何跳过/截断都必须显式失败或重新切块。

### 9.3 检索、引用与 UI

- `search_chunks()` 返回 vector_record_id、chunk_id、index_id、页范围、精确页列表、类型、父块、run/artifact 引用与 metadata；不能在出口又丢回单页字段。UI 详情按 vector_record_id 读取，逻辑 chunk_id 需结合 index_id 解析，不能继续直接 `get(ids=[chunk_id])`。
- 指定页 p 查询使用 `page_start <= p AND page_end >= p` 初筛，再按 pages_json 精确过滤；精确过滤应参与分页/计数，不能先截取一页结果后错误报告总数。
- 回答引用显示“文件，第 28–29 页”等，并能查看 source_spans；非连续页列表不能显示成暗示连续内容的单一区间。
- LLM 上下文预算过小时，对普通文本按安全边界取片段；表格/公式保持原子性。某大块放不下时可继续考虑后续小块，不能简单 break 导致完全无上下文。
- `/chat.sources` 只报告实际送入上下文的来源；检索但未发送的 hit 可另列 retrieved_hits，修正当前预算截断后仍返回全部来源的问题。
- UI 展示 parse/quality/review/index 各状态、采用引擎、原因、页范围、跨页关系、表格 HTML、公式及图像，并提供人工确认操作。
- HTML 表格预览在受限渲染/净化后显示，保留原始 HTML 供下载；图像引用限定为该 run 的资产。
- “涉及该页的 chunk 数”按精确来源计数，与全局去重 chunk 总数区分。

## 10. 严格入库门禁与版本激活

### 10.1 人工确认绑定内容

`review.json` 保存 reviewer、reviewed_at、manifest_sha256、decision、已核对的问题/限制与备注。reviewer 应由实际用户确认动作提供，程序不把质量检查自动通过写成人工 approved。

后续可设计 `scripts.review_parsed` 或 UI 审阅入口，展示待处理项和最终 embedding_text 后，由用户明确确认。该人工门禁来自本次用户确认的要求，不是当前已存在功能。

所有入口统一调用 `validate_indexable_run()`，满足以下条件才继续：

1. schema 是已支持的新版本，parse completed，quality pass，无 unresolved issue。
2. 必需文件与资产存在，manifest hash、JSONL、计数、引用和原 PDF 版本一致。
3. review approved，且指向当前 manifest hash；任何正文/拼接/切块修改使旧 review 失效。
4. 全部待入库 chunk 类型、页码、来源、标量 metadata 和 token 预算合格。
5. Embedding 身份匹配目标集合，index snapshot 计划完整。

`ENABLE_INDEXING=true` 只能表达请求入库，不能跳过上述门禁。新上传/新解析没有人工确认，`index=true` 不再自动写入；API 返回已解析、待审阅状态及 run_id。若实现后需要保留自动入库旧行为，必须另行获得用户对严格门禁规则的变更确认。

无 run 产物或 `--no-log` 仅可用于诊断，不能成为入库依据。默认最近记录选择“已密封、质量通过且已确认”的记录；找不到时明确提示，不取最近失败记录。

### 10.2 幂等、陈旧块与读写一致性

Chroma upsert/delete 的多次调用没有整体事务。不能用“先删旧、后写新”声称原子替换。

建议增加本地索引登记表（标准库 SQLite，独立于 Chroma 内部数据库），维护 `doc_id → active index_id`、集合、parse_revision 及状态；采用单写入队列与事务激活：

1. 先生成/复用完整索引快照与期望 chunk ID 清单。
2. 新 index_id 全部向量批次写入 Chroma，仍为 staged。
3. 校验 ID 集合、数量、关键字段、Embedding 维数和读回文本 hash。
4. 在登记表事务中将 doc_id 的 active index_id 切换为新快照；旧 index_id 保留可回滚。
5. 检索、UI、统计和维护操作先取得同一份 active index_id 快照，再向 Chroma 添加 `$in` 等过滤。未激活数据不能被普通查询命中。
6. 激活失败时旧快照继续可用；清理孤立 staged 数据作为后续恢复任务。登记表成功后再更新 run 的显示状态；崩溃恢复以登记表为准。
7. 显式清理过期 index_id，不能仅按文件名删；资产回收检查仍被 active/保留快照引用的产物。

现有代码以 chunk_id 作为 Chroma ID；本设计改为 `vector_record_id = hash(index_id、chunk_id)`，metadata.chunk_id 保留逻辑 ID，登记表保存每个快照完整的 vector_record_id 清单。这样新旧快照中的相同逻辑切块也有独立记录，不会在 staged upsert 时覆盖旧快照的 index_id、run_id 或来源引用。

登记表读取失败时返回明确错误；active集合或文档映射为空时返回空结果，禁止回退成不带过滤的 Chroma 查询。单次请求固定登记表快照，已取快照的在途查询完成前不回收其旧索引。

Embedding 签名改变时建立新集合，为全部当前 active 文档准备对应的新快照，核验完整文档集合后，在登记表事务中一起切换当前集合、签名与所有文档映射，不能只迁移第一个文档就切换全局集合。该登记表是应用索引版本机制，不修改 Chroma 的内部 SQLite 文件。

### 10.3 旧数据迁移

- 新建 `finance_pdf_chunks_v3`，按新链路重新解析、审阅和向量化；初次切换前旧集合继续使用。
- v1/v2 历史记录只读展示，缺失字段显示未知；不能从旧文本伪造 bbox、表格结构或跨页来源。
- 旧日志不得混入 v3 入库。旧向量不需要直接删，验证新集合检索与引用后再切换，保留回滚。
- `index_parsed_run()` 与即时入库共用 ChunkRecord → metadata 序列化器及 gate，避免两条路径字段不一致。
- `run.index`/index.json 在实际入库后更新，不能延续现有“日志入库成功但 run 仍显示未入库”的行为。

## 11. 配置设计

以下为拟新增/修订的配置，不写入当前 `.env`：

| 配置 | 初始建议值 | 含义 |
|---|---|---|
| PARSER_PROFILE | hybrid_v3 | 明确启用新链路，旧链路不伪装成同一解析器 |
| MINERU_PYTHON | `E:\learn_space\mineru\env\.venv\Scripts\python.exe` | 独立解释器 |
| MINERU_MODEL_BASE_DIR | `E:\learn_space\mineru\model` | 显式本地模型根目录 |
| MINERU_MODEL_SOURCE | local | 仅本地模型路径 |
| MINERU_TIER | standard | 当前确认档位 |
| MINERU_MODEL_SMALL_BACKEND | onnx | 当前已验证小模型后端 |
| MINERU_MODEL_VLM_ENGINE | llama-cpp | 当前已验证本地 VLM 引擎 |
| MINERU_TIMEOUT_SECONDS | 1800，待实测 | 冷启动和任务总时限 |
| MINERU_MAX_CONCURRENCY | 1 | 本机资源控制 |
| MINERU_CACHE_DIR | data/parser_cache/mineru | 原始产物复用 |
| ROUTING_RULE_VERSION | hybrid-v1 | 路由规则追溯；具体阈值进规范配置快照 |
| OCR_DPI | 300 | 真正应用到普通文字区域渲染 |
| REVIEW_REQUIRED | true，首版强制 | 与用户确认一致，不能被 indexing 开关绕过 |
| PARSE_ARTIFACT_SCHEMA | 3 | 所有新解析产物契约 |
| INDEX_REGISTRY_PATH | data/index_registry.sqlite3 | 索引版本登记表 |
| COLLECTION_NAME | finance_pdf_chunks_v3 | 新 schema 集合，迁移时显式设置 |

保持 `ENABLE_INDEXING=false` 的默认行为。页眉/页脚、layout 等参数须实际接入并有验收；无实际接口支持的配置应移除或明确仅作预检策略，不能保留假开关。

## 12. 代码改动清单

### 12.1 现有文件

| 文件/当前位置 | 具体改动 |
|---|---|
| `app/pdf_loader.py:35` | 从直接提取/OCR 改为预检、调度、归一化入口；返回全部页与块；删除全图片无差别 OCR 和静默吞错 |
| `app/chunker.py:5` | 新增按块类型/token预算切分；字符切分只作普通段落内部工具，不切断表格行/公式 |
| `app/rag_pipeline.py:21` | Chunk/ParseResult 接入统一数据模型，补完整页、逻辑块、质量及产物引用 |
| `app/rag_pipeline.py:38` | 替换单页文件名 SHA1 ID 逻辑，采用文档身份/版本/来源与文本指纹 |
| `app/rag_pipeline.py:44` | 移除逐页 `split_text` 主流程，先跨页逻辑组装再切块 |
| `app/rag_pipeline.py:64` | parse_document 按阶段写产物、质量检查、密封 manifest；不自动人工确认 |
| `app/rag_pipeline.py:145` | index_chunks 使用统一序列化、token验证、索引快照和激活策略 |
| `app/rag_pipeline.py:171` | index_pdf 服从 review gate，新的上传结果进入待审阅 |
| `app/rag_pipeline.py:235` | index_parsed_run 严格校验、保留新字段、回写索引状态，不跳过损坏行 |
| `app/rag_pipeline.py:278` | 页范围与来源上下文；表格/公式预算；保存实际发送来源 |
| `app/rag_pipeline.py:327` | sources 与真正发送片段一致，返回完整来源定位字段 |
| `app/parse_log.py:23` | schema v3，真实解析器链和版本；拆分产物/辅助日志可靠性；读取历史日志显式分支 |
| `app/parse_log.py:135` | 审阅/入库使用严格 JSONL reader，损坏即失败；旧日志浏览可用容错读取但显示损坏 |
| `app/vector_store.py:9` | 集合 Embedding 身份检查、按索引快照过滤 |
| `app/vector_store.py:17` | 批量暂存、读回核验，区分 vector_record_id 与 chunk_id |
| `app/vector_store.py:28` | 返回新 metadata 与来源，不丢失跨页信息 |
| `app/embeddings.py:6` | 模型身份/预算读取，逐条 token预检；不增加云端 Embedding |
| `app/logging_config.py:10` | 增加结构化文件 formatter 和任务上下文；保留控制台易读格式 |
| `app/config.py` | 新增 MinerU/路由/审阅/索引版本配置；修正无效开关与快照 |
| `app/api.py:48` | 上传/入库区分解析完成与待审阅，增加doc_id、原件版本固定、本机后台worker与审阅/显式入库接口 |
| `app/web_ui.py:32` | page范围初筛+精确页集合；索引快照过滤 |
| `app/web_ui.py:161` | 详情按vector_record_id读取，按父逻辑块和来源页关联，不再只查单页 siblings |
| `app/web_ui.py:212` | schema识别、OCR统计、跨页计数、质量问题及review/index独立状态 |
| `app/templates/*.html` | 页范围、结构化块/图像预览、合并证据和人工确认入口 |
| `scripts/parse_pdf.py` | 真实引擎/质量统计，修正页眉页脚与OCR提示；展示待审阅原因 |
| `scripts/index_parsed.py` | 只选择合格已审阅记录，错误时明确原因；不提供绕过gate的隐式路径 |
| `scripts/reset_db.py:120` | 按doc_id/index_id管理，保护被索引引用的产物；显示清理与回滚影响 |
| `requirements.txt` | 固定已验证 PyMuPDF/相关依赖；不加入 MinerU，独立记录其版本 |
| `README.md / PROJECT-SC.md / docs` | 按最终实现更新命令、字段、状态和目录，修正文档链接与不存在的脚本 |

### 12.2 建议新增模块

| 模块 | 单一职责 |
|---|---|
| `app/document_schema.py` | Page/Block/LogicalBlock/Chunk/SourceSpan 等模型与版本校验 |
| `app/parse_quality.py` | 指标计算、结果质量与问题规则，不混合业务问答 |
| `app/parse_router.py` | 页面/区域路由、任务分组和所有权计划 |
| `app/mineru_adapter.py` | 独立进程、模型预检、产物/schema适配、页码和坐标转换 |
| `app/document_assembler.py` | 跨页段落/列表/表格组装及证据 |
| `app/parse_review.py` | manifest密封/校验、人工审核及入库门禁 |
| `app/chunk_serializer.py` | 两条入库路径共享的 Chroma 标量metadata序列化 |
| `app/index_registry.py` | 文档身份、索引快照、单写入与原子激活 |
| `scripts/review_parsed.py` | 显式人工确认入口，与UI共用review服务 |

文件拆分可在实现时按代码规模适度合并，但上述职责和数据契约应保持清晰。

## 13. 验证与验收计划

### 13.1 样本和人工基准

首批候选包括 `frm_notes.pdf` 第 5 页公式、第 28–29 页表格，均为原 PDF 一基物理页码。已有 ZIP 是结构契约样本，不是正确答案。人工基准应标注：关键文本/数值、表头/单位、合并单元格、公式结构、续表关系、来源页与区域。

补齐普通原生文字、普通扫描文字、带原生文本层的扫描页、矢量表格、图表、页眉脚注、多栏、旋转/cropbox、空白页、跨页段落、相邻但不应合并的表格。

### 13.2 必须验证的行为

| 验证组 | 必须通过 |
|---|---|
| 调用边界 | 始终使用独立解释器、standard、本地模型；错误码/超时可见；主环境不依赖MinerU |
| 页面契约 | 返回页集合完整；第28/29页不变成1/2；坐标叠框与原页一致 |
| 路由融合 | 普通原生页无多余OCR；复杂内容进入MinerU；原生与识别结果不重复；矢量内容不漏检 |
| 跨页 | 正确续段/续表；章节、新列表、空白/失败页不误并；保留行级来源和去表头证据 |
| 内容准确性 | 人工基准中的关键金融数字、负号、小数点、单位和公式结构逐项正确；失败项待核对 |
| 切块预算 | 表格完整行、LaTeX不截断；所有embedding_text在实际token预算内 |
| 审阅门禁 | pending/rejected、hash改变、缺资产、损坏JSONL、失败页和存疑拼接都不能入库 |
| 数据一致性 | 已解析入库与即时入库字段一致；documents文本hash与向量输入一致 |
| 索引更新 | 重复入库幂等；激活前新数据不可见；失败仍读旧版本；清理不删仍引用资产 |
| 检索引用 | 跨页块按所有来源页命中；来源只包含实际发送的上下文；表格详情可回看原图 |
| 历史兼容 | v1/v2只读展示且标未知，禁止伪造新字段后进入v3集合 |

纯规则与契约测试使用小型固定样本/模拟结果，避免所有单测都启动模型；单独标记真实 MinerU、PDF 和 Embedding 集成验收。现有 `tests/test_retrieval.py` 依赖真实索引且没有断言，应明确为集成试跑或补基准断言，不能拿打印结果当检索正确性测试。

### 13.3 性能验证与实施决策

分别测量 PyMuPDF 预检、MinerU 冷/热启动、每页推理、OCR、组装、Embedding、向量写入的时间和峰值资源。使用同一文档比较全量MinerU与选页混合路线，报告被送入MinerU的页数，不把区域路由解释成页内算力节省。

如模型冷启动是主要瓶颈，再评估独立常驻本机服务；首版不预设其必然更快。质量阈值、任务页数、超时和并发根据实测调整，记录进pipeline指纹。若 standard 在目标公式/表格上不能满足需求，先提供失败样本与影响，再就换模型档位、人工转录或扩大模型范围向用户确认。
