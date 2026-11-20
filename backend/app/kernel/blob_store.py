"""BlobStore：ProdAgent 不可变大对象存储（机制层）。

职责边界：
- EventLog 只记录轻量元数据与 blob://<sha256> 指针，绝不存放大体积 JSON、DDL、SQL 或审计报告；
- 按内容 SHA-256 寻址，天然幂等、不可篡改、支持原子写入。
"""

import hashlib
from pathlib import Path


class LocalBlobStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        target = self.root / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(content)
            temporary.replace(target)
        return f"blob://{digest}"

    def get(self, uri: str) -> bytes:
        digest = uri.removeprefix("blob://")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("Invalid blob URI")
        return (self.root / digest[:2] / digest).read_bytes()

    def delete(self, uri: str) -> None:
        digest = uri.removeprefix("blob://")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("Invalid blob URI")
        (self.root / digest[:2] / digest).unlink(missing_ok=True)
