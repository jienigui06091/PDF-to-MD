import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

import base64
import html
import json
import mimetypes
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Dict, Optional
from urllib.parse import quote, unquote, urlparse

from paddle_pdf_to_md import (
    JOB_URL,
    PaddleOcrError,
    get_input_content_type,
    get_model,
    get_token,
    iter_jsonl_results,
    read_dotenv_value,
    request_with_retries,
    slugify_filename,
    submit_job,
)
from r2_storage import R2Config, R2Storage, R2StorageError, join_key, safe_relative_key


HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
ROOT = Path(__file__).resolve().parent
MAX_UPLOAD_BYTES = 1024 * 1024 * 500
WEB_USERNAME = os.environ.get("WEB_USERNAME") or read_dotenv_value("WEB_USERNAME") or ""
WEB_PASSWORD = os.environ.get("WEB_PASSWORD") or read_dotenv_value("WEB_PASSWORD") or ""
OUTPUT_FORMATS = {
    "markdown": {
        "extension": ".md",
        "content_type": "text/markdown; charset=utf-8",
        "label": "Markdown (.md)",
    },
    "html": {
        "extension": ".html",
        "content_type": "text/html; charset=utf-8",
        "label": "HTML (.html)",
    },
    "text": {
        "extension": ".txt",
        "content_type": "text/plain; charset=utf-8",
        "label": "Plain text (.txt)",
    },
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.Lock()
r2_lock = threading.Lock()
r2_storage: Optional[R2Storage] = None


def get_r2_storage() -> R2Storage:
    global r2_storage
    with r2_lock:
        if r2_storage is None:
            r2_storage = R2Storage(R2Config.from_environment(read_dotenv_value))
        return r2_storage


def get_r2_configuration_error() -> str:
    try:
        R2Config.from_environment(read_dotenv_value)
    except R2StorageError as exc:
        return str(exc)
    return ""


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


def format_r2_time(value: Any) -> str:
    if value is not None and hasattr(value, "astimezone"):
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return time.strftime("%Y-%m-%d %H:%M:%S")


def restore_jobs_from_r2(storage: Optional[R2Storage] = None) -> int:
    storage = storage or get_r2_storage()
    base_prefix = storage.config.prefix.strip("/") + "/"
    grouped: dict[str, list[dict[str, Any]]] = {}

    for item in storage.list_objects(base_prefix):
        object_key = item.get("key", "")
        if not object_key.startswith(base_prefix):
            continue
        remaining = object_key[len(base_prefix) :]
        folder, separator, relative = remaining.partition("/")
        if not separator or not folder or not relative:
            continue
        grouped.setdefault(folder, []).append(
            {
                "key": object_key,
                "relative": relative,
                "size": int(item.get("size", 0)),
                "content_type": mimetypes.guess_type(relative)[0]
                or "application/octet-stream",
                "last_modified": item.get("last_modified"),
            }
        )

    restored = 0
    for folder, folder_objects in grouped.items():
        candidate_id, separator, output_stem = folder.partition("_")
        if not separator or not candidate_id or not output_stem:
            candidate_id = uuid.uuid5(uuid.NAMESPACE_URL, folder).hex[:12]
            output_stem = folder

        root_outputs = sorted(
            (
                item
                for item in folder_objects
                if "/" not in item["relative"]
                and get_output_format_from_filename(item["relative"]) is not None
            ),
            key=lambda item: (
                item["relative"]
                != (
                    f"{output_stem}"
                    f"{OUTPUT_FORMATS[get_output_format_from_filename(item['relative'])]['extension']}"
                ),
                item["relative"].lower(),
            ),
        )
        combined_item = root_outputs[0] if root_outputs else None
        output_format = (
            get_output_format_from_filename(combined_item["relative"])
            if combined_item
            else "markdown"
        )
        page_count = sum(
            1
            for item in folder_objects
            if item["relative"].startswith("pages/")
            and item["relative"].lower().endswith(".md")
        )
        timestamps = [
            item["last_modified"]
            for item in folder_objects
            if item.get("last_modified") is not None
        ]
        created_at = format_r2_time(min(timestamps) if timestamps else None)
        updated_at = format_r2_time(max(timestamps) if timestamps else None)
        public_objects = [
            {key: value for key, value in item.items() if key != "last_modified"}
            for item in sorted(
                folder_objects, key=lambda item: item["relative"].lower()
            )
        ]
        status = "done" if combined_item else "failed"
        message = (
            "Restored from R2"
            if combined_item
            else "Restored from R2, but combined Markdown is missing"
        )
        restored_job = {
            "id": candidate_id,
            "filename": f"{output_stem}.pdf",
            "status": status,
            "message": message,
            "r2_prefix": f"{base_prefix}{folder}",
            "output_format": output_format,
            "combined_name": combined_item["relative"] if combined_item else "",
            "combined_key": combined_item["key"] if combined_item else "",
            "page_count": page_count,
            "total_pages": page_count,
            "extracted_pages": page_count,
            "objects": public_objects,
            "created_at": created_at,
            "updated_at": updated_at,
        }
        with jobs_lock:
            if candidate_id not in jobs:
                jobs[candidate_id] = restored_job
                restored += 1

    return restored


def format_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def get_upload_suffix(file_name: str) -> str:
    get_input_content_type(file_name)
    return Path(file_name).suffix.lower()


def get_output_format(value: str) -> str:
    return value if value in OUTPUT_FORMATS else "markdown"


def get_output_format_from_filename(file_name: str) -> Optional[str]:
    extension = Path(file_name).suffix.lower()
    for output_format, details in OUTPUT_FORMATS.items():
        if details["extension"] == extension:
            return output_format
    return None


def render_markdown_inline(value: str) -> str:
    escaped = html.escape(value, quote=True)
    escaped = re.sub(
        r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)",
        r'<img src="\2" alt="\1">',
        escaped,
    )
    escaped = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)
    return escaped


def markdown_to_html(markdown: str) -> str:
    lines = re.sub(r"<!--.*?-->", "", markdown, flags=re.DOTALL).splitlines()
    parts = [
        "<!doctype html>",
        '<html lang="zh-CN">',
        '<head><meta charset="utf-8"><title>OCR output</title></head>',
        "<body>",
    ]
    index = 0

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith("```"):
            language = line.removeprefix("```").strip()
            code_lines = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                code_lines.append(lines[index])
                index += 1
            class_name = (
                f' class="language-{html.escape(language, quote=True)}"'
                if language
                else ""
            )
            code = html.escape("\n".join(code_lines))
            parts.append(f"<pre><code{class_name}>{code}</code></pre>")
            index += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            level = len(heading.group(1))
            parts.append(f"<h{level}>{render_markdown_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        list_match = re.match(r"^[-*+]\s+(.*)$", line)
        ordered_match = re.match(r"^\d+\.\s+(.*)$", line)
        if list_match or ordered_match:
            tag = "ol" if ordered_match else "ul"
            items = []
            while index < len(lines):
                match = (
                    re.match(r"^\d+\.\s+(.*)$", lines[index])
                    if tag == "ol"
                    else re.match(r"^[-*+]\s+(.*)$", lines[index])
                )
                if not match:
                    break
                items.append(f"<li>{render_markdown_inline(match.group(1))}</li>")
                index += 1
            parts.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue

        paragraph = [line]
        index += 1
        while index < len(lines) and lines[index].strip():
            if re.match(r"^(#{1,6})\s+|^[-*+]\s+|^\d+\.\s+|^```", lines[index]):
                break
            paragraph.append(lines[index])
            index += 1
        rendered_lines = "<br>".join(render_markdown_inline(item) for item in paragraph)
        parts.append(f"<p>{rendered_lines}</p>")

    parts.append("</body></html>")
    return "\n".join(parts)


class VisibleTextParser(HTMLParser):
    BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "dl",
        "dt",
        "dd",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "thead",
        "tfoot",
        "ul",
    }
    HIDDEN_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.HIDDEN_TAGS:
            self.hidden_depth += 1
            return
        if self.hidden_depth:
            return
        if tag == "br" or tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.HIDDEN_TAGS:
            self.hidden_depth = max(0, self.hidden_depth - 1)
            return
        if self.hidden_depth:
            return
        if tag in {"td", "th"}:
            self.parts.append(" ")
        elif tag in self.BLOCK_TAGS or tag == "tr":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)

    def get_text(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text


def markdown_to_text(markdown: str) -> str:
    text = re.sub(r"<!--.*?-->", "", markdown, flags=re.DOTALL)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1: \2", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1: \2", text)
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^>\s?", "", text)
    text = re.sub(r"`{1,3}", "", text)
    text = re.sub(r"(\*\*|__|\*|_|~~)", "", text)

    parser = VisibleTextParser()
    parser.feed(text)
    parser.close()
    return parser.get_text().strip() + "\n"


def render_output(markdown: str, output_format: str) -> tuple[bytes, str]:
    normalized_format = get_output_format(output_format)
    details = OUTPUT_FORMATS[normalized_format]
    if normalized_format == "html":
        content = markdown_to_html(markdown)
    elif normalized_format == "text":
        content = markdown_to_text(markdown)
    else:
        content = markdown
    return content.encode("utf-8"), details["content_type"]


class MultipartFormError(ValueError):
    pass


@dataclass
class UploadedDocument:
    fields: Dict[str, str]
    filename: str
    input_path: Path
    input_size: int


class MultipartReader:
    def __init__(self, stream: BinaryIO, boundary: bytes) -> None:
        self.stream = stream
        self.boundary = boundary
        self.delimiter = b"\r\n--" + boundary
        self.buffer = bytearray()

    def fill(self) -> bool:
        read_available = getattr(self.stream, "read1", None)
        chunk = (
            read_available(64 * 1024)
            if callable(read_available)
            else self.stream.read(64 * 1024)
        )
        if not chunk:
            return False
        self.buffer.extend(chunk)
        return True

    def read_line(self, limit: int = 16 * 1024) -> bytes:
        while True:
            line_end = self.buffer.find(b"\n")
            if line_end >= 0:
                line = bytes(self.buffer[: line_end + 1])
                del self.buffer[: line_end + 1]
                return line
            if len(self.buffer) > limit:
                raise MultipartFormError("Multipart header is too large")
            if not self.fill():
                raise MultipartFormError("Unexpected end of multipart body")

    def read_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        while True:
            line = self.read_line()
            if line in {b"\r\n", b"\n"}:
                return headers
            if b":" not in line:
                raise MultipartFormError("Invalid multipart header")
            name, value = line.rstrip(b"\r\n").split(b":", 1)
            headers[name.decode("ascii", "strict").lower()] = value.lstrip().decode(
                "latin-1"
            )

    def read_part(self, output: BinaryIO, max_size: Optional[int] = None) -> bool:
        written = 0
        keep = len(self.delimiter) + 2

        while True:
            index = self.buffer.find(self.delimiter)
            if index >= 0:
                delimiter_end = index + len(self.delimiter)
                while len(self.buffer) < delimiter_end + 2:
                    if not self.fill():
                        raise MultipartFormError("Invalid multipart boundary")
                suffix = bytes(self.buffer[delimiter_end : delimiter_end + 2])
                if suffix in {b"\r\n", b"--"}:
                    chunk = bytes(self.buffer[:index])
                    output.write(chunk)
                    written += len(chunk)
                    if max_size is not None and written > max_size:
                        raise MultipartFormError("Multipart field is too large")
                    del self.buffer[: delimiter_end + 2]

                    if suffix == b"--":
                        if len(self.buffer) < 2:
                            self.fill()
                        if self.buffer[:2] == b"\r\n":
                            del self.buffer[:2]
                        return True
                    return False

                output.write(bytes(self.buffer[: index + 1]))
                written += index + 1
                if max_size is not None and written > max_size:
                    raise MultipartFormError("Multipart field is too large")
                del self.buffer[: index + 1]
                continue

            if len(self.buffer) > keep:
                chunk = bytes(self.buffer[:-keep])
                output.write(chunk)
                written += len(chunk)
                if max_size is not None and written > max_size:
                    raise MultipartFormError("Multipart field is too large")
                del self.buffer[:-keep]

            if not self.fill():
                raise MultipartFormError("Unexpected end of multipart body")


def get_multipart_boundary(content_type: str) -> bytes:
    message = Message()
    message["Content-Type"] = content_type
    if message.get_content_type() != "multipart/form-data":
        raise MultipartFormError("Expected multipart/form-data")

    boundary = message.get_param("boundary", header="content-type")
    if not isinstance(boundary, str) or not boundary:
        raise MultipartFormError("Missing multipart boundary")
    try:
        return boundary.encode("ascii")
    except UnicodeEncodeError as exc:
        raise MultipartFormError("Invalid multipart boundary") from exc


def get_content_disposition_params(value: str) -> Dict[str, str]:
    message = Message()
    message["Content-Disposition"] = value
    params = message.get_params(header="content-disposition", unquote=True)
    if not params or params[0][0].lower() != "form-data":
        raise MultipartFormError("Invalid multipart content disposition")
    return {
        str(name).lower(): str(param_value)
        for name, param_value in params[1:]
        if param_value is not None
    }


def decode_multipart_filename(value: str) -> str:
    try:
        return value.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return value


def parse_uploaded_document(
    stream: BinaryIO, content_type: str, temp_dir: Optional[str] = None
) -> UploadedDocument:
    reader = MultipartReader(stream, get_multipart_boundary(content_type))
    first_boundary = reader.read_line()
    if first_boundary.rstrip(b"\r\n") != b"--" + reader.boundary:
        raise MultipartFormError("Invalid multipart body")

    fields: Dict[str, str] = {}
    document_path: Optional[Path] = None
    document_name = ""
    document_size = 0

    try:
        while True:
            headers = reader.read_headers()
            disposition = headers.get("content-disposition", "")
            params = get_content_disposition_params(disposition)
            field_name = params.get("name")
            if not field_name:
                raise MultipartFormError("Multipart field has no name")

            filename = params.get("filename")
            if filename is not None:
                if field_name != "document" or document_path is not None:
                    raise MultipartFormError("Expected exactly one document file")

                document_name = Path(decode_multipart_filename(filename)).name
                if not document_name:
                    raise MultipartFormError("Missing document file")
                suffix = Path(document_name).suffix.lower()
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix="document-to-md-upload-",
                    suffix=suffix,
                    delete=False,
                    dir=temp_dir,
                ) as output:
                    document_path = Path(output.name)
                    is_final = reader.read_part(output)
                    document_size = output.tell()
            else:
                output = BytesIO()
                is_final = reader.read_part(output, max_size=64 * 1024)
                fields[field_name] = output.getvalue().decode("utf-8", "replace")

            if is_final:
                break
    except Exception:
        if document_path:
            document_path.unlink(missing_ok=True)
        raise

    if document_path is None:
        raise MultipartFormError("Missing document file")
    if document_size <= 0:
        document_path.unlink(missing_ok=True)
        raise MultipartFormError("Uploaded document is empty")

    return UploadedDocument(
        fields=fields,
        filename=document_name,
        input_path=document_path,
        input_size=document_size,
    )


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


def download_resource(url: str) -> tuple[bytes, str]:
    response = request_with_retries("GET", url, timeout=180)
    try:
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        return response.content, content_type
    finally:
        response.close()


def save_results_to_r2(
    jsonl_url: str,
    storage: R2Storage,
    object_prefix: str,
    combined_name: str,
    output_format: str = "markdown",
) -> tuple[int, list[dict[str, Any]], str]:
    objects: dict[str, dict[str, Any]] = {}
    combined_parts: list[str] = []
    page_num = 1

    def upload(relative_path: str, data: bytes, content_type: str) -> None:
        relative_key = safe_relative_key(relative_path)
        object_key = join_key(object_prefix, relative_key)
        uploaded = storage.put_bytes(object_key, data, content_type=content_type)
        objects[relative_key] = {**uploaded, "relative": relative_key}

    for item in iter_jsonl_results(jsonl_url):
        result = item.get("result") or {}
        layout_results = result.get("layoutParsingResults") or []

        for layout_result in layout_results:
            markdown = layout_result.get("markdown") or {}
            md_text = markdown.get("text") or ""

            images = markdown.get("images") or {}
            for image_path, image_url in images.items():
                relative_key = safe_relative_key(image_path)
                object_key = join_key(object_prefix, relative_key)
                markdown_url = storage.public_url(object_key) or relative_key
                if image_path != markdown_url:
                    md_text = md_text.replace(image_path, markdown_url)
                if relative_key not in objects:
                    image_data, content_type = download_resource(image_url)
                    upload(relative_key, image_data, content_type)

            page_relative = f"pages/page_{page_num:04d}.md"
            upload(page_relative, md_text.encode("utf-8"), "text/markdown; charset=utf-8")

            combined_parts.append(f"\n\n<!-- page {page_num} -->\n\n")
            combined_parts.append(md_text.rstrip())
            combined_parts.append("\n")

            output_images = layout_result.get("outputImages") or {}
            for image_name, image_url in output_images.items():
                safe_name = slugify_filename(f"{image_name}_{page_num:04d}.jpg")
                relative_key = f"output_images/{safe_name}"
                image_data, content_type = download_resource(image_url)
                upload(relative_key, image_data, content_type)

            print(f"Uploaded page {page_num} to R2: {page_relative}")
            page_num += 1

    if page_num == 1:
        raise PaddleOcrError("No layoutParsingResults found in OCR result")

    combined_relative = safe_relative_key(combined_name)
    combined_content, combined_content_type = render_output(
        "".join(combined_parts), output_format
    )
    upload(
        combined_relative,
        combined_content,
        combined_content_type,
    )
    manifest = sorted(objects.values(), key=lambda item: item["relative"].lower())
    return page_num - 1, manifest, join_key(object_prefix, combined_relative)


def run_conversion(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return

    input_path = Path(job["input_path"])
    try:
        token = get_token()
        if not token:
            raise PaddleOcrError("Missing PaddleOCR token configuration")

        storage = get_r2_storage()
        optional_payload = {
            "useDocOrientationClassify": bool(job.get("doc_orientation")),
            "useDocUnwarping": bool(job.get("doc_unwarping")),
            "useChartRecognition": bool(job.get("chart_recognition")),
        }

        update_job(
            job_id, status="submitting", message="Uploading document to PaddleOCR"
        )
        try:
            paddle_job_id = submit_job(
                str(input_path),
                token,
                job["model"],
                optional_payload,
            )
        finally:
            input_path.unlink(missing_ok=True)
        update_job(
            job_id,
            status="running",
            paddle_job_id=paddle_job_id,
            message=f"PaddleOCR job submitted: {paddle_job_id}",
        )

        jsonl_url = poll_paddle_result_url(job_id, token, paddle_job_id)
        update_job(job_id, status="saving", message="Uploading Markdown and images to R2")
        page_count, objects, combined_key = save_results_to_r2(
            jsonl_url,
            storage,
            job["r2_prefix"],
            job["combined_name"],
            job.get("output_format", "markdown"),
        )
        update_job(
            job_id,
            status="done",
            message="Conversion completed and uploaded to R2",
            page_count=page_count,
            objects=objects,
            combined_key=combined_key,
        )
    except Exception as exc:
        update_job(job_id, status="failed", message=str(exc), error=repr(exc))
    finally:
        input_path.unlink(missing_ok=True)


def render_page(title: str, body: str) -> bytes:
    notices = []
    if not get_token() and os.environ.get("SHOW_MISSING_TOKEN_NOTICE") == "1":
        notices.append("""
        <div class="notice error">
          未检测到 <code>PADDLEOCR_TOKEN</code>。请先在项目根目录的 <code>.env</code> 里配置。
        </div>
        """)
    r2_error = get_r2_configuration_error()
    if r2_error:
        notices.append(f"""
        <div class="notice error">
          {html.escape(r2_error)}。请先配置 R2 后再上传文件。
        </div>
        """)

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
    input[type="file"], input[type="text"], input[type="password"], select {{
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
  <header><h1>PDF / 图片转 Markdown</h1></header>
  <main>
    {''.join(notices)}
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
      <h2>上传文件</h2>
      <form id="upload-form" method="post" action="/upload" enctype="multipart/form-data">
        <label for="document">选择 PDF 或图片</label>
        <input id="document" name="document" type="file" accept="application/pdf,.pdf,image/jpeg,.jpg,.jpeg,image/png,.png,image/tiff,.tif,.tiff" required>
        <div class="row" style="margin-top:14px">
          <div>
            <label for="name">输出目录名</label>
            <input id="name" name="name" type="text" placeholder="留空则使用 PDF 文件名">
          </div>
          <div>
            <label for="output_format">输出格式</label>
            <select id="output_format" name="output_format">
              <option value="markdown">Markdown (.md)</option>
              <option value="html">HTML (.html)</option>
              <option value="text">Plain text (.txt)</option>
            </select>
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
    return render_page("PDF / 图片转 Markdown", body)


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
          const outputLabels = {{markdown: 'Markdown', html: 'HTML', text: 'TXT'}};
          const outputLabel = outputLabels[job.output_format] || 'output';
          links.innerHTML = '<a class="button" href="/download/' + encodeURIComponent(jobId) + '">下载 ' + outputLabel + '</a>' +
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
        if path == "/upload":
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.end_headers()
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

        path = urlparse(self.path).path
        if path != "/upload":
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
        try:
            content_length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return
        if content_length <= 0 or content_length > MAX_UPLOAD_BYTES:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return

        try:
            storage = get_r2_storage()
        except R2StorageError as exc:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
            return

        try:
            uploaded = parse_uploaded_document(
                self.rfile, self.headers.get("Content-Type", "")
            )
        except MultipartFormError as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except Exception as exc:
            self.log_error("Unable to parse multipart upload: %s", exc)
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to process upload")
            return

        original_name = uploaded.filename
        upload_path = uploaded.input_path
        input_size = uploaded.input_size
        try:
            input_suffix = get_upload_suffix(original_name)
        except PaddleOcrError as exc:
            upload_path.unlink(missing_ok=True)
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, str(exc))
            return
        display_name = uploaded.fields.get("name", "").strip()
        output_format = get_output_format(uploaded.fields.get("output_format", ""))
        if not get_token():
            upload_path.unlink(missing_ok=True)
            self.send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Missing PaddleOCR token configuration",
            )
            return
        model = get_model()
        output_stem = slugify_filename(display_name or Path(original_name).stem)
        job_id = uuid.uuid4().hex[:12]
        created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        object_prefix = f"{storage.config.prefix}/{job_id}_{output_stem}"
        output_details = OUTPUT_FORMATS[output_format]

        with jobs_lock:
            jobs[job_id] = {
                "id": job_id,
                "filename": original_name,
                "status": "queued",
                "message": "Queued",
                "input_path": str(upload_path),
                "input_size": input_size,
                "model": model,
                "r2_prefix": object_prefix,
                "output_format": output_format,
                "combined_name": f"{output_stem}{output_details['extension']}",
                "doc_orientation": "doc_orientation" in uploaded.fields,
                "doc_unwarping": "doc_unwarping" in uploaded.fields,
                "chart_recognition": "chart_recognition" in uploaded.fields,
                "created_at": created_at,
                "updated_at": created_at,
            }

        thread = threading.Thread(target=run_conversion, args=(job_id,), daemon=True)
        try:
            thread.start()
        except Exception:
            upload_path.unlink(missing_ok=True)
            with jobs_lock:
                jobs.pop(job_id, None)
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to start job")
            return

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
                "objects",
                "combined_key",
                "error",
            }
        }
        self.send_json(public_job)

    def send_json(
        self, payload: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_download(self, job_id: str) -> None:
        job = get_job(job_id)
        if not job or job.get("status") != "done":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        combined_key = job.get("combined_key")
        combined_item = next(
            (
                item
                for item in job.get("objects", [])
                if item.get("key") == combined_key
            ),
            None,
        )
        if not combined_item:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_r2_object(combined_item, attachment=True)

    def send_r2_object(self, item: Dict[str, Any], *, attachment: bool = False) -> None:
        try:
            result = get_r2_storage().get_object(item["key"])
        except Exception as exc:
            print(f"R2 download failed for {item.get('key')}: {exc}")
            self.send_error(HTTPStatus.BAD_GATEWAY, "Unable to read object from R2")
            return

        body = result["Body"]
        content_type = result.get("ContentType") or item.get(
            "content_type", "application/octet-stream"
        )
        if content_type.startswith("text/") and "charset=" not in content_type:
            content_type += "; charset=utf-8"
        content_length = result.get("ContentLength", item.get("size"))

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        if attachment:
            filename = PurePosixPath(item["relative"]).name
            ascii_name = "".join(
                char for char in filename if ord(char) < 128 and char not in '"\\'
            ) or "document"
            disposition = (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(filename)}"
            )
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        try:
            while True:
                chunk = body.read(1024 * 128)
                if not chunk:
                    break
                self.wfile.write(chunk)
        finally:
            body.close()

    def send_file_or_listing(self, request_path: str) -> None:
        parts = request_path.split("/", 3)
        if len(parts) < 3:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        job_id = parts[2]
        rel_path = unquote(parts[3]) if len(parts) > 3 else ""
        job = get_job(job_id)
        if not job or job.get("status") != "done":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        normalized = rel_path.replace("\\", "/").strip("/")
        rel_parts = PurePosixPath(normalized).parts if normalized else ()
        if ".." in rel_parts:
            self.send_error(HTTPStatus.FORBIDDEN)
            return

        objects = job.get("objects", [])
        target_item = next(
            (item for item in objects if item.get("relative") == normalized), None
        )
        if target_item:
            self.send_r2_object(target_item)
            return

        directory_prefix = f"{normalized}/" if normalized else ""
        entries: dict[str, dict[str, Any]] = {}
        for item in objects:
            relative = item.get("relative", "")
            if not relative.startswith(directory_prefix):
                continue
            remainder = relative[len(directory_prefix) :]
            if not remainder:
                continue
            name, separator, _ = remainder.partition("/")
            if separator:
                entries[name] = {"is_dir": True}
            elif name not in entries:
                entries[name] = {"is_dir": False, "item": item}

        if not entries and normalized:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        rows = []
        if normalized:
            parent = PurePosixPath(normalized).parent.as_posix()
            parent = "" if parent == "." else parent
            rows.append(
                f'<li><a href="/files/{job_id}/{quote(parent, safe="/")}">..</a></li>'
            )
        for name, entry in sorted(
            entries.items(), key=lambda pair: (not pair[1]["is_dir"], pair[0].lower())
        ):
            child_relative = f"{directory_prefix}{name}"
            label = name + ("/" if entry["is_dir"] else "")
            size = ""
            if not entry["is_dir"]:
                size = (
                    " <span class='muted'>"
                    f"{format_size(int(entry['item'].get('size', 0)))}</span>"
                )
            rows.append(
                f'<li><a href="/files/{job_id}/{quote(child_relative, safe="/")}">'
                f"{html.escape(label)}</a>{size}</li>"
            )
        body = f"""
        <section class="panel">
          <h2>R2 输出文件</h2>
          <p class="muted"><code>{html.escape(job.get('r2_prefix', ''))}</code></p>
          <ul>{''.join(rows)}</ul>
        </section>
        <p><a class="button secondary" href="/job/{job_id}">返回任务</a></p>
        """
        self.send_html(render_page("R2 输出文件", body))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}")


def main() -> int:
    print(f"Starting PDF to Markdown web app in {ROOT}", flush=True)
    try:
        restored_count = restore_jobs_from_r2()
        print(
            f"Restored {restored_count} job(s) from R2 prefix "
            f"{get_r2_storage().config.prefix}/",
            flush=True,
        )
    except Exception as exc:
        print(f"Unable to restore jobs from R2: {exc}", flush=True)
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
