import unittest
from datetime import datetime, timezone
from io import BytesIO
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from paddle_pdf_to_md import PaddleOcrError, get_input_content_type, get_model
from web_app import (
    MultipartFormError,
    get_upload_suffix,
    jobs,
    jobs_lock,
    markdown_to_html,
    markdown_to_text,
    parse_uploaded_document,
    render_home,
    restore_jobs_from_r2,
    save_results_to_r2,
)


class FakeStorage:
    def __init__(self) -> None:
        self.config = SimpleNamespace(public_base_url="https://files.example.com")
        self.uploads = {}

    def public_url(self, key: str) -> str:
        return f"https://files.example.com/{key}"

    def put_bytes(self, key: str, data: bytes, *, content_type: str):
        self.uploads[key] = (data, content_type)
        return {"key": key, "size": len(data), "content_type": content_type}


class FakeRestoreStorage:
    def __init__(self, objects):
        self.config = SimpleNamespace(prefix="pdftomd")
        self.objects = objects

    def list_objects(self, prefix: str):
        if prefix != "pdftomd/":
            raise AssertionError(f"Unexpected prefix: {prefix}")
        return self.objects


class SaveResultsToR2Tests(unittest.TestCase):
    def test_all_outputs_are_uploaded_and_markdown_urls_are_rewritten(self) -> None:
        results = [
            {
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {
                                "text": "# Page\n\n![scan](imgs/scan.png)",
                                "images": {"imgs/scan.png": "https://source/scan"},
                            },
                            "outputImages": {"layout": "https://source/layout"},
                        }
                    ]
                }
            }
        ]
        storage = FakeStorage()

        with patch("web_app.iter_jsonl_results", return_value=iter(results)), patch(
            "web_app.download_resource",
            side_effect=[(b"image", "image/png"), (b"layout", "image/jpeg")],
        ):
            page_count, manifest, combined_key = save_results_to_r2(
                "https://source/results.jsonl",
                storage,
                "pdf-to-md/job_document",
                "document.md",
            )

        self.assertEqual(page_count, 1)
        self.assertEqual(combined_key, "pdf-to-md/job_document/document.md")
        self.assertEqual(len(manifest), 4)
        self.assertIn("pdf-to-md/job_document/imgs/scan.png", storage.uploads)
        self.assertIn("pdf-to-md/job_document/pages/page_0001.md", storage.uploads)
        self.assertIn("pdf-to-md/job_document/output_images/layout_0001.jpg", storage.uploads)
        combined = storage.uploads[combined_key][0].decode("utf-8")
        self.assertIn(
            "https://files.example.com/pdf-to-md/job_document/imgs/scan.png",
            combined,
        )

    def test_html_and_text_combined_outputs_are_generated(self) -> None:
        results = [
            {
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {
                                "text": "# Heading\n\n![scan](imgs/scan.png)",
                                "images": {},
                            }
                        }
                    ]
                }
            }
        ]
        storage = FakeStorage()

        with patch(
            "web_app.iter_jsonl_results", side_effect=lambda _: iter(results)
        ):
            _, _, html_key = save_results_to_r2(
                "https://source/results.jsonl",
                storage,
                "pdf-to-md/job_html",
                "document.html",
                "html",
            )
            _, _, text_key = save_results_to_r2(
                "https://source/results.jsonl",
                storage,
                "pdf-to-md/job_text",
                "document.txt",
                "text",
            )

        self.assertIn("<h1>Heading</h1>", storage.uploads[html_key][0].decode("utf-8"))
        self.assertEqual(storage.uploads[html_key][1], "text/html; charset=utf-8")
        self.assertIn("Heading", storage.uploads[text_key][0].decode("utf-8"))
        self.assertEqual(storage.uploads[text_key][1], "text/plain; charset=utf-8")


class OutputRenderingTests(unittest.TestCase):
    def test_html_escapes_source_markup(self) -> None:
        html_output = markdown_to_html("# Hello\n\n<script>alert(1)</script>")

        self.assertIn("<h1>Hello</h1>", html_output)
        self.assertIn("&lt;script&gt;", html_output)

    def test_plain_text_removes_markdown_syntax(self) -> None:
        self.assertEqual(markdown_to_text("# Title\n\n**Bold**"), "Title\n\nBold\n")

    def test_plain_text_extracts_visible_text_from_html_tables(self) -> None:
        text = markdown_to_text(
            "<table><tr><th>Name</th><th>Age</th></tr>"
            "<tr><td>Alice</td><td>30</td></tr></table>"
        )

        self.assertEqual(text, "Name Age\nAlice 30\n")
        self.assertNotIn("<", text)
        self.assertNotIn(">", text)


class PaddleOcrConfigurationTests(unittest.TestCase):
    def test_model_uses_environment_configuration(self) -> None:
        with patch.dict("os.environ", {"PADDLEOCR_MODEL": "custom-model"}):
            self.assertEqual(get_model(), "custom-model")

    def test_home_does_not_expose_api_key_or_model_endpoint(self) -> None:
        page = render_home().decode("utf-8")

        self.assertNotIn('name="api_key"', page)
        self.assertNotIn('name="model"', page)
        self.assertNotIn("/api/models", page)

    def test_image_input_formats_have_expected_mime_types(self) -> None:
        self.assertEqual(get_input_content_type("photo.JPG"), "image/jpeg")
        self.assertEqual(get_input_content_type("scan.png"), "image/png")
        self.assertEqual(get_input_content_type("archive.TIFF"), "image/tiff")
        self.assertEqual(get_upload_suffix("report.pdf"), ".pdf")
        self.assertEqual(get_upload_suffix("photo.jpg"), ".jpg")

    def test_unsupported_input_format_is_rejected(self) -> None:
        with self.assertRaisesRegex(PaddleOcrError, "Unsupported input format"):
            get_input_content_type("document.docx")


class MultipartUploadTests(unittest.TestCase):
    def test_upload_parser_streams_file_and_preserves_form_values(self) -> None:
        boundary = "----TestBoundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="document"; filename="scan.jpg"\r\n'
            "Content-Type: image/jpeg\r\n"
            "\r\n"
        ).encode() + b"image-bytes\r\n" + (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="name"\r\n'
            "\r\n"
            "my output\r\n"
            f"--{boundary}--\r\n"
        ).encode()

        with TemporaryDirectory() as temp_dir:
            upload = parse_uploaded_document(
                BytesIO(body),
                f"multipart/form-data; boundary={boundary}",
                temp_dir,
            )
            self.assertEqual(upload.filename, "scan.jpg")
            self.assertEqual(upload.fields["name"], "my output")
            self.assertEqual(upload.input_path.read_bytes(), b"image-bytes")
            upload.input_path.unlink()

    def test_empty_upload_is_rejected(self) -> None:
        boundary = "----TestBoundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="document"; filename="empty.jpg"\r\n'
            "Content-Type: image/jpeg\r\n"
            "\r\n"
            f"\r\n--{boundary}--\r\n"
        ).encode()

        with TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(MultipartFormError, "empty"):
                parse_uploaded_document(
                    BytesIO(body),
                    f"multipart/form-data; boundary={boundary}",
                    temp_dir,
                )


class RestoreJobsFromR2Tests(unittest.TestCase):
    def setUp(self) -> None:
        with jobs_lock:
            jobs.clear()

    def tearDown(self) -> None:
        with jobs_lock:
            jobs.clear()

    def test_completed_jobs_are_rebuilt_from_object_keys(self) -> None:
        changed_at = datetime(2026, 7, 13, 8, 30, tzinfo=timezone.utc)
        storage = FakeRestoreStorage(
            [
                {
                    "key": "pdftomd/abc123def456_report/report.md",
                    "size": 120,
                    "last_modified": changed_at,
                },
                {
                    "key": "pdftomd/abc123def456_report/pages/page_0001.md",
                    "size": 80,
                    "last_modified": changed_at,
                },
                {
                    "key": "pdftomd/abc123def456_report/imgs/chart.png",
                    "size": 256,
                    "last_modified": changed_at,
                },
            ]
        )

        restored = restore_jobs_from_r2(storage)

        self.assertEqual(restored, 1)
        job = jobs["abc123def456"]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["filename"], "report.pdf")
        self.assertEqual(job["page_count"], 1)
        self.assertEqual(
            job["combined_key"], "pdftomd/abc123def456_report/report.md"
        )
        self.assertEqual(len(job["objects"]), 3)

    def test_incomplete_prefix_is_shown_as_failed(self) -> None:
        storage = FakeRestoreStorage(
            [
                {
                    "key": "pdftomd/abc123def456_report/pages/page_0001.md",
                    "size": 80,
                    "last_modified": None,
                }
            ]
        )

        restore_jobs_from_r2(storage)

        self.assertEqual(jobs["abc123def456"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
