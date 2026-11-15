"""给存量码表补 source_ordinal，让左侧列表按原文出现顺序排列。

排序依据优先级：
1. 码值来源块（code_values.source_locator.block_id）的 ordinal；
2. 引用该码表的字段所属报表块的 ordinal（内联枚举没有自身块位置）；
3. 都定位不到就留空，列表里排最后。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.asset_models import (  # noqa: E402
    CodeSetModel,
    CodeValueModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    RawBlockModel,
    TableMetadataModel,
)
from app.db import SessionLocal  # noqa: E402


def main(apply: bool = True) -> None:
    with SessionLocal() as session:
        block_ordinal = {row.id: row.ordinal for row in session.query(RawBlockModel).all()}
        table_block = {row.id: row.block_id for row in session.query(TableMetadataModel).all()}
        field_table = {row.id: row.table_id for row in session.query(FieldMetadataModel).all()}

        from_value = {}
        for value in session.query(CodeValueModel).all():
            block_id = (value.source_locator or {}).get("block_id")
            if block_id in block_ordinal:
                ordinal = block_ordinal[block_id] * 1000
                from_value[value.code_set_id] = min(from_value.get(value.code_set_id, ordinal), ordinal)

        from_field = {}
        for ref in session.query(FieldCodeReferenceModel).all():
            table_id = field_table.get(ref.field_id)
            block_id = table_block.get(table_id)
            if block_id in block_ordinal:
                ordinal = block_ordinal[block_id] * 1000 + 1
                from_field[ref.code_set_id] = min(from_field.get(ref.code_set_id, ordinal), ordinal)

        filled = unresolved = 0
        for row in session.query(CodeSetModel).all():
            if row.source_ordinal is not None:
                continue
            ordinal = from_value.get(row.id) or from_field.get(row.id)
            if ordinal is None:
                unresolved += 1
                continue
            row.source_ordinal = ordinal
            filled += 1
        print(f"回填 {filled} 张，仍无来源位置 {unresolved} 张")
        if apply:
            session.commit()
            print("已提交")
        else:
            session.rollback()
            print("（未提交）")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
