"""AI coding tool set: web search, file editing and command execution.

The three tools follow the regular ``Tool`` contract (a :class:`ToolSpec` plus
an async ``invoke``).  Network access is injectable through
:class:`SearchBackend`, file access is sandboxed under an optional root, and
shell execution is delegated to the registered ``Capability.ENVIRONMENT``
provider instead of importing a concrete implementation.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from ...models import ToolSpec
from ...registry import Capability
from ..base import Plugin, PluginContext, PluginMeta
from ..interfaces import ToolContext


@dataclass(slots=True)
class SearchResult:
    """One web search hit."""

    title: str
    url: str
    snippet: str = ""


class SearchBackend(Protocol):
    """A pluggable search implementation (real network, fake, cached...)."""

    def search(self, query: str, *, limit: int, timeout: float) -> list[SearchResult]:
        ...


def _decode_result_url(href: str) -> str:
    """Resolve DuckDuckGo redirect links to the original URL."""

    url = href.strip()
    if url.startswith("//"):
        url = "https:" + url
    if "uddg=" not in url:
        return url
    values = parse_qs(urlparse(url).query).get("uddg")
    if not values:
        return url
    target = values[0].strip()
    if target.startswith("//"):
        target = "https:" + target
    return target


class _DuckDuckGoLiteParser(HTMLParser):
    """Extract result links and snippets from a DuckDuckGo Lite page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.snippets: list[str] = []
        self.fallback_links: list[tuple[str, str]] = []
        self._link_href: str | None = None
        self._link_text: list[str] = []
        self._fallback_href: str | None = None
        self._fallback_text: list[str] = []
        self._snippet_tag: str | None = None
        self._snippet_depth = 0
        self._snippet_parts: list[str] | None = None

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> list[str]:
        for key, value in attrs:
            if key == "class" and value:
                return value.split()
        return []

    @staticmethod
    def _href(attrs: list[tuple[str, str | None]]) -> str:
        for key, value in attrs:
            if key == "href" and value:
                return value
        return ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if tag == "a":
            href = self._href(attrs)
            if "result-link" in classes:
                self._link_href = href
                self._link_text = []
            elif href.startswith(("http://", "https://", "//")):
                if self._fallback_href is None:
                    self._fallback_href = href
                    self._fallback_text = []
        if tag in ("td", "div") and "result-snippet" in classes:
            if self._snippet_parts is None:
                self._snippet_tag = tag
                self._snippet_depth = 1
                self._snippet_parts = []
        elif self._snippet_parts is not None and tag == self._snippet_tag:
            self._snippet_depth += 1

    def handle_data(self, data: str) -> None:
        if self._link_href is not None:
            self._link_text.append(data)
        if self._fallback_href is not None:
            self._fallback_text.append(data)
        if self._snippet_parts is not None:
            self._snippet_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            if self._link_href is not None:
                title = " ".join("".join(self._link_text).split())
                self.links.append((self._link_href, title))
                self._link_href = None
                self._link_text = []
            elif self._fallback_href is not None:
                title = " ".join("".join(self._fallback_text).split())
                self.fallback_links.append((self._fallback_href, title))
                self._fallback_href = None
                self._fallback_text = []
        if self._snippet_parts is not None and tag == self._snippet_tag:
            self._snippet_depth -= 1
            if self._snippet_depth <= 0:
                snippet = " ".join("".join(self._snippet_parts).split())
                self.snippets.append(snippet)
                self._snippet_tag = None
                self._snippet_parts = None


def _parse_search_results(html_text: str, limit: int) -> list[SearchResult]:
    """Turn a DuckDuckGo Lite HTML response into ordered search results."""

    parser = _DuckDuckGoLiteParser()
    parser.feed(html_text)
    parser.close()

    results: list[SearchResult] = []
    seen: set[str] = set()
    for index, (href, title) in enumerate(parser.links):
        if len(results) >= limit:
            break
        url = _decode_result_url(href)
        if not url or url in seen:
            continue
        seen.add(url)
        snippet = parser.snippets[index].strip() if index < len(parser.snippets) else ""
        results.append(SearchResult(title=title or url, url=url, snippet=snippet))
    if results:
        return results

    for href, title in parser.fallback_links:
        if len(results) >= limit:
            break
        url = _decode_result_url(href)
        if not url or url in seen or not url.startswith(("http://", "https://")):
            continue
        if "duckduckgo.com" in urlparse(url).netloc.lower():
            continue
        seen.add(url)
        results.append(SearchResult(title=title or url, url=url))
    return results


class DuckDuckGoSearchBackend:
    """Search the public web through the DuckDuckGo Lite endpoint."""

    def __init__(
        self,
        endpoint: str = "https://lite.duckduckgo.com/lite/",
        user_agent: str = "ChatAnything/0.1",
    ) -> None:
        self.endpoint = endpoint
        self.user_agent = user_agent

    def search(self, query: str, *, limit: int, timeout: float) -> list[SearchResult]:
        if limit <= 0:
            return []
        separator = "&" if "?" in self.endpoint else "?"
        url = f"{self.endpoint}{separator}{urlencode({'q': query})}"
        request = Request(
            url,
            headers={"User-Agent": self.user_agent, "Accept": "text/html"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                html_text = response.read().decode("utf-8", errors="replace")
        except OSError as exc:
            raise RuntimeError(f"web_search failed: {exc}") from exc
        return _parse_search_results(html_text, limit)


class WebSearchTool:
    """Search the public web and format the hits for the model."""

    spec = ToolSpec(
        name="web_search",
        description="Search the public web and return titles, URLs and snippets.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default 5)",
                },
                "timeout": {
                    "type": "number",
                    "description": "Network timeout in seconds (default 10)",
                },
            },
            "required": ["query"],
        },
    )

    def __init__(
        self,
        backend: SearchBackend,
        *,
        default_max_results: int = 5,
        default_timeout: float = 10.0,
    ) -> None:
        self.backend = backend
        self.default_max_results = default_max_results
        self.default_timeout = default_timeout

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")
        try:
            requested = int(arguments.get("max_results", self.default_max_results))
        except (TypeError, ValueError):
            requested = self.default_max_results
        limit = max(1, min(10, requested))
        try:
            timeout = float(arguments.get("timeout", self.default_timeout))
        except (TypeError, ValueError):
            timeout = self.default_timeout

        results = await asyncio.to_thread(
            self.backend.search, query, limit=limit, timeout=timeout
        )
        if not results:
            return f"No search results for '{query}'"

        lines: list[str] = []
        for index, result in enumerate(results, start=1):
            lines.append(f"{index}. {result.title}")
            lines.append(f"   {result.url}")
            if result.snippet:
                lines.append(f"   {result.snippet}")
        return "\n".join(lines)


class FileEditTool:
    """Read, write, append, replace, list or stat files under a sandbox root."""

    spec = ToolSpec(
        name="file_edit",
        description="Read, write, append, replace, list or stat files.",
        parameters={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["read", "write", "append", "replace", "list", "stat"],
                },
                "path": {"type": "string"},
                "content": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "encoding": {
                    "type": "string",
                    "description": "Default utf-8",
                },
            },
            "required": ["operation", "path"],
        },
    )

    def __init__(
        self,
        root: str | None = None,
        *,
        allow_outside: bool = False,
        max_read_bytes: int = 262144,
    ) -> None:
        self.root = Path(root).resolve() if root else Path.cwd().resolve()
        self.allow_outside = allow_outside
        self.max_read_bytes = max_read_bytes

    def _resolve(self, raw: str) -> Path:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        if self.allow_outside:
            return resolved
        try:
            resolved.relative_to(self.root)
        except ValueError:
            root_key = os.path.normcase(str(self.root))
            resolved_key = os.path.normcase(str(resolved))
            if resolved_key == root_key or resolved_key.startswith(root_key + os.sep):
                return resolved
            raise ValueError(f"path outside allowed root: {resolved}") from None
        return resolved

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> str:
        operation = str(arguments.get("operation", "")).strip().lower()
        raw_path = str(arguments.get("path", "")).strip()
        if not raw_path:
            raise ValueError("path is required")
        path = self._resolve(raw_path)
        encoding = str(arguments.get("encoding") or "utf-8")

        if operation == "read":
            if not path.exists() or not path.is_file():
                raise ValueError(f"file not found: {path}")
            if path.stat().st_size > self.max_read_bytes:
                raise ValueError(
                    f"file exceeds max_read_bytes ({self.max_read_bytes}): {path}"
                )
            return path.read_text(encoding=encoding)

        if operation == "write":
            content = arguments.get("content")
            if content is None:
                raise ValueError("content is required")
            text = str(content)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding=encoding)
            return f"Wrote {len(text)} chars to {path}"

        if operation == "append":
            content = arguments.get("content")
            if content is None:
                raise ValueError("content is required")
            text = str(content)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding=encoding) as handle:
                handle.write(text)
            return f"Appended {len(text)} chars to {path}"

        if operation == "replace":
            old_text = arguments.get("old_text")
            new_text = arguments.get("new_text")
            if old_text is None or new_text is None:
                raise ValueError("old_text and new_text are required")
            if not path.exists() or not path.is_file():
                raise ValueError(f"file not found: {path}")
            old = str(old_text)
            new = str(new_text)
            text = path.read_text(encoding=encoding)
            count = text.count(old)
            if count == 0:
                raise ValueError("old_text not found")
            if count > 1:
                raise ValueError(
                    f"old_text matched {count} times; provide a unique fragment"
                )
            path.write_text(text.replace(old, new, 1), encoding=encoding)
            return f"Replaced 1 occurrence in {path}"

        if operation == "list":
            if not path.exists() or not path.is_dir():
                raise ValueError(f"directory not found: {path}")
            lines: list[str] = []
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                if child.is_dir():
                    lines.append(f"[D] {child.name}")
                else:
                    lines.append(f"[F] {child.name} ({child.stat().st_size} bytes)")
            return "\n".join(lines) if lines else "(empty directory)"

        if operation == "stat":
            if not path.exists():
                raise ValueError(f"path not found: {path}")
            info = path.stat()
            kind = "directory" if path.is_dir() else "file"
            size = info.st_size if path.is_file() else 0
            mtime = datetime.fromtimestamp(info.st_mtime).isoformat()
            return "\n".join(
                [
                    f"path: {path}",
                    f"type: {kind}",
                    f"size: {size} bytes",
                    f"mtime: {mtime}",
                ]
            )

        raise ValueError(f"unknown operation: {operation}")


class RunCommandTool:
    """Execute a shell command through the registered environment provider."""

    spec = ToolSpec(
        name="run_command",
        description=(
            "Execute a system command in a working directory and return its output."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {
                    "type": "string",
                    "description": "Working directory; defaults to environment cwd",
                },
                "timeout": {
                    "type": "number",
                    "description": "Timeout seconds (default 30)",
                },
            },
            "required": ["command"],
        },
    )

    def __init__(self, environment: Any, *, default_timeout: float = 30.0) -> None:
        self.environment = environment
        self.default_timeout = default_timeout

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> str:
        command = str(arguments.get("command", "")).strip()
        if not command:
            raise ValueError("command is required")
        raw_cwd = arguments.get("cwd")
        cwd = str(raw_cwd).strip() if raw_cwd not in (None, "") else None
        try:
            timeout = float(arguments.get("timeout", self.default_timeout))
        except (TypeError, ValueError):
            timeout = self.default_timeout

        result = await self.environment.execute(command, cwd=cwd, timeout=timeout)
        lines: list[str] = [
            f"exit_code: {result.exit_code}",
            f"duration: {result.duration:.3f}s",
        ]
        if result.stdout:
            lines.append("stdout:")
            lines.append(result.stdout.rstrip("\n"))
        if result.stderr:
            lines.append("stderr:")
            lines.append(result.stderr.rstrip("\n"))
        return "\n".join(lines)


class AICodingToolsPlugin(Plugin):
    meta = PluginMeta(
        name="tools.aicoding",
        version="0.1.0",
        description=(
            "Web search, file editing and command execution tools for AI coding."
        ),
        provides=("tool",),
        priority=10,
    )

    async def install(self, context: PluginContext) -> None:
        root = str(context.setting("root", str(Path.cwd())))
        allow_outside = bool(context.setting("allow_outside", False))
        endpoint = str(
            context.setting("search_endpoint", "https://lite.duckduckgo.com/lite/")
        )
        search_timeout = float(context.setting("search_timeout", 10.0))
        max_results = int(context.setting("max_search_results", 5))
        environment = context.container.resolve(Capability.ENVIRONMENT)
        backend = DuckDuckGoSearchBackend(endpoint=endpoint)
        tools = [
            WebSearchTool(
                backend,
                default_max_results=max_results,
                default_timeout=search_timeout,
            ),
            FileEditTool(root, allow_outside=allow_outside),
            RunCommandTool(environment),
        ]
        for tool in tools:
            context.container.register(
                Capability.TOOL,
                tool.spec.name,
                tool,
                plugin=self.meta.name,
                priority=self.meta.priority,
                default=False,
            )
