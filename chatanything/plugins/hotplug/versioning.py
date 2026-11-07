"""Dependency-free semantic version parsing and constraint matching.

Only the subset needed by plugin manifests is supported: numeric dotted
versions plus ``==``/``!=``/``>=``/``<=``/``>``/``<``/``~=`` specifiers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

from ...errors import PluginValidationError

_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){0,3}$")
_SPEC_RE = re.compile(r"^(==|!=|>=|<=|>|<|~=)?\s*(.*)$")
_REQUIREMENT_RE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*(.*)$")


@total_ordering
class Version:
    """A numeric dotted version with zero-padding comparison semantics."""

    __slots__ = ("raw", "parts")

    def __init__(self, raw: str) -> None:
        text = str(raw).strip()
        if not _VERSION_RE.match(text):
            raise PluginValidationError(f"invalid version: {raw!r}")
        self.raw = text
        self.parts = tuple(int(part) for part in text.split("."))

    @property
    def normalized(self) -> tuple[int, ...]:
        parts = list(self.parts)
        while len(parts) > 1 and parts[-1] == 0:
            parts.pop()
        return tuple(parts)

    def _compare(self, other: "Version") -> int:
        left, right = self.normalized, other.normalized
        width = max(len(left), len(right))
        padded_left = left + (0,) * (width - len(left))
        padded_right = right + (0,) * (width - len(right))
        if padded_left == padded_right:
            return 0
        return -1 if padded_left < padded_right else 1

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self._compare(other) == 0

    def __lt__(self, other: "Version") -> bool:
        return self._compare(other) < 0

    def __hash__(self) -> int:
        return hash(self.normalized)

    def __repr__(self) -> str:
        return f"Version({self.raw!r})"

    def __str__(self) -> str:
        return self.raw


@dataclass(frozen=True, slots=True)
class Specifier:
    """One ``operator version`` constraint."""

    operator: str
    version: Version

    def matches(self, candidate: Version) -> bool:
        if self.operator == "==":
            return candidate == self.version
        if self.operator == "!=":
            return candidate != self.version
        if self.operator == ">=":
            return candidate >= self.version
        if self.operator == "<=":
            return candidate <= self.version
        if self.operator == ">":
            return candidate > self.version
        if self.operator == "<":
            return candidate < self.version
        if self.operator == "~=":
            upper = self._compatible_upper()
            return candidate >= self.version and candidate < upper
        return False  # pragma: no cover - guarded by the parser

    def _compatible_upper(self) -> Version:
        parts = list(self.version.parts)
        if len(parts) == 1:
            return Version(f"{parts[0] + 1}.0")
        prefix = parts[:-1]
        bumped = prefix[:-1] + [prefix[-1] + 1]
        return Version(".".join(str(part) for part in bumped))

    def __str__(self) -> str:
        return f"{self.operator}{self.version}"


@dataclass(frozen=True, slots=True)
class Requirement:
    """A plugin name plus zero or more version constraints."""

    name: str
    specifiers: tuple[Specifier, ...] = ()

    def matches(self, version: Version | str) -> bool:
        candidate = version if isinstance(version, Version) else Version(version)
        return all(specifier.matches(candidate) for specifier in self.specifiers)

    def has_constraints(self) -> bool:
        return bool(self.specifiers)

    def __str__(self) -> str:
        if not self.specifiers:
            return self.name
        return f"{self.name}{','.join(str(item) for item in self.specifiers)}"


def parse_version(raw: str) -> Version:
    return Version(raw)


def parse_specifiers(text: str) -> tuple[Specifier, ...]:
    """Parse ``">=1.0,<2.0"`` (or a bare ``"1.0"``) into specifiers."""

    raw = str(text).strip()
    if not raw:
        return ()
    if raw[0] not in "=!<>~":
        # A bare version is treated as an exact match (common in manifests).
        return (Specifier("==", Version(raw)),)
    specifiers: list[Specifier] = []
    for chunk in raw.split(","):
        piece = chunk.strip()
        if not piece:
            continue
        match = _SPEC_RE.match(piece)
        if match is None:
            raise PluginValidationError(f"invalid version constraint: {piece!r}")
        operator = match.group(1) or "=="
        version_text = match.group(2).strip()
        if not version_text:
            raise PluginValidationError(f"missing version in constraint: {piece!r}")
        specifiers.append(Specifier(operator, Version(version_text)))
    return tuple(specifiers)


def parse_requirement(text: str) -> Requirement:
    """Parse ``"storage.sqlite>=1.0,<2.0"`` into a :class:`Requirement`."""

    raw = str(text).strip()
    match = _REQUIREMENT_RE.match(raw)
    if match is None or not match.group(1):
        raise PluginValidationError(f"invalid plugin requirement: {text!r}")
    name = match.group(1)
    specifiers = parse_specifiers(match.group(2))
    return Requirement(name=name, specifiers=specifiers)


def parse_requirements(values: object) -> tuple[Requirement, ...]:
    """Accept a list of strings or a ``{name: spec}`` mapping."""

    if values is None:
        return ()
    if isinstance(values, str):
        return (parse_requirement(values),)
    if isinstance(values, dict):
        requirements = []
        for name, spec in values.items():
            if spec is None or spec == "":
                requirements.append(Requirement(str(name)))
            elif isinstance(spec, str):
                requirements.append(
                    Requirement(str(name), parse_specifiers(spec))
                )
            else:
                raise PluginValidationError(
                    f"invalid version spec for dependency {name!r}: {spec!r}"
                )
        return tuple(requirements)
    if isinstance(values, (list, tuple)):
        return tuple(parse_requirement(str(item)) for item in values)
    raise PluginValidationError(f"invalid plugin requirements: {values!r}")
