"""
scrapers/naver_shopping_scraper.py — 네이버쇼핑 스크래퍼 (ProCrawler 8신호 통합)
────────────────────────────────────────────────────────────────────────────────
우회 전략: engine/pro_crawler.py 위임 (8가지 신호 통합)
  1. IP 평판      → ip_manager
  2. 세션 나이     → session_manager (쿠키 예열)
  3. 쿠키 이력     → session_manager + bypass_engine
  4. TLS/HTTP2    → fingerprint_suite (curl_cffi Chrome 임포소네이션)
  5. 행동 패턴     → human_behavior (관성 스크롤 + 읽기 정지)
  6. 요청 속도     → rate_limiter (토큰 버킷)
  7. 탐색 경로     → navigation (naver.com → shopping → 검색)
  8. 지역 신호     → regional (ko-KR + Asia/Seoul)
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

import requests
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


def _extract_seller_name(node) -> str:
    selectors = [
        '[class*="mall_name"]',
        '[class*="basicList_mall"]',
        '[class*="product_mall"]',
        '[class*="seller"]',
        '[class*="store"]',
    ]
    bad_terms = ("원", "배송", "리뷰", "찜", "광고", "가격", "구매")
    for selector in selectors:
        found = node.select_one(selector)
        if not found:
            continue
        text = re.sub(r'\s+', ' ', found.get_text(' ', strip=True)).strip()
        text = re.sub(r'^(판매처|판매자|스토어|쇼핑몰|몰)\s*[:：>\-]?\s*', '', text).strip()
        if text and not re.search(r'\d{3,}', text) and not any(term in text for term in bad_terms):
            return text[:40]
    return ""


# ─── HTML 파싱 ────────────────────────────────────────────────────────────────

def _parse_naver_shopping_html(html: str, max_count: int) -> List[ProductResult]:
    soup     = BeautifulSoup(html, 'html.parser')
    results  = []
    seen_ids = set()

    item_selectors = [
        'li[class*="basicList_item"]',
        'li[class*="product_item"]',
        'div[class*="productCard"]',
        'li[class*="ProductCard"]',
        '[class*="basicList_list"] li',
        '[class*="product-list"] li',
    ]
    items = []
    for sel in item_selectors:
        try:
            found = soup.select(sel)
            if found:
                items = found
                logger.debug(f'[파싱] 셀렉터 "{sel}" → {len(found)}개')
                break
        except Exception:
            continue

    if items:
        for item in items:
            if len(results) >= max_count:
                break
            try:
                link_el = (
                    item.select_one('a[class*="product_link"]') or
                    item.select_one('a[class*="basicList_link"]') or
                    item.select_one('a[href*="shopping.naver.com/catalog"]') or
                    item.select_one('a[href*="nv_mid"]') or
                    item.select_one('a[href]')
                )
                if not link_el:
                    continue
                href = link_el.get('href', '')
                if not href:
                    continue
                if href.startswith('/'):
                    href = 'https://search.shopping.naver.com' + href

                nv_mid = None
                for pat in [r'nv_mid=(\d+)', r'/catalog/(\d+)', r'nvMid=(\d+)']:
                    m = re.search(pat, href)
                    if m:
                        nv_mid = m.group(1); break
                if not nv_mid or nv_mid in seen_ids:
                    continue
                seen_ids.add(nv_mid)

                title_el = (
                    item.select_one('[class*="basicList_title"]') or
                    item.select_one('[class*="product_title"]') or
                    item.select_one('[class*="ProductTitle"]') or
                    item.select_one('strong') or item.select_one('h3') or link_el
                )
                title = title_el.get_text(strip=True) if title_el else ''
                if not title or len(title) < 2:
                    title = item.get_text(separator=' ', strip=True)[:80]

                price_el = (
                    item.select_one('[class*="price_num"]') or
                    item.select_one('[class*="price_current"]') or
                    item.select_one('[class*="basicList_price"]')
                )
                price = '0'
                if price_el:
                    pn = re.sub(r'[^0-9]', '', price_el.get_text())
                    price = pn if pn else '0'
                if price == '0':
                    pm = re.search(r'(\d{3,3},\d{3}|\d{4,})\s*원', item.get_text())
                    if pm:
                        price = re.sub(r'[^0-9]', '', pm.group(1))

                img_el    = item.select_one('img')
                thumb_url = ''
                if img_el:
                    thumb_url = img_el.get('src') or img_el.get('data-src') or ''
                    if thumb_url.startswith('//'):
                        thumb_url = 'https:' + thumb_url

                product_url = f'https://search.shopping.naver.com/catalog/{nv_mid}'
                if 'smartstore.naver.com' in href:
                    product_url = href
                seller_name = _extract_seller_name(item)

                results.append(ProductResult(
                    id=f'naver_{nv_mid}', platform='네이버쇼핑',
                    title=title[:120], price=price,
                    product_url=product_url, thumbnail_url=thumb_url,
                    seller_name=seller_name or '네이버쇼핑',
                ))
            except Exception as e:
                logger.debug(f'[파싱] 항목 오류: {e}')
                continue

    # 전략 2: nv_mid 링크 직접 탐색
    if not results:
        for a in soup.find_all('a', href=re.compile(r'nv_mid=\d+|/catalog/\d+')):
            if len(results) >= max_count:
                break
            href  = a.get('href', '')
            m     = re.search(r'nv_mid=(\d+)|/catalog/(\d+)', href)
            if not m:
                continue
            nv_mid = m.group(1) or m.group(2)
            if nv_mid in seen_ids:
                continue
            seen_ids.add(nv_mid)

            container = (
                a.find_parent('li') or a.find_parent('article') or a.find_parent('div')
            )
            if not container:
                continue

            all_text = container.get_text(separator=' ', strip=True)
            pm       = re.search(r'(\d{1,3}(?:,\d{3})+|\d{4,})\s*원', all_text)
            price    = pm.group(1).replace(',', '') if pm else '0'

            title = ''
            for ca in container.find_all('a'):
                t = ca.get_text(strip=True)
                if 5 <= len(t) <= 150 and len(t) > len(title):
                    title = t
            if not title:
                title = re.sub(r'\s+', ' ', all_text).strip()[:80]

            thumb = ''
            img   = container.find('img')
            if img:
                thumb = img.get('src') or img.get('data-src') or ''
                if thumb.startswith('//'):
                    thumb = 'https:' + thumb

            results.append(ProductResult(
                id=f'naver_{nv_mid}', platform='네이버쇼핑',
                title=title[:120], price=price,
                product_url=f'https://search.shopping.naver.com/catalog/{nv_mid}',
                thumbnail_url=thumb,
                seller_name=_extract_seller_name(container) or '네이버쇼핑',
            ))

    return results


def _parse_naver_integrated(html: str, max_count: int) -> List[dict]:
    """search.naver.com 통합검색 HTML 파싱 (requests 폴백용)."""
    soup      = BeautifulSoup(html, 'html.parser')
    products  = []
    seen_mids = set()

    for a in soup.find_all('a', href=re.compile(r'nv_mid=\d+')):
        href    = a.get('href', '')
        mid_m   = re.search(r'nv_mid=(\d+)', href)
        if not mid_m:
            continue
        nv_mid  = mid_m.group(1)
        if nv_mid in seen_mids:
            continue
        seen_mids.add(nv_mid)

        container = a.find_parent('li') or a.find_parent('div') or a.find_parent('article')
        if not container:
            continue

        all_text = container.get_text(separator=' ', strip=True)
        price_m  = re.search(r'(\d{1,3}(?:,\d{3})+|\d{4,})\s*원', all_text)
        price    = price_m.group(1).replace(',', '') if price_m else '0'

        title = ''
        for child_a in container.find_all('a'):
            t = child_a.get_text(strip=True)
            if len(t) > len(title) and 5 <= len(t) <= 150:
                title = t
        if not title:
            title = re.sub(r'\s+', ' ', all_text).strip()[:80]

        thumb = ''
        img   = container.find('img')
        if img:
            thumb = img.get('src') or img.get('data-src') or ''
            if thumb.startswith('//'):
                thumb = 'https:' + thumb

        products.append({
            'nv_mid': nv_mid, 'title': title[:120], 'price': price,
            'product_url': f'https://search.shopping.naver.com/catalog/{nv_mid}',
            'thumbnail_url': thumb,
            'seller_name': _extract_seller_name(container) or '네이버쇼핑',
        })
        if len(products) >= max_count:
            break

    return products


# ─── ProCrawler 기반 검색 ────────────────────────────────────────────────────

def _scrape_naver_pro(query: str, max_count: int) -> List[ProductResult]:
    """
    ProCrawler로 네이버쇼핑 검색 (8신호 통합).
    naver.com → shopping.naver.com → 검색 결과 순서로 탐색.
    """
    search_url = (
        f'https://search.shopping.naver.com/search/all'
        f'?query={quote_plus(query)}&sort=rel'
    )

    try:
        from engine.pro_crawler import get_crawler
        crawler = get_crawler()
    except ImportError:
        return _scrape_naver_playwright_fallback(query, max_count)

    logger.info(f'[네이버쇼핑] ProCrawler 검색: "{query}"')

    # 1) SmartSession 시도
    resp = crawler.fetch(
        search_url,
        referer='https://shopping.naver.com/',
        platform='naver',
    )
    if resp and resp.ok:
        results = _parse_naver_shopping_html(resp.text, max_count)
        logger.info(f'[네이버쇼핑] SmartSession → {len(results)}개')
        if results:
            return results

    # 2) Playwright 폴백
    logger.info(f'[네이버쇼핑] Playwright 폴백: "{query}"')
    html = crawler.fetch_playwright(
        search_url,
        platform='naver',
        referer='https://shopping.naver.com/',
    )
    if not html:
        return []

    results = _parse_naver_shopping_html(html, max_count)
    logger.info(f'[네이버쇼핑] Playwright → {len(results)}개')

    if not results:
        dump = os.path.join(LOG_DIR, _safe_log_name('naver_fail', query))
        with open(dump, 'w', encoding='utf-8') as f:
            f.write(html)

    return results


# ─── 레거시 폴백 ─────────────────────────────────────────────────────────────

def _scrape_naver_playwright_fallback(query: str, max_count: int) -> List[ProductResult]:
    """ProCrawler import 실패 시 Playwright 단독 폴백."""
    try:
        from playwright.sync_api import sync_playwright
        from engine.stealth import get_full_stealth_script
    except ImportError:
        return []

    url = f'https://search.shopping.naver.com/search/all?query={quote_plus(query)}&sort=rel'
    logger.info(f'[네이버쇼핑] 레거시 Playwright 폴백: "{query}"')
    results = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage',
                      '--disable-blink-features=AutomationControlled', '--lang=ko-KR'],
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
                page.goto(url, wait_until='domcontentloaded', timeout=25000)
            except Exception: pass

            time.sleep(random.uniform(2.0, 3.5))
            for _ in range(4):
                page.mouse.wheel(0, random.randint(300, 600))
                time.sleep(random.uniform(0.3, 0.6))
            time.sleep(1.5)

            html = page.content()
            browser.close()

            results = _parse_naver_shopping_html(html, max_count)
            logger.info(f'[네이버쇼핑] 레거시 Playwright → {len(results)}개')

    except Exception as e:
        logger.error(f'[네이버쇼핑] 레거시 Playwright 오류: {e}')

    return results


def _scrape_with_requests_fallback(query: str, max_count: int) -> List[ProductResult]:
    """requests 최후 폴백."""
    url     = f'https://search.naver.com/search.naver?query={quote_plus(query)}&where=nexearch'
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept-Language': 'ko-KR,ko;q=0.9',
    }
    logger.info(f'[네이버쇼핑] requests 폴백: "{query}"')
    try:
        session = requests.Session()
        session.headers.update(headers)
        resp  = session.get(url, timeout=15)
        items = _parse_naver_integrated(resp.text, max_count)
        return [ProductResult(
            id=f'naver_{it["nv_mid"]}', platform='네이버쇼핑',
            title=it['title'], price=it['price'],
            product_url=it['product_url'], thumbnail_url=it['thumbnail_url'],
            seller_name=it.get('seller_name', '') or '네이버쇼핑',
        ) for it in items]
    except Exception as e:
        logger.error(f'[네이버쇼핑] requests 오류: {e}')
        return []


# ─── 스크래퍼 클래스 ─────────────────────────────────────────────────────────

class NaverShoppingScraper(BaseScraper):
    def __init__(self):
        super().__init__()
        self.platform_name = '네이버쇼핑'

    def _scrape_sync(self, keyword: str) -> List[ProductResult]:
        queries = _build_queries(keyword)
        logger.info(f'[네이버쇼핑] 쿼리 목록: {queries}')

        seen_ids:    set           = set()
        all_results: List[ProductResult] = []

        for query in queries:
            if len(all_results) >= config.MAX_CANDIDATES:
                break

            # 1순위: ProCrawler (8신호 통합)
            results = _scrape_naver_pro(query, config.MAX_CANDIDATES)

            # 최후 폴백: requests
            if not results:
                logger.info('[네이버쇼핑] 모든 방법 실패 → requests 최후 폴백')
                results = _scrape_with_requests_fallback(query, config.MAX_CANDIDATES)

            for r in results:
                if r.id not in seen_ids:
                    seen_ids.add(r.id)
                    all_results.append(r)
                    if len(all_results) >= config.MAX_CANDIDATES:
                        break

            if len(all_results) >= 3:
                break

        logger.info(f'[네이버쇼핑] 최종 {len(all_results)}개 수집')
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
