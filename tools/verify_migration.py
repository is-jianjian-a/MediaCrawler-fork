#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sqlite3
import sys
from pathlib import Path

# Add project root to sys.path
project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

from config.db_config import SQLITE_DB_PATH

def verify_data_integrity():
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    
    print("=" * 80)
    print("数据库迁移验证报告")
    print("=" * 80)
    
    # 验证xhs_note表
    print("\n[1] xhs_note表验证")
    print("-" * 80)
    cursor.execute('SELECT COUNT(*) FROM xhs_note')
    count = cursor.fetchone()[0]
    print(f"总行数: {count}")
    
    # 检查数字字段是否正确转换
    cursor.execute('SELECT liked_count, collected_count, comment_count, share_count FROM xhs_note LIMIT 5')
    print("\n前5条记录的数字字段:")
    for row in cursor.fetchall():
        print(f"  liked: {row[0]}, collected: {row[1]}, comment: {row[2]}, share: {row[3]}")
    
    # 检查raw_data列是否存在且为空
    cursor.execute('SELECT COUNT(*) FROM xhs_note WHERE raw_data IS NOT NULL')
    raw_data_count = cursor.fetchone()[0]
    print(f"\n有raw_data的记录数: {raw_data_count} (应该为0)")
    
    # 验证xhs_note_comment表
    print("\n[2] xhs_note_comment表验证")
    print("-" * 80)
    cursor.execute('SELECT COUNT(*) FROM xhs_note_comment')
    count = cursor.fetchone()[0]
    print(f"总行数: {count}")
    
    cursor.execute('SELECT like_count FROM xhs_note_comment LIMIT 5')
    print("\n前5条记录的like_count:")
    for row in cursor.fetchall():
        print(f"  like_count: {row[0]}")
    
    cursor.execute('SELECT COUNT(*) FROM xhs_note_comment WHERE raw_data IS NOT NULL')
    raw_data_count = cursor.fetchone()[0]
    print(f"\n有raw_data的记录数: {raw_data_count} (应该为0)")
    
    # 验证xhs_creator表
    print("\n[3] xhs_creator表验证")
    print("-" * 80)
    cursor.execute('SELECT COUNT(*) FROM xhs_creator')
    count = cursor.fetchone()[0]
    print(f"总行数: {count}")
    
    # 检查数据库文件
    print("\n[4] 备份文件列表")
    print("-" * 80)
    import os
    db_dir = Path(SQLITE_DB_PATH).parent
    backup_files = sorted([f for f in os.listdir(db_dir) if 'backup' in f])
    for f in backup_files:
        f_path = db_dir / f
        size_mb = os.path.getsize(f_path) / (1024 * 1024)
        print(f"  {f}: {size_mb:.2f} MB")
    
    conn.close()
    print("\n" + "=" * 80)
    print("验证完成！")
    print("=" * 80)

if __name__ == "__main__":
    verify_data_integrity()
