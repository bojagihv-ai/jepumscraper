from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import random
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

import config

logger = logging.getLogger(__name__)

DETAIL_CACHE_VERSION = 73

DB_PATH = os.path.join(config.DATA_DIR, "history.db")
EVENT_LOG_PATH = os.path.join(config.BASE_DIR, "logs", "adaptive_events.jsonl")

_LOCK = threading.Lock()
_LAST_ACTION: Dict[str, float] = {}


class CircuitOpen(RuntimeError):
    def __init__(self, platform: str, until: str, reason: str):
        self.platform = platform
        self.until = until
        self.reason = reason
        super().__init__(f"{platform} cooldown until {until}: {reason}")


def _now() -> datetime:
    return datetime.now()


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(EVENT_LOG_PATH), exist_ok=True)
    with _connect() as conn:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS adaptive_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT,
                stage TEXT,
                platform TEXT,
                method TEXT,
                status TEXT,
                success INTEGER DEFAULT 0,
                url TEXT,
                duration_ms INTEGER DEFAULT 0,
                message TEXT,
                metadata_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS method_stats (
                platform TEXT NOT NULL,
                stage TEXT NOT NULL,
                method TEXT NOT NULL,
                attempts INTEGER DEFAULT 0,
                successes INTEGER DEFAULT 0,
                failures INTEGER DEFAULT 0,
                consecutive_failures INTEGER DEFAULT 0,
                avg_duration_ms REAL DEFAULT 0,
                score REAL DEFAULT 50,
                last_status TEXT,
                updated_at TEXT,
                PRIMARY KEY(platform, stage, method)
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_state (
                platform TEXT PRIMARY KEY,
                consecutive_failures INTEGER DEFAULT 0,
                cooldown_until TEXT,
                last_status TEXT,
                delay_min REAL DEFAULT 8,
                delay_max REAL DEFAULT 18,
                updated_at TEXT
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS detail_cache (
                url_hash TEXT PRIMARY KEY,
                normalized_url TEXT,
                platform TEXT,
                product_id TEXT,
                detail_json TEXT,
                status TEXT,
                method TEXT,
                screenshot_count INTEGER DEFAULT 0,
                updated_at TEXT
            )
            """
        )
        conn.commit()


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), netloc, path, "", "", ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def normalize_platform(value: str = "", url: str = "") -> str:
    text = f"{value} {url}".lower()
    host = urlparse(url).netloc.lower() if url else ""
    text = f"{text} {host}"
    if "coupang" in text or "쿠팡" in text:
        return "coupang"
    if "naver" in text or "smartstore" in text or "네이버" in text:
        return "naver"
    if "gmarket" in text or "g마켓" in text:
        return "gmarket"
    if "auction" in text or "옥션" in text:
        return "auction"
    if "11st" in text or "11번가" in text or "eleven" in text:
        return "elevenst"
    if value:
        return value.strip().lower()[:40]
    return "unknown"


def method_name(scraper: Any) -> str:
    name = type(scraper).__name__.lower()
    if "naver" in name and "scraper" in name:
        return "api" if name == "naverscraper" else "browser"
    if "api" in name:
        return "api"
    return "browser"


def classify_exception(exc: Exception) -> str:
    msg = str(exc).lower()
    if "timeout" in msg:
        return "timeout"
    if "captcha" in msg:
        return "captcha"
    if "login" in msg or "auth" in msg:
        return "login_required"
    if "429" in msg or "too many" in msg:
        return "throttled"
    if "access denied" in msg or "blocked" in msg or "403" in msg:
        return "blocked"
    return "error"


def _append_event_log(payload: Dict[str, Any]) -> None:
    try:
        with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("[Adaptive] file event log failed: %s", e)


def log_event(
    *,
    job_id: str = "",
    stage: str,
    platform: str,
    method: str,
    status: str,
    success: bool = False,
    url: str = "",
    duration_ms: int = 0,
    message: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    init_db()
    metadata = metadata or {}
    payload = {
        "job_id": job_id,
        "stage": stage,
        "platform": normalize_platform(platform, url),
        "method": method,
        "status": status,
        "success": bool(success),
        "url": url,
        "duration_ms": int(duration_ms or 0),
        "message": message,
        "metadata": metadata,
        "created_at": _now_iso(),
    }
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO adaptive_events
                    (job_id, stage, platform, method, status, success, url,
                     duration_ms, message, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["job_id"],
                    payload["stage"],
                    payload["platform"],
                    payload["method"],
                    payload["status"],
                    1 if payload["success"] else 0,
                    payload["url"],
                    payload["duration_ms"],
                    payload["message"],
                    json.dumps(metadata, ensure_ascii=False),
                    payload["created_at"],
                ),
            )
            conn.commit()
    _append_event_log(payload)


def _calc_score(attempts: int, successes: int, consecutive_failures: int, avg_ms: float) -> float:
    if attempts <= 0:
        return 50.0
    success_rate = successes / attempts
    duration_penalty = min(20.0, (avg_ms or 0) / 10000.0)
    streak_penalty = min(30.0, consecutive_failures * 7.5)
    return max(0.0, min(100.0, success_rate * 100.0 - duration_penalty - streak_penalty))


def _update_platform_state(platform: str, success: bool, status: str) -> None:
    platform = normalize_platform(platform)
    neutral_statuses = {"zero_result", "manual_required"}
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM platform_state WHERE platform = ?",
            (platform,),
        ).fetchone()
        failures = int(row["consecutive_failures"]) if row else 0
        if success or status in neutral_statuses:
            failures = max(0, failures - 1)
            cooldown_until = None
        else:
            failures += 1
            cooldown_until = row["cooldown_until"] if row else None
            severe = status in {"blocked", "captcha", "login_required", "throttled", "timeout"}
            if severe or failures >= 3:
                minutes = min(45, 5 * failures)
                cooldown_until = (_now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")

        delay_min, delay_max = _recommended_delay_values(platform, failures)
        conn.execute(
            """
            INSERT INTO platform_state
                (platform, consecutive_failures, cooldown_until, last_status,
                 delay_min, delay_max, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform) DO UPDATE SET
                consecutive_failures = excluded.consecutive_failures,
                cooldown_until = excluded.cooldown_until,
                last_status = excluded.last_status,
                delay_min = excluded.delay_min,
                delay_max = excluded.delay_max,
                updated_at = excluded.updated_at
            """,
            (platform, failures, cooldown_until, status, delay_min, delay_max, _now_iso()),
        )
        conn.commit()


def _recommended_delay_values(platform: str, failures: int = 0) -> tuple[float, float]:
    base = {
        "naver": (8.0, 18.0),
        "coupang": (12.0, 28.0),
        "gmarket": (12.0, 25.0),
        "auction": (12.0, 25.0),
        "elevenst": (10.0, 22.0),
    }.get(normalize_platform(platform), (8.0, 20.0))
    multiplier = 1.0 + min(1.0, failures * 0.25)
    return round(base[0] * multiplier, 1), round(base[1] * multiplier, 1)


def record_method_result(
    *,
    platform: str,
    stage: str,
    method: str,
    success: bool,
    status: str,
    duration_ms: int = 0,
    job_id: str = "",
    url: str = "",
    message: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    init_db()
    platform = normalize_platform(platform, url)
    log_event(
        job_id=job_id,
        stage=stage,
        platform=platform,
        method=method,
        status=status,
        success=success,
        url=url,
        duration_ms=duration_ms,
        message=message,
        metadata=metadata,
    )
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM method_stats
                WHERE platform = ? AND stage = ? AND method = ?
                """,
                (platform, stage, method),
            ).fetchone()
            attempts = (int(row["attempts"]) if row else 0) + 1
            successes = (int(row["successes"]) if row else 0) + (1 if success else 0)
            failures = (int(row["failures"]) if row else 0) + (0 if success else 1)
            consecutive = 0 if success else ((int(row["consecutive_failures"]) if row else 0) + 1)
            old_avg = float(row["avg_duration_ms"]) if row else 0.0
            avg = duration_ms if attempts == 1 else (old_avg * (attempts - 1) + duration_ms) / attempts
            score = _calc_score(attempts, successes, consecutive, avg)
            conn.execute(
                """
                INSERT INTO method_stats
                    (platform, stage, method, attempts, successes, failures,
                     consecutive_failures, avg_duration_ms, score, last_status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, stage, method) DO UPDATE SET
                    attempts = excluded.attempts,
                    successes = excluded.successes,
                    failures = excluded.failures,
                    consecutive_failures = excluded.consecutive_failures,
                    avg_duration_ms = excluded.avg_duration_ms,
                    score = excluded.score,
                    last_status = excluded.last_status,
                    updated_at = excluded.updated_at
                """,
                (
                    platform,
                    stage,
                    method,
                    attempts,
                    successes,
                    failures,
                    consecutive,
                    avg,
                    score,
                    status,
                    _now_iso(),
                ),
            )
            conn.commit()
    _update_platform_state(platform, success, status)


def get_platform_policy(platform: str, url: str = "") -> Dict[str, Any]:
    init_db()
    platform = normalize_platform(platform, url)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM platform_state WHERE platform = ?",
            (platform,),
        ).fetchone()
    failures = int(row["consecutive_failures"]) if row else 0
    delay_min, delay_max = _recommended_delay_values(platform, failures)
    cooldown_until = row["cooldown_until"] if row else None
    cooldown_active = False
    if cooldown_until:
        try:
            cooldown_active = datetime.fromisoformat(cooldown_until) > _now()
        except ValueError:
            cooldown_active = False
    return {
        "platform": platform,
        "consecutive_failures": failures,
        "cooldown_until": cooldown_until,
        "cooldown_active": cooldown_active,
        "last_status": row["last_status"] if row else "",
        "delay_min": float(row["delay_min"]) if row and row["delay_min"] else delay_min,
        "delay_max": float(row["delay_max"]) if row and row["delay_max"] else delay_max,
    }


def assert_platform_available(platform: str, url: str = "") -> None:
    if not getattr(config, "AUTO_TUNE_ENABLED", True):
        return
    policy = get_platform_policy(platform, url)
    if policy["cooldown_active"]:
        raise CircuitOpen(policy["platform"], policy["cooldown_until"], policy["last_status"] or "cooldown")


def _delay_key(platform: str, stage: str, method: str) -> str:
    return f"{normalize_platform(platform)}:{stage}:{method}"


def wait_turn_sync(platform: str, stage: str, method: str = "default", url: str = "") -> None:
    if not getattr(config, "AUTO_TUNE_ENABLED", True):
        return
    assert_platform_available(platform, url)
    policy = get_platform_policy(platform, url)
    key = _delay_key(platform, stage, method)
    with _LOCK:
        last = _LAST_ACTION.get(key, 0.0)
        elapsed = time.time() - last
        wait_sec = max(0.0, random.uniform(policy["delay_min"], policy["delay_max"]) - elapsed)
        _LAST_ACTION[key] = time.time() + wait_sec
    if wait_sec > 0:
        logger.info("[Adaptive] %s/%s/%s %.1fs wait", platform, stage, method, wait_sec)
        time.sleep(wait_sec)


async def wait_turn(platform: str, stage: str, method: str = "default", url: str = "") -> None:
    if not getattr(config, "AUTO_TUNE_ENABLED", True):
        return
    assert_platform_available(platform, url)
    policy = get_platform_policy(platform, url)
    key = _delay_key(platform, stage, method)
    with _LOCK:
        last = _LAST_ACTION.get(key, 0.0)
        elapsed = time.time() - last
        wait_sec = max(0.0, random.uniform(policy["delay_min"], policy["delay_max"]) - elapsed)
        _LAST_ACTION[key] = time.time() + wait_sec
    if wait_sec > 0:
        logger.info("[Adaptive] %s/%s/%s %.1fs wait", platform, stage, method, wait_sec)
        await asyncio.sleep(wait_sec)


def get_detail_method_order(platform: str = "", url: str = "") -> List[str]:
    platform = normalize_platform(platform, url)
    if platform == "naver":
        defaults = ["chrome_screen", "playwright_user_profile", "drission"]
    elif platform == "coupang":
        defaults = ["chrome_screen", "ahk_screen", "playwright_user_profile", "drission"]
    elif platform == "elevenst":
        defaults = ["chrome_screen", "playwright_user_profile", "drission"]
    elif platform == "gmarket":
        defaults = ["chrome_screen", "playwright_user_profile", "drission"]
    elif platform == "auction":
        defaults = ["playwright_user_profile", "drission", "chrome_screen"]
    else:
        defaults = ["playwright_user_profile", "drission"]
    if not getattr(config, "AUTO_TUNE_ENABLED", True):
        return defaults

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT method, score, attempts, successes, consecutive_failures,
                   avg_duration_ms, last_status
            FROM method_stats
            WHERE platform = ? AND stage = 'detail_capture'
            ORDER BY score DESC, attempts DESC
            """,
            (platform,),
        ).fetchall()
    rows_by_method = {r["method"]: r for r in rows}
    known = [r["method"] for r in rows if r["method"] in defaults and int(r["attempts"]) >= 2]
    ordered = []
    for method in known + defaults:
        if method not in ordered:
            ordered.append(method)

    screen_row = rows_by_method.get("chrome_screen")
    if screen_row and "chrome_screen" in ordered:
        screen_successes = int(screen_row["successes"] or 0)
        screen_score = float(screen_row["score"] or 0)
        blockedish = {"blocked", "captcha", "login_required", "timeout", "throttled"}
        non_screen_stuck = any(
            rows_by_method.get(method)
            and int(rows_by_method[method]["consecutive_failures"] or 0) >= 1
            and str(rows_by_method[method]["last_status"] or "") in blockedish
            for method in ("playwright_user_profile", "drission")
        )
        if screen_successes >= 1 and (screen_score >= 35 or non_screen_stuck):
            ordered.remove("chrome_screen")
            ordered.insert(0, "chrome_screen")

    priority_pins = {
        "coupang": ["chrome_screen", "ahk_screen"],
    }
    for preferred in reversed(priority_pins.get(platform, [])):
        if preferred in ordered:
            ordered.remove(preferred)
            ordered.insert(0, preferred)
    return ordered


def get_detail_cache(url: str, max_age_days: Optional[int] = None) -> Optional[Dict[str, Any]]:
    init_db()
    max_age_days = max_age_days or int(getattr(config, "DETAIL_CACHE_TTL_DAYS", 14))
    key = url_hash(url)
    with _connect() as conn:
        row = conn.execute("SELECT * FROM detail_cache WHERE url_hash = ?", (key,)).fetchone()
    if not row:
        return None
    try:
        updated = datetime.fromisoformat(row["updated_at"])
    except Exception:
        return None
    if updated < _now() - timedelta(days=max_age_days):
        return None
    try:
        detail = json.loads(row["detail_json"] or "{}")
    except json.JSONDecodeError:
        return None
    diagnostics = detail.get("diagnostics") or {}
    try:
        cache_version = int(diagnostics.get("capture_version") or 0)
    except Exception:
        cache_version = 0
    if cache_version < DETAIL_CACHE_VERSION or diagnostics.get("ok") is False:
        return None
    target_title = str(diagnostics.get("target_title") or "").lower()
    if any(word in target_title for word in ("login", "sign in", "\ub85c\uadf8\uc778")):
        return None
    screenshots = detail.get("screenshots") or []
    if screenshots and all(os.path.exists(p) for p in screenshots):
        detail["cache_hit"] = True
        detail["method"] = row["method"] or detail.get("method", "cache")
        return detail
    return None


def save_detail_cache(url: str, platform: str, product_id: str, detail: Dict[str, Any], status: str) -> None:
    init_db()
    screenshots = detail.get("screenshots") or []
    if not screenshots:
        return
    method = detail.get("method", "unknown")
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO detail_cache
                    (url_hash, normalized_url, platform, product_id, detail_json,
                     status, method, screenshot_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url_hash) DO UPDATE SET
                    platform = excluded.platform,
                    product_id = excluded.product_id,
                    detail_json = excluded.detail_json,
                    status = excluded.status,
                    method = excluded.method,
                    screenshot_count = excluded.screenshot_count,
                    updated_at = excluded.updated_at
                """,
                (
                    url_hash(url),
                    normalize_url(url),
                    normalize_platform(platform, url),
                    product_id,
                    json.dumps(detail, ensure_ascii=False),
                    status,
                    method,
                    len(screenshots),
                    _now_iso(),
                ),
            )
            conn.commit()


def get_learning_summary(limit: int = 100) -> Dict[str, Any]:
    init_db()
    with _connect() as conn:
        stats = [dict(r) for r in conn.execute(
            "SELECT * FROM method_stats ORDER BY platform, stage, score DESC"
        ).fetchall()]
        platforms = [dict(r) for r in conn.execute(
            "SELECT * FROM platform_state ORDER BY platform"
        ).fetchall()]
        recent = [dict(r) for r in conn.execute(
            "SELECT * FROM adaptive_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()]
        cache_count = conn.execute("SELECT COUNT(*) AS n FROM detail_cache").fetchone()["n"]
    return {
        "stats": stats,
        "platforms": platforms,
        "recent_events": recent,
        "detail_cache_count": cache_count,
        "event_log_path": EVENT_LOG_PATH,
    }


def get_job_events(job_id: str) -> List[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM adaptive_events WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_effective_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    effective = copy.deepcopy(settings)
    gentle = bool(effective.get("gentle_scraping_mode", True))
    effective["auto_tune_enabled"] = bool(effective.get("auto_tune_enabled", True))
    if gentle:
        effective["max_candidates_effective"] = max(3, min(int(effective.get("max_candidates") or 30), 100))
        effective["search_concurrency_effective"] = 1
        effective["detail_capture_concurrency_effective"] = max(
            1, min(int(effective.get("detail_capture_concurrency") or 2), 2)
        )
        effective["scraping_delay_min_effective"] = max(float(effective.get("scraping_delay_min") or 8), 8.0)
        effective["scraping_delay_max_effective"] = max(
            float(effective.get("scraping_delay_max") or 18),
            effective["scraping_delay_min_effective"] + 6,
            18.0,
        )
    else:
        effective["max_candidates_effective"] = int(effective.get("max_candidates") or 30)
        effective["search_concurrency_effective"] = int(effective.get("search_concurrency") or 2)
        effective["detail_capture_concurrency_effective"] = int(effective.get("detail_capture_concurrency") or 2)
        effective["scraping_delay_min_effective"] = float(effective.get("scraping_delay_min") or 3)
        effective["scraping_delay_max_effective"] = float(effective.get("scraping_delay_max") or 5)

    effective["screen_capture_queue_effective"] = 1
    effective["screen_capture_queue_note"] = "화면 캡처 방식은 브라우저 포커스 충돌 방지를 위해 1개씩 처리됩니다."
    effective["coupang_first_mode_effective"] = bool(effective.get("coupang_first_mode", True))
    effective["coupang_min_final_effective"] = max(0, min(5, int(effective.get("coupang_min_final") or 3)))

    platform_keys = ["naver", "coupang", "gmarket", "auction", "elevenst"]
    effective["platform_policies"] = {key: get_platform_policy(key) for key in platform_keys}
    effective["detail_method_priority"] = {
        key: get_detail_method_order(key) for key in platform_keys
    }
    effective["detail_cache_ttl_days"] = int(effective.get("detail_cache_ttl_days") or getattr(config, "DETAIL_CACHE_TTL_DAYS", 14))
    try:
        from engine.ip_manager import get_manager as get_proxy_manager
        effective["proxy_manager"] = get_proxy_manager().summary()
    except Exception as exc:
        effective["proxy_manager"] = {"enabled": False, "error": str(exc)}
    return effective


init_db()
