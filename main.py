import os
import uuid
import asyncio
import json
import logging
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file, redirect
from werkzeug.utils import secure_filename
import config

# 서비스 및 엔진 로드
from scrapers.base_scraper import ProductResult
from services.search_service import SearchService
from services.detail_scraper import DetailScraper
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
    "max_candidates": 15,
    "scraping_delay_min": 2.0,
    "scraping_delay_max": 4.0,
    "platform_timeout_sec": 70,
    "slice_height": 0
}

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                # 기본값과 병합 (새 키 추가 대응)
                merged = DEFAULT_SETTINGS.copy()
                for k, v in saved.items():
                    if isinstance(v, dict) and k in merged:
                        merged[k].update(v)
                    else:
                        merged[k] = v
                return merged
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

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
    config.MAX_CANDIDATES = 60
    
    if settings.get('scraping_delay_min') is not None:
        config.SCRAPING_DELAY_MIN = settings['scraping_delay_min']
    if settings.get('scraping_delay_max') is not None:
        config.SCRAPING_DELAY_MAX = settings['scraping_delay_max']

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
    }


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
    return p


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
            categories[tier] = [_dict_to_product(d) for d in plist]

        top_candidates = [_dict_to_product(d) for d in payload.get('top_candidates', [])]
        all_products = {p.id: p for plist in categories.values() for p in plist}

        data = {
            "source_image": payload.get('source_image', ''),
            "source_name": payload.get('source_name', ''),
            "results": categories,
            "all_products": all_products,
            "search_report": payload.get('search_report', {}),
            "top_candidates": top_candidates,
        }
        session_id = payload.get('session_id', 'last')
        logging.info(f"[Session] 마지막 세션 복원 완료: {session_id} ({payload.get('source_name')})")
        return session_id, data
    except Exception as e:
        logging.warning(f"[Session] 세션 복원 실패: {e}")
        return None, None


search_service = SearchService()
detail_scraper = DetailScraper()
excel_exporter = ExcelExporter()

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

# 설정 조회
@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify(load_settings())

# 설정 저장
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
    for key in ('max_candidates', 'scraping_delay_min', 'scraping_delay_max', 'platform_timeout_sec', 'slice_height'):
        if key in data:
            settings[key] = data[key]

    save_settings(settings)
    apply_settings(settings)
    # SearchService 재초기화
    global search_service
    search_service = SearchService(settings)
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
    progress_store.set_status(f"온라인 쇼핑몰 검색을 시작합니다...")

    settings = load_settings()
    raw_results = await search_service.search_all_platforms(product_name, settings)

    if not raw_results:
        return jsonify({"error": "검색 결과가 없습니다. 플랫폼 설정 또는 API 키를 확인해주세요."}), 404

    logging.info(f"[{session_id}] {len(raw_results)}개 후보 매칭 중...")

    # ── 유사도 점수 기반 top-10 선별 ──
    # SearchService 단계에서 이미 scorer.score_all()이 실행됐지만,
    # 이미지 임베딩까지 포함한 정밀 점수를 여기서 재계산
    try:
        from engines.image_analyzer import get_analyzer
        analyzer = get_analyzer()
        scorer = SimilarityScorer(source_name=product_name)
        scorer.source_embedding = analyzer.get_embedding(save_path)
        top_candidates = scorer.top_n(raw_results, n=10, image_analyzer=analyzer)
    except Exception as e:
        logging.warning(f"[{session_id}] 이미지 임베딩 점수 실패 (텍스트 점수만 사용): {e}")
        top_candidates = raw_results[:10]

    logging.info(f"[{session_id}] top-10 선별 완료")

    # ── match_service: top-10 기준으로 tier 분류 ──
    # CLIP 모델은 첫 매칭 시점에 지연 로드한다.
    from services.match_service import MatchService
    categorized = MatchService().classify_matches(save_path, product_name, top_candidates)

    all_products = {p.id: p for plist in categorized.values() for p in plist}
    total = len(all_products)

    # 검색 보고서 추가
    search_report = search_service.last_report

    session_data = {
        "source_image": save_path,
        "source_name": product_name,
        "results": categorized,
        "all_products": all_products,
        "search_report": search_report,
        "top_candidates": top_candidates,
    }
    user_sessions[session_id] = session_data

    # ✅ 마지막 세션으로 파일에 저장 (앱 재시작해도 복원됨)
    save_last_session(session_id, session_data)

    progress_store.set_status("✅ 완료!")
    return jsonify({
        "session_id": session_id,
        "total": total,
        "redirect_url": f"/review/{session_id}"
    })

@app.route('/api/progress')
def get_progress():
    return jsonify({"status": progress_store.get_status()})

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
            "url": f"/review/{sid}" if sid else None
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
    return render_template('review.html',
                           session_id=session_id,
                           categories=data['results'],
                           top_candidates=data.get('top_candidates', []),
                           source_name=data['source_name'],
                           search_report=data.get('search_report', {}))

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
    for product in selected_products:
        detail_result = await detail_scraper.capture_detail_page(product.product_url, product.id)
        scraped_data[product.id] = detail_result

        # 네이버 상품 캡처 실패 시 자동으로 브라우저 탭 열기
        is_naver = "naver.com" in product.product_url or "smartstore" in product.product_url
        if is_naver and not detail_result.get("screenshots"):
            logging.info(f"[자동 열기] 네이버 상품 브라우저 자동 오픈: {product.product_url}")
            webbrowser.open(product.product_url)
            _time.sleep(0.8)  # 탭 여러개 동시에 열릴 때 간격

    excel_filename = f"result_{session_id[:8]}.xlsx"
    excel_path = os.path.join(config.OUTPUT_DIR, excel_filename)
    success = excel_exporter.export(selected_products, scraped_data, excel_path)

    if success:
        # 클라이언트에 타이틀 + product_url 정보 넘기기
        for pid, d in scraped_data.items():
            if pid in all_products:
                d['title'] = all_products[pid].title
                d['product_url'] = all_products[pid].product_url
        
        return jsonify({"message": "완료!", "download_url": f"/download/{excel_filename}", "scraped_data": scraped_data})
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
    file_path = os.path.join(config.OUTPUT_DIR, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    return "파일을 찾을 수 없습니다.", 404

@app.route('/api/local_image')
def serve_local_image():
    path = request.args.get('path')
    if path and os.path.exists(path):
        return send_file(path)
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
