import unittest
from types import SimpleNamespace
from unittest.mock import patch

from paddle_pdf_to_md import DEFAULT_MODEL, DOCUMENT_PARSING_MODELS
from web_app import get_document_parsing_models, save_results_to_r2


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


class DocumentParsingModelsTests(unittest.TestCase):
    def test_model_options_include_the_default_and_supported_models(self) -> None:
        models = get_document_parsing_models()

        self.assertEqual([model["id"] for model in models], list(DOCUMENT_PARSING_MODELS))
        self.assertEqual(models[0]["id"], DEFAULT_MODEL)
        self.assertTrue(models[0]["label"].endswith("(recommended)"))


if __name__ == "__main__":
    unittest.main()
