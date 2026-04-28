"""
scrapers/coupang_scraper.py — 쿠팡 스크래퍼 (ProCrawler 8신호 통합)
────────────────────────────────────────────────────────────────────────────
우회 전략 (engine/pro_crawler.py 위임):
  1. IP 평판      : 프록시 로테이션 (ip_manager)
  2. 세션 나이     : 쿠키 예열 세션 재사용 (session_manager)
  3. 쿠키 이력     : 도메인별 쿠키 축적 (session_manager + bypass_engine)
  4. TLS/HTTP2    : Chrome 124 JA3/JA4 + H2 SETTINGS (fingerprint_suite)
  5. 행동 패턴     : 베지어 마우스 + 관성 스크롤 + 읽기 정지 (human_behavior)
  6. 요청 속도     : 토큰 버킷 + 적응형 백오프 (rate_limiter)
  7. 탐색 경로     : 메인 → 검색 경로 시뮬레이션 (navigation)
  8. 지역 신호     : 한국 로케일 + KR 쿠키 (regional)
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from engines.text_matcher import extract_keywords
from scrapers.base_scraper import BaseScraper, ProductResult, download_image_sync

logger = logging.getLogger(__name__)

LOG_DIR = os.path.join(str(config.BASE_DIR), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)


# ─── 유틸 ────────────────────────────────────────────────────────────────────

def _safe_log_name(prefix: str, query: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', query).strip(' ._')
    cleaned = re.sub(r'\s+', '_', cleaned)[:40] or 'query'
    digest  = hashlib.sha1(query.encode('utf-8', errors='ignore')).hexdigest()[:8]
    return f'{prefix}_{cleaned}_{digest}.html'


def _build_queries(keyword: str) -> List[str]:
    queries = [keyword]
    kws     = extract_keywords(keyword)
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


# ─── HTML 파싱 ────────────────────────────────────────────────────────────────

def _parse_coupang_html(html: str, max_count: int) -> List[ProductResult]:
    soup    = BeautifulSoup(html, 'html.parser')
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
    for sel in selector_strategies:
        try:
            found = soup.select(sel)
            if found:
                items = found
                logger.debug(f"[쿠팡] 셀렉터 '{sel}' → {len(found)}개")
                break
        except Exception:
            continue

    if not items:
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

            if texts[-1].upper() in ('AD', '광고', 'ADVERTISEMENT'):
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
                            price = int(nums); break
                        except Exception:
                            pass
            if price == 0:
                m = re.search(r'(\d{3},\d{3})', item.get_text())
                if m:
                    try: price = int(m.group(1).replace(',', ''))
                    except Exception: pass

            img_el    = item.select_one('img[src], img[data-src]')
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
                    item_link = href; break
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
                id=prod_id, platform="쿠팡", title=title,
                price=str(price), product_url=item_link, thumbnail_url=thumb_url,
            ))
        except Exception as e:
            logger.debug(f"[쿠팡] 파싱 오류: {e}")

    return results


# ─── ProCrawler 기반 검색 ────────────────────────────────────────────────────

def _scrape_coupang_pro(query: str, max_count: int) -> List[ProductResult]:
    """
    ProCrawler로 쿠팡 검색 (8신호 통합).
    1순위: SmartSession (TLS+쿠키)
    2순위: Playwright (JS 렌더링)
    """
    search_url = (
        f'https://www.coupang.com/np/search'
        f'?q={quote_plus(query)}&channel=user&sorter=bestAsc'
    )

    try:
        from engine.pro_crawler import get_crawler
        crawler = get_crawler()
    except ImportError:
        return _scrape_coupang_playwright_fallback(query, max_count)

    logger.info(f'[쿠팡] ProCrawler 검색: "{query}"')

    # requests 시도
    resp = crawler.fetch(
        search_url,
        referer='https://www.coupang.com/',
        platform='coupang',
    )

    if resp and resp.ok:
        results = _parse_coupang_html(resp.text, max_count)
        logger.info(f'[쿠팡] SmartSession → {len(results)}개')
        if results:
            return results

    # Playwright 폴백
    logger.info(f'[쿠팡] Playwright 폴백: "{query}"')
    html = crawler.fetch_playwright(
        search_url,
        platform='coupang',
        referer='https://www.coupang.com/',
    )
    if not html:
        return []

    results = _parse_coupang_html(html, max_count)
    logger.info(f'[쿠팡] Playwright → {len(results)}개')

    if not results:
        dump = os.path.join(LOG_DIR, _safe_log_name('coupang_fail', query))
        with open(dump, 'w', encoding='utf-8') as f:
            f.write(html)

    return results


# ─── 레거시 Playwright 폴백 (ProCrawler import 실패 시) ───────────────────────

def _scrape_coupang_playwright_fallback(query: str, max_count: int) -> List[ProductResult]:
    """ProCrawler 없이 Playwright만으로 검색 (구형 폴백)."""
    try:
        from playwright.sync_api import sync_playwright
        from engine.stealth import get_full_stealth_script
    except ImportError as e:
        logger.warning(f'[쿠팡] Playwright 미설치: {e}')
        return []

    search_url = (
        f'https://www.coupang.com/np/search'
        f'?q={quote_plus(query)}&channel=user&sorter=bestAsc'
    )
    logger.info(f'[쿠팡] 레거시 Playwright 폴백: "{query}"')
    results = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=['--no-sandbox','--disable-dev-shm-usage',
                      '--disable-blink-features=AutomationControlled',
                      '--lang=ko-KR','--window-size=1920,1080'],
            )
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
                locale='ko-KR', timezone_id='Asia/Seoul',
            )
            page = context.new_page()
            page.add_init_script(get_full_stealth_script())

            try:
                page.goto('https://www.coupang.com', wait_until='domcontentloaded', timeout=20000)
            except Exception: pass
            time.sleep(random.uniform(2.0, 3.5))
            for _ in range(random.randint(2, 4)):
                page.mouse.wheel(0, random.randint(200, 500))
                time.sleep(random.uniform(0.4, 0.8))
            time.sleep(random.uniform(1.0, 2.0))

            try:
                page.goto(search_url, wait_until='domcontentloaded', timeout=30000)
            except Exception as eg:
                logger.warning(f'[쿠팡] goto 실패: {eg}')
                browser.close()
                return []

            time.sleep(random.uniform(3.0, 4.5))
            html = page.content()
            if 'Access Denied' in html or len(html) < 2000:
                time.sleep(5)
                try:
                    page.goto(search_url, wait_until='domcontentloaded', timeout=25000)
                    time.sleep(3.0)
                    html = page.content()
                except Exception: pass

            for _ in range(6):
                page.mouse.wheel(0, random.randint(300, 600))
                time.sleep(random.uniform(0.3, 0.6))
            html = page.content()
            browser.close()

            results = _parse_coupang_html(html, max_count)
            logger.info(f'[쿠팡] 레거시 Playwright → {len(results)}개')

    except Exception as e:
        logger.error(f'[쿠팡] 레거시 Playwright 오류: {e}')

    return results


# ─── 스크래퍼 클래스 ─────────────────────────────────────────────────────────

class CoupangScraper(BaseScraper):
    def __init__(self):
        super().__init__()
        self.platform_name = '쿠팡'

    def _scrape_sync(self, keyword: str) -> List[ProductResult]:
        queries = _build_queries(keyword)
        logger.info(f'[쿠팡] 쿼리 목록: {queries}')

        seen_ids:    set           = set()
        all_results: List[ProductResult] = []

        for query in queries:
            if len(all_results) >= config.MAX_CANDIDATES:
                break

            results = _scrape_coupang_pro(query, config.MAX_CANDIDATES)

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
        loop    = asyncio.get_running_loop()
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
