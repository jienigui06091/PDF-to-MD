import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import requests


JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
DEFAULT_MODEL = "PaddleOCR-VL-1.6"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class PaddleOcrError(RuntimeError):
    pass


def read_dotenv_value(name: str) -> Optional[str]:
    search_paths = [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]
    for env_path in dict.fromkeys(search_paths):
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    return None


def get_token() -> Optional[str]:
    return os.environ.get("PADDLEOCR_TOKEN") or read_dotenv_value("PADDLEOCR_TOKEN")


def request_with_retries(
    method: str,
    url: str,
    *,
    retries: int = 3,
    timeout: int = 60,
    **kwargs: Any,
) -> requests.Response:
    last_error: Optional[BaseException] = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
            if response.status_code < 500:
                return response
            last_error = PaddleOcrError(
                f"HTTP {response.status_code}: {response.text[:500]}"
            )
        except requests.RequestException as exc:
            last_error = exc

        if attempt < retries:
            time.sleep(min(2 ** attempt, 10))

    raise PaddleOcrError(f"Request failed after {retries} attempts: {last_error}")


def safe_output_path(output_dir: Path, relative_path: str) -> Path:
    clean_path = Path(relative_path.replace("\\", "/"))
    if clean_path.is_absolute() or ".." in clean_path.parts:
        clean_path = Path(clean_path.name)

    target = (output_dir / clean_path).resolve()
    output_root = output_dir.resolve()
    if output_root != target and output_root not in target.parents:
        raise PaddleOcrError(f"Unsafe output path from API: {relative_path}")
    return target


def slugify_filename(name: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return stem or "document"


def submit_job(
    file_path: str,
    token: str,
    model: str,
    optional_payload: Dict[str, bool],
) -> str:
    headers = {"Authorization": f"bearer {token}"}

    if file_path.startswith(("http://", "https://")):
        headers["Content-Type"] = "application/json"
        payload = {
            "fileUrl": file_path,
            "model": model,
            "optionalPayload": optional_payload,
        }
        response = request_with_retries(
            "POST", JOB_URL, headers=headers, json=payload, timeout=120
        )
    else:
        path = Path(file_path)
        if not path.exists():
            raise PaddleOcrError(f"File not found: {path}")
        if not path.is_file():
            raise PaddleOcrError(f"Path is not a file: {path}")

        data = {
            "model": model,
            "optionalPayload": json.dumps(optional_payload, ensure_ascii=False),
        }
        with path.open("rb") as file_obj:
            response = request_with_retries(
                "POST",
                JOB_URL,
                headers=headers,
                data=data,
                files={"file": file_obj},
                timeout=300,
            )

    if response.status_code != 200:
        raise PaddleOcrError(
            f"Job submit failed, HTTP {response.status_code}: {response.text}"
        )

    body = response.json()
    try:
        return body["data"]["jobId"]
    except KeyError as exc:
        raise PaddleOcrError(f"Unexpected submit response: {body}") from exc


def wait_for_result_url(
    job_id: str,
    token: str,
    poll_interval: int,
    max_wait_seconds: int,
) -> str:
    headers = {"Authorization": f"bearer {token}"}
    deadline = time.monotonic() + max_wait_seconds

    while True:
        if time.monotonic() > deadline:
            raise PaddleOcrError(
                f"Timed out waiting for job {job_id} after {max_wait_seconds} seconds"
            )

        response = request_with_retries(
            "GET", f"{JOB_URL}/{job_id}", headers=headers, timeout=60
        )
        if response.status_code != 200:
            raise PaddleOcrError(
                f"Job status failed, HTTP {response.status_code}: {response.text}"
            )

        body = response.json()
        data = body.get("data", {})
        state = data.get("state")

        if state == "pending":
            print("Job status: pending")
        elif state == "running":
            progress = data.get("extractProgress") or {}
            total_pages = progress.get("totalPages")
            extracted_pages = progress.get("extractedPages")
            if total_pages is not None and extracted_pages is not None:
                print(f"Job status: running, pages {extracted_pages}/{total_pages}")
            else:
                print("Job status: running")
        elif state == "done":
            progress = data.get("extractProgress") or {}
            print(
                "Job completed, extracted pages: "
                f"{progress.get('extractedPages', 'unknown')}"
            )
            try:
                return data["resultUrl"]["jsonUrl"]
            except KeyError as exc:
                raise PaddleOcrError(f"Result URL missing in response: {body}") from exc
        elif state == "failed":
            raise PaddleOcrError(f"Job failed: {data.get('errorMsg', body)}")
        else:
            print(f"Job status: {state or 'unknown'}")

        time.sleep(poll_interval)


def get_jsonl_text(jsonl_url: str) -> str:
    last_error: Optional[BaseException] = None
    for attempt in range(1, 6):
        try:
            response = request_with_retries("GET", jsonl_url, timeout=300)
            response.raise_for_status()
            return response.content.decode("utf-8-sig")
        except requests.RequestException as exc:
            last_error = exc
        if attempt < 5:
            wait_seconds = min(2 ** attempt, 20)
            print(f"Result download failed, retrying in {wait_seconds}s: {last_error}")
            time.sleep(wait_seconds)
    raise PaddleOcrError(f"Result download failed after retries: {last_error}")


def iter_jsonl_results(jsonl_url: str) -> Iterable[Dict[str, Any]]:
    for line_number, raw_line in enumerate(get_jsonl_text(jsonl_url).splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            raise PaddleOcrError(f"Invalid JSONL at line {line_number}") from exc


def download_file(url: str, target: Path) -> None:
    if target.exists() and target.stat().st_size > 0:
        return
    response = request_with_retries("GET", url, timeout=180)
    response.raise_for_status()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.content)


def save_results(jsonl_url: str, output_dir: Path, combined_md: Path) -> None:
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    page_num = 1
    with combined_md.open("w", encoding="utf-8", newline="\n") as combined_file:
        for item in iter_jsonl_results(jsonl_url):
            result = item.get("result") or {}
            layout_results = result.get("layoutParsingResults") or []

            for layout_result in layout_results:
                markdown = layout_result.get("markdown") or {}
                md_text = markdown.get("text") or ""

                page_file = pages_dir / f"page_{page_num:04d}.md"
                with page_file.open("w", encoding="utf-8", newline="\n") as page_handle:
                    page_handle.write(md_text)

                combined_file.write(f"\n\n<!-- page {page_num} -->\n\n")
                combined_file.write(md_text.rstrip())
                combined_file.write("\n")

                images = markdown.get("images") or {}
                for image_path, image_url in images.items():
                    target = safe_output_path(output_dir, image_path)
                    download_file(image_url, target)

                output_images = layout_result.get("outputImages") or {}
                for image_name, image_url in output_images.items():
                    safe_name = slugify_filename(f"{image_name}_{page_num:04d}.jpg")
                    download_file(image_url, output_dir / "output_images" / safe_name)

                print(f"Saved page {page_num}: {page_file}")
                page_num += 1

    if page_num == 1:
        raise PaddleOcrError("No layoutParsingResults found in OCR result")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a local PDF or file URL to Markdown with PaddleOCR."
    )
    parser.add_argument("input", help="Local PDF path or file URL")
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Output directory. Default: output",
    )
    parser.add_argument(
        "--combined-md",
        default=None,
        help="Combined Markdown path. Default: <output-dir>/<input-name>.md",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Default: {DEFAULT_MODEL}")
    parser.add_argument(
        "--job-id",
        default=None,
        help="Reuse an existing PaddleOCR job and skip uploading the input file.",
    )
    parser.add_argument("--poll-interval", type=int, default=5, help="Default: 5")
    parser.add_argument(
        "--max-wait-seconds",
        type=int,
        default=60 * 60 * 3,
        help="Default: 10800",
    )
    parser.add_argument(
        "--doc-orientation",
        action="store_true",
        help="Enable document orientation classification.",
    )
    parser.add_argument(
        "--doc-unwarping",
        action="store_true",
        help="Enable document unwarping.",
    )
    parser.add_argument(
        "--chart-recognition",
        action="store_true",
        help="Enable chart recognition.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = get_token()
    if not token:
        print(
            "Missing PaddleOCR token. Set it first:\n"
            '  PowerShell: $env:PADDLEOCR_TOKEN="your-token"\n'
            '  CMD:        set PADDLEOCR_TOKEN=your-token\n'
            '  .env:       PADDLEOCR_TOKEN=your-token',
            file=sys.stderr,
        )
        return 2

    output_dir = Path(args.output_dir)
    if args.combined_md:
        combined_md = Path(args.combined_md)
    else:
        input_name = Path(args.input).stem if not args.input.startswith("http") else "document"
        combined_md = output_dir / f"{slugify_filename(input_name)}.md"

    optional_payload = {
        "useDocOrientationClassify": bool(args.doc_orientation),
        "useDocUnwarping": bool(args.doc_unwarping),
        "useChartRecognition": bool(args.chart_recognition),
    }

    try:
        if args.job_id:
            job_id = args.job_id
            print(f"Reusing job: {job_id}")
        else:
            print(f"Submitting job: {args.input}")
            job_id = submit_job(args.input, token, args.model, optional_payload)
            print(f"Job submitted: {job_id}")

        jsonl_url = wait_for_result_url(
            job_id,
            token,
            poll_interval=args.poll_interval,
            max_wait_seconds=args.max_wait_seconds,
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        combined_md.parent.mkdir(parents=True, exist_ok=True)
        save_results(jsonl_url, output_dir, combined_md)

        print(f"Combined Markdown saved: {combined_md}")
        print(f"Output directory: {output_dir}")
        return 0
    except PaddleOcrError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
