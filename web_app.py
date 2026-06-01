import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

import cgi
import base64
import html
import json
import mimetypes
import os
import shutil
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from paddle_pdf_to_md import (
    DEFAULT_MODEL,
    JOB_URL,
    PaddleOcrError,
    get_token,
    read_dotenv_value,
    request_with_retries,
    save_results,
    slugify_filename,
    submit_job,
)


HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT / "uploads"
WEB_OUTPUT_DIR = ROOT / "output" / "web"
MAX_UPLOAD_BYTES = 1024 * 1024 * 500
WEB_USERNAME = os.environ.get("WEB_USERNAME") or read_dotenv_value("WEB_USERNAME") or ""
WEB_PASSWORD = os.environ.get("WEB_PASSWORD") or read_dotenv_value("WEB_PASSWORD") or ""

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.Lock()


def update_job(job_id: str, **values: Any) -> None:
    with jobs_lock:
        job = jobs[job_id]
        job.update(values)
        job["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with jobs_lock:
        job = jobs.get(job_id)
        return dict(job) if job else None


def list_jobs() -> list[Dict[str, Any]]:
    with jobs_lock:
        return sorted(
            (dict(job) for job in jobs.values()),
            key=lambda item: item.get("created_at", ""),
            reverse=True,
        )


def format_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def poll_paddle_result_url(job_id: str, token: str, paddle_job_id: str) -> str:
    headers = {"Authorization": f"bearer {token}"}

    while True:
        response = request_with_retries(
            "GET", f"{JOB_URL}/{paddle_job_id}", headers=headers, timeout=60
        )
        if response.status_code != 200:
            raise PaddleOcrError(
                f"Job status failed, HTTP {response.status_code}: {response.text}"
            )

        body = response.json()
        data = body.get("data", {})
        state = data.get("state") or "unknown"
        progress = data.get("extractProgress") or {}

        update_job(
            job_id,
            paddle_state=state,
            total_pages=progress.get("totalPages"),
            extracted_pages=progress.get("extractedPages"),
            message=f"PaddleOCR status: {state}",
        )

        if state == "done":
            try:
                return data["resultUrl"]["jsonUrl"]
            except KeyError as exc:
                raise PaddleOcrError(f"Result URL missing in response: {body}") from exc
        if state == "failed":
            raise PaddleOcrError(f"Job failed: {data.get('errorMsg', body)}")

        time.sleep(5)


def run_conversion(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return

    try:
        token = get_token()
        if not token:
            raise PaddleOcrError("Missing PADDLEOCR_TOKEN in .env or environment")

        input_path = Path(job["input_path"])
        output_dir = Path(job["output_dir"])
        combined_md = Path(job["combined_md"])
        optional_payload = {
            "useDocOrientationClassify": bool(job.get("doc_orientation")),
            "useDocUnwarping": bool(job.get("doc_unwarping")),
            "useChartRecognition": bool(job.get("chart_recognition")),
        }

        update_job(job_id, status="submitting", message="Uploading PDF to PaddleOCR")
        paddle_job_id = submit_job(str(input_path), token, DEFAULT_MODEL, optional_payload)
        update_job(
            job_id,
            status="running",
            paddle_job_id=paddle_job_id,
            message=f"PaddleOCR job submitted: {paddle_job_id}",
        )

        jsonl_url = poll_paddle_result_url(job_id, token, paddle_job_id)
        update_job(job_id, status="saving", message="Saving Markdown and images")
        output_dir.mkdir(parents=True, exist_ok=True)
        combined_md.parent.mkdir(parents=True, exist_ok=True)
        save_results(jsonl_url, output_dir, combined_md)

        page_count = len(list((output_dir / "pages").glob("*.md")))
        update_job(
            job_id,
            status="done",
            message="Conversion completed",
            page_count=page_count,
        )
    except Exception as exc:
        update_job(job_id, status="failed", message=str(exc), error=repr(exc))


def render_page(title: str, body: str) -> bytes:
    token_notice = ""
    if not get_token():
        token_notice = """
        <div class="notice error">
          未检测到 <code>PADDLEOCR_TOKEN</code>。请先在项目根目录的 <code>.env</code> 里配置。
        </div>
        """

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d9dee7;
      --text: #18202f;
      --muted: #667085;
      --accent: #166534;
      --accent-soft: #e7f6ed;
      --danger: #b42318;
      --danger-soft: #fff1f0;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
    }}
    header {{
      background: #202938;
      color: #fff;
      padding: 18px 24px;
      border-bottom: 1px solid #111827;
    }}
    header h1 {{
      margin: 0;
      font-size: 20px;
      font-weight: 650;
      letter-spacing: 0;
    }}
    main {{
      max-width: 1040px;
      margin: 0 auto;
      padding: 24px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 20px;
      margin-bottom: 16px;
    }}
    h2 {{
      font-size: 16px;
      margin: 0 0 14px;
      letter-spacing: 0;
    }}
    label {{
      display: block;
      font-weight: 600;
      margin-bottom: 8px;
    }}
    input[type="file"], input[type="text"] {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      background: #fff;
      color: var(--text);
    }}
    .row {{
      display: grid;
      gap: 14px;
      grid-template-columns: 1fr 1fr;
    }}
    .checks {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin: 14px 0;
    }}
    .check {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      font-weight: 500;
      background: #fff;
    }}
    button, .button {{
      appearance: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      padding: 8px 14px;
      border: 1px solid #14532d;
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      font-weight: 650;
      text-decoration: none;
      cursor: pointer;
    }}
    .button.secondary {{
      background: #fff;
      color: #1f2937;
      border-color: var(--line);
    }}
    .notice {{
      border-radius: 6px;
      padding: 10px 12px;
      margin-bottom: 16px;
      background: var(--accent-soft);
      border: 1px solid #bbebc9;
    }}
    .notice.error {{
      background: var(--danger-soft);
      border-color: #fecdca;
      color: var(--danger);
    }}
    .muted {{ color: var(--muted); }}
    .jobs {{
      width: 100%;
      border-collapse: collapse;
    }}
    .jobs th, .jobs td {{
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    .status {{
      display: inline-block;
      border-radius: 999px;
      padding: 2px 8px;
      background: #eef2f6;
      color: #344054;
      font-size: 12px;
      font-weight: 650;
    }}
    .status.done {{ background: var(--accent-soft); color: var(--accent); }}
    .status.failed {{ background: var(--danger-soft); color: var(--danger); }}
    progress {{
      width: 100%;
      height: 14px;
      accent-color: var(--accent);
    }}
    code {{
      background: #eef2f6;
      border-radius: 4px;
      padding: 1px 4px;
    }}
    @media (max-width: 720px) {{
      main {{ padding: 16px; }}
      .row {{ grid-template-columns: 1fr; }}
      .jobs {{ display: block; overflow-x: auto; }}
    }}
  </style>
</head>
<body>
  <header><h1>PDF 转 Markdown</h1></header>
  <main>
    {token_notice}
    {body}
  </main>
</body>
</html>"""
    return page.encode("utf-8")


def render_home() -> bytes:
    rows = []
    for job in list_jobs():
        job_id = html.escape(job["id"])
        filename = html.escape(job.get("filename", ""))
        status = html.escape(job.get("status", "queued"))
        updated = html.escape(job.get("updated_at", ""))
        message = html.escape(job.get("message", ""))
        rows.append(
            f"""<tr>
              <td><a href="/job/{job_id}">{filename}</a></td>
              <td><span class="status {status}">{status}</span></td>
              <td>{message}</td>
              <td>{updated}</td>
            </tr>"""
        )

    jobs_table = "<p class='muted'>还没有任务。</p>"
    if rows:
        jobs_table = f"""<table class="jobs">
          <thead><tr><th>文件</th><th>状态</th><th>消息</th><th>更新时间</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>"""

    body = f"""
    <section class="panel">
      <h2>上传 PDF</h2>
      <form method="post" action="/upload" enctype="multipart/form-data">
        <label for="pdf">选择 PDF 文件</label>
        <input id="pdf" name="pdf" type="file" accept="application/pdf,.pdf" required>
        <div class="row" style="margin-top:14px">
          <div>
            <label for="name">输出目录名</label>
            <input id="name" name="name" type="text" placeholder="留空则使用 PDF 文件名">
          </div>
        </div>
        <div class="checks">
          <label class="check"><input type="checkbox" name="doc_orientation"> 页面方向检测</label>
          <label class="check"><input type="checkbox" name="doc_unwarping"> 页面矫正</label>
          <label class="check"><input type="checkbox" name="chart_recognition"> 图表识别</label>
        </div>
        <button type="submit">开始转换</button>
      </form>
    </section>
    <section class="panel">
      <h2>任务</h2>
      {jobs_table}
    </section>
    """
    return render_page("PDF 转 Markdown", body)


def render_job(job_id: str) -> bytes:
    job = get_job(job_id)
    if not job:
        return render_page("任务不存在", "<section class='panel'>任务不存在。</section>")

    filename = html.escape(job.get("filename", ""))
    body = f"""
    <section class="panel">
      <h2>{filename}</h2>
      <p><span id="status" class="status">loading</span></p>
      <progress id="progress" value="0" max="100"></progress>
      <p id="message" class="muted"></p>
      <p id="meta" class="muted"></p>
      <p id="links"></p>
    </section>
    <p><a class="button secondary" href="/">返回上传页</a></p>
    <script>
      const jobId = {json.dumps(job_id)};
      function esc(text) {{
        return String(text ?? '').replace(/[&<>"']/g, c => ({{
          '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }}[c]));
      }}
      async function refresh() {{
        const res = await fetch('/api/jobs/' + encodeURIComponent(jobId), {{cache: 'no-store'}});
        const job = await res.json();
        const status = document.getElementById('status');
        status.textContent = job.status || 'unknown';
        status.className = 'status ' + (job.status || '');
        document.getElementById('message').textContent = job.message || '';
        const total = Number(job.total_pages || 0);
        const done = Number(job.extracted_pages || 0);
        const progress = document.getElementById('progress');
        if (total > 0) {{
          progress.value = Math.round(done * 100 / total);
          document.getElementById('meta').textContent = `页数进度：${{done}}/${{total}}`;
        }} else {{
          progress.value = job.status === 'done' ? 100 : 0;
          document.getElementById('meta').textContent = job.updated_at ? `更新时间：${{job.updated_at}}` : '';
        }}
        const links = document.getElementById('links');
        if (job.status === 'done') {{
          links.innerHTML = '<a class="button" href="/download/' + encodeURIComponent(jobId) + '">下载 Markdown</a>' +
            ' <a class="button secondary" href="/files/' + encodeURIComponent(jobId) + '/">查看输出文件</a>';
          return;
        }}
        if (job.status === 'failed') {{
          links.innerHTML = '<span class="notice error">转换失败：' + esc(job.message || '') + '</span>';
          return;
        }}
        setTimeout(refresh, 3000);
      }}
      refresh();
    </script>
    """
    return render_page(f"任务 {filename}", body)


class WebHandler(BaseHTTPRequestHandler):
    server_version = "PaddlePdfToMd/1.0"

    def do_GET(self) -> None:
        if not self.check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self.send_html(render_home())
            return
        if path.startswith("/job/"):
            self.send_html(render_job(path.rsplit("/", 1)[-1]))
            return
        if path.startswith("/api/jobs/"):
            self.send_json_job(path.rsplit("/", 1)[-1])
            return
        if path.startswith("/download/"):
            self.send_download(path.rsplit("/", 1)[-1])
            return
        if path.startswith("/files/"):
            self.send_file_or_listing(path)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self.check_auth():
            return

        if urlparse(self.path).path != "/upload":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.handle_upload()

    def check_auth(self) -> bool:
        if not WEB_USERNAME or not WEB_PASSWORD:
            return True

        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            self.request_auth()
            return False

        try:
            raw = base64.b64decode(auth_header.removeprefix("Basic ").strip()).decode(
                "utf-8"
            )
        except Exception:
            self.request_auth()
            return False

        username, separator, password = raw.partition(":")
        if separator and username == WEB_USERNAME and password == WEB_PASSWORD:
            return True

        self.request_auth()
        return False

    def request_auth(self) -> None:
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="PDF to Markdown"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Authentication required".encode("utf-8"))

    def handle_upload(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        if content_length <= 0 or content_length > MAX_UPLOAD_BYTES:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(content_length),
            },
        )
        file_field = form["pdf"] if "pdf" in form else None
        if file_field is None or not getattr(file_field, "filename", ""):
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing PDF file")
            return

        original_name = Path(file_field.filename).name
        safe_name = slugify_filename(original_name)
        if not safe_name.lower().endswith(".pdf"):
            safe_name += ".pdf"

        display_name = form.getfirst("name", "").strip()
        output_stem = slugify_filename(display_name or Path(original_name).stem)
        job_id = uuid.uuid4().hex[:12]
        created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        upload_path = UPLOAD_DIR / f"{job_id}_{safe_name}"
        output_dir = WEB_OUTPUT_DIR / f"{job_id}_{output_stem}"
        combined_md = output_dir / f"{output_stem}.md"

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        with upload_path.open("wb") as output:
            shutil.copyfileobj(file_field.file, output)

        with jobs_lock:
            jobs[job_id] = {
                "id": job_id,
                "filename": original_name,
                "status": "queued",
                "message": "Queued",
                "input_path": str(upload_path),
                "input_size": upload_path.stat().st_size,
                "output_dir": str(output_dir),
                "combined_md": str(combined_md),
                "doc_orientation": "doc_orientation" in form,
                "doc_unwarping": "doc_unwarping" in form,
                "chart_recognition": "chart_recognition" in form,
                "created_at": created_at,
                "updated_at": created_at,
            }

        thread = threading.Thread(target=run_conversion, args=(job_id,), daemon=True)
        thread.start()

        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/job/{job_id}")
        self.end_headers()

    def send_html(self, body: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json_job(self, job_id: str) -> None:
        job = get_job(job_id)
        if not job:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        public_job = {
            key: value
            for key, value in job.items()
            if key
            not in {
                "input_path",
                "output_dir",
                "combined_md",
                "error",
            }
        }
        data = json.dumps(public_job, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_download(self, job_id: str) -> None:
        job = get_job(job_id)
        if not job or job.get("status") != "done":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        file_path = Path(job["combined_md"])
        if not file_path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        data = file_path.read_bytes()
        filename = file_path.name.encode("utf-8", "ignore").decode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file_or_listing(self, request_path: str) -> None:
        parts = request_path.split("/", 3)
        if len(parts) < 3:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        job_id = parts[2]
        rel_path = parts[3] if len(parts) > 3 else ""
        job = get_job(job_id)
        if not job or job.get("status") != "done":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        root = Path(job["output_dir"]).resolve()
        target = (root / rel_path).resolve()
        if root != target and root not in target.parents:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        if target.is_dir():
            rows = []
            if target != root:
                parent = Path(rel_path).parent.as_posix()
                rows.append(f'<li><a href="/files/{job_id}/{html.escape(parent)}">..</a></li>')
            for child in sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
                child_rel = child.relative_to(root).as_posix()
                label = child.name + ("/" if child.is_dir() else "")
                size = "" if child.is_dir() else f" <span class='muted'>{format_size(child.stat().st_size)}</span>"
                rows.append(
                    f'<li><a href="/files/{job_id}/{html.escape(child_rel)}">{html.escape(label)}</a>{size}</li>'
                )
            body = f"""
            <section class="panel">
              <h2>输出文件</h2>
              <p class="muted"><code>{html.escape(str(root))}</code></p>
              <ul>{''.join(rows)}</ul>
            </section>
            <p><a class="button secondary" href="/job/{job_id}">返回任务</a></p>
            """
            self.send_html(render_page("输出文件", body))
            return

        data = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or target.suffix.lower() == ".md":
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}")


def main() -> int:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    WEB_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Starting PDF to Markdown web app in {ROOT}", flush=True)
    server = ThreadingHTTPServer((HOST, PORT), WebHandler)
    print(f"PDF to Markdown web app running at http://{HOST}:{PORT}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
