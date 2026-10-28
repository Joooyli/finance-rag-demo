# MinerU 独立环境准备与验证

更新日期：2026-10-25。

本文按“直接执行安装命令、手动填写配置、按步骤验证”的方式编写。命令均用于 Windows PowerShell，无需激活虚拟环境，也不使用自动生成配置文件的脚本。

## 1. 固定配置

| 项目 | 使用值 |
|---|---|
| Python | 3.12，64 位 |
| 安装工具 | pip |
| MinerU 版本 | `4.0.12`，便于首次验证和问题复现 |
| 独立虚拟环境 | `E:\learn_space\mineru\env\.venv` |
| 解析档位 | `standard` |
| 模型根目录 | `E:\learn_space\mineru\model` |

## 2. 创建目录和虚拟环境

打开 PowerShell，直接执行：

```powershell
uv venv --python 3.12 .venv
```

激活虚拟环境

.venv\Scripts\Activate.ps1

## 3. 安装

在重新打开的 PowerShell 中执行：

```powershell
uv pip install -U "mineru==4.0.12"
```

```
结果：
warning: Failed to hardlink files; falling back to full copy. This may lead to degraded performance.
         If the cache and target directories are on different filesystems, hardlinking may not be supported.
         If this is intentional, set `export UV_LINK_MODE=copy` or use `--link-mode=copy` to suppress this warning.
Installed 118 packages in 17.83s
 + aiofiles==25.1.0
 + aiosqlite==0.22.1
 + annotated-doc==0.0.5
 + annotated-types==0.8.0
 + anyio==4.15.1
 + beautifulsoup4==4.15.0
 + brotli==1.2.1
 + certifi==2026.10.23
 + cffi==2.1.1
 + charset-normalizer==3.5.2
 + click==8.5.0
 + cobble==0.1.4
 + colorama==0.4.6
 + cryptography==50.0.2
 + distro==1.9.0
 + docvortex==0.6.1
 + et-xmlfile==2.0.0
 + fastapi==0.162.0
 + fastapi-offline==1.7.7
 + filelock==4.2.4
 + flatbuffers==25.12.19
 + fonttools==4.67.0
 + fsspec==2026.9.0
 + ftfy==6.3.1
 + gradio==6.30.0
 + gradio-client==2.7.2
 + groovy==0.1.2
 + h11==0.16.0
 + hf-gradio==0.4.1
 + hf-xet==1.7.0
 + httpcore==1.0.9
 + httpcore2==2.16.0
 + httpx==0.28.1
 + httpx-retries==0.6.0
 + httpx2==2.16.0
 + huggingface-hub==2.3.0
 + idna==3.20
 + jieba==0.42.1
 + jinja2==3.1.6
 + jiter==0.18.0
 + json-repair==0.64.2
 + latex2mathml==3.81.1
 + loguru==0.7.3
 + lxml==6.1.3
 + mammoth==1.13.0
 + markdown-it-py==4.2.0
 + markupsafe==3.0.4
 + mathml2omml==0.0.2
 + mdurl==0.1.2
 + metafile-render==0.4.0
 + mineru==4.0.12
 + mineru-llama-cpp==0.2.0
 + mineru-magika==1.0.3
 + mineru-vl-utils==2.0.7
 + modelscope==1.40.2
 + modelscope-hub==0.5.0
 + mpmath==1.3.0
 + nh3==0.3.7
 + numpy==2.5.4
 + olefile==0.47
 + onnxruntime==1.31.0
 + onnxruntime-directml==1.24.4
 + openai==2.54.0
 + openpyxl==3.1.5
 + opentelemetry-api==1.45.1
 + orjson==3.13.0
 + packaging==26.3
 + pandas==3.1.0
 + pillow==12.3.0
 + protobuf==7.36.2
 + pyclipper==1.4.0
 + pycparser==3.12
 + pydantic==2.14.1
 + pydantic-core==2.50.1
 + pydub==0.25.1
 + pygments==2.21.0
 + pylatexenc==2.11
 + pypdf==6.21.2
 + pypdfium2==5.14.0
 + pypptx-with-oxml==1.0.3
 + python-dateutil==3.0.0
 + python-docx==1.2.0
 + python-multipart==0.0.32
 + pytz==2026.5
 + pyyaml==6.0.3
 + reportlab==5.1.0
 + requests==2.34.2
 + resvg-py==0.5.0
 + rich==15.0.0
 + rl-accel==0.9.1
 + safehttpx==0.1.7
 + semantic-version==2.10.0
 + setuptools==84.2.1
 + shapely==2.2.0
 + shellingham==1.5.4
 + six==1.17.0
 + sniffio==1.3.1
 + soupsieve==3.0.4
 + starlette==1.10.0
 + sympy==1.14.0
 + tinycss2==1.5.1
 + tokenizers==0.23.3
 + tomlkit==0.14.0
 + tqdm==4.70.1
 + truststore==0.10.5
 + typer==0.27.3
 + typing-extensions==4.16.0
 + typing-inspection==0.4.4
 + tzdata==2026.5
 + urllib3==2.8.0
 + uvicorn==0.54.0
 + watchfiles==1.3.0
 + wcwidth==0.9.4
 + webencodings==0.6.1
 + win32-setctime==1.2.0
 + xlsxwriter==3.3.0
 + ziafont==0.11
 + ziamath==0.13
```



## 6. 下载 Standard 模型并校验

1. 设置指定目录下载位置

```
$env:MINERU_MODEL_BASE_DIR = "E:\learn_space\mineru\model"
```

2. 下载模型

   ```
   mineru-kit models download --tier standard --source modelscope
   ```

3. 验证

   ```
   mineru-kit models verify --tier standard
   ```

4. 显示配置，看下本地模型在哪里，指向本地模型还是联网

   ```
   mineru-kit models show
   ```

5. 设置模型采用本地

   ```
   $env:MINERU_MODEL_SOURCE = "local"
   ```

6. 后续更新本地模型

   ```
   mineru-kit models download --tier standard --source modelscope
   ```

   

## 7. 验证

以下页码均为 PDF 物理页码，从 1 开始。直接使用原 PDF 的 `--pages` 参数，不要先拆分 PDF，否则源页码会改变。

### 7.1 单页验证

解析公式第 4 页：

```powershell
mineru-kit parse "E:\learn_space\finance-rag-learning\finance-rag-learning\data\source\frm_notes.pdf" --pages "5" -o "E:\learn_space\mineru\resultmd\resultpage5.md" --tier standard
```

解析表格及跨页

```powershell
mineru-kit parse "E:\learn_space\finance-rag-learning\finance-rag-learning\data\source\frm_notes.pdf" --pages "28-29" --tier standard --format zip -o "E:\learn_space\mineru\resultmd\table-p28-p29.zip"
```



