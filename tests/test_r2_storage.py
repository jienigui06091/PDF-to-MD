import os
import unittest
from unittest.mock import patch

from r2_storage import R2Config, R2StorageError, join_key, safe_relative_key


class R2ConfigTests(unittest.TestCase):
    def test_account_id_builds_endpoint(self) -> None:
        values = {
            "R2_ACCOUNT_ID": "account-id",
            "R2_ACCESS_KEY_ID": "access-key",
            "R2_SECRET_ACCESS_KEY": "secret-key",
            "R2_BUCKET_NAME": "documents",
            "R2_REGION": "auto",
            "R2_PREFIX": "/converted/",
        }
        with patch.dict(os.environ, {}, clear=True):
            config = R2Config.from_environment(values.get)

        self.assertEqual(
            config.endpoint_url,
            "https://account-id.r2.cloudflarestorage.com",
        )
        self.assertEqual(config.bucket_name, "documents")
        self.assertEqual(config.region, "auto")
        self.assertEqual(config.prefix, "converted")

    def test_missing_configuration_is_reported(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(R2StorageError, "R2_ACCESS_KEY_ID"):
                R2Config.from_environment(lambda _name: None)


class R2KeyTests(unittest.TestCase):
    def test_relative_keys_are_normalized(self) -> None:
        self.assertEqual(safe_relative_key(r"imgs\page.png"), "imgs/page.png")
        self.assertEqual(safe_relative_key("../page.png"), "page.png")
        self.assertEqual(join_key("jobs/one", "pages/1.md"), "jobs/one/pages/1.md")


if __name__ == "__main__":
    unittest.main()
