import mimetypes
import os
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Optional
from urllib.parse import quote

import boto3
from botocore.config import Config


class R2StorageError(RuntimeError):
    pass


DotenvReader = Callable[[str], Optional[str]]


def _setting(name: str, dotenv_reader: DotenvReader) -> str:
    return (os.environ.get(name) or dotenv_reader(name) or "").strip()


@dataclass(frozen=True)
class R2Config:
    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    bucket_name: str
    region: str = "auto"
    prefix: str = "pdf-to-md"
    public_base_url: str = ""

    @classmethod
    def from_environment(cls, dotenv_reader: DotenvReader) -> "R2Config":
        account_id = _setting("R2_ACCOUNT_ID", dotenv_reader)
        endpoint_url = (
            _setting("R2_ENDPOINT_URL", dotenv_reader)
            or _setting("R2_ENDPOINT", dotenv_reader)
            or (
                f"https://{account_id}.r2.cloudflarestorage.com"
                if account_id
                else ""
            )
        )
        access_key_id = _setting("R2_ACCESS_KEY_ID", dotenv_reader)
        secret_access_key = _setting("R2_SECRET_ACCESS_KEY", dotenv_reader)
        bucket_name = _setting("R2_BUCKET_NAME", dotenv_reader) or _setting(
            "R2_BUCKET", dotenv_reader
        )

        missing = []
        if not endpoint_url:
            missing.append("R2_ENDPOINT_URL 或 R2_ACCOUNT_ID")
        if not access_key_id:
            missing.append("R2_ACCESS_KEY_ID")
        if not secret_access_key:
            missing.append("R2_SECRET_ACCESS_KEY")
        if not bucket_name:
            missing.append("R2_BUCKET_NAME")
        if missing:
            raise R2StorageError(f"缺少 R2 配置：{', '.join(missing)}")

        prefix = _setting("R2_PREFIX", dotenv_reader).strip("/") or "pdf-to-md"
        region = _setting("R2_REGION", dotenv_reader) or "auto"
        public_base_url = _setting("R2_PUBLIC_BASE_URL", dotenv_reader).rstrip("/")
        return cls(
            endpoint_url=endpoint_url.rstrip("/"),
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            bucket_name=bucket_name,
            region=region,
            prefix=prefix,
            public_base_url=public_base_url,
        )


def safe_relative_key(relative_path: str) -> str:
    normalized = relative_path.replace("\\", "/").lstrip("/")
    path = PurePosixPath(normalized)
    if not normalized or normalized in {".", "/"}:
        raise R2StorageError("R2 对象相对路径不能为空")
    if ".." in path.parts:
        path = PurePosixPath(path.name)
    clean = path.as_posix().lstrip("/")
    if not clean or clean == ".":
        raise R2StorageError(f"无效的 R2 对象路径：{relative_path}")
    return clean


def join_key(prefix: str, relative_path: str) -> str:
    return f"{prefix.strip('/')}/{safe_relative_key(relative_path)}"


class R2Storage:
    def __init__(self, config: R2Config):
        self.config = config
        self.client = boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            region_name=config.region,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: Optional[str] = None,
    ) -> dict[str, Any]:
        clean_key = safe_relative_key(key)
        detected_type = content_type or mimetypes.guess_type(clean_key)[0]
        request: dict[str, Any] = {
            "Bucket": self.config.bucket_name,
            "Key": clean_key,
            "Body": data,
        }
        if detected_type:
            request["ContentType"] = detected_type.split(";", 1)[0].strip()
        self.client.put_object(**request)
        return {
            "key": clean_key,
            "size": len(data),
            "content_type": detected_type or "application/octet-stream",
        }

    def get_object(self, key: str) -> dict[str, Any]:
        return self.client.get_object(
            Bucket=self.config.bucket_name,
            Key=safe_relative_key(key),
        )

    def public_url(self, key: str) -> str:
        if not self.config.public_base_url:
            return ""
        return f"{self.config.public_base_url}/{quote(safe_relative_key(key), safe='/')}"
