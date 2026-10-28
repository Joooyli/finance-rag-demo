# 项目结构与处理链路

当前实现为本地中文金融PDF混合解析与RAG。操作见[使用说明](docs/PDF混合解析使用说明.md)，逐文件变更见[改动记录](docs/PDF混合解析改动记录.md)。

```text
固定PDF版本 → 页面/区域预检 → 原生文字/PyMuPDF、复杂内容/MinerU、普通文字OCR/Tesseract
→ 结果融合 → 跨页逻辑块 → 类型与token预算切块 → 质量检查/密封 → 人工确认
→ 本地Embedding → 暂存向量/核验 → 激活文档版本 → 本地检索 → 可选云端问答
```

| 文件 | 职责 |
|---|---|
| app/document_schema.py | 数据契约、稳定hash、来源标签 |
| app/document_source.py | 文档身份和不可变PDF原件 |
| app/page_geometry.py | 旋转/裁剪/归一化区域转换 |
| app/pdf_loader.py | PyMuPDF预检、普通区域OCR、路由调度与结果保存 |
| app/parse_router.py、parse_quality.py | 路由、正文所有权及质量问题 |
| app/mineru_adapter.py | 独立本地standard进程、结构化产物、页码日志、缓存及取消 |
| app/document_assembler.py | 跨页段落、续表、逻辑父块与证据 |
| app/chunker.py、text_utils.py | 类型切块、结构保留、最终检索文本 |
| app/embeddings.py | 本地模型身份、真实token预算与向量 |
| app/parse_log.py、logging_config.py | v3产物与结构化运行事件 |
| app/parse_review.py | 密封、人工审阅与入库校验 |
| app/chunk_serializer.py | Chroma标量metadata转换 |
| app/index_registry.py | SQLite文档/索引登记、暂存与活动版本 |
| app/vector_store.py | Chroma读写、核验和活动版本过滤 |
| app/rag_pipeline.py | 解析/索引编排、检索、上下文与引用 |
| app/parse_jobs.py | 单worker后台队列、持久状态与重启中断 |
| app/api.py、web_ui.py、templates/ | 上传、任务、审阅、检索及来源展示 |
| scripts/parse_pdf.py | 只解析 |
| scripts/review_parsed.py | 查看产物并显式人工确认/拒绝 |
| scripts/index_parsed.py | 入库已确认产物 |
| scripts/reset_db.py | 数据维护和来源产物保护 |

原始输入收集在data/source；不可变版本在data/documents；v3审阅产物在data/parse_log；模型输出缓存在data/parser_cache；索引登记表为data/index_registry.sqlite3；任务状态在data/parse_jobs。旧Chroma集合保留，新集合使用HYBRID_COLLECTION_NAME。
