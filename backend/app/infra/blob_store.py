"""BlobStore：不可变大对象内容寻址存储（基础设施层 Layer 0）。

职责边界：
- 位于 Layer 0（infra），供策略层、执行引擎、交付层与 API 按需调用；
- EventLog 只记录轻量元数据与 `blob://<sha256>` 指针，绝不存放大体积 JSON、DDL、SQL 或审计报告；
- 按内容 SHA-256 寻址，采用每写唯一临时文件 + 原子 `os.replace`，并发写入同一内容零竞态、不可篡改。
"""

import hashlib
import os
import tempfile
from pathlib import Path


class LocalBlobStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve_path(self, uri: str) -> Path:
        if not isinstance(uri, str) or not uri.startswith("blob://"):
            raise ValueError("Invalid blob URI")
        digest = uri[len("blob://") :]
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("Invalid blob URI")
        return self.root / digest[:2] / digest

    def put(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        target = self.root / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{digest[:8]}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                os.replace(tmp_name, target)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        return f"blob://{digest}"

    def get(self, uri: str) -> bytes:
        return self._resolve_path(uri).read_bytes()

    def delete(self, uri: str) -> None:
        self._resolve_path(uri).unlink(missing_ok=True)
