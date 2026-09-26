"""Object storage adapter: local filesystem (development, small installs) or S3-compatible EU storage.

Keys are random and carry no personal data; the project relation lives in the database so recordings
can be re-assigned without copying files. Originals are never overwritten."""
from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO, Iterator
from urllib.parse import quote

from sqlalchemy.orm import Session

from .config import get_settings
from .db import utcnow
from .models import MediaAsset

EXT_BY_MIME = {
    "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/amr": ".amr", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/webm": ".webm", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic",
    "video/mp4": ".mp4", "video/3gpp": ".3gp", "application/pdf": ".pdf", "application/zip": ".zip",
    "text/plain": ".txt",
}


def base_mime(mime: str | None) -> str:
    return (mime or "application/octet-stream").split(";")[0].strip().lower()


def ext_for_mime(mime: str | None) -> str:
    return EXT_BY_MIME.get(base_mime(mime), ".bin")


def new_key(kind: str, ext: str) -> str:
    now = utcnow()
    return f"{kind}/{now:%Y/%m}/{uuid.uuid4().hex}{ext}"


def file_sha256(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def content_disposition(filename: str, disposition: str = "attachment") -> str:
    ascii_name = filename.encode("ascii", "ignore").decode() or "bestand"
    ascii_name = ascii_name.replace('"', "")
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


class StorageError(RuntimeError):
    pass


class LocalStorage:
    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise StorageError("invalid key")
        return p

    def put_file(self, key: str, src: Path, content_type: str) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            raise StorageError("refusing to overwrite existing object")
        tmp = dest.with_suffix(dest.suffix + ".part")
        shutil.copyfile(src, tmp)
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, dest)

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(data)
        try:
            self.put_file(key, Path(tmp.name), content_type)
        finally:
            os.unlink(tmp.name)

    def open(self, key: str) -> BinaryIO:
        return open(self._path(key), "rb")

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()

    def size(self, key: str) -> int:
        return self._path(key).stat().st_size

    @contextmanager
    def local_path(self, key: str) -> Iterator[Path]:
        yield self._path(key)

    def _sig(self, key: str, exp: int, filename: str, disp: str, ctype: str) -> str:
        msg = f"{key}|{exp}|{filename}|{disp}|{ctype}".encode()
        return hmac.new(get_settings().secret_key.encode(), msg, hashlib.sha256).hexdigest()[:40]

    def url(self, key: str, *, filename: str, content_type: str, disposition: str = "inline", ttl: int = 900) -> str:
        exp = int(time.time()) + ttl
        sig = self._sig(key, exp, filename, disposition, content_type)
        return (f"/media/{quote(key)}?e={exp}&n={quote(filename)}&d={disposition}"
                f"&t={quote(content_type)}&s={sig}")

    def verify(self, key: str, exp: str, filename: str, disp: str, ctype: str, sig: str) -> bool:
        try:
            exp_i = int(exp)
        except (TypeError, ValueError):
            return False
        if exp_i < time.time():
            return False
        return hmac.compare_digest(self._sig(key, exp_i, filename, disp, ctype), sig or "")


class S3Storage:
    """S3-compatible storage (e.g. Scaleway Object Storage, OVH, AWS eu-central-1). Requires boto3."""
    name = "s3"

    def __init__(self) -> None:
        s = get_settings()
        try:
            import boto3  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on deployment
            raise StorageError("VT_STORAGE_BACKEND=s3 vereist 'pip install boto3'") from exc
        self.bucket = s.s3_bucket
        self.client = boto3.client(
            "s3", endpoint_url=s.s3_endpoint or None, region_name=s.s3_region,
            aws_access_key_id=s.s3_access_key or None, aws_secret_access_key=s.s3_secret_key or None,
        )

    def put_file(self, key: str, src: Path, content_type: str) -> None:
        self.client.upload_file(str(src), self.bucket, key, ExtraArgs={"ContentType": content_type})

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def open(self, key: str) -> BinaryIO:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"]

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def size(self, key: str) -> int:
        return int(self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"])

    @contextmanager
    def local_path(self, key: str) -> Iterator[Path]:
        fd, name = tempfile.mkstemp(suffix=Path(key).suffix)
        os.close(fd)
        try:
            self.client.download_file(self.bucket, key, name)
            yield Path(name)
        finally:
            os.unlink(name)

    def url(self, key: str, *, filename: str, content_type: str, disposition: str = "inline", ttl: int = 900) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key, "ResponseContentType": content_type,
                    "ResponseContentDisposition": content_disposition(filename, disposition)},
            ExpiresIn=ttl,
        )


@lru_cache(maxsize=1)
def get_storage():
    s = get_settings()
    if s.storage_backend == "s3":
        return S3Storage()
    return LocalStorage(s.storage_local_path)


def store_path(session: Session, path: Path, *, kind: str, mime: str, project_id: str | None,
               source: str, is_original: bool = True, derived_from_id: str | None = None,
               original_filename: str = "", duration: float | None = None, width: int | None = None,
               height: int | None = None, story_id: str | None = None, ext: str | None = None) -> MediaAsset:
    """Copy a local file into storage and register it. Returns the flushed MediaAsset."""
    storage = get_storage()
    key = new_key(kind, ext or ext_for_mime(mime))
    storage.put_file(key, path, base_mime(mime))
    asset = MediaAsset(
        project_id=project_id, kind=kind, storage_key=key, mime_type=mime, size_bytes=path.stat().st_size,
        sha256=file_sha256(path), source=source, is_original=is_original, derived_from_id=derived_from_id,
        original_filename=original_filename, duration_seconds=duration, width=width, height=height,
        story_id=story_id, status="stored",
    )
    session.add(asset)
    session.flush()
    return asset


def asset_url(asset: MediaAsset, *, filename: str | None = None, disposition: str = "inline", ttl: int = 900) -> str:
    name = filename or asset.original_filename or f"{asset.kind}{ext_for_mime(asset.mime_type)}"
    return get_storage().url(asset.storage_key, filename=name, content_type=base_mime(asset.mime_type),
                             disposition=disposition, ttl=ttl)
