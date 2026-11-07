"""Functional tests for the AI coding tool set (offline, injected backends)."""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any

from chatanything.models import ExecResult
from chatanything.plugins.builtin.aicoding_tools import (
    FileEditTool,
    RunCommandTool,
    SearchResult,
    WebSearchTool,
)


class FakeSearchBackend:
    def __init__(
        self,
        results: list[SearchResult] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.results = results or []
        self.error = error
        self.calls: list[tuple[str, int, float]] = []

    def search(self, query: str, *, limit: int, timeout: float) -> list[SearchResult]:
        self.calls.append((query, limit, timeout))
        if self.error is not None:
            raise self.error
        return list(self.results)[:limit]


class FakeEnvironment:
    def __init__(self, result: ExecResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str | None, float]] = []

    async def execute(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float = 30.0,
    ) -> ExecResult:
        self.calls.append((command, cwd, timeout))
        return ExecResult(
            command=command,
            exit_code=self.result.exit_code,
            stdout=self.result.stdout,
            stderr=self.result.stderr,
            duration=self.result.duration,
        )


class FileEditToolTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "root")
        os.makedirs(self.root, exist_ok=True)
        self.tool = FileEditTool(root=self.root)

    async def test_write_read_append_list_and_stat(self):
        result = await self.tool.invoke(
            {"operation": "write", "path": "nested/deep/note.txt", "content": "hello world"},
            None,
        )
        self.assertIn("Wrote", result)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "nested", "deep")))

        content = await self.tool.invoke(
            {"operation": "read", "path": "nested/deep/note.txt"}, None
        )
        self.assertEqual(content, "hello world")

        appending = await self.tool.invoke(
            {"operation": "append", "path": "nested/deep/note.txt", "content": "!!"}, None
        )
        self.assertIn("Appended", appending)
        self.assertEqual(
            await self.tool.invoke({"operation": "read", "path": "nested/deep/note.txt"}, None),
            "hello world!!",
        )

        await self.tool.invoke(
            {"operation": "write", "path": "plain.txt", "content": "alpha beta"}, None
        )
        listing = await self.tool.invoke({"operation": "list", "path": "."}, None)
        self.assertIn("plain.txt", listing)
        self.assertIn("[D] nested", listing)

        info = await self.tool.invoke({"operation": "stat", "path": "plain.txt"}, None)
        self.assertIn("size:", info)
        self.assertIn("bytes", info)
        self.assertIn("plain.txt", info)

    async def test_replace_unique_occurrence(self):
        await self.tool.invoke(
            {"operation": "write", "path": "replace.txt", "content": "hello world"}, None
        )
        result = await self.tool.invoke(
            {
                "operation": "replace",
                "path": "replace.txt",
                "old_text": "hello",
                "new_text": "hi",
            },
            None,
        )
        self.assertIn("Replaced", result)
        self.assertEqual(
            await self.tool.invoke({"operation": "read", "path": "replace.txt"}, None),
            "hi world",
        )

    async def test_replace_rejects_missing_or_duplicate_text(self):
        await self.tool.invoke(
            {"operation": "write", "path": "dup.txt", "content": "aa aa"}, None
        )
        with self.assertRaises(ValueError):
            await self.tool.invoke(
                {
                    "operation": "replace",
                    "path": "dup.txt",
                    "old_text": "aa",
                    "new_text": "bb",
                },
                None,
            )
        # No partial write may happen on a failed replace.
        self.assertEqual(
            await self.tool.invoke({"operation": "read", "path": "dup.txt"}, None), "aa aa"
        )

        with self.assertRaises(ValueError):
            await self.tool.invoke(
                {
                    "operation": "replace",
                    "path": "dup.txt",
                    "old_text": "missing",
                    "new_text": "x",
                },
                None,
            )
        self.assertEqual(
            await self.tool.invoke({"operation": "read", "path": "dup.txt"}, None), "aa aa"
        )

    async def test_sandbox_rejects_outside_and_allow_outside_permits_it(self):
        with self.assertRaises(ValueError):
            await self.tool.invoke(
                {"operation": "write", "path": "../outside.txt", "content": "nope"}, None
            )
        self.assertFalse(os.path.exists(os.path.join(self._tmp.name, "outside.txt")))

        permissive = FileEditTool(root=self.root, allow_outside=True)
        await permissive.invoke(
            {"operation": "write", "path": "../outside.txt", "content": "allowed"}, None
        )
        outside_path = os.path.join(self._tmp.name, "outside.txt")
        self.assertTrue(os.path.isfile(outside_path))
        with open(outside_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "allowed")


class WebSearchToolTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_results_are_numbered_with_title_url_and_snippet(self):
        backend = FakeSearchBackend(
            results=[
                SearchResult(title="First result", url="https://one.example", snippet="one snippet"),
                SearchResult(title="Second result", url="https://two.example", snippet="two snippet"),
            ]
        )
        tool = WebSearchTool(backend)
        output = await tool.invoke({"query": "x"}, None)

        self.assertIn("1. First result", output)
        self.assertIn("https://one.example", output)
        self.assertIn("one snippet", output)
        self.assertIn("2. Second result", output)
        self.assertIn("https://two.example", output)
        self.assertEqual(backend.calls[0][0], "x")
        self.assertEqual(backend.calls[0][1], 5)

    async def test_empty_results_message(self):
        tool = WebSearchTool(FakeSearchBackend(results=[]))
        output = await tool.invoke({"query": "nothing"}, None)
        self.assertIn("No search results", output)

    async def test_backend_errors_propagate(self):
        tool = WebSearchTool(FakeSearchBackend(error=RuntimeError("boom")))
        with self.assertRaises(RuntimeError):
            await tool.invoke({"query": "x"}, None)

    async def test_missing_query_raises(self):
        tool = WebSearchTool(FakeSearchBackend())
        with self.assertRaises(ValueError):
            await tool.invoke({"query": "   "}, None)


class RunCommandToolTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_output_contains_exit_code_stdout_and_stderr(self):
        environment = FakeEnvironment(
            ExecResult(command="", exit_code=3, stdout="out", stderr="err", duration=0.1)
        )
        tool = RunCommandTool(environment)
        output = await tool.invoke({"command": "exit 3"}, None)

        self.assertIn("exit_code: 3", output)
        self.assertIn("stdout:", output)
        self.assertIn("out", output)
        self.assertIn("stderr:", output)
        self.assertIn("err", output)
        self.assertEqual(environment.calls[0][0], "exit 3")
        self.assertIsNone(environment.calls[0][1])

    async def test_empty_command_raises(self):
        tool = RunCommandTool(FakeEnvironment(ExecResult(command="", exit_code=0)))
        with self.assertRaises(ValueError):
            await tool.invoke({"command": "   "}, None)


class ToolSpecTestCase(unittest.TestCase):
    def _assert_object_spec(self, spec: Any, name: str, required: list[str]) -> None:
        self.assertEqual(spec.name, name)
        self.assertEqual(spec.parameters["type"], "object")
        self.assertEqual(spec.parameters["required"], required)

    def test_tool_spec_names_and_parameters(self):
        self._assert_object_spec(WebSearchTool.spec, "web_search", ["query"])
        self._assert_object_spec(FileEditTool.spec, "file_edit", ["operation", "path"])
        self._assert_object_spec(RunCommandTool.spec, "run_command", ["command"])


if __name__ == "__main__":
    unittest.main()
