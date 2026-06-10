# -*- coding: utf-8 -*-
import asyncio
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from database.db_session import get_session, get_async_engine
from database.models import XhsNote, XhsNoteComment

SAMPLE_LIMIT = 20
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "docs" / "xhs_api_schema.json"

PYTHON_TYPE_TO_JSON_SCHEMA = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "NoneType": "null",
}


def infer_schema(value):
    python_type = type(value).__name__

    if python_type == "dict":
        properties = {}
        for k, v in value.items():
            properties[k] = infer_schema(v)
        return {"type": "object", "properties": properties}

    if python_type == "list":
        if len(value) == 0:
            return {"type": "array", "items": {}}
        merged = {}
        for item in value:
            item_schema = infer_schema(item)
            merged = merge_schemas(merged, item_schema)
        return {"type": "array", "items": merged}

    json_type = PYTHON_TYPE_TO_JSON_SCHEMA.get(python_type)
    if json_type:
        return {"type": json_type}

    return {"type": "string"}


def merge_schemas(a, b):
    if not a:
        return b
    if not b:
        return a

    a_types = set(_ensure_type_list(a.get("type", "object")))
    b_types = set(_ensure_type_list(b.get("type", "object")))
    merged_types = a_types | b_types

    result = {}

    if merged_types == {"object"}:
        result["type"] = "object"
        a_props = a.get("properties", {})
        b_props = b.get("properties", {})
        merged_props = {}
        all_keys = set(a_props.keys()) | set(b_props.keys())
        for key in all_keys:
            p_a = a_props.get(key, {})
            p_b = b_props.get(key, {})
            if p_a and p_b:
                merged_props[key] = merge_schemas(p_a, p_b)
            else:
                merged_props[key] = p_a or p_b
        result["properties"] = merged_props
        return result

    if "array" in merged_types:
        result["type"] = "array"
        a_items = a.get("items", {})
        b_items = b.get("items", {})
        if a_items or b_items:
            result["items"] = merge_schemas(a_items, b_items)
        return result

    type_list = sorted(merged_types)
    result["type"] = type_list[0] if len(type_list) == 1 else type_list
    return result


def _ensure_type_list(t):
    if isinstance(t, list):
        return t
    return [t]


async def fetch_raw_data_samples(model_class, limit):
    samples = []
    table_name = model_class.__tablename__
    try:
        async with get_session() as session:
            stmt = select(model_class.raw_data).where(model_class.raw_data.isnot(None)).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            for raw in rows:
                try:
                    data = json.loads(raw)
                    samples.append(data)
                except (json.JSONDecodeError, TypeError):
                    continue
    except OperationalError as e:
        print(f"[generate_api_schema] 警告: {table_name} 表查询失败 ({e})，可能尚未迁移 raw_data 列，跳过")
    return samples


def build_merged_schema(samples):
    merged = {}
    for sample in samples:
        schema = infer_schema(sample)
        merged = merge_schemas(merged, schema)
    return merged


async def main():
    print("[generate_api_schema] 从 XhsNote 表采样 raw_data ...")
    note_samples = await fetch_raw_data_samples(XhsNote, SAMPLE_LIMIT)
    print(f"[generate_api_schema] XhsNote 有效样本数: {len(note_samples)}")

    print("[generate_api_schema] 从 XhsNoteComment 表采样 raw_data ...")
    comment_samples = await fetch_raw_data_samples(XhsNoteComment, SAMPLE_LIMIT)
    print(f"[generate_api_schema] XhsNoteComment 有效样本数: {len(comment_samples)}")

    if not note_samples and not comment_samples:
        print("[generate_api_schema] 错误: 两张表均无 raw_data 数据，无法推断 Schema")
        print("[generate_api_schema] 请确认数据库中已存在 raw_data 列且有数据")

    note_schema = build_merged_schema(note_samples) if note_samples else {}
    comment_schema = build_merged_schema(comment_samples) if comment_samples else {}

    output = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "XHS API Schema",
        "description": "从小红书 API 原始响应数据推断的 JSON Schema",
        "definitions": {
            "XhsNote": note_schema,
            "XhsNoteComment": comment_schema,
        },
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[generate_api_schema] Schema 已输出到: {OUTPUT_PATH}")

    engine = get_async_engine()
    if engine:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
