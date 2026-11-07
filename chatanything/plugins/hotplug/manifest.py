"""Plugin manifest parsing, validation and host compatibility checks."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...errors import PluginValidationError, PluginVersionConflictError
from ..base import PLUGIN_API_VERSION, PluginMeta
from .versioning import (
    Version,
    parse_requirements,
    parse_specifiers,
    parse_version,
)

MANIFEST_FILES = ("plugin.json", "manifest.json")

_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_ENTRY_RE = re.compile(r"^[A-Za-z0-9_.\-/\\]+(?::[A-Za-z0-9_.\-]+)?$")

HOST_API_VERSION = PLUGIN_API_VERSION


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """Declarative description of one plugin package."""

    name: str
    version: str
    entry: str = ""
    description: str = ""
    author: str = ""
    requires: tuple = ()
    provides: tuple[str, ...] = ()
    priority: int = 10
    api: str = ">=1.0,<2.0"
    python: str = ">=3.10"
    sandbox: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    origin: str = ""

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, origin: str = "") -> "PluginManifest":
        if not isinstance(data, Mapping):
            raise PluginValidationError("plugin manifest must be a JSON object")

        name = _require_str(data, "name", origin)
        if not _NAME_RE.match(name):
            raise PluginValidationError(f"invalid plugin name: {name!r}")

        version = _require_str(data, "version", origin)
        parse_version(version)

        entry = str(data.get("entry", "")).strip()
        if entry and not _ENTRY_RE.match(entry):
            raise PluginValidationError(f"invalid plugin entry: {entry!r}")
        if entry and not _entry_is_safe(entry):
            raise PluginValidationError(f"plugin entry escapes the package root: {entry!r}")

        provides = _str_tuple(data.get("provides", ()), "provides")
        requires = parse_requirements(data.get("requires"))
        priority = data.get("priority", 10)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise PluginValidationError("plugin manifest field 'priority' must be an integer")

        api = str(data.get("api", ">=1.0,<2.0")).strip()
        python = str(data.get("python", ">=3.10")).strip()
        # Parse eagerly so malformed constraints fail during validation.
        parse_specifiers(api)
        parse_specifiers(python)

        sandbox = data.get("sandbox", {}) or {}
        if not isinstance(sandbox, Mapping):
            raise PluginValidationError("plugin manifest field 'sandbox' must be an object")
        sandbox = _validate_sandbox(sandbox)

        metadata = data.get("metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            raise PluginValidationError("plugin manifest field 'metadata' must be an object")

        return cls(
            name=name,
            version=version,
            entry=entry,
            description=str(data.get("description", "")),
            author=str(data.get("author", "")),
            requires=requires,
            provides=provides,
            priority=priority,
            api=api,
            python=python,
            sandbox=dict(sandbox),
            metadata=dict(metadata),
            origin=origin,
        )

    @classmethod
    def from_meta(cls, meta: PluginMeta, *, origin: str = "") -> "PluginManifest":
        sandbox: Mapping[str, Any] = {}
        raw_sandbox = meta.metadata.get("sandbox") if meta.metadata else None
        if isinstance(raw_sandbox, Mapping):
            sandbox = _validate_sandbox(raw_sandbox)
        return cls(
            name=meta.name,
            version=meta.version,
            description=meta.description,
            author=meta.author,
            requires=parse_requirements(list(meta.requires)),
            provides=tuple(meta.provides),
            priority=meta.priority,
            api=meta.api_version,
            python=meta.python_requires,
            sandbox=dict(sandbox),
            metadata=dict(meta.metadata),
            origin=origin or "in-memory",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @property
    def is_package(self) -> bool:
        return bool(self.entry)

    @property
    def requires_names(self) -> tuple[str, ...]:
        return tuple(requirement.name for requirement in self.requires)

    def to_meta(self) -> PluginMeta:
        return PluginMeta(
            name=self.name,
            version=self.version,
            description=self.description,
            author=self.author,
            requires=tuple(str(item) for item in self.requires),
            provides=self.provides,
            priority=self.priority,
            metadata=dict(self.metadata),
            api_version=self.api,
            python_requires=self.python,
        )

    def validate_host(self) -> None:
        """Check plugin ABI and Python compatibility with the running host."""

        _ensure_compatible(
            self.api,
            HOST_API_VERSION,
            what=f"plugin {self.name!r} requires plugin API",
        )
        current_python = Version(
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )
        _ensure_compatible(
            self.python,
            str(current_python),
            what=f"plugin {self.name!r} requires Python",
        )


def load_manifest(path: str | Path) -> PluginManifest:
    """Load and validate a ``plugin.json``/``manifest.json`` file."""

    import json

    manifest_path = Path(path)
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PluginValidationError(f"plugin manifest not found: {manifest_path}") from exc
    except (OSError, ValueError) as exc:
        raise PluginValidationError(
            f"cannot read plugin manifest {manifest_path}: {exc}"
        ) from exc
    manifest = PluginManifest.from_mapping(data, origin=str(manifest_path.parent))
    manifest.validate_host()
    return manifest


def find_manifest(path: str | Path) -> Path | None:
    """Return the manifest file for a package root (or its only child package)."""

    root = Path(path)
    for filename in MANIFEST_FILES:
        candidate = root / filename
        if candidate.is_file():
            return candidate
    if not root.is_dir():
        return None
    children = sorted(
        (child for child in root.iterdir() if child.is_dir()),
        key=lambda item: item.name,
    )
    matches: list[Path] = []
    for child in children:
        for filename in MANIFEST_FILES:
            candidate = child / filename
            if candidate.is_file():
                matches.append(candidate)
                break
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise PluginValidationError(
            f"ambiguous plugin package in {root}: {len(matches)} child manifests found"
        )
    return None


def _entry_is_safe(entry: str) -> bool:
    module_part = entry.partition(":")[0].replace("\\", "/").strip("/")
    if not module_part:
        return False
    if module_part.startswith("/") or Path(module_part).is_absolute():
        return False
    parts = Path(module_part.replace(".py", "")).parts
    return all(part not in ("", ".", "..") for part in parts)


def _require_str(data: Mapping[str, Any], key: str, origin: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        location = f" in {origin}" if origin else ""
        raise PluginValidationError(f"plugin manifest field {key!r} is required{location}")
    return value.strip()


def _str_tuple(value: Any, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value)
    raise PluginValidationError(f"plugin manifest field {key!r} must be a list of strings")


def _validate_sandbox(sandbox: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if "allow_network" in sandbox:
        if not isinstance(sandbox["allow_network"], bool):
            raise PluginValidationError("sandbox.allow_network must be a boolean")
        result["allow_network"] = sandbox["allow_network"]
    if "allow_subprocess" in sandbox:
        if not isinstance(sandbox["allow_subprocess"], bool):
            raise PluginValidationError("sandbox.allow_subprocess must be a boolean")
        result["allow_subprocess"] = sandbox["allow_subprocess"]
    if "deny_imports" in sandbox:
        raw = sandbox["deny_imports"]
        if isinstance(raw, str):
            result["deny_imports"] = (raw,)
        elif isinstance(raw, Sequence):
            result["deny_imports"] = tuple(str(item) for item in raw)
        else:
            raise PluginValidationError("sandbox.deny_imports must be a list of module names")
    if "max_hook_seconds" in sandbox:
        value = sandbox["max_hook_seconds"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PluginValidationError("sandbox.max_hook_seconds must be a number")
        if float(value) <= 0:
            raise PluginValidationError("sandbox.max_hook_seconds must be positive")
        result["max_hook_seconds"] = float(value)
    return result


def _ensure_compatible(constraint: str, actual: str, *, what: str) -> None:
    specifiers = parse_specifiers(constraint)
    candidate = Version(actual)
    if not all(specifier.matches(candidate) for specifier in specifiers):
        raise PluginVersionConflictError(
            f"{what} {constraint}, but host provides {actual}"
        )
