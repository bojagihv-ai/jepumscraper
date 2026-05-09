"""
engine/bypass_engine.py - 중앙 집중식 봇 방어 우회 엔진
─────────────────────────────────────────────────────────────────────
지원 방어 시스템:
  - Akamai Bot Manager 3.0  (_abck 쿠키 + sensor_data 검증)
  - DataDome                (datadome 쿠키 + JS 챌린지)
  - PerimeterX              (_px3 + _pxhd + 동적 스크립트)
  - Cloudflare Bot Fight    (cf_clearance 쿠키)
  - 일반 쿠키/토큰 캐싱 (도메인별 TTL 관리)

핵심 전략:
  1. Playwright + 완전 스텔스로 실제 브라우저처럼 방문
  2. 봇 방어 JS가 실행되어 검증 토큰 생성될 때까지 대기
  3. 검증된 쿠키를 캐시에 저장
  4. 이후 요청은 tls-client + 캐시 쿠키로 빠르게 처리

Akamai _abck 쿠키 검증 로직:
  - 미검증: 값에 "~-1~" 또는 "~0~" 포함
  - 검증됨: 위 패턴 없음 (또는 다른 값)
  - Playwright로 인간 행동 유발 → sensor_data POST → 쿠키 업데이트 대기
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 캐시 저장소 (메모리 + 파일)
_COOKIE_CACHE: dict[str, dict] = {}

# 보호 시스템별 TTL (초)
_TTL = {
    'akamai':    3000,   # 50분 (_abck는 약 1시간 유효)
    'datadome':  1800,   # 30분
    'perimeterx': 1200,  # 20분
    'cloudflare': 3600,  # 1시간
    'generic':   600,    # 10분
}

CACHE_DIR = Path(__file__).parent.parent / 'data' / 'bypass_cache'
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Playwright 전용 스레드풀 — sync_playwright 가 asyncio 이벤트 루프와
# 충돌하지 않도록 항상 별도 스레드에서 실행한다.
_PW_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="bypass-pw"
)


def _run_in_thread(fn, *args, timeout: float = 90, **kwargs):
    """동기 Playwright 작업을 별도 스레드에서 실행한다."""
    future = _PW_EXECUTOR.submit(fn, *args, **kwargs)
    return future.result(timeout=timeout)


# ──────────────────────────────────────────────────────────────────────
#  탐지 유틸
# ──────────────────────────────────────────────────────────────────────

def detect_protection(html: str, headers: dict = None) -> str:
    """
    응답 HTML / 헤더에서 사용 중인 봇 방어 시스템을 감지한다.
    반환: 'akamai' | 'datadome' | 'perimeterx' | 'cloudflare' | 'none'
    """
    html_low = html.lower() if html else ''
    hdr_low  = str(headers).lower() if headers else ''

    if any(k in html_low or k in hdr_low for k in [
        'ak_bmsc', 'akamai', 'bm_sz', 'bmak.js', '_abck',
        'akamai-bot-manager', 'akam_',
    ]):
        return 'akamai'

    if any(k in html_low or k in hdr_low for k in [
        'datadome', '/js/?', 'dd_sitekey',
    ]):
        return 'datadome'

    if any(k in html_low or k in hdr_low for k in [
        '_pxvid', '_px3', 'px-captcha', 'perimeterx', 'pxchallenge',
        'pxscript', '/_Incapsula_Resource',
    ]):
        return 'perimeterx'

    if any(k in html_low or k in hdr_low for k in [
        'cf-ray', 'cf_clearance', '__cf_bm', 'cloudflare',
        'just a moment', 'checking your browser',
    ]):
        return 'cloudflare'

    return 'none'


def is_blocked(html: str, status_code: int = 200) -> bool:
    """차단 페이지인지 확인."""
    if status_code in (403, 429, 503):
        return True
    low = html.lower()
    return any(k in low for k in [
        # 영문 차단 패턴
        'access denied', 'access is denied',
        'just a moment', 'checking your browser',
        'please wait', 'human verification',
        'captcha', 'robot', 'bot detection',
        'ddos protection', 'security check',
        'your ip has been blocked', 'ip blocked',
        'too many requests', 'rate limit',
        'forbidden', 'enable javascript',
        # 한국어 차단 패턴
        '비정상적인 접근', '자동화된 요청',
        '쇼핑 서비스 접속이 일시적으로 제한',
        '비정상적인 방법으로', '자동화 프로그램',
        '비정상 접근', '접근이 차단', '차단되었습니다',
        '잠시 후 다시', '일시적으로 차단',
        '보안 문자를 입력', '본인 확인',
        '사람인지 확인', '로봇이 아님을 확인',
        '서비스 이용이 일시 중단', '접속이 제한',
        '정상적인 방법으로', '비정상적으로 많은',
    ]) or len(html) < 1000


# ──────────────────────────────────────────────────────────────────────
#  쿠키 캐시 관리
# ──────────────────────────────────────────────────────────────────────

def _cache_key(domain: str, protection: str) -> str:
    return f'{protection}:{domain}'


def _get_cached(domain: str, protection: str) -> Optional[dict]:
    key = _cache_key(domain, protection)

    # 메모리 캐시
    entry = _COOKIE_CACHE.get(key)
    if entry and time.monotonic() < entry['expire']:
        logger.debug('[캐시] 히트 (메모리): %s', key)
        return entry['cookies']

    # 파일 캐시
    cache_file = CACHE_DIR / f'{domain.replace(".", "_")}_{protection}.json'
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding='utf-8'))
            if time.time() < data.get('expire', 0):
                logger.debug('[캐시] 히트 (파일): %s', key)
                # 메모리에도 올림
                _COOKIE_CACHE[key] = {
                    'cookies': data['cookies'],
                    'expire': time.monotonic() + (data['expire'] - time.time()),
                }
                return data['cookies']
        except Exception:
            pass

    return None


def _set_cached(domain: str, protection: str, cookies: dict) -> None:
    ttl = _TTL.get(protection, _TTL['generic'])
    key = _cache_key(domain, protection)

    _COOKIE_CACHE[key] = {
        'cookies': cookies,
        'expire': time.monotonic() + ttl,
    }
    cache_file = CACHE_DIR / f'{domain.replace(".", "_")}_{protection}.json'
    try:
        cache_file.write_text(json.dumps({
            'cookies': cookies,
            'expire': time.time() + ttl,
            'protection': protection,
            'domain': domain,
        }, ensure_ascii=False), encoding='utf-8')
    except Exception as e:
        logger.debug('[캐시] 파일 저장 실패: %s', e)


def invalidate_cache(domain: str, protection: str = None) -> None:
    """특정 도메인의 캐시를 무효화한다."""
    if protection:
        key = _cache_key(domain, protection)
        _COOKIE_CACHE.pop(key, None)
    else:
        for p in list(_TTL.keys()):
            _COOKIE_CACHE.pop(_cache_key(domain, p), None)
    logger.info('[캐시] 무효화: %s (%s)', domain, protection or 'all')


# ──────────────────────────────────────────────────────────────────────
#  Akamai Bot Manager 우회
# ──────────────────────────────────────────────────────────────────────

def _is_abck_validated(abck_value: str) -> bool:
    """
    _abck 쿠키가 검증(validated) 상태인지 확인한다.
    Akamai는 검증 전 쿠키에 '~-1~' 패턴을 포함한다.
    검증 후: 패턴이 사라지거나 다른 숫자로 변경됨.
    """
    # 검증 안됨: "~-1~" 또는 길이가 너무 짧음
    if '~-1~' in abck_value:
        return False
    if len(abck_value) < 50:
        return False
    return True


def acquire_akamai_cookies(url: str, proxy: Optional[str] = None,
                            force_refresh: bool = False) -> dict:
    """
    Akamai Bot Manager _abck 쿠키를 획득한다.

    동작 원리:
    1. Playwright + 스텔스로 페이지 로드
    2. Akamai sensor_data POST 요청 모니터링
    3. _abck 쿠키가 validation=1이 될 때까지 인간 행동 반복
    4. 검증된 쿠키 세트 반환 + 캐시 저장

    Args:
        url: 대상 URL
        proxy: 프록시 (예: 'http://user:pass@host:port')
        force_refresh: True이면 캐시 무시하고 새로 획득

    Returns:
        {'cookie_name': 'cookie_value', ...}
    """
    domain = urlparse(url).netloc
    protection = 'akamai'

    if not force_refresh:
        cached = _get_cached(domain, protection)
        if cached:
            return cached

    logger.info('[Akamai] 쿠키 획득 시작: %s', domain)

    try:
        from playwright.sync_api import sync_playwright
        from engine.stealth import apply_stealth_to_context, apply_stealth_to_page
        from engine.human_behavior import HumanBehavior
    except ImportError as e:
        logger.error('[Akamai] 필수 모듈 없음: %s', e)
        return {}

    cookies_result = {}
    sensor_data_sent = [False]
    abck_validated = [False]

    def _do_playwright():
        with sync_playwright() as pw:
            launch_opts = {
                'headless': True,
                'args': [
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled',
                    '--lang=ko-KR',
                    '--window-size=1920,1080',
                    '--disable-features=IsolateOrigins,site-per-process',
                ],
            }
            if proxy:
                launch_opts['proxy'] = {'server': proxy}

            browser = pw.chromium.launch(**launch_opts)
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/136.0.0.0 Safari/537.36'
                ),
                locale='ko-KR',
                timezone_id='Asia/Seoul',
                extra_http_headers={
                    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="136", "Google Chrome";v="136"',
                    'sec-ch-ua-mobile': '?0',
                    'sec-ch-ua-platform': '"Windows"',
                    'sec-fetch-dest': 'document',
                    'sec-fetch-mode': 'navigate',
                    'sec-fetch-site': 'none',
                    'sec-fetch-user': '?1',
                    'upgrade-insecure-requests': '1',
                },
            )
            # ── playwright-stealth + 자체 JS 이중 적용 ──
            apply_stealth_to_context(context)
            page = context.new_page()
            apply_stealth_to_page(page)

            # ── 네트워크 요청 모니터링 ──
            def on_request(request):
                url_lower = request.url.lower()
                if any(k in url_lower for k in ['sensor_data', 'akam', '_bm_', 'bm_sz']):
                    sensor_data_sent[0] = True
                    logger.debug('[Akamai] sensor_data 요청 감지: %s', request.url[:80])

            def on_response(response):
                # _abck 쿠키 업데이트 감지
                set_cookie = response.headers.get('set-cookie', '')
                if '_abck' in set_cookie:
                    m = re.search(r'_abck=([^;]+)', set_cookie)
                    if m and _is_abck_validated(m.group(1)):
                        abck_validated[0] = True
                        logger.debug('[Akamai] _abck 검증 완료!')

            page.on('request', on_request)
            page.on('response', on_response)

            # 페이지 로드
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except Exception as eg:
                logger.warning('[Akamai] goto 실패: %s', eg)

            time.sleep(random.uniform(1.5, 2.5))

            # 인간 행동 루프 (최대 30초)
            _behavior_loop(page, sensor_data_sent, abck_validated, max_secs=25)

            # 최종 쿠키 추출
            all_cookies = context.cookies()
            for c in all_cookies:
                cookies_result[c['name']] = c['value']

            logger.info('[Akamai] 추출된 쿠키: %s', list(cookies_result.keys()))

            # _abck 최종 검증 확인
            abck_val = cookies_result.get('_abck', '')
            if abck_val and _is_abck_validated(abck_val):
                logger.info('[Akamai] ✅ _abck 검증 성공')
            elif abck_val:
                logger.warning('[Akamai] ⚠️ _abck 미검증 상태 (강제 진행)')
            else:
                logger.warning('[Akamai] ⚠️ _abck 쿠키 없음')

            browser.close()

    try:
        _run_in_thread(_do_playwright)
    except concurrent.futures.TimeoutError:
        logger.warning('[Akamai] Playwright 스레드 타임아웃')

    if cookies_result:
        _set_cached(domain, protection, cookies_result)

    return cookies_result


def _behavior_loop(page, sensor_data_sent: list, validated: list,
                   max_secs: float = 25.0) -> None:
    """
    Akamai sensor_data가 전송되고 _abck가 검증될 때까지
    인간다운 행동을 반복한다.
    """
    import time
    deadline = time.time() + max_secs
    phase = 0
    # 마우스 현재 위치를 자체 변수로 추적 (page._mouse_pos 비공개 속성 미사용)
    _mouse_x, _mouse_y = 960, 540

    while time.time() < deadline:
        # 검증 완료 시 즉시 탈출
        if validated[0]:
            logger.debug('[Akamai] 행동 루프 완료 (검증됨)')
            break

        vp = page.viewport_size or {'width': 1920, 'height': 1080}
        w, h = vp['width'], vp['height']

        if phase == 0:
            # 랜덤 마우스 이동
            _mouse_x = random.randint(int(w * 0.2), int(w * 0.8))
            _mouse_y = random.randint(int(h * 0.2), int(h * 0.8))
            page.mouse.move(_mouse_x, _mouse_y)
            time.sleep(random.uniform(0.15, 0.4))

        elif phase == 1:
            # 자연스러운 스크롤
            scroll_px = random.randint(150, 450)
            page.mouse.wheel(0, scroll_px)
            time.sleep(random.uniform(0.3, 0.8))

        elif phase == 2:
            # 위로 스크롤 (뭔가 다시 보는 행동)
            if random.random() < 0.4:
                page.mouse.wheel(0, -random.randint(80, 200))
            time.sleep(random.uniform(0.5, 1.2))

        elif phase == 3:
            # 마우스 미세 이동 (손 떨림 모방)
            for _ in range(random.randint(3, 8)):
                _mouse_x = max(10, min(w - 10, int(_mouse_x + random.gauss(0, 3))))
                _mouse_y = max(10, min(h - 10, int(_mouse_y + random.gauss(0, 3))))
                page.mouse.move(_mouse_x, _mouse_y)
                time.sleep(random.uniform(0.02, 0.08))

        phase = (phase + 1) % 4

        # 0.5초마다 쿠키 재확인
        if not validated[0]:
            try:
                cookies = page.context.cookies()
                for c in cookies:
                    if c['name'] == '_abck' and _is_abck_validated(c['value']):
                        validated[0] = True
                        break
            except Exception:
                pass

    if not validated[0] and not sensor_data_sent[0]:
        logger.warning('[Akamai] sensor_data 미전송 (차단 가능성 있음)')


# ──────────────────────────────────────────────────────────────────────
#  DataDome 우회
# ──────────────────────────────────────────────────────────────────────

def acquire_datadome_cookie(url: str, proxy: Optional[str] = None,
                             force_refresh: bool = False) -> Optional[str]:
    """
    DataDome 챌린지를 해결하고 datadome 쿠키를 반환한다.

    동작:
    1. Playwright로 페이지 로드 → DataDome /js/ 리다이렉트 발생
    2. 챌린지 자동 해결 대기
    3. datadome 쿠키 추출
    """
    domain = urlparse(url).netloc
    protection = 'datadome'

    if not force_refresh:
        cached = _get_cached(domain, protection)
        if cached:
            return cached.get('datadome')

    logger.info('[DataDome] 챌린지 해결 시작: %s', domain)

    try:
        from playwright.sync_api import sync_playwright
        from engine.stealth import apply_stealth_to_context, apply_stealth_to_page
    except ImportError as e:
        logger.error('[DataDome] 필수 모듈 없음: %s', e)
        return None

    dd_cookie = [None]

    def _do_datadome():
        with sync_playwright() as pw:
            launch_opts = {'headless': True, 'args': [
                '--no-sandbox', '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--lang=ko-KR', '--window-size=1920,1080',
            ]}
            if proxy:
                launch_opts['proxy'] = {'server': proxy}

            browser = pw.chromium.launch(**launch_opts)
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/136.0.0.0 Safari/537.36'
                ),
                locale='ko-KR', timezone_id='Asia/Seoul',
            )
            apply_stealth_to_context(context)
            page = context.new_page()
            apply_stealth_to_page(page)

            def on_response(response):
                sc = response.headers.get('set-cookie', '')
                m = re.search(r'datadome=([^;]+)', sc)
                if m:
                    dd_cookie[0] = m.group(1)
                    logger.debug('[DataDome] 쿠키 감지: %s…', m.group(1)[:20])

            page.on('response', on_response)

            try:
                page.goto(url, wait_until='domcontentloaded', timeout=25000)
            except Exception:
                pass

            # 챌린지 해결 대기 (최대 25초로 연장)
            for _ in range(50):
                if dd_cookie[0]:
                    break
                for c in context.cookies():
                    if c['name'] == 'datadome':
                        dd_cookie[0] = c['value']
                        break
                if not dd_cookie[0]:
                    page.mouse.move(
                        random.randint(300, 900), random.randint(200, 600)
                    )
                    page.mouse.wheel(0, random.randint(100, 300))
                    time.sleep(0.5)

            browser.close()

    try:
        _run_in_thread(_do_datadome)
    except concurrent.futures.TimeoutError:
        logger.warning('[DataDome] Playwright 스레드 타임아웃')

    if dd_cookie[0]:
        cookies = {'datadome': dd_cookie[0]}
        _set_cached(domain, protection, cookies)
        logger.info('[DataDome] ✅ 쿠키 획득 성공: %s…', dd_cookie[0][:20])
    else:
        logger.warning('[DataDome] ⚠️ 쿠키 획득 실패')

    return dd_cookie[0]


# ──────────────────────────────────────────────────────────────────────
#  PerimeterX 우회
# ──────────────────────────────────────────────────────────────────────

def acquire_perimeterx_cookies(url: str, proxy: Optional[str] = None,
                                force_refresh: bool = False) -> dict:
    """
    PerimeterX _px3 / _pxhd / _pxvid 쿠키를 획득한다.
    """
    domain = urlparse(url).netloc
    protection = 'perimeterx'

    if not force_refresh:
        cached = _get_cached(domain, protection)
        if cached:
            return cached

    logger.info('[PerimeterX] 쿠키 획득 시작: %s', domain)

    try:
        from playwright.sync_api import sync_playwright
        from engine.stealth import apply_stealth_to_context, apply_stealth_to_page
    except ImportError as e:
        logger.error('[PerimeterX] 필수 모듈 없음: %s', e)
        return {}

    px_cookies = {}
    px_script_seen = [False]

    def _do_perimeterx():
        with sync_playwright() as pw:
            launch_opts = {'headless': True, 'args': [
                '--no-sandbox', '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--lang=ko-KR', '--window-size=1920,1080',
            ]}
            if proxy:
                launch_opts['proxy'] = {'server': proxy}

            browser = pw.chromium.launch(**launch_opts)
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/136.0.0.0 Safari/537.36'
                ),
                locale='ko-KR', timezone_id='Asia/Seoul',
            )
            apply_stealth_to_context(context)
            page = context.new_page()
            apply_stealth_to_page(page)

            def on_request(request):
                if 'px-cloud.net' in request.url or '_pxchallenge' in request.url:
                    px_script_seen[0] = True

            page.on('request', on_request)

            try:
                page.goto(url, wait_until='domcontentloaded', timeout=25000)
            except Exception:
                pass

            deadline = time.time() + 20
            while time.time() < deadline:
                cookies = context.cookies()
                px_found = {}
                for c in cookies:
                    if c['name'].startswith('_px') or c['name'] in ('_pxhd', '_pxvid', '_pxff'):
                        px_found[c['name']] = c['value']

                if px_found:
                    nonlocal px_cookies
                    px_cookies = px_found
                    logger.debug('[PerimeterX] 쿠키 감지: %s', list(px_found.keys()))
                    break

                page.mouse.move(
                    random.randint(200, 800), random.randint(150, 600)
                )
                page.mouse.wheel(0, random.randint(100, 300))
                time.sleep(0.6)

            browser.close()

    try:
        _run_in_thread(_do_perimeterx)
    except concurrent.futures.TimeoutError:
        logger.warning('[PerimeterX] Playwright 스레드 타임아웃')

    if px_cookies:
        _set_cached(domain, protection, px_cookies)
        logger.info('[PerimeterX] ✅ 쿠키 획득: %s', list(px_cookies.keys()))
    else:
        logger.warning('[PerimeterX] ⚠️ 쿠키 획득 실패')

    return px_cookies


# ──────────────────────────────────────────────────────────────────────
#  Cloudflare Bot Fight / Turnstile 우회
# ──────────────────────────────────────────────────────────────────────

def acquire_cloudflare_cookies(url: str, proxy: Optional[str] = None,
                                force_refresh: bool = False) -> dict:
    """
    Cloudflare Bot Fight Mode의 cf_clearance 쿠키를 획득한다.
    __cf_bm 쿠키도 함께 획득.
    """
    domain = urlparse(url).netloc
    protection = 'cloudflare'

    if not force_refresh:
        cached = _get_cached(domain, protection)
        if cached:
            return cached

    logger.info('[Cloudflare] 쿠키 획득 시작: %s', domain)

    try:
        from playwright.sync_api import sync_playwright
        from engine.stealth import apply_stealth_to_context, apply_stealth_to_page
    except ImportError as e:
        logger.error('[Cloudflare] 필수 모듈 없음: %s', e)
        return {}

    cf_cookies = {}

    def _do_cloudflare():
        with sync_playwright() as pw:
            launch_opts = {'headless': True, 'args': [
                '--no-sandbox', '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--lang=ko-KR', '--window-size=1920,1080',
            ]}
            if proxy:
                launch_opts['proxy'] = {'server': proxy}

            browser = pw.chromium.launch(**launch_opts)
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/136.0.0.0 Safari/537.36'
                ),
                locale='ko-KR', timezone_id='Asia/Seoul',
            )
            apply_stealth_to_context(context)
            page = context.new_page()
            apply_stealth_to_page(page)

            try:
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except Exception:
                pass

            # Cloudflare 챌린지 대기 (최대 35초)
            deadline_cf = time.time() + 35
            turnstile_attempted = False
            while time.time() < deadline_cf:
                try:
                    title = page.title()
                    content = page.content()
                except Exception:
                    break

                is_challenge = (
                    'just a moment' in title.lower()
                    or 'checking your browser' in content.lower()
                    or 'cf-challenge' in content.lower()
                )
                if not is_challenge:
                    break

                # ── Turnstile 감지 시 API 서비스로 해결 시도 ──
                if not turnstile_attempted and 'turnstile' in content.lower():
                    turnstile_attempted = True
                    try:
                        import re as _re
                        m = _re.search(r'data-sitekey=["\']([0-9a-zA-Z_\-]{10,})["\']', content)
                        if m:
                            site_key = m.group(1)
                            logger.info('[Cloudflare] Turnstile 감지 → API 해결 시도: %s', site_key[:20])
                            from engine.captcha import get_solver
                            solver = get_solver()
                            if solver.available():
                                token = solver.solve_turnstile(site_key, url)
                                if token:
                                    import json as _json
                                    safe_token = _json.dumps(token)
                                    page.evaluate(f"""
                                        (() => {{
                                            const el = document.querySelector('[name="cf-turnstile-response"]');
                                            if (el) el.value = {safe_token};
                                            try {{
                                                if (window.turnstile) turnstile.implicitRender();
                                            }} catch(e) {{}}
                                        }})();
                                    """)
                                    logger.info('[Cloudflare] Turnstile 토큰 주입 완료')
                                    time.sleep(3)
                                    continue
                    except Exception as te:
                        logger.warning('[Cloudflare] Turnstile API 실패: %s', te)

                time.sleep(1.5)

            # 쿠키 추출
            nonlocal cf_cookies
            for c in context.cookies():
                if c['name'] in ('cf_clearance', '__cf_bm', '__cflb', '_cfuvid'):
                    cf_cookies[c['name']] = c['value']

            browser.close()

    try:
        _run_in_thread(_do_cloudflare, timeout=60)
    except concurrent.futures.TimeoutError:
        logger.warning('[Cloudflare] Playwright 스레드 타임아웃')

    if cf_cookies:
        _set_cached(domain, protection, cf_cookies)
        logger.info('[Cloudflare] ✅ 쿠키 획득: %s', list(cf_cookies.keys()))
    else:
        logger.warning('[Cloudflare] ⚠️ 쿠키 획득 실패')

    return cf_cookies


# ──────────────────────────────────────────────────────────────────────
#  통합 우회 API
# ──────────────────────────────────────────────────────────────────────

def get_bypass_cookies(url: str, protection: str = None,
                       proxy: Optional[str] = None,
                       force_refresh: bool = False) -> dict:
    """
    URL에 맞는 우회 쿠키를 자동으로 획득한다.

    Args:
        url: 대상 URL
        protection: 강제 지정 ('akamai'|'datadome'|'perimeterx'|'cloudflare')
                    None이면 자동 감지 시도 후 Akamai로 기본 처리
        proxy: 프록시 URL
        force_refresh: 캐시 무시

    Returns:
        {cookie_name: cookie_value, ...}
    """
    domain = urlparse(url).netloc

    # 보호 시스템 감지 (지정 없으면 캐시에서 힌트 찾기)
    if not protection:
        for p in _TTL:
            cached = _get_cached(domain, p)
            if cached:
                logger.debug('[bypass] 캐시에서 보호 시스템 감지: %s=%s', domain, p)
                return cached

    # 우회 함수 매핑
    handlers = {
        'akamai':     lambda: acquire_akamai_cookies(url, proxy, force_refresh),
        'datadome':   lambda: {'datadome': acquire_datadome_cookie(url, proxy, force_refresh)},
        'perimeterx': lambda: acquire_perimeterx_cookies(url, proxy, force_refresh),
        'cloudflare': lambda: acquire_cloudflare_cookies(url, proxy, force_refresh),
    }

    handler = handlers.get(protection or 'akamai')
    if handler:
        cookies = handler()
        cookies = {k: v for k, v in (cookies or {}).items() if v}
        return cookies

    return {}


def inject_cookies_to_session(session, cookies: dict, domain: str = None) -> None:
    """
    requests / tls-client 세션에 쿠키를 주입한다.
    session.cookies.set() 또는 session.cookies.update() 지원.
    """
    if not cookies:
        return
    for name, value in cookies.items():
        if not value:
            continue
        try:
            if domain:
                session.cookies.set(name, value, domain=domain)
            else:
                session.cookies.set(name, value)
        except Exception:
            try:
                session.cookies.update({name: value})
            except Exception as e:
                logger.debug('[cookie inject] %s=%s 실패: %s', name, value[:20], e)
