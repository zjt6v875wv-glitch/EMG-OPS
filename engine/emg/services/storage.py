"""Object storage for original uploads and report versions.

Files are content-addressed by SHA-256 and never modified after writing.
Backends: local directory (dev) or S3-compatible (MinIO, AWS S3, R2).
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, data: bytes, prefix: str, file_name: str) -> tuple[str, str]:
        sha = hashlib.sha256(data).hexdigest()
        key = f"{prefix}/{sha[:2]}/{sha}/{file_name}"
        dest = self.root / key
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            dest.chmod(0o444)
        return key, sha

    def put_file(self, path: str | Path, prefix: str, file_name: str | None = None) -> tuple[str, str]:
        return self.put_bytes(Path(path).read_bytes(), prefix, file_name or Path(path).name)

    def path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("invalid storage key")
        return p

    def get_bytes(self, key: str) -> bytes:
        return self.path(key).read_bytes()

    def copy_to(self, key: str, dest: str | Path) -> Path:
        dest = Path(dest)
        shutil.copyfile(self.path(key), dest)
        return dest


class S3Storage:  # pragma: no cover - exercised in deployment
    def __init__(self, bucket: str, endpoint: str | None = None):
        import boto3

        self.bucket = bucket
        self.s3 = boto3.client("s3", endpoint_url=endpoint)

    def put_bytes(self, data: bytes, prefix: str, file_name: str) -> tuple[str, str]:
        sha = hashlib.sha256(data).hexdigest()
        key = f"{prefix}/{sha[:2]}/{sha}/{file_name}"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)
        return key, sha

    def put_file(self, path, prefix, file_name=None):
        return self.put_bytes(Path(path).read_bytes(), prefix, file_name or Path(path).name)

    def get_bytes(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def copy_to(self, key: str, dest) -> Path:
        Path(dest).write_bytes(self.get_bytes(key))
        return Path(dest)


def get_storage():
    if os.environ.get("STORAGE_BACKEND", "local") == "s3":
        return S3Storage(os.environ["S3_BUCKET"], os.environ.get("S3_ENDPOINT"))
    return LocalStorage(os.environ.get("STORAGE_DIR", Path(__file__).resolve().parents[3] / "storage"))
