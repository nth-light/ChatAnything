"""Plugin package sources: local directories, archives and remote URLs."""

from __future__ import annotations

import hashlib
import shutil
import stat
import tempfile
import urllib.request
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...errors import PluginSourceError, PluginValidationError
from .manifest import PluginManifest, find_manifest, load_manifest

DEFAULT_MAX_PACKAGE_BYTES = 50 * 1024 * 1024
DEFAULT_DOWNLOAD_TIMEOUT = 20.0


@dataclass(slots=True)
class StagedPackage:
    """A validated plugin package extracted into a readable directory."""

    manifest: PluginManifest
    root: Path
    origin: str
    checksum: str = ""
    temporary: Any = field(default=None, repr=False, compare=False)

    def cleanup(self) -> None:
        """Remove temporary staging resources (directories are left intact)."""

        temporary = self.temporary
        self.temporary = None
        if temporary is not None:
            try:
                temporary.cleanup()
            except Exception:  # noqa: BLE001 - cleanup is best effort
                pass


class PluginSource(ABC):
    """Base class for anything that can produce a :class:`StagedPackage`."""

    @abstractmethod
    def stage(self) -> StagedPackage:
        """Validate and materialise the plugin package."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"


class DirectorySource(PluginSource):
    """A local directory containing ``plugin.json`` (or a single child package)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def stage(self) -> StagedPackage:
        if not self.path.is_dir():
            raise PluginSourceError(f"plugin directory not found: {self.path}")
        manifest_file = find_manifest(self.path)
        if manifest_file is None:
            raise PluginSourceError(
                f"no plugin.json/manifest.json found in {self.path}"
            )
        manifest = load_manifest(manifest_file)
        return StagedPackage(
            manifest=manifest,
            root=manifest_file.parent,
            origin=str(self.path.resolve()),
        )

    def __repr__(self) -> str:
        return f"<DirectorySource {self.path}>"


class ArchiveSource(PluginSource):
    """A local ``.zip`` archive containing one plugin package."""

    def __init__(self, path: str | Path, *, checksum: str = "") -> None:
        self.path = Path(path)
        self.checksum = checksum

    def stage(self) -> StagedPackage:
        if not self.path.is_file():
            raise PluginSourceError(f"plugin archive not found: {self.path}")
        expected = _normalize_checksum(self.checksum)
        actual = _sha256_file(self.path)
        if expected and expected != actual:
            raise PluginSourceError(
                f"plugin archive checksum mismatch for {self.path}: "
                f"expected {expected}, got {actual}"
            )
        temporary = tempfile.TemporaryDirectory(prefix="chatanything-plugin-")
        extraction_root = Path(temporary.name) / "package"
        extraction_root.mkdir(parents=True, exist_ok=True)
        try:
            _extract_zip(self.path, extraction_root)
            manifest_file = find_manifest(extraction_root)
            if manifest_file is None:
                raise PluginValidationError(
                    f"archive {self.path} does not contain plugin.json/manifest.json"
                )
            manifest = load_manifest(manifest_file)
        except Exception:
            temporary.cleanup()
            raise
        return StagedPackage(
            manifest=manifest,
            root=manifest_file.parent,
            origin=str(self.path.resolve()),
            checksum=actual,
            temporary=temporary,
        )

    def __repr__(self) -> str:
        return f"<ArchiveSource {self.path}>"


class RemoteSource(PluginSource):
    """Download a plugin ``.zip`` archive from ``http(s)://`` or ``file://``."""

    def __init__(
        self,
        url: str,
        *,
        checksum: str = "",
        timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_PACKAGE_BYTES,
    ) -> None:
        self.url = url
        self.checksum = checksum
        self.timeout = timeout
        self.max_bytes = max_bytes

    def stage(self) -> StagedPackage:
        scheme = self.url.split(":", 1)[0].lower()
        if scheme not in ("http", "https", "file"):
            raise PluginSourceError(
                f"unsupported plugin source scheme: {scheme!r} (expected http/https/file)"
            )
        temporary = tempfile.TemporaryDirectory(prefix="chatanything-remote-")
        archive_path = Path(temporary.name) / "package.zip"
        extraction_root = Path(temporary.name) / "package"
        extraction_root.mkdir(parents=True, exist_ok=True)
        expected = _normalize_checksum(self.checksum)
        try:
            actual = _download(self.url, archive_path, self.timeout, self.max_bytes)
            if expected and expected != actual:
                raise PluginSourceError(
                    f"remote plugin checksum mismatch for {self.url}: "
                    f"expected {expected}, got {actual}"
                )
            _extract_zip(archive_path, extraction_root)
            manifest_file = find_manifest(extraction_root)
            if manifest_file is None:
                raise PluginValidationError(
                    f"remote archive {self.url} does not contain plugin.json/manifest.json"
                )
            manifest = load_manifest(manifest_file)
        except Exception:
            temporary.cleanup()
            raise
        return StagedPackage(
            manifest=manifest,
            root=manifest_file.parent,
            origin=self.url,
            checksum=actual,
            temporary=temporary,
        )

    def __repr__(self) -> str:
        return f"<RemoteSource {self.url}>"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def is_url(value: str) -> bool:
    scheme = value.split(":", 1)[0].lower()
    return "://" in value and scheme in ("http", "https", "file")


def is_archive(value: str | Path) -> bool:
    return str(value).lower().endswith(".zip")


def source_from(value: Any) -> PluginSource | None:
    """Build a package source, or return ``None`` for a module reference."""

    if isinstance(value, PluginSource):
        return value
    if isinstance(value, Path):
        if value.is_dir():
            return DirectorySource(value)
        if is_archive(value):
            return ArchiveSource(value)
        return None
    if isinstance(value, str):
        text = value.strip()
        if is_url(text):
            return RemoteSource(text)
        path = Path(text)
        if path.is_dir():
            return DirectorySource(path)
        if path.is_file() and is_archive(path):
            return ArchiveSource(path)
    return None


def _normalize_checksum(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith("sha256:"):
        raw = raw[len("sha256:") :]
    return raw


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, target: Path, timeout: float, max_bytes: int) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise PluginSourceError(
                    f"remote plugin exceeds size limit ({content_length} > {max_bytes} bytes)"
                )
            with target.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise PluginSourceError(
                            f"remote plugin exceeds size limit of {max_bytes} bytes"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
    except PluginSourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - network errors vary by platform
        raise PluginSourceError(f"failed to download plugin from {url}: {exc}") from exc
    return digest.hexdigest()


def _extract_zip(archive: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                target = _safe_zip_target(destination, info.filename)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise PluginSourceError(
                        f"archive {archive} contains a symbolic link: {info.filename}"
                    )
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
    except zipfile.BadZipFile as exc:
        raise PluginSourceError(f"invalid plugin archive {archive}: {exc}") from exc


def _safe_zip_target(destination: Path, name: str) -> Path:
    normalized = name.replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise PluginSourceError(f"unsafe path in plugin archive: {name!r}")
    candidate = destination.joinpath(*parts)
    resolved_destination = destination.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_destination != resolved_candidate and resolved_destination not in resolved_candidate.parents:
        raise PluginSourceError(f"plugin archive entry escapes extraction root: {name!r}")
    return candidate
