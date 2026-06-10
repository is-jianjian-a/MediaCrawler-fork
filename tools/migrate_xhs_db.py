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

def safe_convert_to_int(value):
    """安全地将值转换为整数"""
    if value is None or value == '':
        return 0
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0

def migrate_xhs_note(conn):
    """迁移xhs_note表"""
    print("\n=== Migrating xhs_note ===")
    cursor = conn.cursor()
    
    # 1. 创建新表
    cursor.execute('''
        CREATE TABLE xhs_note_new (
            id INTEGER PRIMARY KEY,
            user_id VARCHAR(255),
            nickname TEXT,
            avatar TEXT,
            ip_location TEXT,
            add_ts BIGINT,
            last_modify_ts BIGINT,
            note_id VARCHAR(255),
            type TEXT,
            title TEXT,
            desc TEXT,
            video_url TEXT,
            time BIGINT,
            last_update_time BIGINT,
            liked_count BIGINT DEFAULT 0,
            collected_count BIGINT DEFAULT 0,
            comment_count BIGINT DEFAULT 0,
            share_count BIGINT DEFAULT 0,
            image_list TEXT,
            tag_list TEXT,
            note_url TEXT,
            source_keyword TEXT DEFAULT '',
            xsec_token TEXT,
            raw_data TEXT
        )
    ''')
    
    # 2. 迁移数据
    cursor.execute('SELECT * FROM xhs_note')
    rows = cursor.fetchall()
    print(f"Found {len(rows)} rows to migrate")
    
    for row in rows:
        # 原始列顺序（根据之前的PRAGMA table_info）:
        # id, user_id, nickname, avatar, ip_location, add_ts, last_modify_ts, note_id, 
        # type, title, desc, video_url, time, last_update_time, liked_count, collected_count, 
        # comment_count, share_count, image_list, tag_list, note_url, source_keyword, xsec_token
        
        # 转换数据
        new_row = (
            row[0],    # id
            row[1],    # user_id
            row[2],    # nickname
            row[3],    # avatar
            row[4],    # ip_location
            row[5],    # add_ts
            row[6],    # last_modify_ts
            row[7],    # note_id
            row[8],    # type
            row[9],    # title
            row[10],   # desc
            row[11],   # video_url
            row[12],   # time
            row[13],   # last_update_time
            safe_convert_to_int(row[14]),  # liked_count
            safe_convert_to_int(row[15]),  # collected_count
            safe_convert_to_int(row[16]),  # comment_count
            safe_convert_to_int(row[17]),  # share_count
            row[18],   # image_list
            row[19],   # tag_list
            row[20],   # note_url
            row[21],   # source_keyword
            row[22],   # xsec_token
            None       # raw_data (新列)
        )
        
        cursor.execute('''
            INSERT INTO xhs_note_new VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        ''', new_row)
    
    # 3. 替换旧表
    cursor.execute('DROP TABLE xhs_note')
    cursor.execute('ALTER TABLE xhs_note_new RENAME TO xhs_note')
    
    # 4. 重建索引
    cursor.execute('CREATE INDEX idx_xhs_note_note_id ON xhs_note(note_id)')
    cursor.execute('CREATE INDEX idx_xhs_note_time ON xhs_note(time)')
    
    conn.commit()
    print("xhs_note migrated successfully")

def migrate_xhs_note_comment(conn):
    """迁移xhs_note_comment表"""
    print("\n=== Migrating xhs_note_comment ===")
    cursor = conn.cursor()
    
    # 1. 创建新表
    cursor.execute('''
        CREATE TABLE xhs_note_comment_new (
            id INTEGER PRIMARY KEY,
            user_id VARCHAR(255),
            nickname TEXT,
            avatar TEXT,
            ip_location TEXT,
            add_ts BIGINT,
            last_modify_ts BIGINT,
            comment_id VARCHAR(255),
            create_time BIGINT,
            note_id VARCHAR(255),
            content TEXT,
            sub_comment_count INTEGER,
            pictures TEXT,
            parent_comment_id VARCHAR(255),
            like_count BIGINT DEFAULT 0,
            raw_data TEXT
        )
    ''')
    
    # 2. 迁移数据
    cursor.execute('SELECT * FROM xhs_note_comment')
    rows = cursor.fetchall()
    print(f"Found {len(rows)} rows to migrate")
    
    for row in rows:
        # 原始列顺序:
        # id, user_id, nickname, avatar, ip_location, add_ts, last_modify_ts, comment_id,
        # create_time, note_id, content, sub_comment_count, pictures, parent_comment_id, like_count
        
        new_row = (
            row[0],    # id
            row[1],    # user_id
            row[2],    # nickname
            row[3],    # avatar
            row[4],    # ip_location
            row[5],    # add_ts
            row[6],    # last_modify_ts
            row[7],    # comment_id
            row[8],    # create_time
            row[9],    # note_id
            row[10],   # content
            row[11],   # sub_comment_count
            row[12],   # pictures
            row[13],   # parent_comment_id
            safe_convert_to_int(row[14]),  # like_count
            None       # raw_data (新列)
        )
        
        cursor.execute('''
            INSERT INTO xhs_note_comment_new VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        ''', new_row)
    
    # 3. 替换旧表
    cursor.execute('DROP TABLE xhs_note_comment')
    cursor.execute('ALTER TABLE xhs_note_comment_new RENAME TO xhs_note_comment')
    
    # 4. 重建索引
    cursor.execute('CREATE INDEX idx_xhs_note_comment_comment_id ON xhs_note_comment(comment_id)')
    cursor.execute('CREATE INDEX idx_xhs_note_comment_create_time ON xhs_note_comment(create_time)')
    
    conn.commit()
    print("xhs_note_comment migrated successfully")

def migrate_xhs_creator(conn):
    """迁移xhs_creator表"""
    print("\n=== Migrating xhs_creator ===")
    cursor = conn.cursor()
    
    # 1. 创建新表
    cursor.execute('''
        CREATE TABLE xhs_creator_new (
            id INTEGER PRIMARY KEY,
            user_id VARCHAR(255),
            nickname TEXT,
            avatar TEXT,
            ip_location TEXT,
            add_ts BIGINT,
            last_modify_ts BIGINT,
            desc TEXT,
            gender TEXT,
            follows BIGINT DEFAULT 0,
            fans BIGINT DEFAULT 0,
            interaction BIGINT DEFAULT 0,
            tag_list TEXT
        )
    ''')
    
    # 2. 迁移数据
    cursor.execute('SELECT * FROM xhs_creator')
    rows = cursor.fetchall()
    print(f"Found {len(rows)} rows to migrate")
    
    for row in rows:
        # 原始列顺序:
        # id, user_id, nickname, avatar, ip_location, add_ts, last_modify_ts, desc,
        # gender, follows, fans, interaction, tag_list
        
        new_row = (
            row[0],    # id
            row[1],    # user_id
            row[2],    # nickname
            row[3],    # avatar
            row[4],    # ip_location
            row[5],    # add_ts
            row[6],    # last_modify_ts
            row[7],    # desc
            row[8],    # gender
            safe_convert_to_int(row[9]),   # follows
            safe_convert_to_int(row[10]),  # fans
            safe_convert_to_int(row[11]),  # interaction
            row[12]    # tag_list
        )
        
        cursor.execute('''
            INSERT INTO xhs_creator_new VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        ''', new_row)
    
    # 3. 替换旧表
    cursor.execute('DROP TABLE xhs_creator')
    cursor.execute('ALTER TABLE xhs_creator_new RENAME TO xhs_creator')
    
    conn.commit()
    print("xhs_creator migrated successfully")

def verify_migration(conn):
    """验证迁移结果"""
    print("\n=== Verifying Migration ===")
    cursor = conn.cursor()
    
    xhs_tables = ['xhs_note', 'xhs_note_comment', 'xhs_creator']
    for table_name in xhs_tables:
        print(f"\nTable: {table_name}")
        cursor.execute(f"PRAGMA table_info({table_name});")
        columns = cursor.fetchall()
        print("Columns:")
        for col in columns:
            print(f"  {col[1]}: {col[2]}")
        
        cursor.execute(f"SELECT COUNT(*) FROM {table_name};")
        count = cursor.fetchone()[0]
        print(f"Row count: {count}")

def main():
    print(f"Database path: {SQLITE_DB_PATH}")
    
    # 连接数据库
    conn = sqlite3.connect(SQLITE_DB_PATH)
    
    try:
        # 执行迁移
        migrate_xhs_note(conn)
        migrate_xhs_note_comment(conn)
        migrate_xhs_creator(conn)
        
        # 验证迁移
        verify_migration(conn)
        
        print("\n=== Migration completed successfully! ===")
        
    except Exception as e:
        print(f"\nError during migration: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    main()
