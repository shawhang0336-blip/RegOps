"""跨方言 SQL 安全标识符转义工具（Oracle / MySQL）。"""


def quote_identifier(name: str, dialect: str = "mysql") -> str:
    if not name.replace("_", "").isalnum() or not name.isascii():
        raise ValueError(f"Unsafe SQL identifier: {name}")
    return f'"{name}"' if dialect == "oracle" else f"`{name}`"
