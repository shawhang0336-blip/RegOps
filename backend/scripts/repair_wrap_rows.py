"""按新的折行重排逻辑，重解析指定报表的原文块并重建字段。

用法：
    python var/repair_wrap_rows.py --dry-run
    python var/repair_wrap_rows.py
"""

import argparse
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm.attributes import flag_modified

from app.asset_models import FieldMetadataModel, RawBlockModel, SourceDocumentModel, TableMetadataModel
from app.catalog import parse_document_catalog
from app.config import get_settings
from app.db import SessionLocal
from app.metadata_service import extract_metadata_for_block
from app.spec_quality import inspect_block

TARGET_TABLES = ("SAFE_6_7_5_3", "SAFE_6_9_2")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--spec-only", action="store_true", help="只修原文块里的 spec，不重建字段（留给接口层重跑）")
    parser.add_argument("--document", default="safe13")
    args = parser.parse_args()

    settings = get_settings()
    entries = {entry["table_code"]: entry for entry in parse_document_catalog(settings.safe_13_spec_path)}

    with SessionLocal() as session:
        document = (
            session.query(SourceDocumentModel)
            .filter(SourceDocumentModel.file_name.like(f"%{args.document}%"))
            .first()
        )
        if not document:
            raise SystemExit("document not found")
        for table_code in TARGET_TABLES:
            entry = entries.get(table_code)
            if not entry or not entry.get("spec"):
                print(f"[skip] {table_code}: 新解析没有产出 spec")
                continue
            table = (
                session.query(TableMetadataModel)
                .filter(TableMetadataModel.document_id == document.id, TableMetadataModel.table_code == table_code)
                .first()
            )
            if not table:
                print(f"[skip] {table_code}: 库中没有这张表")
                continue
            block = session.get(RawBlockModel, table.block_id)
            spec = entry["spec"]
            fields = {field["name"].upper(): field for field in spec["fields"]}
            print(f"\n=== {table_code} 原文块 {block.ordinal}，新解析 {len(spec['fields'])} 个字段")
            for row in (
                session.query(FieldMetadataModel)
                .filter(FieldMetadataModel.table_id == table.id)
                .order_by(FieldMetadataModel.sort_order)
                .all()
            ):
                new = fields.get(row.field_en.upper())
                if not new:
                    print(f"  [缺] {row.field_en} 新解析里没有")
                    continue
                if new["content"] != row.field_cn or new["check_text"] != row.check_text_raw:
                    print(f"  {row.field_en}:")
                    print(f"    名称 {row.field_cn!r} -> {new['content']!r}")
                    print(f"    校验 {row.check_text_raw[:60]!r} -> {new['check_text'][:60]!r}")
            if args.dry_run:
                continue

            content_json = copy.deepcopy(block.content_json)
            content_json["spec"] = spec
            content_json["field_count"] = len(spec["fields"])
            content_json["parse_warnings"] = entry.get("parse_warnings") or []
            content_json["human_required"] = bool(entry.get("human_required"))
            content_json["quality_issues"] = inspect_block({"content_json": content_json})
            block.content_json = content_json
            flag_modified(block, "content_json")
            session.commit()
            print(f"  原文块已更新 block={block.id}")

            if args.spec_only:
                continue
            table_id = extract_metadata_for_block(block.id, settings)
            print(f"  重建完成 table={table_id}")
    print("\n完成" if not args.dry_run else "\n（dry-run，未写库）")


if __name__ == "__main__":
    main()
