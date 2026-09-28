"""File implementations only; registry/executor own the invocation protocol."""
from pathlib import Path
import os
from .contracts import ToolFailure, ToolResult, ToolSpec
from .registry import ToolRegistry


class FileTools:
    MAX_BYTES = 32_000

    def __init__(self, root: Path, *, workspace_lock=False):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace_lock = workspace_lock

    def registry(self) -> ToolRegistry:
        path = ("path", "Workspace-relative path")
        return ToolRegistry([
            ToolSpec("list_files", "List at most 100 directory entries.", (path,), self.list_files, self.authorize,
                     parallel_safe=True),
            ToolSpec("read_file", "Read a UTF-8 file, at most 32000 bytes.", (path,), self.read_file, self.authorize,
                     parallel_safe=True),
            ToolSpec("write_file", "Create a new UTF-8 file; never overwrite existing files.",
                     (path, ("content", "UTF-8 text")), self.write_file, self.authorize,
                     read_only=False, idempotent=False),
        ])

    def _path(self, value):
        relative = Path(value)
        if relative.is_absolute() or relative.drive or "\x00" in value or ":" in value:
            raise ToolFailure("permission_denied")
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ToolFailure("permission_denied")
        return path

    def authorize(self, args, context):
        path = self._path(args["path"])
        return os.path.normcase(str(self.root if self.workspace_lock else path))

    def list_files(self, args, context):
        entries = []
        for child in self._path(args["path"]).iterdir():
            if len(entries) == 100:
                return ToolResult(True, content={"entries": sorted(entries), "truncated": True})
            entries.append(child.name[:256])
        return ToolResult(True, content={"entries": sorted(entries), "truncated": False})

    def read_file(self, args, context):
        with self._path(args["path"]).open("rb") as file:
            data = file.read(self.MAX_BYTES + 1)
        if len(data) > self.MAX_BYTES:
            raise ToolFailure("result_too_large")
        return ToolResult(True, content={"text": data.decode("utf-8")})

    def write_file(self, args, context):
        path = self._path(args["path"])
        content = args["content"].encode("utf-8")
        if len(content) > self.MAX_BYTES:
            raise ToolFailure("arguments_too_large")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as file:
                file.write(content)
        except FileExistsError:
            raise ToolFailure("already_exists") from None
        # Other failures may leave a partial file: executor classifies them as unknown.
        return ToolResult(True, content={"path": args["path"], "bytes": len(content)},
                          artifacts=(args["path"],), effect="confirmed")
