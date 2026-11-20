"""BlobStore 兼容门面：真实实现位于 ProdAgent 微内核 `app.kernel.blob_store`。"""

from .kernel.blob_store import LocalBlobStore

__all__ = ["LocalBlobStore"]
