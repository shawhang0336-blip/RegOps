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
