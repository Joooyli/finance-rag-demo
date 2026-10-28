# PDF 混合解析代码改动记录

日期：2026-10-26。本次已修改代码，关键流程附中文注释；现有独立环境、本地模型和真实`.env`未被重新安装或覆盖。旧Chroma集合未迁移、清空或写入。

操作命令与配置见[使用说明](PDF混合解析使用说明.md)。以下描述已实现内容；设计稿中的长期设想不自动等同已验收。

## 1. 逐文件改动

| 文件 | 改动 |
|---|---|
| `app/document_schema.py`（新增） | v3版本、稳定hash、文档原件身份和完整页码标签 |
| `app/document_source.py`（新增） | 输入复制后计算hash；原件按doc_id/PDF版本固定，不覆盖历史文件 |
| `app/page_geometry.py`（新增） | 原生/归一化区域转换；统一视觉页面坐标，保留rotation/cropbox信息 |
| `app/mineru_adapter.py`（新增） | 独立Python、本地standard、预检、原PDF选页、ZIP/Middle2.0适配、资产/表格HTML、缓存校验、超时/取消和子进程树终止 |
| `app/parse_quality.py`、`app/parse_router.py`（新增） | 质量指标、区域/整页路由、复杂结构检查、唯一正文所有权；明显续表邻页上下文扩展 |
| `app/pdf_loader.py` | PyMuPDF2 layout预检且关闭隐式OCR；融合后普通坏文字区域Tesseract兜底；保留所有物理页、原始候选和裁图，不再直接追加两套全页文本 |
| `app/document_assembler.py`（新增） | 邻页续段/列表/续表关系、保守拼接、逻辑父表、行级来源与合并证据；不跨空白/失败页盲拼 |
| `app/text_utils.py` | 保留原始偏移契约；新增HTML表格结构解析，保留rowspan/colspan和金融符号，缺列/未转录图片标记待核对 |
| `app/chunker.py` | 文档逻辑块切块；保护表格行、公式与金融数字，构造embedding_text；超token内容显式待核对 |
| `app/embeddings.py` | 延迟加载本地缓存、真实token计数/预算、模型身份；encode前拒绝静默截断 |
| `app/config.py` | MinerU/路由/原件/缓存/后台任务/索引登记配置；新链路独立HYBRID_COLLECTION_NAME，人工确认强制 |
| `app/parse_log.py` | schema v3，真实解析器链、完整产物、严格写入/读取、run级引擎事件；不再误报builtin OCR |
| `app/logging_config.py` | 控制台保留易读日志，文件JSON事件包含引擎/页码/阶段等字段 |
| `app/parse_review.py`（新增） | 产物manifest、hash/页集合/块来源/父块校验，人工批准绑定内容；存疑不能批准，失败记录可拒绝 |
| `app/chunk_serializer.py`（新增） | Chroma标量metadata、页范围/精确页集合/来源区域JSON、逻辑chunk_id与实际vector_record_id |
| `app/index_registry.py`（新增） | SQLite文档登记、单写入锁、索引暂存/事务激活、活动映射及产物引用保护 |
| `app/vector_store.py` | 集合Embedding身份、向量读回校验；检索/列表/详情/统计统一活动快照过滤，暂存和旧版本不可见 |
| `app/rag_pipeline.py` | 解析→组装→切块→密封；严格审阅后入库、版本激活、模型/runtime/处理代码指纹；跨页引用与实际上下文一致 |
| `app/parse_jobs.py`（新增） | 单worker后台队列、任务JSON持久化、单进程目录锁、重启中断、协作取消 |
| `app/api.py` | 上传202后台任务、不覆盖原件；任务查询/取消、人工审阅、显式入库；新上传index=true仍待审阅 |
| `app/web_ui.py`及六个模板 | 活动索引展示、跨页来源、独立状态、逐页问题、表格/公式/图像与合并证据、人工确认入口 |
| `scripts/parse_pdf.py` | 真实引擎/质量统计，--json输出，修正页眉/OCR误报 |
| `scripts/review_parsed.py`（新增） | 默认仅预览；--approve/--reject加实际reviewer才记录决定 |
| `scripts/index_parsed.py` | 只选择已通过且已确认的v3记录，共用门禁与入库服务 |
| `scripts/reset_db.py` | 按doc_id/index_id维护，已登记快照引用保护；实际删除成功后更新登记表 |
| `.env.example`（新增）、`.gitignore`、`requirements.txt`、`pytest.ini` | 配置模板、新生成目录忽略、固定PyMuPDF2.0、隔离真实模型集成测试 |
| `README.md`、`PROJECT-SC.md`、使用说明及测试 | 按实际三阶段操作同步说明，补契约、失败路径和应用链路验证 |

## 2. 页面日志

MinerU和Tesseract都打印原PDF一基物理页号，事件同时保存在当前run的events.jsonl。控制台示例来自真实调用，省略时间前缀和较长区域ID：

```text
MinerU 页面=5 page_started batch_pages=[5] tier=standard
MinerU 页面=5 page_completed batch_pages=[5] batch_finished=True blocks=9 cache_hit=False duration_ms=34219
[tesseract] page_started page=1 region=<区域ID> reason=ordinary_text_fallback
[tesseract] page_completed page=1 region=<区域ID> reason=ordinary_text_fallback chars=25 duration_ms=437
```

MinerU的page_started表示本批次请求已提交；page_completed在整批返回并校验后逐页打印，不代表CLI提供了逐页实时回调。引擎异常、缺模型、超时等输出page_failed并保留诊断；Tesseract日志另含区域ID、耗时与字符数。最终融合后输出page_finalized，记录最终路由和质量。

## 3. 已执行验证

- 初始混合解析改动自动测试为113项通过；本轮页眉清洗完成后完整`python -m pytest -q`为144项通过。存在一条第三方Starlette/AnyIO弃用提醒，不影响测试结果。
- 自动测试覆盖引擎调用、页码/坐标、旋转裁图、质量路由、三引擎同页融合、跨页、表格/公式/token保护、密封审阅、后台任务、API/UI和清理保护。
- 应用链路使用真实小型PDF、临时真实Chroma、模拟tokenizer/向量验证解析→未批准拒绝→批准→入库→幂等重入→新版本替换；不写用户正式向量库。
- 真实PyMuPDF layout加本地缓存tokenizer完成普通文字/空白两页的解析、切块和密封校验，未执行encode或入库。
- 真实独立MinerU standard解析`frm_notes.pdf`原第5页成功，输出9个块；开始/完成日志可见，约34.2秒。这里只验证调用与产物契约，不以块数量宣称公式准确。
- 真实Tesseract识别生成的普通文字图像，恢复`Market risk 12.50 percent`，页/区域开始完成日志可见。
- 已有原第28–29页ZIP只读适配通过，验证表格HTML、资产、原页号和结构字段。

本地模型路径齐备性检查不等于权重内容hash或准确率验收。尚未逐页人工核验整份财务PDF，也未在用户正式集合执行重建/激活。

## 4. 当前边界

复杂表格缺少可转录内容、无重复表头且列对应不明确、跨页拼接证据不足时，整次记录进入needs_review，不能靠批准忽略问题。目前没有人工编辑/转录后重新密封的编辑器；需修正识别或处理规则并重新解析。

表格子块保留行组来源；普通长段落子块按父逻辑块保留保守区域来源，mapping_kind为block，尚不提供逐字偏移精确归属。图表业务解释、曲线数值自动恢复不在本期已确认范围。

后台队列要求单服务进程、单worker；已开始的入库任务不支持中途取消。当前集合的Embedding身份不允许混用，换模型须显式使用新集合完整重建。旧快照保留用于回滚/审计，默认维护操作保护其来源产物。

## 5. 页眉广告：改为统一块清洗

本轮按用户确认的单次清洗方案修改，调用顺序为：所有页PyMuPDF/MinerU结果融合及Tesseract兜底完成 → `clean_document_blocks()` → 页面正文/质量/日志 → 跨页组装与切块。详细功能、方法和配置见[页眉广告统一块清洗说明](页眉广告统一块清洗说明.md)。

| 文件 | 本轮改动 |
|---|---|
| `app/header_filter.py`（新增） | 唯一清洗入口，基于明确header类型、顶部广告规则、同位置重复文本排除；支持精确行拆分、上下文去广告、证据和页面事件 |
| `app/pdf_loader.py` | 移除layout阶段内容删除；为候选/最终块提供可靠行来源；全文融合/OCR后只调用一次清洗，再写页面正文与日志 |
| `app/mineru_adapter.py`、`app/parse_router.py` | 不再在适配/融合时按header/footer开关提前排除；路由可忽略装饰类型，内容是否删除由统一入口决定 |
| `app/document_assembler.py`、`app/chunker.py` | 只消费最终selected块，不按header/footer类型再执行删除；关闭对应开关时能保留到切块 |
| `app/parse_quality.py` | 纯广告清空页保留物理记录，不误报无内容；已有识别错误和存疑仍阻断质量通过 |
| `app/config.py`、`app/parse_log.py`、`app/rag_pipeline.py` | 新规则校验、配置快照及清洗代码加入流水线指纹 |
| `.env`、`.env.example` | 实际运行配置和模板同步补齐候选范围、重复阈值、位置容差及已知广告规则；既有配置值和密钥保留 |
| `tests/test_header_filter.py`、`tests/test_header_pipeline.py`等 | 覆盖多引擎、单次执行时机、原文保留、行拆分、误删保护、关闭开关和密封来源引用 |
| `README.md`、使用说明、本清洗说明 | 更新配置、真实执行顺序、日志和旧数据重建步骤 |

被排除广告和原混合块仍在raw/blocks证据中，最终页面正文、逻辑块及检索文本不再包含已确认排除的广告。缺乏可靠行坐标时保留内容并标`needs_review`，不猜测删除整块；顶部表格/公式受到保护。新事件为`header_excluded`、`cleaning_completed`，页面记录增加`cleaning`证据。

本轮使用固定样本和模拟引擎验证，不重新运行整份PDF的模型推理或正式库迁移。已有向量需重新解析、人工确认并入库后替换活动版本；不直接修改旧manifest或旧审阅记录。

本轮另对原PDF第3、5、28页做临时PyMuPDF预检/清洗验证，固定广告已从最终正文排除，原件及原始块不修改。新增误删/漏检回归保护：混合header大块保留正文行，跨顶部边界的结构含广告时标存疑，广告从formula改判header不误报公式缺失，但真正正文公式漏识别仍阻断质量通过。
