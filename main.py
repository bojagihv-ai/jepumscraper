import os
import uuid
import asyncio
import json
import logging
import copy
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlparse
from flask import Flask, render_template, request, jsonify, send_file, redirect, abort
from werkzeug.utils import secure_filename
import config

# 서비스 및 엔진 로드
from scrapers.base_scraper import ProductResult, download_image_sync
from services.search_service import SearchService
from services.detail_scraper import (
    DetailScraper,
    get_detail_capture_method_order,
    is_detail_result_usable,
    requires_screen_capture,
    _normalize_detail_url,
)
from services import adaptive_learning
from engine.ip_manager import (
    get_manager as get_proxy_manager,
    reload_manager as reload_proxy_manager,
    use_proxy_for,
)
from exporters.excel_exporter import ExcelExporter
import progress_store
from services.job_queue import job_queue
from services.history_db import get_all_jobs, get_job_results
from engines.similarity_scorer import SimilarityScorer

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = config.INPUT_DIR

# 세션 관리
user_sessions = {}

# 설정 저장 파일 경로
SETTINGS_FILE = os.path.join(config.BASE_DIR, 'user_settings.json')
# 마지막 검색 세션 저장 파일
LAST_SESSION_FILE = os.path.join(config.BASE_DIR, 'last_session.json')


def setup_logging():
    log_path = os.path.join(config.BASE_DIR, 'logs', 'server.log')
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

    class _ProgressPollFilter(logging.Filter):
        def filter(self, record):
            return "/api/progress" not in record.getMessage()

    if not any(
        isinstance(h, logging.FileHandler) and getattr(h, 'baseFilename', '') == log_path
        for h in root.handlers
    ):
        handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=5, encoding='utf-8')
        handler.setFormatter(formatter)
        handler.addFilter(_ProgressPollFilter())
        root.addHandler(handler)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.addFilter(_ProgressPollFilter())
        root.addHandler(stream)
    logging.getLogger('werkzeug').addFilter(_ProgressPollFilter())


setup_logging()

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_under(path_value, root) -> Path | None:
    try:
        root_path = Path(root).resolve()
        candidate = Path(path_value).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None

    if not candidate.is_file() or not _is_relative_to(candidate, root_path):
        return None
    return candidate

DEFAULT_SETTINGS = {
    "platforms": {
        "naver":    {"api_enabled": False, "scraping_enabled": False},
        "coupang":  {"api_enabled": False, "scraping_enabled": False},
        "gmarket":  {"api_enabled": False, "scraping_enabled": False},
        "auction":  {"api_enabled": False, "scraping_enabled": False},
        "elevenst": {"api_enabled": False, "scraping_enabled": False},
    },
    "api_keys": {
        "naver_client_id": "",
        "naver_client_secret": "",
        "coupang_access_key": "",
        "coupang_secret_key": "",
        "gmarket_api_key": "",
        "auction_api_key": "",
        "elevenst_app_key": ""
    },
    "naver_login": {
        "id": "",
        "pw": ""
    },
    "match_thresholds": {
        "phash": 5,
        "clip_tier2": 0.82,
        "name_tier2": 0.40,
        "clip_tier3": 0.75,
        "color_tier3": 0.60
    },
    "max_candidates": 30,
    "scraping_delay_min": 8.0,
    "scraping_delay_max": 18.0,
    "platform_timeout_sec": 70,
    "gentle_scraping_mode": True,
    "auto_tune_enabled": True,
    "enable_clip_analysis": False,
    "search_concurrency": 1,
    "inter_platform_delay_min": 8.0,
    "inter_platform_delay_max": 18.0,
    "detail_capture_concurrency": 2,
    "detail_cache_ttl_days": 14,
    "coupang_first_mode": True,
    "coupang_min_final": 3,
    "enable_proxy_profiles": False,
    "proxy_health_check_url": "https://api.ipify.org?format=json",
    "proxy_health_timeout_sec": 8,
    "proxy_profiles": [],
    "use_user_browser_session": True,
    "chrome_user_data_dir": "",
    "chrome_profile_directory": "Default",
    "coupang_use_dedicated_profile": True,
    "coupang_browser_profile_dir": "",
    "coupang_assisted_capture": True,
    "coupang_debug_port": 9223,
    "auction_browser_profile_dir": "",
    "enable_ahk_fallback": True,
    "autohotkey_exe": "",
    "slice_height": 0
}

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                # 기본값과 병합 (새 키 추가 대응)
                merged = copy.deepcopy(DEFAULT_SETTINGS)
                for k, v in saved.items():
                    if isinstance(v, dict) and k in merged:
                        merged[k].update(v)
                    else:
                        merged[k] = v
                return merged
        except Exception as e:
            logging.warning(f"[Settings] load failed, using defaults: {e}")
    return copy.deepcopy(DEFAULT_SETTINGS)

def save_settings(settings):
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

# 설정 로드 및 config 적용
def apply_settings(settings):
    ak = settings.get('api_keys', {})
    if ak.get('naver_client_id'):
        config.NAVER_CLIENT_ID = ak['naver_client_id']
    if ak.get('naver_client_secret'):
        config.NAVER_CLIENT_SECRET = ak['naver_client_secret']
    mt = settings.get('match_thresholds', {})
    if mt.get('phash') is not None:
        config.PHASH_THRESHOLD = mt['phash']
    if mt.get('clip_tier2') is not None:
        config.CLIP_SIMILARITY_TIER2 = mt['clip_tier2']
    if mt.get('name_tier2') is not None:
        config.NAME_SIMILARITY_TIER2 = mt['name_tier2']
    if mt.get('clip_tier3') is not None:
        config.CLIP_SIMILARITY_TIER3 = mt['clip_tier3']
    if mt.get('color_tier3') is not None:
        config.COLOR_SIMILARITY_MAX = mt['color_tier3']
    # 항상 충분히 수집하도록 max_candidates 강제 확대
    gentle_mode = bool(settings.get('gentle_scraping_mode', True))
    config.GENTLE_SCRAPING_MODE = gentle_mode
    config.AUTO_TUNE_ENABLED = bool(settings.get('auto_tune_enabled', True))
    config.ENABLE_CLIP_ANALYSIS = bool(settings.get('enable_clip_analysis', False))
    config.MAX_CANDIDATES = max(3, min(int(settings.get('max_candidates') or 30), 100))
    
    delay_min = float(settings.get('scraping_delay_min') or 8.0)
    delay_max = float(settings.get('scraping_delay_max') or 18.0)
    if gentle_mode:
        delay_min = max(delay_min, 8.0)
        delay_max = max(delay_max, delay_min + 6.0, 18.0)
    config.SCRAPING_DELAY_MIN = delay_min
    config.SCRAPING_DELAY_MAX = delay_max

    config.SEARCH_CONCURRENCY = int(settings.get('search_concurrency') or (1 if gentle_mode else 5))
    if gentle_mode:
        config.SEARCH_CONCURRENCY = 1
    config.INTER_PLATFORM_DELAY_MIN = float(settings.get('inter_platform_delay_min') or delay_min)
    config.INTER_PLATFORM_DELAY_MAX = float(settings.get('inter_platform_delay_max') or delay_max)
    if gentle_mode:
        config.INTER_PLATFORM_DELAY_MIN = max(config.INTER_PLATFORM_DELAY_MIN, 8.0)
        config.INTER_PLATFORM_DELAY_MAX = max(config.INTER_PLATFORM_DELAY_MAX, 18.0)

    config.DETAIL_CAPTURE_CONCURRENCY = int(settings.get('detail_capture_concurrency') or 2)
    if gentle_mode:
        config.DETAIL_CAPTURE_CONCURRENCY = max(1, min(config.DETAIL_CAPTURE_CONCURRENCY, 2))
    config.DETAIL_CACHE_TTL_DAYS = int(settings.get('detail_cache_ttl_days') or 14)
    config.COUPANG_FIRST_MODE = bool(settings.get('coupang_first_mode', True))
    config.COUPANG_MIN_FINAL = max(0, min(5, int(settings.get('coupang_min_final') or 3)))
    config.ENABLE_PROXY_PROFILES = bool(settings.get('enable_proxy_profiles', False))
    config.PROXY_HEALTH_CHECK_URL = settings.get('proxy_health_check_url') or 'https://api.ipify.org?format=json'
    config.PROXY_HEALTH_TIMEOUT_SEC = int(settings.get('proxy_health_timeout_sec') or 8)
    config.PROXY_PROFILES = settings.get('proxy_profiles') or []

    config.USE_USER_BROWSER_SESSION = bool(settings.get('use_user_browser_session', True))
    if settings.get('chrome_user_data_dir'):
        config.CHROME_USER_DATA_DIR = settings['chrome_user_data_dir']
    if settings.get('chrome_profile_directory'):
        config.CHROME_PROFILE_DIRECTORY = settings['chrome_profile_directory']
    config.COUPANG_USE_DEDICATED_PROFILE = bool(settings.get('coupang_use_dedicated_profile', True))
    if settings.get('coupang_browser_profile_dir'):
        config.COUPANG_BROWSER_PROFILE_DIR = settings['coupang_browser_profile_dir']
    config.COUPANG_ASSISTED_CAPTURE = bool(settings.get('coupang_assisted_capture', True))
    config.COUPANG_DEBUG_PORT = int(settings.get('coupang_debug_port') or 9223)
    if settings.get('auction_browser_profile_dir'):
        config.AUCTION_BROWSER_PROFILE_DIR = settings['auction_browser_profile_dir']
    config.ENABLE_AHK_FALLBACK = bool(settings.get('enable_ahk_fallback', True))
    if settings.get('autohotkey_exe'):
        config.AUTOHOTKEY_EXE = settings['autohotkey_exe']

current_settings = load_settings()
apply_settings(current_settings)


# ─── 마지막 세션 저장/복원 ────────────────────────────────────
def _product_to_dict(p) -> dict:
    """ProductResult → JSON 직렬화용 dict"""
    return {
        "id": p.id,
        "platform": p.platform,
        "title": p.title,
        "price": str(p.price),
        "product_url": p.product_url,
        "thumbnail_url": getattr(p, 'thumbnail_url', ''),
        "local_thumbnail_path": getattr(p, 'local_thumbnail_path', ''),
        "match_tier": getattr(p, 'match_tier', 0),
        "similarity_score": getattr(p, 'similarity_score', 0),
    }


def _json_safe(value):
    """Make nested scrape result data safe for last_session.json."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return {}


def _dict_to_product(d: dict):
    """dict → ProductResult 복원"""
    p = ProductResult(
        id=d['id'],
        platform=d['platform'],
        title=d['title'],
        price=d['price'],
        product_url=d['product_url'],
        thumbnail_url=d.get('thumbnail_url', ''),
    )
    p.local_thumbnail_path = d.get('local_thumbnail_path', '')
    p.match_tier = d.get('match_tier', 0)
    p.similarity_score = float(d.get('similarity_score', 0) or 0)
    return p


def _cors_json(payload: dict, status: int = 200):
    response = jsonify(payload)
    response.status_code = status
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
    return response


def _session_or_last(session_id: str = '') -> tuple[str | None, dict | None]:
    session_id = (session_id or '').strip()
    if session_id and session_id in user_sessions:
        return session_id, user_sessions[session_id]

    loaded_sid, loaded_data = load_last_session()
    if loaded_sid and loaded_data:
        user_sessions[loaded_sid] = loaded_data
        if not session_id or session_id == loaded_sid:
            return loaded_sid, loaded_data

    return None, None


def _infer_page_query(page_url: str, fallback: str = '') -> str:
    try:
        parsed = urlparse(page_url or '')
        params = parse_qs(parsed.query)
        query = params.get('q', [''])[0] or params.get('keyword', [''])[0]
        return unquote_plus(query).strip() or fallback
    except Exception:
        return fallback


def _infer_coupang_query(page_url: str, fallback: str = '') -> str:
    return _infer_page_query(page_url, fallback)


def _download_missing_thumbnails(products: list[ProductResult]) -> None:
    for product in products:
        if not product.thumbnail_url or product.local_thumbnail_path:
            continue
        local_path = os.path.join(str(config.THUMBNAIL_DIR), f'{product.id}.jpg')
        if os.path.exists(local_path):
            product.local_thumbnail_path = local_path
            continue
        try:
            if download_image_sync(product.thumbnail_url, local_path):
                product.local_thumbnail_path = local_path
        except Exception as exc:
            logging.debug("[CoupangManual] thumbnail download failed for %s: %s", product.id, exc)


def _merge_products(existing: list[ProductResult], incoming: list[ProductResult]) -> list[ProductResult]:
    merged = {}
    for product in [*existing, *incoming]:
        key = adaptive_learning.normalize_url(product.product_url) or product.id
        if not key:
            continue
        merged[key] = product
    return list(merged.values())


def _sort_products_by_score(products: list[ProductResult]) -> list[ProductResult]:
    return sorted(
        products,
        key=lambda p: float(getattr(p, "similarity_score", 0) or 0),
        reverse=True,
    )


def _refresh_session_products(
    session_id: str,
    products: list[ProductResult],
    source_url: str = '',
    platform_key: str = 'coupang',
    platform_label: str = 'Coupang',
    method: str = 'manual_html',
) -> dict:
    from services.match_service import MatchService

    session_data = user_sessions[session_id]
    existing = list(session_data.get("all_products", {}).values())
    combined = _merge_products(existing, products)

    source_name = session_data.get("source_name", "")
    source_image = session_data.get("source_image", "")
    scorer = SimilarityScorer(source_name=source_name)
    scored = scorer.score_all(combined, image_analyzer=None)
    categorized = MatchService().classify_matches(source_image, source_name, scored)
    all_products = {p.id: p for plist in categorized.values() for p in plist}
    all_candidates = _sort_products_by_score(list(all_products.values()))
    top_candidates = all_candidates[:10]

    session_data["results"] = categorized
    session_data["all_products"] = all_products
    session_data["all_candidates"] = all_candidates
    session_data["top_candidates"] = top_candidates

    report = session_data.setdefault("search_report", {})
    report.setdefault("keyword", source_name)
    report.setdefault("platforms", {})
    platform_key_lower = platform_key.lower()
    imported_final = sum(
        1 for p in all_products.values()
        if platform_key_lower in ((p.product_url or "") + " " + (p.platform or "")).lower()
    )
    with_thumbnail = sum(1 for p in products if getattr(p, "local_thumbnail_path", ""))
    report["platforms"][platform_label] = {
        "status": "수동 가져오기 성공",
        "raw_count": len(products),
        "with_thumbnail": with_thumbnail,
        "without_thumbnail": len(products) - with_thumbnail,
        "method": method,
        "duration_ms": 0,
        "error": None,
        "final_count": imported_final,
    }
    report["total_raw"] = max(int(report.get("total_raw") or 0), len(combined))
    report["total_final"] = len(all_products)
    report[f"manual_{platform_key_lower}_import"] = {
        "source_url": source_url,
        "imported_count": len(products),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    save_last_session(session_id, session_data)
    return {
        "total": len(all_products),
        "imported_final": imported_final,
        "top_candidates": len(top_candidates),
    }


def save_last_session(session_id: str, data: dict):
    """마지막 검색 결과를 파일에 저장합니다."""
    try:
        categories_serial = {}
        for tier, plist in data['results'].items():
            categories_serial[tier] = [_product_to_dict(p) for p in plist]

        payload = {
            "session_id": session_id,
            "source_image": data['source_image'],
            "source_name": data['source_name'],
            "search_report": data.get('search_report', {}),
            "results": categories_serial,
            "top_candidates": [_product_to_dict(p) for p in data.get('top_candidates', [])],
            "all_candidates": [_product_to_dict(p) for p in data.get('all_candidates', [])],
            "last_scraped_data": _json_safe(data.get('last_scraped_data', {})),
            "last_detail_summary": _json_safe(data.get('last_detail_summary', {})),
            "last_download_url": data.get('last_download_url', ''),
            "last_selected_ids": list(data.get('last_selected_ids', []) or []),
            "last_detail_saved_at": data.get('last_detail_saved_at', ''),
        }
        with open(LAST_SESSION_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logging.info(f"[Session] 마지막 세션 저장 완료: {session_id}")
    except Exception as e:
        logging.warning(f"[Session] 세션 저장 실패: {e}")


def load_last_session() -> tuple:
    """저장된 마지막 세션을 복원합니다. (session_id, data) 반환. 없으면 (None, None)"""
    if not os.path.exists(LAST_SESSION_FILE):
        return None, None
    try:
        with open(LAST_SESSION_FILE, 'r', encoding='utf-8') as f:
            payload = json.load(f)

        categories = {}
        for tier, plist in payload.get('results', {}).items():
            try:
                tier_key = int(tier)
            except (TypeError, ValueError):
                tier_key = tier
            categories[tier_key] = [_dict_to_product(d) for d in plist]

        top_candidates = [_dict_to_product(d) for d in payload.get('top_candidates', [])]
        all_products = {p.id: p for plist in categories.values() for p in plist}
        all_candidates = [_dict_to_product(d) for d in payload.get('all_candidates', [])]
        if not all_candidates:
            all_candidates = _sort_products_by_score(list(all_products.values()))

        data = {
            "source_image": payload.get('source_image', ''),
            "source_name": payload.get('source_name', ''),
            "results": categories,
            "all_products": all_products,
            "search_report": payload.get('search_report', {}),
            "top_candidates": top_candidates,
            "all_candidates": all_candidates,
            "last_scraped_data": payload.get('last_scraped_data', {}) or {},
            "last_detail_summary": payload.get('last_detail_summary', {}) or {},
            "last_download_url": payload.get('last_download_url', '') or '',
            "last_selected_ids": payload.get('last_selected_ids', []) or [],
            "last_detail_saved_at": payload.get('last_detail_saved_at', '') or '',
        }
        session_id = payload.get('session_id', 'last')
        logging.info(f"[Session] 마지막 세션 복원 완료: {session_id} ({payload.get('source_name')})")
        return session_id, data
    except Exception as e:
        logging.warning(f"[Session] 세션 복원 실패: {e}")
        return None, None


search_service = SearchService(current_settings)
detail_scraper = DetailScraper()
excel_exporter = ExcelExporter()
try:
    import inspect
    import services.detail_scraper as _detail_module
    logging.info(
        "[Startup] main=%s detail_file=%s detail_markers=%s/%s",
        __file__,
        getattr(_detail_module, "__file__", ""),
        "method_order product=" in inspect.getsource(DetailScraper._capture_sync),
        "incomplete_capture" in inspect.getsource(DetailScraper._capture_naver_via_screen),
    )
except Exception as _startup_exc:
    logging.info("[Startup] detail marker check failed: %s", _startup_exc)

# 앱 시작 시 마지막 세션 자동 복원
_last_sid, _last_data = load_last_session()
if _last_sid and _last_data:
    user_sessions[_last_sid] = _last_data
    logging.info(f"[Session] 시작 시 세션 복원: /review/{_last_sid}")

# ─── 라우트 ───────────────────────────────────────────

@app.route('/')
def index():
    settings = load_settings()
    return render_template('index.html', settings=settings)


@app.route('/health')
def health():
    return jsonify({"ok": True, "capture_engine_version": "detail-v82"})


@app.route('/api/debug_route_marker')
def debug_route_marker():
    return jsonify({
        "pid": os.getpid(),
        "main_file": __file__,
        "capture_engine_version": "detail-v82",
        "scrape_details_has_marker": "capture_engine_version" in scrape_details.__code__.co_names,
    })

# 설정 조회
@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify(load_settings())

@app.route('/api/effective_settings', methods=['GET'])
def get_effective_settings():
    return jsonify(adaptive_learning.get_effective_settings(load_settings()))

@app.route('/api/learning', methods=['GET'])
def get_learning_summary():
    limit = request.args.get('limit', 100, type=int)
    return jsonify(adaptive_learning.get_learning_summary(limit=max(1, min(limit, 500))))


@app.route('/api/proxy_profiles', methods=['GET'])
def get_proxy_profiles():
    reload_proxy_manager()
    return jsonify(get_proxy_manager().summary())


@app.route('/api/proxy_profiles/health_check', methods=['POST'])
def proxy_profiles_health_check():
    reload_proxy_manager()
    payload = request.get_json(silent=True) or {}
    profile_id = payload.get('profile_id', '')
    return jsonify(get_proxy_manager().health_check(profile_id=profile_id))


@app.route('/api/coupang_profile/status', methods=['GET'])
def coupang_profile_status():
    import sqlite3
    from engine.browser_profile import coupang_browser_profile_dir

    profile_dir = coupang_browser_profile_dir()
    cookie_db = profile_dir / 'Default' / 'Network' / 'Cookies'
    if not cookie_db.exists():
        cookie_db = profile_dir / 'Default' / 'Cookies'
    if not cookie_db.exists():
        return jsonify({
            "ok": False,
            "profile_dir": str(profile_dir),
            "cookie_db": "",
            "message": "쿠팡 전용 Chrome 프로필이 아직 준비되지 않았습니다."
        })

    try:
        conn = sqlite3.connect(f'file:{cookie_db}?mode=ro', uri=True)
        count = conn.execute(
            "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%coupang.com%'"
        ).fetchone()[0]
        names = [
            row[0] for row in conn.execute(
                "SELECT DISTINCT name FROM cookies WHERE host_key LIKE '%coupang.com%' ORDER BY name LIMIT 20"
            ).fetchall()
        ]
        conn.close()
        return jsonify({
            "ok": count > 0,
            "profile_dir": str(profile_dir),
            "cookie_db": str(cookie_db),
            "coupang_cookie_count": count,
            "cookie_names": names,
            "message": "쿠팡 쿠키가 확인되었습니다." if count > 0 else "쿠팡 쿠키가 없습니다. 전용 Chrome에서 쿠팡에 접속/로그인 후 창을 닫아주세요."
        })
    except Exception as exc:
        return jsonify({
            "ok": False,
            "profile_dir": str(profile_dir),
            "cookie_db": str(cookie_db),
            "error": str(exc),
            "message": "쿠키 DB를 읽을 수 없습니다. 쿠팡 전용 Chrome 창이 열려 있다면 닫고 다시 확인하세요."
        }), 409


@app.route('/api/coupang_profile/open', methods=['POST'])
def open_coupang_profile():
    import subprocess
    from engine.browser_profile import chrome_browser_path, coupang_browser_profile_dir, coupang_import_extension_dir

    chrome = chrome_browser_path()
    if not chrome:
        return jsonify({"ok": False, "error": "Chrome 실행 파일을 찾지 못했습니다."}), 500

    payload = request.get_json(silent=True) or {}
    query = (payload.get("query") or "").strip()
    if not query:
        _, session_data = _session_or_last(payload.get("session_id", ""))
        if session_data:
            query = (session_data.get("source_name") or "").strip()

    target_url = "https://www.coupang.com/"

    profile_dir = coupang_browser_profile_dir()
    extension_dir = coupang_import_extension_dir()
    args = [
        chrome,
        f'--user-data-dir={profile_dir}',
        '--profile-directory=Default',
        f'--remote-debugging-port={int(getattr(config, "COUPANG_DEBUG_PORT", 9223) or 9223)}',
        '--remote-allow-origins=*',
    ]
    if (extension_dir / "manifest.json").is_file():
        args.extend([
            f'--disable-extensions-except={extension_dir}',
            f'--load-extension={extension_dir}',
        ])
    args.append(target_url)
    subprocess.Popen(args, close_fds=True)
    return jsonify({
        "ok": True,
        "profile_dir": str(profile_dir),
        "extension_dir": str(extension_dir),
        "url": target_url,
        "query": query,
        "message": "쿠팡 전용 Chrome을 열었습니다. 이 창에서 직접 검색한 뒤 JepumScraper 검색을 다시 실행하거나 콘솔 가져오기를 사용하세요."
    })

# 설정 저장
@app.route('/api/auction_profile/open', methods=['POST'])
def open_auction_profile():
    import subprocess
    from engine.browser_profile import chrome_browser_path, auction_browser_profile_dir, coupang_import_extension_dir

    chrome = chrome_browser_path()
    if not chrome:
        return jsonify({"ok": False, "error": "Chrome 실행 파일을 찾지 못했습니다."}), 500

    payload = request.get_json(silent=True) or {}
    session_id = (payload.get("session_id") or "").strip()
    query = (payload.get("query") or "").strip()
    if not query:
        _, session_data = _session_or_last(session_id)
        if session_data:
            query = (session_data.get("source_name") or "").strip()

    target_url = "https://browse.auction.co.kr/"
    if query:
        target_url = f"https://browse.auction.co.kr/search?keyword={quote_plus(query)}"
    if session_id:
        target_url = f"{target_url}#jepum_session={quote_plus(session_id)}"

    profile_dir = auction_browser_profile_dir()
    extension_dir = coupang_import_extension_dir()
    args = [
        chrome,
        f'--user-data-dir={profile_dir}',
        '--profile-directory=Default',
        f'--remote-debugging-port={int(getattr(config, "AUCTION_DEBUG_PORT", 9224) or 9224)}',
        '--remote-allow-origins=*',
    ]
    if (extension_dir / "manifest.json").is_file():
        args.extend([
            f'--disable-extensions-except={extension_dir}',
            f'--load-extension={extension_dir}',
        ])
    args.append(target_url)
    subprocess.Popen(args, close_fds=True)
    return jsonify({
        "ok": True,
        "profile_dir": str(profile_dir),
        "extension_dir": str(extension_dir),
        "url": target_url,
        "message": "옥션 전용 Chrome을 열었습니다. 검색 결과가 보이면 JepumScraper로 자동 가져옵니다."
    })


@app.route('/api/coupang/import_html', methods=['POST', 'OPTIONS'])
def import_coupang_html():
    if request.method == 'OPTIONS':
        return _cors_json({"ok": True})

    started = time.monotonic()
    payload = request.get_json(silent=True) or {}
    html = payload.get('html') or request.form.get('html', '')
    page_url = payload.get('url') or request.form.get('url', '')
    session_id = payload.get('session_id') or request.form.get('session_id', '')
    if not session_id and page_url:
        session_id = (parse_qs(urlparse(page_url).fragment).get("jepum_session") or [""])[0]
    query = payload.get('query') or request.form.get('query', '')
    source_name = _infer_coupang_query(page_url, query)

    session_id, session_data = _session_or_last(session_id)
    if not session_id or not session_data:
        return _cors_json({
            "ok": False,
            "error": "먼저 이미지 검색 세션을 만든 뒤 쿠팡 결과를 가져와야 합니다."
        }, 404)

    if not html or len(html) < 1000:
        return _cors_json({
            "ok": False,
            "error": "쿠팡 페이지 HTML이 비어 있거나 너무 짧습니다."
        }, 400)

    from scrapers.coupang_scraper import _looks_blocked, _parse_coupang_html, _safe_log_name

    if _looks_blocked(html):
        dump = os.path.join(config.BASE_DIR, 'logs', _safe_log_name('coupang_manual_blocked', source_name or 'query'))
        with open(dump, 'w', encoding='utf-8') as f:
            f.write(html)
        adaptive_learning.log_event(
            job_id=session_id,
            stage="search",
            platform="coupang",
            method="manual_html",
            status="blocked",
            success=False,
            url=page_url,
            message="manual HTML was blocked",
            metadata={"keyword": source_name},
        )
        return _cors_json({
            "ok": False,
            "status": "blocked",
            "error": "가져온 쿠팡 페이지가 Access Denied/보안 확인 화면입니다.",
            "dump": dump,
        }, 409)

    max_count = max(3, min(50, int(getattr(config, "MAX_CANDIDATES", 30) or 30)))
    products = _parse_coupang_html(html, max_count)
    if not products:
        dump = os.path.join(config.BASE_DIR, 'logs', _safe_log_name('coupang_manual_empty', source_name or 'query'))
        with open(dump, 'w', encoding='utf-8') as f:
            f.write(html)
        adaptive_learning.log_event(
            job_id=session_id,
            stage="search",
            platform="coupang",
            method="manual_html",
            status="zero_result",
            success=False,
            url=page_url,
            message="manual HTML parsed 0 products",
            metadata={"keyword": source_name},
        )
        return _cors_json({
            "ok": False,
            "status": "zero_result",
            "error": "쿠팡 HTML에서 상품 카드를 찾지 못했습니다.",
            "dump": dump,
        }, 422)

    _download_missing_thumbnails(products)
    merge_stats = _refresh_session_products(session_id, products, source_url=page_url)
    duration_ms = int((time.monotonic() - started) * 1000)
    adaptive_learning.record_method_result(
        platform="coupang",
        stage="search",
        method="manual_html",
        success=True,
        status="success",
        duration_ms=duration_ms,
        job_id=session_id,
        url=page_url,
        message=f"{len(products)} imported",
        metadata={"keyword": source_name, "raw_count": len(products)},
    )
    logging.info("[CoupangManual] %s imported %d products from visible page", session_id, len(products))
    return _cors_json({
        "ok": True,
        "session_id": session_id,
        "imported": len(products),
        "with_thumbnail": sum(1 for p in products if getattr(p, "local_thumbnail_path", "")),
        "duration_ms": duration_ms,
        "redirect_url": f"/review/{session_id}",
        **merge_stats,
    })


@app.route('/api/auction/import_html', methods=['POST', 'OPTIONS'])
def import_auction_html():
    if request.method == 'OPTIONS':
        return _cors_json({"ok": True})

    started = time.monotonic()
    payload = request.get_json(silent=True) or {}
    html = payload.get('html') or request.form.get('html', '')
    page_url = payload.get('url') or request.form.get('url', '')
    session_id = payload.get('session_id') or request.form.get('session_id', '')
    if not session_id and page_url:
        session_id = (parse_qs(urlparse(page_url).fragment).get("jepum_session") or [""])[0]
    query = payload.get('query') or request.form.get('query', '')
    source_name = _infer_page_query(page_url, query)

    session_id, session_data = _session_or_last(session_id)
    if not session_id or not session_data:
        return _cors_json({
            "ok": False,
            "error": "먼저 이미지 검색 세션을 만든 뒤 옥션 결과를 가져올 수 있습니다."
        }, 404)

    if not html or len(html) < 1000:
        return _cors_json({
            "ok": False,
            "error": "옥션 페이지 HTML이 비어 있거나 너무 짧습니다."
        }, 400)

    from scrapers.auction_scraper import _looks_blocked, _parse_auction_html, _safe_log_name

    if _looks_blocked(html):
        dump = os.path.join(config.BASE_DIR, 'logs', _safe_log_name('auction_manual_blocked', source_name or 'query'))
        with open(dump, 'w', encoding='utf-8') as f:
            f.write(html)
        adaptive_learning.log_event(
            job_id=session_id,
            stage="search",
            platform="auction",
            method="manual_html",
            status="blocked",
            success=False,
            url=page_url,
            message="manual HTML was blocked",
            metadata={"keyword": source_name},
        )
        return _cors_json({
            "ok": False,
            "status": "blocked",
            "error": "가져온 옥션 페이지가 Cloudflare/보안 확인 화면입니다.",
            "dump": dump,
        }, 409)

    max_count = max(3, min(50, int(getattr(config, "MAX_CANDIDATES", 30) or 30)))
    products = _parse_auction_html(html, max_count)
    if not products:
        dump = os.path.join(config.BASE_DIR, 'logs', _safe_log_name('auction_manual_empty', source_name or 'query'))
        with open(dump, 'w', encoding='utf-8') as f:
            f.write(html)
        adaptive_learning.log_event(
            job_id=session_id,
            stage="search",
            platform="auction",
            method="manual_html",
            status="zero_result",
            success=False,
            url=page_url,
            message="manual HTML parsed 0 products",
            metadata={"keyword": source_name},
        )
        return _cors_json({
            "ok": False,
            "status": "zero_result",
            "error": "옥션 HTML에서 상품 카드를 찾지 못했습니다.",
            "dump": dump,
        }, 422)

    _download_missing_thumbnails(products)
    merge_stats = _refresh_session_products(
        session_id,
        products,
        source_url=page_url,
        platform_key="auction",
        platform_label="Auction",
        method="manual_html",
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    adaptive_learning.record_method_result(
        platform="auction",
        stage="search",
        method="manual_html",
        success=True,
        status="success",
        duration_ms=duration_ms,
        job_id=session_id,
        url=page_url,
        message=f"{len(products)} imported",
        metadata={"keyword": source_name, "raw_count": len(products)},
    )
    logging.info("[AuctionManual] %s imported %d products from visible page", session_id, len(products))
    return _cors_json({
        "ok": True,
        "session_id": session_id,
        "imported": len(products),
        "with_thumbnail": sum(1 for p in products if getattr(p, "local_thumbnail_path", "")),
        "duration_ms": duration_ms,
        "redirect_url": f"/review/{session_id}",
        **merge_stats,
    })


@app.route('/api/settings', methods=['POST'])
def update_settings():
    data = request.json
    settings = load_settings()
    # 플랫폼 활성화 상태 (api_enabled / scraping_enabled)
    if 'platforms' in data:
        for pkey, pval in data['platforms'].items():
            if pkey not in settings['platforms']:
                settings['platforms'][pkey] = {}
            settings['platforms'][pkey]['api_enabled']      = pval.get('api_enabled', False)
            settings['platforms'][pkey]['scraping_enabled'] = pval.get('scraping_enabled', False)
    # API 키
    if 'api_keys' in data:
        settings['api_keys'].update(data['api_keys'])
    # 임계값
    if 'match_thresholds' in data:
        settings['match_thresholds'].update(data['match_thresholds'])
    # 네이버 로그인 정보
    if 'naver_login' in data:
        settings['naver_login'] = data['naver_login']
    # 기타
    for key in (
        'max_candidates', 'scraping_delay_min', 'scraping_delay_max',
        'platform_timeout_sec', 'gentle_scraping_mode', 'auto_tune_enabled',
        'enable_clip_analysis', 'search_concurrency',
        'inter_platform_delay_min', 'inter_platform_delay_max',
        'detail_capture_concurrency', 'detail_cache_ttl_days', 'use_user_browser_session',
        'coupang_first_mode', 'coupang_min_final',
        'enable_proxy_profiles', 'proxy_health_check_url', 'proxy_health_timeout_sec',
        'proxy_profiles', 'chrome_user_data_dir', 'chrome_profile_directory',
        'coupang_use_dedicated_profile', 'coupang_browser_profile_dir',
        'coupang_assisted_capture', 'coupang_debug_port',
        'auction_browser_profile_dir',
        'enable_ahk_fallback', 'autohotkey_exe', 'slice_height'
    ):
        if key in data:
            settings[key] = data[key]

    save_settings(settings)
    apply_settings(settings)
    # SearchService 재초기화
    global search_service
    search_service = SearchService(settings)
    reload_proxy_manager()
    return jsonify({"ok": True})

# 검색 API
@app.route('/api/search', methods=['POST'])
async def search():
    if 'image' not in request.files:
        return jsonify({"error": "이미지가 없습니다."}), 400
    file = request.files['image']
    product_name = request.form.get('productName', '').strip()
    if not file.filename or not product_name:
        return jsonify({"error": "제품 이름과 이미지를 모두 입력해주세요."}), 400

    ext = Path(file.filename).suffix or '.jpg'
    session_id = str(uuid.uuid4())
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{session_id}{ext}")
    file.save(save_path)

    logging.info(f"[{session_id}] 검색 시작: {product_name}")
    progress_store.set_status(f"온라인 쇼핑몰 검색을 시작합니다...", session_id)

    settings = load_settings()
    adaptive_learning.log_event(
        job_id=session_id,
        stage="search",
        platform="all",
        method="start",
        status="started",
        success=True,
        metadata={"keyword": product_name},
    )
    raw_results = await search_service.search_all_platforms(product_name, settings, job_id=session_id)

    if not raw_results:
        adaptive_learning.log_event(
            job_id=session_id,
            stage="search",
            platform="all",
            method="complete",
            status="zero_result",
            success=False,
            metadata={"keyword": product_name},
        )
        return jsonify({"error": "검색 결과가 없습니다. 플랫폼 설정 또는 API 키를 확인해주세요."}), 404

    logging.info(f"[{session_id}] {len(raw_results)}개 후보 매칭 중...")

    # ── 유사도 점수 기반 전체 후보 정렬 ──
    # SearchService 단계에서 이미 scorer.score_all()이 실행됐지만,
    progress_store.set_status("전체 후보 유사도 순 정렬 중...", session_id)
    scored_results = list(raw_results)
    if getattr(config, 'ENABLE_CLIP_ANALYSIS', False):
        try:
            from engines.image_analyzer import get_analyzer
            analyzer = get_analyzer()
            scorer = SimilarityScorer(source_name=product_name)
            scorer.source_embedding = analyzer.get_embedding(save_path)
            scored_results = scorer.score_all(raw_results, image_analyzer=analyzer)
        except Exception as e:
            logging.warning(f"[{session_id}] 이미지 임베딩 점수 실패, 텍스트 점수만 사용: {e}")
            scored_results = _sort_products_by_score(raw_results)
    else:
        scored_results = _sort_products_by_score(raw_results)

    logging.info(f"[{session_id}] 전체 후보 {len(scored_results)}개 정렬 완료")

    # ── match_service: 전체 후보 기준으로 tier 분류 ──
    # CLIP 모델은 첫 매칭 시점에 지연 로드한다.
    from services.match_service import MatchService
    categorized = MatchService().classify_matches(save_path, product_name, scored_results)

    all_products = {p.id: p for plist in categorized.values() for p in plist}
    all_candidates = _sort_products_by_score(list(all_products.values()))
    top_candidates = all_candidates[:10]
    total = len(all_products)

    # 검색 보고서 추가
    search_report = search_service.last_report

    session_data = {
        "source_image": save_path,
        "source_name": product_name,
        "results": categorized,
        "all_products": all_products,
        "search_report": search_report,
        "all_candidates": all_candidates,
        "top_candidates": top_candidates,
    }
    user_sessions[session_id] = session_data

    # ✅ 마지막 세션으로 파일에 저장 (앱 재시작해도 복원됨)
    save_last_session(session_id, session_data)

    progress_store.set_status("✅ 완료!", session_id)
    adaptive_learning.log_event(
        job_id=session_id,
        stage="search",
        platform="all",
        method="complete",
        status="success",
        success=True,
        metadata={"total": total},
    )
    return jsonify({
        "session_id": session_id,
        "total": total,
        "effective_settings": adaptive_learning.get_effective_settings(settings),
        "redirect_url": f"/review/{session_id}"
    })

@app.route('/api/progress')
def get_progress():
    job_id = request.args.get('job_id')
    payload = {"status": progress_store.get_status(job_id)}
    if job_id:
        payload["events"] = progress_store.get_events(job_id)
    return jsonify(payload)

@app.route('/api/last_session')
def api_last_session():
    """마지막 검색 세션 ID를 반환합니다."""
    if not os.path.exists(LAST_SESSION_FILE):
        return jsonify({"session_id": None, "source_name": None})
    try:
        with open(LAST_SESSION_FILE, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        sid = payload.get('session_id')
        # 세션이 메모리에 없으면 복원
        if sid and sid not in user_sessions:
            _, data = load_last_session()
            if data:
                user_sessions[sid] = data
        return jsonify({
            "session_id": sid,
            "source_name": payload.get('source_name', ''),
            "url": f"/review/{sid}" if sid else None,
            "candidate_count": len(payload.get('all_candidates', []) or []),
            "has_detail_results": bool(payload.get('last_scraped_data')),
            "detail_summary": payload.get('last_detail_summary', {}) or {},
            "last_detail_saved_at": payload.get('last_detail_saved_at', '') or '',
        })
    except Exception as e:
        return jsonify({"session_id": None, "error": str(e)})


@app.route('/review/<session_id>')
def review(session_id):
    # 메모리에 없으면 파일에서 복원 시도
    if session_id not in user_sessions:
        _, data = load_last_session()
        if data:
            user_sessions[session_id] = data
        else:
            return "세션이 만료되었습니다. 처음부터 다시 시작해주세요.", 404
    data = user_sessions[session_id]
    all_candidates = data.get('all_candidates')
    if not all_candidates:
        all_candidates = _sort_products_by_score(list(data.get('all_products', {}).values()))
    return render_template('review.html',
                           session_id=session_id,
                           categories=data['results'],
                           all_candidates=all_candidates,
                           top_candidates=data.get('top_candidates', []),
                           source_name=data['source_name'],
                           search_report=data.get('search_report', {}),
                           last_scraped_data=data.get('last_scraped_data', {}) or {},
                           last_download_url=data.get('last_download_url', '') or '',
                           last_detail_summary=data.get('last_detail_summary', {}) or {})

@app.route('/api/search_report/<session_id>')
def get_search_report(session_id):
    if session_id not in user_sessions:
        return jsonify({"error": "세션 없음"}), 404
    return jsonify(user_sessions[session_id].get('search_report', {}))

@app.route('/api/thumbnail/<product_id>')
def send_thumbnail(product_id):
    for session_data in user_sessions.values():
        prod = session_data["all_products"].get(product_id)
        if prod and prod.local_thumbnail_path and os.path.exists(prod.local_thumbnail_path):
            return send_file(prod.local_thumbnail_path)
    return "", 404

@app.route('/api/source_image/<session_id>')
def send_source(session_id):
    data = user_sessions.get(session_id)
    if data and os.path.exists(data["source_image"]):
        return send_file(data["source_image"])
    return "", 404

@app.route('/api/scrape_details', methods=['POST'])
async def scrape_details():
    logging.warning("[scrape_details-entry] pid=%s route_version=detail-v82", os.getpid())
    data = request.json
    session_id = data.get('session_id')
    selected_ids = data.get('selected_ids', [])
    if not session_id or session_id not in user_sessions:
        return jsonify({"error": "세션이 유효하지 않습니다."}), 400

    all_products = user_sessions[session_id]["all_products"]
    selected_products = [all_products[pid] for pid in selected_ids if pid in all_products]
    if not selected_products:
        return jsonify({"error": "선택된 제품이 없습니다."}), 400

    import webbrowser, time as _time
    scraped_data = {}  # {product_id: {"screenshots": [...], "mhtml_path": "..."}}
    capture_limit = int(getattr(config, 'DETAIL_CAPTURE_CONCURRENCY', 2) or 2)
    capture_limit = max(1, min(capture_limit, 2 if getattr(config, 'GENTLE_SCRAPING_MODE', True) else 3))
    semaphore = asyncio.Semaphore(capture_limit)
    screen_capture_lock = asyncio.Lock()
    progress_lock = asyncio.Lock()
    total_selected = len(selected_products)
    started_count = 0
    completed_count = 0
    details_started_at = _time.monotonic()
    progress_store.set_status(
        f"상세 캡처 준비: {total_selected}개 선택됨. 화면 캡처는 안정성을 위해 1개씩 처리합니다.",
        session_id,
        metadata={
            "stage": "detail_capture",
            "total": total_selected,
            "screen_queue_concurrency": 1,
            "non_screen_concurrency": capture_limit,
        },
    )

    async def _mark_detail_complete(product, detail_result):
        nonlocal completed_count
        async with progress_lock:
            completed_count += 1
            elapsed = max(0.1, _time.monotonic() - details_started_at)
            avg = elapsed / max(1, completed_count)
            remaining = max(0, total_selected - completed_count)
            eta = int(avg * remaining)
            method = detail_result.get("method", "") if isinstance(detail_result, dict) else ""
            progress_store.set_status(
                f"상세 캡처 완료 {completed_count}/{total_selected}: {product.platform} - {method or 'unknown'} (남은 예상 {eta}초)",
                session_id,
                metadata={
                    "stage": "detail_capture",
                    "completed": completed_count,
                    "total": total_selected,
                    "eta_sec": eta,
                    "product_id": product.id,
                    "method": method,
                },
            )

    async def _capture_product(product):
        nonlocal started_count
        detail_url = _normalize_detail_url(product.product_url)
        is_naver = "naver.com" in detail_url or "smartstore" in detail_url
        platform_key = adaptive_learning.normalize_platform(product.platform, detail_url)
        method_order_preview = get_detail_capture_method_order(platform_key, detail_url)
        uses_screen_capture = requires_screen_capture(platform_key, detail_url)
        try:
            import inspect as _inspect
            logging.warning(
                "[detail-runtime] pid=%s product=%s platform=%s raw_url=%s detail_url=%s uses_screen=%s order=%s scraper=%s sync_marker=%s",
                os.getpid(),
                product.id,
                platform_key,
                product.product_url,
                detail_url,
                uses_screen_capture,
                method_order_preview,
                type(detail_scraper).__module__,
                "DetailRuntime" in _inspect.getsource(detail_scraper._capture_sync),
            )
        except Exception as _runtime_exc:
            logging.warning("[detail-runtime] marker check failed for %s: %s", product.id, _runtime_exc)
        cached = None if platform_key == "naver" else adaptive_learning.get_detail_cache(detail_url)
        if platform_key == "naver":
            logging.info(f"[detail] bypass cache for naver product {product.id}")
        if cached and is_detail_result_usable(cached):
            logging.info(f"[detail] cache hit for {product.id}")
            adaptive_learning.log_event(
                job_id=session_id,
                stage="detail_capture",
                platform=platform_key,
                method="cache",
                status="cache_hit",
                success=True,
                url=detail_url,
                metadata={"product_id": product.id},
            )
            await _mark_detail_complete(product, cached)
            return product.id, cached
        if cached:
            logging.info(f"[detail] ignored stale/bad cache for {product.id}")

        lock = screen_capture_lock if uses_screen_capture else semaphore
        started = _time.monotonic()
        async with progress_lock:
            started_count += 1
            mode_label = "화면 캡처 큐 1개씩" if uses_screen_capture else f"비화면 캡처 동시성 {capture_limit}"
            progress_store.set_status(
                f"상세 캡처 시작 {started_count}/{total_selected}: {product.platform} - {product.title[:30]} ({mode_label})",
                session_id,
                metadata={
                    "stage": "detail_capture",
                    "started": started_count,
                    "total": total_selected,
                    "product_id": product.id,
                    "platform": platform_key,
                    "uses_screen_capture": uses_screen_capture,
                    "method_order": method_order_preview,
                },
            )
        async with lock:
            proxy_selection = None
            try:
                with use_proxy_for(platform_key, "detail_capture") as proxy_selection:
                    detail_result = await detail_scraper.capture_detail_page(
                        detail_url,
                        product.id,
                        job_id=session_id,
                        platform=product.platform,
                    )
                get_proxy_manager().record_result(
                    proxy_selection,
                    platform=platform_key,
                    status=detail_result.get("status", "success" if detail_result.get("screenshots") else "error"),
                    success=bool(detail_result.get("screenshots")),
                    duration_ms=int((_time.monotonic() - started) * 1000),
                    error=detail_result.get("error", ""),
                )
            except Exception as e:
                logging.error(f"[detail] capture failed for {product.id}: {e}", exc_info=True)
                detail_result = {"screenshots": [], "mhtml_path": "", "status": "error", "method": "unknown"}
                get_proxy_manager().record_result(
                    proxy_selection,
                    platform=platform_key,
                    status=adaptive_learning.classify_exception(e),
                    success=False,
                    duration_ms=int((_time.monotonic() - started) * 1000),
                    error=str(e),
                )
                adaptive_learning.record_method_result(
                    platform=platform_key,
                    stage="detail_capture",
                    method="unknown",
                    success=False,
                    status=adaptive_learning.classify_exception(e),
                    duration_ms=int((_time.monotonic() - started) * 1000),
                    job_id=session_id,
                    url=detail_url,
                    message=str(e),
                    metadata={
                        "product_id": product.id,
                        **(proxy_selection.metadata() if proxy_selection else {"proxy_mode": "direct", "proxy_profile_id": "direct"}),
                    },
                )

        if detail_result.get("screenshots") and is_detail_result_usable(detail_result):
            adaptive_learning.save_detail_cache(
                detail_url,
                product.platform,
                product.id,
                detail_result,
                detail_result.get("status", "success"),
            )

        # 네이버 상품 캡처 실패 시 자동으로 브라우저 탭 열기
        if False and is_naver and not detail_result.get("screenshots"):
            logging.info(f"[자동 열기] 네이버 상품 브라우저 자동 오픈: {product.product_url}")
            webbrowser.open(detail_url)
            _time.sleep(0.8)  # 탭 여러개 동시에 열릴 때 간격

        await _mark_detail_complete(product, detail_result)
        return product.id, detail_result

    for pid, detail_result in await asyncio.gather(
        *(_capture_product(product) for product in selected_products)
    ):
        scraped_data[pid] = detail_result

    excel_filename = f"result_{session_id[:8]}.xlsx"
    excel_path = os.path.join(config.OUTPUT_DIR, excel_filename)
    success = excel_exporter.export(selected_products, scraped_data, excel_path)

    if success:
        # 클라이언트에 타이틀 + product_url 정보 넘기기
        failed_details = []
        for pid, d in scraped_data.items():
            if pid in all_products:
                d['title'] = all_products[pid].title
                d['platform'] = all_products[pid].platform
                d['product_url'] = _normalize_detail_url(all_products[pid].product_url)
            capture_ok = bool(d.get("screenshots")) and is_detail_result_usable(d)
            d["capture_success"] = capture_ok
            if not capture_ok:
                failed_details.append({
                    "product_id": pid,
                    "title": d.get("title", pid),
                    "platform": d.get("platform", ""),
                    "status": d.get("status", "failed"),
                    "method": d.get("method", ""),
                    "reason": d.get("reason") or d.get("error") or d.get("status", "failed"),
                })

        detail_summary = {
            "total": len(selected_products),
            "success": max(0, len(selected_products) - len(failed_details)),
            "failed": len(failed_details),
            "failed_items": failed_details,
        }
        final_status = "partial_success" if failed_details else "success"
        final_message = (
            f"일부 완료: {detail_summary['success']}/{detail_summary['total']}개 성공, {detail_summary['failed']}개 실패"
            if failed_details else "완료!"
        )
        
        progress_store.set_status(final_message, session_id, metadata={
            "stage": "detail_capture",
            "summary": detail_summary,
        })
        adaptive_learning.log_event(
            job_id=session_id,
            stage="export",
            platform="all",
            method="excel",
            status=final_status,
            success=not failed_details,
            metadata={"selected": len(selected_products), "excel": excel_filename, **detail_summary},
        )
        session_data = user_sessions.get(session_id)
        if session_data is not None:
            session_data["last_scraped_data"] = scraped_data
            session_data["last_detail_summary"] = detail_summary
            session_data["last_download_url"] = f"/download/{excel_filename}"
            session_data["last_selected_ids"] = [p.id for p in selected_products]
            session_data["last_detail_saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_last_session(session_id, session_data)
        return jsonify({
            "message": final_message,
            "download_url": f"/download/{excel_filename}",
            "scraped_data": scraped_data,
            "detail_summary": detail_summary,
            "partial_success": bool(failed_details),
            "capture_engine_version": "detail-v82",
            "capture_queue": {
                "total": total_selected,
                "screen_queue_concurrency": 1,
                "non_screen_concurrency": capture_limit,
            },
        })
    return jsonify({"error": "엑셀 생성 실패"}), 500

@app.route('/api/reslice', methods=['POST'])
async def reslice_image():
    from PIL import Image
    import numpy as np
    import glob
    
    data = request.json
    product_id = data.get('product_id')
    slice_height = data.get('slice_height', 0)
    
    if not product_id:
        return jsonify({"error": "제품 ID가 필요합니다."}), 400
        
    product_detail_dir = os.path.join(config.DETAIL_DIR, str(product_id))
    fullpage_path = os.path.join(product_detail_dir, f"{product_id}_fullpage.jpg")
    
    if slice_height == 0:
        if os.path.exists(fullpage_path):
            return jsonify({"screenshots": [fullpage_path]})
        else:
            chunks = sorted(glob.glob(os.path.join(product_detail_dir, "part_*.jpg")))
            return jsonify({"screenshots": chunks})
            
    old_chunks = glob.glob(os.path.join(product_detail_dir, "part_*.jpg"))
    for f in old_chunks:
        try: os.remove(f)
        except: pass
            
    if not os.path.exists(fullpage_path):
        return jsonify({"error": "원본(풀페이지) 이미지가 없어 다시 자를 수 없습니다."}), 400
        
    try:
        img = Image.open(fullpage_path)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
            
        w, h = img.size
        new_chunks = []
        
        # --- 스마트 자르기 로직 (변동성이 적은 단색 배경 라인 찾기) ---
        img_arr = np.array(img) # (H, W, 3)
        # 행별 픽셀 표준편차(색상 변화량) 계산 (평균을 내어 1D 배열로 만듦)
        row_variance = np.std(img_arr, axis=(1, 2)) 
        
        # 탐색 범위 파라미터
        search_range = 150 # 목표 컷 지점 위아래로 탐색할 마진 (총 300px)
        
        current_y = 0
        part_idx = 1
        
        while current_y < h:
            target_y = current_y + slice_height
            if target_y >= h:
                # 남은 부분이 슬라이스 높이보다 작으면 그대로 자르고 끝
                crop_img = img.crop((0, current_y, w, h))
                part_path = os.path.join(product_detail_dir, f"part_{part_idx:03d}.jpg")
                crop_img.save(part_path, "JPEG", quality=85)
                new_chunks.append(part_path)
                break
                
            # target_y 주변에서 가장 변화성(표준편차)이 적은 y 좌표 찾기
            search_start = max(current_y + 100, target_y - search_range)
            search_end = min(h - 1, target_y + search_range)
            
            # 탐색할 라벨들
            sub_variances = row_variance[search_start:search_end]
            if len(sub_variances) > 0:
                # 가장 변화량이 적은 (단색에 가까운) 행의 로컬 인덱스
                min_idx = np.argmin(sub_variances)
                best_y = search_start + min_idx
            else:
                best_y = target_y
                
            crop_img = img.crop((0, current_y, w, best_y))
            part_path = os.path.join(product_detail_dir, f"part_{part_idx:03d}.jpg")
            crop_img.save(part_path, "JPEG", quality=85)
            new_chunks.append(part_path)
            
            current_y = best_y
            part_idx += 1
            
        return jsonify({"screenshots": new_chunks})
    except Exception as e:
        logging.error(f"Image smart slice error: {e}")
        return jsonify({"error": f"자르기 중 오류 발생: {str(e)}"}), 500

@app.route('/api/autotest', methods=['POST'])
async def autotest():
    """
    자동 테스트: 마지막 세션의 이미지+키워드를 재사용하여
    선택된 플랫폼에서 상품 1개씩 뽑아 상세페이지까지 캡처.
    """
    data = request.json or {}
    platforms = data.get('platforms', [])  # ['쿠팡', '네이버쇼핑'] 등
    session_id = data.get('session_id', '')

    # 세션에서 소스 이미지 + 키워드 가져오기
    session_data = user_sessions.get(session_id)
    if not session_data:
        # 마지막 세션 파일에서 복원 시도
        if os.path.exists(LAST_SESSION_FILE):
            try:
                with open(LAST_SESSION_FILE, 'r', encoding='utf-8') as f:
                    last = json.load(f)
                session_id = last.get('session_id', '')
                session_data = user_sessions.get(session_id)
            except Exception:
                pass
    if not session_data:
        return jsonify({"error": "이전 검색 세션이 없습니다. 먼저 검색을 실행해주세요."}), 400

    all_products = session_data.get('all_products', {})
    if not all_products:
        return jsonify({"error": "검색 결과가 없습니다."}), 400

    # 플랫폼 이름 정규화 매핑 (UI 표시명 → ProductResult.platform 영문명)
    PLATFORM_ALIAS = {
        '쿠팡': ['쿠팡', 'Coupang', 'coupang'],
        '네이버쇼핑': ['Naver', 'naver', '네이버', '네이버쇼핑', 'NaverShopping'],
        '11번가': ['11번가', 'Elevenst', 'elevenst', '11st'],
        'G마켓': ['G마켓', 'Gmarket', 'gmarket'],
        '옥션': ['옥션', 'Auction', 'auction'],
    }

    def _plat_matches(plat: str, ui_platforms: list) -> bool:
        if not ui_platforms:
            return True
        for ui_name in ui_platforms:
            allowed = PLATFORM_ALIAS.get(ui_name, [ui_name])
            if plat in allowed:
                return True
        return False

    # 플랫폼별로 상품 1개씩 선택
    selected = []
    selected_plats = []
    for pid, prod in all_products.items():
        plat = getattr(prod, 'platform', '')
        if _plat_matches(plat, platforms):
            # 이미 선택된 플랫폼은 스킵 (플랫폼당 1개)
            if plat not in selected_plats:
                selected.append(pid)
                selected_plats.append(plat)

    if not selected:
        return jsonify({"error": "선택된 플랫폼에 해당하는 상품이 없습니다."}), 400

    logging.info(f"[autotest] 플랫폼별 1개씩 선택: {selected}")

    # 상세페이지 캡처
    scraped_data = {}
    for pid in selected:
        prod = all_products[pid]
        logging.info(f"[autotest] 캡처 중: {prod.platform} - {prod.title[:30]}")
        detail_result = await detail_scraper.capture_detail_page(prod.product_url, prod.id)
        scraped_data[pid] = detail_result
        scraped_data[pid]['title'] = prod.title
        scraped_data[pid]['platform'] = prod.platform
        scraped_data[pid]['product_url'] = prod.product_url

        # 네이버 캡처 실패 시 브라우저 자동 열기
        is_naver = 'naver.com' in prod.product_url or 'smartstore' in prod.product_url
        if is_naver and not detail_result.get('screenshots'):
            import webbrowser, time as _t
            webbrowser.open(prod.product_url)
            _t.sleep(0.8)

    # 결과 요약
    results_summary = []
    for pid, d in scraped_data.items():
        prod = all_products[pid]
        results_summary.append({
            'platform': d.get('platform', ''),
            'title': d.get('title', '')[:40],
            'product_url': d.get('product_url', ''),
            'screenshot_count': len(d.get('screenshots', [])),
            'success': len(d.get('screenshots', [])) > 0,
        })

    return jsonify({
        "ok": True,
        "tested": len(selected),
        "results": results_summary,
        "scraped_data": scraped_data,
        "session_id": session_id,
    })


@app.route('/download/<filename>')
def download_excel(filename):
    if Path(filename).name != filename or Path(filename).suffix.lower() != '.xlsx':
        abort(404)
    file_path = _resolve_under(Path(config.OUTPUT_DIR) / filename, config.OUTPUT_DIR)
    if file_path:
        return send_file(file_path, as_attachment=True)
    return "파일을 찾을 수 없습니다.", 404

@app.route('/api/local_image')
def serve_local_image():
    path = request.args.get('path')
    file_path = _resolve_under(path, config.DATA_DIR) if path else None
    if file_path and file_path.suffix.lower() in IMAGE_EXTENSIONS:
        return send_file(file_path, as_attachment=request.args.get('download') == '1')
    return "", 404

@app.route('/api/select_folder', methods=['GET'])
def select_folder():
    import tkinter as tk
    from tkinter import filedialog
    folder_path = ""
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        folder_path = filedialog.askdirectory(title="저장할 폴더를 선택하세요")
        root.destroy()
    except Exception as e:
        pass
    return jsonify({"folder_path": folder_path})

@app.route('/api/save_local', methods=['POST'])
def save_local_images():
    import shutil
    import glob
    data = request.json
    session_id = data.get('session_id')
    product_ids = data.get('product_ids', [])
    if 'product_id' in data and not product_ids:
        product_ids = [data['product_id']]
        
    target_dir = data.get('target_dir')
    
    if not all([session_id, product_ids, target_dir]):
        return jsonify({"error": "파라미터가 모두 입력되지 않았습니다."}), 400
        
    try:
        os.makedirs(target_dir, exist_ok=True)
        count = 0
        for pid in product_ids:
            product_detail_dir = os.path.join(config.DETAIL_DIR, str(pid))
            if not os.path.exists(product_detail_dir):
                continue
                
            files_to_copy = glob.glob(os.path.join(product_detail_dir, "*.jpg"))
            for f in files_to_copy:
                filename = os.path.basename(f)
                safe_id = str(pid).replace(":", "_").replace("/", "_")
                if not filename.startswith(safe_id):
                    target_filename = f"{safe_id}_{filename}"
                else:
                    target_filename = filename
                    
                target_path = os.path.join(target_dir, target_filename)
                shutil.copy2(f, target_path)
                count += 1
                
        return jsonify({"message": "success", "saved_count": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- V2 백그라운드 자동수집 관련 API ---

@app.route('/api/jobs', methods=['POST'])
def create_job():
    if 'images' not in request.files and 'image' not in request.files:
        return jsonify({"error": "이미지가 없습니다."}), 400
        
    product_name = request.form.get('productName', '').strip()
    if not product_name:
        return jsonify({"error": "제품 이름을 입력해주세요."}), 400
        
    files = request.files.getlist('images')
    if not files:
        files = request.files.getlist('image')
        
    settings = load_settings()    
    job_ids = []
    
    for file in files:
        if not file.filename: continue
        ext = Path(file.filename).suffix or '.jpg'
        session_id = str(uuid.uuid4())
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{session_id}{ext}")
        file.save(save_path)
        
        job_id = job_queue.add_job(product_name, save_path, settings)
        job_ids.append(job_id)
        
    return jsonify({"message": f"{len(job_ids)}개의 자동 스크래핑 작업이 큐에 등록되었습니다.", "job_ids": job_ids})

@app.route('/api/jobs', methods=['GET'])
def list_jobs():
    jobs = get_all_jobs()
    return jsonify({"jobs": jobs})

@app.route('/api/jobs/<job_id>/events', methods=['GET'])
def job_events(job_id):
    return jsonify({
        "job_id": job_id,
        "progress_events": progress_store.get_events(job_id),
        "learning_events": adaptive_learning.get_job_events(job_id),
    })

@app.route('/api/jobs/<job_id>/export', methods=['GET'])
def export_job_results(job_id):
    results = get_job_results(job_id)
    if not results:
        return "결과가 없습니다.", 404
        
    products = []
    detail_data = {}
    for r in results:
        pid = r['product_id']
        # ProductResult Mock
        p = ProductResult(
            platform=r['platform'],
            title=r['title'],
            price=r['price'],
            product_url=r['product_url'],
            id=pid
        )
        p.match_tier = r['match_tier']
        p.local_thumbnail_path = r['thumbnail_path']
        products.append(p)
        
        detail_path = r['detail_path']
        screenshots = detail_path.split(';') if detail_path else []
        detail_data[pid] = {
            "screenshots": screenshots,
            "mhtml_path": ""
        }
        
    excel_filename = f"job_result_{job_id[:8]}.xlsx"
    excel_path = os.path.join(config.OUTPUT_DIR, excel_filename)
    success = excel_exporter.export(products, detail_data, excel_path)
    if success:
        return redirect(f"/download/{excel_filename}")
    return "엑셀 생성 실패", 500

@app.route('/report')
def report_page():
    return render_template('report.html')


if __name__ == '__main__':
    log_path = os.path.join(config.BASE_DIR, 'logs', 'server.log')
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_path, encoding='utf-8'),
            logging.StreamHandler(),
        ]
    )
    app.run(host='127.0.0.1', port=5002, debug=False, use_reloader=False, threaded=True)
