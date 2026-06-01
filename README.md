# PDF to Markdown with PaddleOCR

这个目录里放的是一个 PaddleOCR 云端 OCR 转 Markdown 工具，适合扫描版 PDF、图片型 PDF、影印书。

## 网页上传

启动本地网页：

```powershell
& "C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe" .\web_app.py
```

浏览器打开：

```text
http://127.0.0.1:8765
```

页面上传 PDF 后会在后台转换，完成后可下载合并后的 Markdown，也可以查看分页 Markdown 和图片。

## 方案 A：直接用 PowerShell 运行

这台机器当前没有真实可用的 Python。你可以先用 PowerShell 版本，不需要安装 `requests`：

```powershell
$env:PADDLEOCR_TOKEN="你的PaddleOCR Token"
.\paddle_pdf_to_md.ps1 "C:\Users\Admin\Desktop\查理九世\22•所罗们王的魔戒.pdf" -OutputDir ".\output\charlie22"
```

如果 PowerShell 拦截脚本执行，先在当前窗口临时放开：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## 方案 B：用 Python 运行

### 1. 安装依赖

当前机器可用的 Python 是：

```powershell
C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe
```

如果 `python` 命令仍然指向 Windows Store 占位符，就先用完整路径执行：

```powershell
& "C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe" -m pip install --user -r requirements.txt
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

### 3. 转换你的 PDF

```powershell
& "C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe" .\paddle_pdf_to_md.py "C:\Users\Admin\Desktop\查理九世\22•所罗们王的魔戒.pdf" --output-dir ".\output\charlie22"
```

输出内容：

```text
output\charlie22\
  22•所罗们王的魔戒.md
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
& "C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe" .\paddle_pdf_to_md.py "C:\Users\Admin\Desktop\查理九世\22•所罗们王的魔戒.pdf" --output-dir ".\output\charlie22" --doc-orientation
```
