import unittest
from types import SimpleNamespace
from unittest.mock import patch

from web_app import save_results_to_r2


class FakeStorage:
    def __init__(self) -> None:
        self.config = SimpleNamespace(public_base_url="https://files.example.com")
        self.uploads = {}

    def public_url(self, key: str) -> str:
        return f"https://files.example.com/{key}"

    def put_bytes(self, key: str, data: bytes, *, content_type: str):
        self.uploads[key] = (data, content_type)
        return {"key": key, "size": len(data), "content_type": content_type}


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


if __name__ == "__main__":
    unittest.main()
