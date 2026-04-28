"""
scrapers/coupang_scraper.py - 쿠팡 스크래퍼 (bypass_engine + Playwright 스텔스)
────────────────────────────────────────────────────────────────────────────────
Akamai Bot Manager 3.0 우회 전략:
1. bypass_engine.get_bypass_cookies('akamai') → _abck 쿠키 획득 + 검증
2. SmartSession (tls-client Chrome JA3) + bypass 쿠키로 requests 시도
3. requests 실패 시 → Playwright 스텔스 폴백 (메인 방문 → 검색)
4. CAPTCHA 감지 시 CaptchaSolver 자동 주입
"""
import asyncio
import hashlib
import logging
import os
import random
import re
import sys
import time
from typing import List
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

# 상위 디렉토리 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from engines.text_matcher import extract_keywords
from scrapers.base_scraper import BaseScraper, ProductResult, download_image_sync

logger = logging.getLogger(__name__)

LOG_DIR = os.path.join(str(config.BASE_DIR), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)


def _safe_log_name(prefix: str, query: str) -> str:
    """Windows에서 안전한 실패 로그 파일명을 만든다."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', query).strip(' ._')
    cleaned = re.sub(r'\s+', '_', cleaned)[:40] or 'query'
    digest = hashlib.sha1(query.encode('utf-8', errors='ignore')).hexdigest()[:8]
    return f'{prefix}_{cleaned}_{digest}.html'


def _build_queries(keyword: str) -> List[str]:
    queries = [keyword]
    kws = extract_keywords(keyword)
    if kws:
        q2 = ' '.join(kws)
        if q2 != keyword:
            queries.append(q2)
        core = [w for w in kws if len(w) >= 3]
        if core:
            q3 = ' '.join(core[:4])
            if q3 != q2:
                queries.append(q3)
    return queries


def _parse_coupang_html(html: str, max_count: int) -> List[ProductResult]:
    """BeautifulSoup 다중 셀렉터 파싱."""
    soup = BeautifulSoup(html, 'html.parser')
    results: List[ProductResult] = []

    selector_strategies = [
        'li[class*="ProductUnit_productUnit"]',
        'li[class*="productUnit"]',
        'li[class*="search-product"]',
        'li.search-product',
        'div[class*="ProductCard"]',
        'li[class*="ProductCard"]',
        'article[class*="product"]',
        'div[class*="searchResult"] li',
    ]

    items = []
    for selector in selector_strategies:
        try:
            found = soup.select(selector)
            if found:
                items = found
                logger.debug(f"[쿠팡] 셀렉터 '{selector}' → {len(found)}개")
                break
        except Exception:
            continue

    if not items:
        logger.debug("[쿠팡] URL 패턴 기반 파싱")
        for a in soup.find_all('a', href=True):
            href = a.get('href', '')
            if '/vp/products/' in href or '/products/' in href:
                container = (
                    a.find_parent('li') or a.find_parent('article') or a.find_parent('div')
                )
                if container and container not in items:
                    items.append(container)
            if len(items) >= max_count:
                break

    for item in items:
        if len(results) >= max_count:
            break
        try:
            texts = list(item.stripped_strings)
            if not texts:
                continue

            last = texts[-1].upper()
            if last in ('AD', '광고', 'ADVERTISEMENT'):
                continue

            title = texts[0][:120]
            if len(title) < 3:
                continue

            price = 0
            for t in texts[1:]:
                if '원' in t and any(c.isdigit() for c in t):
                    nums = re.sub(r'[^0-9]', '', t)
                    if nums:
                        try:
                            price = int(nums)
                            break
                        except Exception:
                            pass
            if price == 0:
                all_text = item.get_text()
                m = re.search(r'(\d{3,3},\d{3})', all_text)
                if m:
                    try:
                        price = int(m.group(1).replace(',', ''))
                    except Exception:
                        pass

            img_el = item.select_one('img[src], img[data-src]')
            if not img_el:
                continue
            thumb_url = img_el.get('src') or img_el.get('data-src') or ''
            if thumb_url.startswith('//'):
                thumb_url = 'https:' + thumb_url
            if not thumb_url or 'data:image' in thumb_url:
                continue

            item_link = ''
            for a in item.find_all('a', href=True):
                href = a.get('href', '')
                if '/vp/products/' in href or '/products/' in href:
                    item_link = href
                    break
            if not item_link:
                a_tag = item.select_one('a[href]')
                if a_tag:
                    item_link = a_tag.get('href', '')
            if not item_link:
                continue
            if item_link.startswith('/'):
                item_link = 'https://www.coupang.com' + item_link

            prod_id = f"coupang_{random.randint(100000, 999999)}"
            m = re.search(r'products/(\d+)', item_link)
            if m:
                prod_id = f"coupang_{m.group(1)}"

            results.append(ProductResult(
                id=prod_id,
                platform="쿠팡",
                title=title,
                price=str(price),
                product_url=item_link,
                thumbnail_url=thumb_url,
            ))
        except Exception as e:
            logger.debug(f"[쿠팡] 항목 오류: {e}")

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 방법 1: bypass_engine + SmartSession (TLS 지문 위장 requests)
# ══════════════════════════════════════════════════════════════════════════════

def _scrape_coupang_smart_session(query: str, max_count: int) -> List[ProductResult]:
    """
    bypass_engine으로 Akamai 우회 쿠키를 획득한 후
    SmartSession (tls-client Chrome JA3) 으로 검색 결과를 가져온다.
    """
    search_url = (
        f'https://www.coupang.com/np/search'
        f'?q={quote_plus(query)}&channel=user&sorter=bestAsc'
    )

    try:
        from engine.bypass_engine import get_bypass_cookies, is_blocked, detect_protection
        from engine.smart_session import SmartSession
    except ImportError as e:
        logger.warning(f'[쿠팡] bypass_engine/smart_session 미설치: {e}')
        return []

    logger.info(f'[쿠팡] SmartSession 검색 시도: "{query}"')

    try:
        # 1) Akamai 우회 쿠키 획득
        bypass_cookies = get_bypass_cookies(
            'https://www.coupang.com',
            protection='akamai',
        )
        logger.debug(f'[쿠팡] bypass 쿠키: {list(bypass_cookies.keys())}')

        with SmartSession() as sess:
            sess.inject_bypass_cookies(bypass_cookies, 'https://www.coupang.com')
            sess.update_cookies({'lang': 'ko-KR'})

            # Referer 추가
            sess._session.headers.update({
                'Referer': 'https://www.coupang.com/',
                'Accept-Language': 'ko-KR,ko;q=0.9',
            })

            resp = sess.get(search_url)

            if resp.status_code not in (200, 206):
                logger.warning(f'[쿠팡] SmartSession HTTP {resp.status_code}')
                return []

            html = resp.text

            # 차단 감지
            if is_blocked(html, resp.status_code):
                protection = detect_protection(html, resp.headers)
                logger.warning(f'[쿠팡] SmartSession 차단 감지 ({protection})')
                # 차단된 경우 캐시 무효화 후 재시도
                bypass_cookies = get_bypass_cookies(
                    'https://www.coupang.com',
                    protection='akamai',
                    force_refresh=True,
                )
                sess.inject_bypass_cookies(bypass_cookies, 'https://www.coupang.com')
                resp = sess.get(search_url)
                html = resp.text

                if is_blocked(html, resp.status_code):
                    logger.warning('[쿠팡] SmartSession 재시도 후에도 차단')
                    return []

            results = _parse_coupang_html(html, max_count)
            logger.info(f'[쿠팡] SmartSession → {len(results)}개 추출')

            if not results:
                dump_path = os.path.join(LOG_DIR, _safe_log_name('coupang_smart_fail', query))
                with open(dump_path, 'w', encoding='utf-8') as f:
                    f.write(html)

            return results

    except Exception as e:
        logger.warning(f'[쿠팡] SmartSession 오류: {e}')
        return []


# ══════════════════════════════════════════════════════════════════════════════
# 방법 2: Playwright 스텔스 폴백
# ══════════════════════════════════════════════════════════════════════════════

def _scrape_coupang_playwright(query: str, max_count: int) -> List[ProductResult]:
    """
    Playwright + 스텔스로 쿠팡 검색 (SmartSession 실패 시 폴백).
    Akamai 우회: 메인 페이지 먼저 방문 → 세션 신뢰도 쌓기 → 검색
    """
    try:
        from playwright.sync_api import sync_playwright
        from engine.stealth import get_full_stealth_script
    except ImportError as e:
        logger.warning(f'[쿠팡] Playwright/스텔스 미설치: {e}')
        return []

    search_url = f'https://www.coupang.com/np/search?q={quote_plus(query)}&channel=user&sorter=bestAsc'
    logger.info(f'[쿠팡] Playwright 스텔스 폴백: "{query}"')

    results = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled',
                    '--lang=ko-KR',
                    '--window-size=1920,1080',
                    '--disable-web-security',
                    '--allow-running-insecure-content',
                ],
            )
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
                locale='ko-KR',
                timezone_id='Asia/Seoul',
                extra_http_headers={
                    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
                    'sec-ch-ua-mobile': '?0',
                    'sec-ch-ua-platform': '"Windows"',
                },
            )
            page = context.new_page()

            # ProScraper 풀 스텔스 주입
            page.add_init_script(get_full_stealth_script())

            # ── Step 1: 메인 페이지 방문 (Akamai 세션 쿠키 획득) ──
            logger.info('[쿠팡] 메인 페이지 방문 중...')
            try:
                page.goto('https://www.coupang.com', wait_until='domcontentloaded', timeout=20000)
            except Exception:
                pass

            time.sleep(random.uniform(2.0, 3.5))

            # 인간다운 스크롤 (메인 페이지 탐색 흉내)
            for _ in range(random.randint(2, 4)):
                page.mouse.wheel(0, random.randint(200, 500))
                time.sleep(random.uniform(0.4, 0.8))
            page.mouse.wheel(0, -random.randint(100, 300))
            time.sleep(random.uniform(1.0, 2.0))

            # ── Step 2: 검색 페이지로 이동 ──
            logger.info(f'[쿠팡] 검색 페이지 이동: "{query}"')
            try:
                page.goto(search_url, wait_until='domcontentloaded', timeout=30000)
            except Exception as eg:
                logger.warning(f'[쿠팡] 검색 페이지 goto 실패: {eg}')
                browser.close()
                return []

            time.sleep(random.uniform(3.0, 4.5))

            # CAPTCHA 자동 해결 시도
            try:
                from engine.captcha import get_solver
                solver = get_solver()
                if solver.available():
                    solved = solver.auto_solve_page(page)
                    if solved:
                        logger.info('[쿠팡] Playwright CAPTCHA 자동 해결 완료')
                        time.sleep(2.0)
            except Exception:
                pass

            # 차단 감지
            html = page.content()
            if 'Access Denied' in html or len(html) < 2000:
                logger.warning(f'[쿠팡] Access Denied 감지 (길이: {len(html)})')
                dump_path = os.path.join(LOG_DIR, _safe_log_name('coupang_block', query))
                with open(dump_path, 'w', encoding='utf-8') as f:
                    f.write(html)
                # 5초 대기 후 재시도
                time.sleep(5)
                try:
                    page.goto(search_url, wait_until='domcontentloaded', timeout=25000)
                    time.sleep(3.0)
                    html = page.content()
                except Exception:
                    pass

            if 'Access Denied' in html and len(html) < 2000:
                logger.error('[쿠팡] 재시도 후에도 차단됨')
                browser.close()
                return []

            # 스크롤로 lazy-load 유발
            for _ in range(6):
                page.mouse.wheel(0, random.randint(300, 600))
                time.sleep(random.uniform(0.3, 0.6))
            time.sleep(1.0)
            html = page.content()

            browser.close()

            results = _parse_coupang_html(html, max_count)
            logger.info(f'[쿠팡] Playwright → {len(results)}개 추출')

            if not results:
                dump_path = os.path.join(LOG_DIR, _safe_log_name('coupang_fail', query))
                with open(dump_path, 'w', encoding='utf-8') as f:
                    f.write(html)

    except Exception as e:
        logger.error(f'[쿠팡] Playwright 오류: {e}')

    return results


# ══════════════════════════════════════════════════════════════════════════════
# CoupangScraper
# ══════════════════════════════════════════════════════════════════════════════

class CoupangScraper(BaseScraper):
    def __init__(self):
        super().__init__()
        self.platform_name = '쿠팡'

    def _scrape_sync(self, keyword: str) -> List[ProductResult]:
        queries = _build_queries(keyword)
        logger.info(f'[쿠팡] 쿼리 목록: {queries}')

        seen_ids = set()
        all_results: List[ProductResult] = []

        for query in queries:
            if len(all_results) >= config.MAX_CANDIDATES:
                break

            # 방법 1: SmartSession + bypass_engine (빠름)
            results = _scrape_coupang_smart_session(query, config.MAX_CANDIDATES)

            # 방법 2: Playwright 스텔스 폴백 (느리지만 강력)
            if not results:
                logger.info(f'[쿠팡] SmartSession 실패 → Playwright 폴백: "{query}"')
                results = _scrape_coupang_playwright(query, config.MAX_CANDIDATES)

            for r in results:
                if r.id not in seen_ids:
                    seen_ids.add(r.id)
                    all_results.append(r)
                    if len(all_results) >= config.MAX_CANDIDATES:
                        break

            if len(all_results) >= 5:
                break

        logger.info(f'[쿠팡] 최종 {len(all_results)}개 수집')
        return all_results

    async def search(self, keyword: str) -> List[ProductResult]:
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, self._scrape_sync, keyword)

        for res in results:
            if res.thumbnail_url:
                local_path = os.path.join(str(config.THUMBNAIL_DIR), f'{res.id}.jpg')
                ok = await loop.run_in_executor(
                    None, download_image_sync, res.thumbnail_url, local_path
                )
                if ok:
                    res.local_thumbnail_path = local_path

        return results
