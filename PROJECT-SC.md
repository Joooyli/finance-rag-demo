## 项目背景
本地中文金融 PDF 知识库
通过解析pdf文件，采用Python + 本地 PDF 解析与 OCR + 本地 Embedding + Chroma 向量库 + 云端 OpenAI 兼容大模型 API完成本地中文金融 PDF 知识库搭建。

解析与入库拆成两步：先解析并把每页、每个切块写入 data/parse_log，人工检查后再入库（由 ENABLE_INDEXING 与 scripts/index_parsed.py 控制）。

## 项目结构
finance-rag-learning/
├── app/
│   ├── config.py          # 环境变量和参数（含 ENABLE_INDEXING、PARSE_LOG_*）
│   ├── logging_config.py  # 运行日志（控制台 + data/logs/app.log）
│   ├── parse_log.py       # 解析日志：run.json / pages.jsonl / chunks.jsonl
│   ├── pdf_loader.py      # PDF 文本提取、OCR、逐页解析统计
│   ├── chunker.py         # 文本切块
│   ├── embeddings.py      # 本地向量计算
│   ├── vector_store.py    # Chroma 建库与检索
│   ├── rag_pipeline.py    # 解析阶段、入库阶段、检索、问答
│   ├── web_ui.py          # /ui 可视化路由
│   ├── templates/         # 页面模板（概览、切块、解析记录）
│   └── api.py             # FastAPI 接口
├── scripts/
│   ├── parse_pdf.py       # 只解析并写日志，不入库
│   ├── index_parsed.py    # 用已检查的解析日志入库（不重新解析）
│   ├── reset_db.py        # 删除集合/切块/解析日志
│   └── test_retrieval.py  # 本地检索测试
├── tests/
│   ├── test_chunker.py    # 切块单元测试
│   └── test_parse_log.py  # 解析日志与 chunk_id 单元测试
├── data/
│   ├── source/            # PDF 原文件
│   ├── chroma_db/         # 本地向量库
│   ├── parse_log/         # 解析日志（每次解析一个目录）
│   └── logs/              # 运行日志
├── pytest.ini             # pytest 只收集 tests/ 目录
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
