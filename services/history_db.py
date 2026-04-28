import os
import sqlite3
import json
from datetime import datetime
import config

DB_PATH = os.path.join(config.DATA_DIR, 'history.db')

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS crawling_jobs (
            id TEXT PRIMARY KEY,
            keyword TEXT,
            image_path TEXT,
            status TEXT,
            total_found INTEGER DEFAULT 0,
            total_saved INTEGER DEFAULT 0,
            settings_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS crawling_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            platform TEXT,
            product_id TEXT,
            title TEXT,
            price TEXT,
            product_url TEXT,
            thumbnail_path TEXT,
            detail_path TEXT,
            match_tier INTEGER,
            FOREIGN KEY(job_id) REFERENCES crawling_jobs(id)
        )
    ''')
    conn.commit()
    conn.close()

def create_job(job_id: str, keyword: str, image_path: str, settings: dict):
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO crawling_jobs (id, keyword, image_path, status, settings_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (job_id, keyword, image_path, 'pending', json.dumps(settings), datetime.now().isoformat()))
    conn.commit()
    conn.close()

def update_job_status(job_id: str, status: str, total_found=0, total_saved=0):
    conn = get_connection()
    c = conn.cursor()
    if status in ['completed', 'failed']:
        c.execute('''
            UPDATE crawling_jobs 
            SET status = ?, total_found = ?, total_saved = ?, completed_at = ?
            WHERE id = ?
        ''', (status, total_found, total_saved, datetime.now().isoformat(), job_id))
    else:
        c.execute('''
            UPDATE crawling_jobs 
            SET status = ?, total_found = ?, total_saved = ?
            WHERE id = ?
        ''', (status, total_found, total_saved, job_id))
    conn.commit()
    conn.close()

def add_result(job_id: str, result: dict):
    # result: dict containing platform, id (product_id), title, price, url, thumb, detail, tier
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO crawling_results (
            job_id, platform, product_id, title, price, product_url, 
            thumbnail_path, detail_path, match_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        job_id,
        result.get('platform', ''),
        result.get('id', ''),
        result.get('title', ''),
        result.get('price', ''),
        result.get('product_url', ''),
        result.get('thumbnail_path', ''),
        result.get('detail_path', ''),
        result.get('match_tier', 0)
    ))
    conn.commit()
    conn.close()

def get_all_jobs():
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM crawling_jobs ORDER BY created_at DESC')
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_job_results(job_id: str):
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM crawling_results WHERE job_id = ?', (job_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# Initialize DB on import
init_db()
