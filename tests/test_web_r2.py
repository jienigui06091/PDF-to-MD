import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from paddle_pdf_to_md import get_model
from web_app import (
    jobs,
    jobs_lock,
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


class PaddleOcrConfigurationTests(unittest.TestCase):
    def test_model_uses_environment_configuration(self) -> None:
        with patch.dict("os.environ", {"PADDLEOCR_MODEL": "custom-model"}):
            self.assertEqual(get_model(), "custom-model")

    def test_home_does_not_expose_api_key_or_model_endpoint(self) -> None:
        page = render_home().decode("utf-8")

        self.assertNotIn('name="api_key"', page)
        self.assertNotIn('name="model"', page)
        self.assertNotIn("/api/models", page)


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
