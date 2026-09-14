# PDF to Markdown with PaddleOCR

这个目录里放的是一个 PaddleOCR 云端 OCR 转 Markdown 工具，支持 PDF、JPG/JPEG、PNG 和 TIFF，适合扫描版 PDF、图片型 PDF、影印书和文档图片。

## 网页上传

启动本地网页：

```powershell
& "C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe" .\web_app.py
```

浏览器打开：

```text
http://127.0.0.1:8765
```

页面上传 PDF 或图片后会在后台转换，完成后可下载合并后的 Markdown，也可以查看分页 Markdown 和图片。网页转换产生的 Markdown、分页文件和图片会直接上传到 Cloudflare R2，不会写入本地输出目录。上传的原始文件仅在系统临时目录中停留到 PaddleOCR 接收完成，之后立即删除。

在 `.env` 里配置 PaddleOCR、R2 和网页登录信息。网页和命令行都会使用 `PADDLEOCR_TOKEN` 与 `PADDLEOCR_MODEL`，上传页面不会传递或展示 PaddleOCR API Key。

```text
PADDLEOCR_TOKEN=你的PaddleOCR Token
PADDLEOCR_MODEL=PaddleOCR-VL-1.6

WEB_USERNAME=admin
WEB_PASSWORD=换成强密码

R2_ACCOUNT_ID=你的Cloudflare账户ID
R2_ACCESS_KEY_ID=你的R2访问密钥ID
R2_SECRET_ACCESS_KEY=你的R2私钥
R2_BUCKET_NAME=你的R2桶名
R2_PREFIX=pdf-to-md
```

也可以用完整的 `R2_ENDPOINT_URL` 代替 `R2_ACCOUNT_ID`：

```text
R2_ENDPOINT_URL=https://你的账户ID.r2.cloudflarestorage.com
```

R2 API Token 至少需要目标桶的“对象读取”和“对象写入”权限。R2 桶可以保持私有，网页会从 R2 代理下载和文件查看。如果桶配置了公开域名，可额外设置：

```text
R2_PUBLIC_BASE_URL=https://files.example.com
```

设置后，生成的 Markdown 内图片链接会直接指向公开 R2 地址。配置完成后重启 `web_app.py`；设置了网页账号密码时，浏览器会弹出登录框。

网页服务每次启动时会扫描 `R2_PREFIX`（默认 `pdf-to-md/`）下的一级任务目录，并从 R2 对象键恢复历史任务、分页数和文件列表。因此容器重启不会再清空页面中的已完成任务。目录结构应保持为：

```text
<R2_PREFIX>/<任务ID>_<文件名>/
```

## 云服务器 Docker 部署

服务器上推荐用 Docker Compose 跑。

1. 克隆仓库：

```bash
git clone https://github.com/jienigui06091/PDF-to-MD.git
cd PDF-to-MD
```

2. 创建 `.env`：

```bash
cp .env.example .env
nano .env
```

至少配置：

```text
PADDLEOCR_TOKEN=你的PaddleOCR Token
PADDLEOCR_MODEL=PaddleOCR-VL-1.6
WEB_USERNAME=admin
WEB_PASSWORD=换成强密码
R2_ACCOUNT_ID=你的Cloudflare账户ID
R2_ACCESS_KEY_ID=你的R2访问密钥ID
R2_SECRET_ACCESS_KEY=你的R2私钥
R2_BUCKET_NAME=你的R2桶名
```

3. 启动：

```bash
docker compose up -d --build
```

4. 浏览器访问：

```text
http://服务器IP:8765
```

网页转换产物会保存在 R2 的以下对象前缀：

```text
pdf-to-md/<任务ID>_<文件名>/
```

Docker Compose 不再挂载本地 `output/` 和 `uploads/` 目录。注意：这个网页服务能上传文件并调用你的 PaddleOCR token，云服务器上不要不设密码直接暴露公网。`WEB_USERNAME` 和 `WEB_PASSWORD` 配好后，浏览器会弹出登录框。

## 方案 A：直接用 PowerShell 运行

这台机器当前没有真实可用的 Python。你可以先用 PowerShell 版本，不需要安装 `requests`：

```powershell
$env:PADDLEOCR_TOKEN="你的PaddleOCR Token"
.\paddle_pdf_to_md.ps1 "C:\yourpath\xxxx.pdf" -OutputDir ".\output\charlie22"
```

如果 PowerShell 拦截脚本执行，先在当前窗口临时放开：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## 方案 B：用 Python 运行

### 1. 安装依赖

当前机器可用的 Python 是：

```powershell
C:\yourpath\python.exe
```

如果 `python` 命令仍然指向 Windows Store 占位符，就先用完整路径执行：

```powershell
& "C:\yourpath\python.exe" -m pip install --user -r requirements.txt
```

### 2. 配置 Token

推荐在项目根目录创建 `.env` 文件：

```text
PADDLEOCR_TOKEN=你的PaddleOCR Token
```

`.env` 已经加入 `.gitignore`，不会被提交。也可以继续用 PowerShell 临时配置：

```powershell
$env:PADDLEOCR_TOKEN="你的PaddleOCR Token"
```

环境变量优先级高于 `.env`。

### 3. 转换你的 PDF 或图片

```powershell
& "C:\yourpath\python.exe" .\paddle_pdf_to_md.py "C:\yourpath\xxx.pdf" --output-dir ".\output\charlie22"
```

本地输入支持 `.pdf`、`.jpg`、`.jpeg`、`.png`、`.tif` 和 `.tiff`。

输出内容：

```text
output\charlie22\
  xxx.md
  pages\
    page_0001.md
    page_0002.md
    ...
  output_images\
    ...
```

PaddleOCR Markdown 里引用的图片会按接口返回的相对路径保存到输出目录中，这样总 Markdown 里的图片链接可以直接打开。

## 可选参数

如果 PDF 有页面旋转，可以加：

```powershell
--doc-orientation
```

如果是拍照弯曲、页面不平整，可以加：

```powershell
--doc-unwarping
```

完整示例：

```powershell
& "C:\yourpath\python.exe" .\paddle_pdf_to_md.py "C:\yourpath\xxx.pdf" --output-dir ".\output\charlie22" --doc-orientation
```
