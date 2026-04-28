"""
scrapers/naver_shopping_scraper.py - 네이버쇼핑 스크래퍼 (bypass_engine + ProScraper 스텔스)
────────────────────────────────────────────────────────────────────────────────────────────
개선된 전략:
1. [PRIMARY]   SmartSession (tls-client) → SSR 응답 + 상품 파싱
2. [SECONDARY] Playwright + 스텔스 → search.shopping.naver.com (JS 렌더링)
3. [FALLBACK]  requests → search.naver.com 통합검색 (빠르지만 결과 제한적)

SmartSession으로 빠르게 시도하고, JS 렌더링이 필요하면 Playwright로 폴백.
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

# 상위 디렉토리 추가 (engine, config import용)
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

# requests 기본 헤더
_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}


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


# ─── SmartSession 스크래퍼 (1순위: 빠름) ─────────────────────

def _scrape_with_smart_session(query: str, max_count: int) -> List[ProductResult]:
    """
    SmartSession (tls-client Chrome JA3) 으로 네이버쇼핑 SSR 응답 파싱.
    JS 렌더링 없이 상품 데이터가 HTML에 포함된 경우 유효하다.
    """
    search_url = (
        f'https://search.shopping.naver.com/search/all'
        f'?query={quote_plus(query)}&sort=rel'
    )

    try:
        from engine.smart_session import SmartSession
        from engine.bypass_engine import is_blocked
    except ImportError:
        return []

    logger.info(f'[네이버쇼핑] SmartSession 검색 시도: "{query}"')

    try:
        with SmartSession() as sess:
            sess._session.headers.update({
                'Referer': 'https://www.naver.com/',
                'Accept-Language': 'ko-KR,ko;q=0.9',
            })
            resp = sess.get(search_url)

            if resp.status_code not in (200, 206):
                logger.debug(f'[네이버쇼핑] SmartSession HTTP {resp.status_code}')
                return []

            html = resp.text

            if is_blocked(html, resp.status_code):
                logger.debug('[네이버쇼핑] SmartSession 차단 감지')
                return []

            results = _parse_naver_shopping_html(html, max_count)
            if results:
                logger.info(f'[네이버쇼핑] SmartSession → {len(results)}개 추출')
            return results

    except Exception as e:
        logger.debug(f'[네이버쇼핑] SmartSession 오류: {e}')
        return []


# ─── Playwright 스텔스 스크래퍼 (2순위) ──────────────────────

def _scrape_with_playwright(query: str, max_count: int) -> List[ProductResult]:
    """
    Playwright + ProScraper 스텔스로 네이버쇼핑 목록 크롤링.
    search.shopping.naver.com을 직접 타겟으로 한다.
    """
    try:
        from playwright.sync_api import sync_playwright
        from engine.stealth import get_full_stealth_script
    except ImportError as e:
        logger.warning(f'[네이버쇼핑] Playwright/스텔스 미설치: {e}')
        return []

    url = f'https://search.shopping.naver.com/search/all?query={quote_plus(query)}&sort=rel'
    logger.info(f'[네이버쇼핑] Playwright 스텔스 검색: "{query}"')

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
                },
            )
            page = context.new_page()

            # ProScraper 스텔스 스크립트 주입
            page.add_init_script(get_full_stealth_script())

            # 페이지 로드
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=25000)
            except Exception:
                try:
                    page.goto(url, wait_until='commit', timeout=20000)
                except Exception as eg:
                    logger.warning(f'[네이버쇼핑] goto 실패: {eg}')
                    browser.close()
                    return []

            # 자연스러운 대기 (JS 렌더링)
            time.sleep(random.uniform(2.0, 3.5))

            # CAPTCHA 자동 해결 시도
            try:
                from engine.captcha import get_solver
                solver = get_solver()
                if solver.available():
                    solved = solver.auto_solve_page(page)
                    if solved:
                        logger.info('[네이버쇼핑] Playwright CAPTCHA 자동 해결 완료')
                        time.sleep(2.0)
            except Exception:
                pass

            # 인간다운 스크롤로 lazy-load 유발
            for _ in range(4):
                page.mouse.wheel(0, random.randint(300, 600))
                time.sleep(random.uniform(0.3, 0.6))
            time.sleep(1.5)

            html = page.content()
            logger.info(f'[네이버쇼핑] HTML 길이: {len(html)}')

            # 차단 감지
            if '서비스 접속이 일시적으로 제한' in html or len(html) < 5000:
                logger.warning('[네이버쇼핑] 차단 또는 빈 페이지 감지')
                dump_path = os.path.join(LOG_DIR, _safe_log_name('naver_pw_block', query))
                with open(dump_path, 'w', encoding='utf-8') as f:
                    f.write(html)
                browser.close()
                return []

            browser.close()

            # 파싱
            results = _parse_naver_shopping_html(html, max_count)
            logger.info(f'[네이버쇼핑] Playwright → {len(results)}개 추출')

    except Exception as e:
        logger.error(f'[네이버쇼핑] Playwright 오류: {e}')

    return results


def _parse_naver_shopping_html(html: str, max_count: int) -> List[ProductResult]:
    """
    네이버쇼핑 search.shopping.naver.com 페이지 파싱.
    CSS Module 클래스명 변화에 대응하는 다중 전략.
    """
    soup = BeautifulSoup(html, 'html.parser')
    results = []
    seen_ids = set()

    # 전략 1: 상품 카드 컨테이너 (네이버쇼핑 basicList 구조)
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
                # 링크 / 상품 ID 추출
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

                # nv_mid 또는 catalog ID 추출
                nv_mid = None
                m = re.search(r'nv_mid=(\d+)', href)
                if m:
                    nv_mid = m.group(1)
                else:
                    m2 = re.search(r'/catalog/(\d+)', href)
                    if m2:
                        nv_mid = m2.group(1)
                    else:
                        m3 = re.search(r'nvMid=(\d+)', href)
                        if m3:
                            nv_mid = m3.group(1)
                if not nv_mid:
                    continue
                if nv_mid in seen_ids:
                    continue
                seen_ids.add(nv_mid)

                # 제목
                title_el = (
                    item.select_one('[class*="basicList_title"]') or
                    item.select_one('[class*="product_title"]') or
                    item.select_one('[class*="ProductTitle"]') or
                    item.select_one('strong') or
                    item.select_one('h3') or
                    link_el
                )
                title = title_el.get_text(strip=True) if title_el else ''
                if not title or len(title) < 2:
                    title = item.get_text(separator=' ', strip=True)[:80]

                # 가격
                price_el = (
                    item.select_one('[class*="price_num"]') or
                    item.select_one('[class*="price_current"]') or
                    item.select_one('[class*="basicList_price"]')
                )
                price = '0'
                if price_el:
                    price_text = re.sub(r'[^0-9]', '', price_el.get_text())
                    price = price_text if price_text else '0'
                if price == '0':
                    all_text = item.get_text()
                    pm = re.search(r'(\d{3,3},\d{3}|\d{4,})\s*원', all_text)
                    if pm:
                        price = re.sub(r'[^0-9]', '', pm.group(1))

                # 이미지
                img_el = item.select_one('img')
                thumb_url = ''
                if img_el:
                    thumb_url = img_el.get('src') or img_el.get('data-src') or ''
                    if thumb_url.startswith('//'):
                        thumb_url = 'https:' + thumb_url

                # 상품 URL
                product_url = f'https://search.shopping.naver.com/catalog/{nv_mid}'
                # smartstore 링크이면 그걸 사용
                if 'smartstore.naver.com' in href:
                    product_url = href

                results.append(ProductResult(
                    id=f'naver_{nv_mid}',
                    platform='네이버쇼핑',
                    title=title[:120],
                    price=price,
                    product_url=product_url,
                    thumbnail_url=thumb_url,
                ))
            except Exception as e:
                logger.debug(f'[파싱] 항목 오류: {e}')
                continue

    # 전략 2: nv_mid 링크 직접 탐색 (전략 1 실패 시)
    if not results:
        logger.debug('[파싱] 전략 2 (nv_mid 직접 탐색)')
        for a in soup.find_all('a', href=re.compile(r'nv_mid=\d+|/catalog/\d+')):
            if len(results) >= max_count:
                break
            href = a.get('href', '')
            m = re.search(r'nv_mid=(\d+)|/catalog/(\d+)', href)
            if not m:
                continue
            nv_mid = m.group(1) or m.group(2)
            if nv_mid in seen_ids:
                continue
            seen_ids.add(nv_mid)

            container = (a.find_parent('li') or a.find_parent('article') or
                         a.find_parent('div'))
            if not container:
                continue

            all_text = container.get_text(separator=' ', strip=True)
            pm = re.search(r'(\d{1,3}(?:,\d{3})+|\d{4,})\s*원', all_text)
            price = pm.group(1).replace(',', '') if pm else '0'

            title = ''
            for ca in container.find_all('a'):
                t = ca.get_text(strip=True)
                if 5 <= len(t) <= 150 and len(t) > len(title):
                    title = t
            if not title:
                title = re.sub(r'\s+', ' ', all_text).strip()[:80]

            thumb = ''
            img = container.find('img')
            if img:
                thumb = img.get('src') or img.get('data-src') or ''
                if thumb.startswith('//'):
                    thumb = 'https:' + thumb

            results.append(ProductResult(
                id=f'naver_{nv_mid}',
                platform='네이버쇼핑',
                title=title[:120],
                price=price,
                product_url=f'https://search.shopping.naver.com/catalog/{nv_mid}',
                thumbnail_url=thumb,
            ))

    return results


# ─── requests 폴백 (2순위) ────────────────────────────────────

def _parse_naver_integrated(html: str, max_count: int) -> List[dict]:
    """search.naver.com 통합검색 HTML에서 상품 파싱 (폴백용)."""
    soup = BeautifulSoup(html, 'html.parser')
    products = []
    seen_mids = set()

    for a in soup.find_all('a', href=re.compile(r'nv_mid=\d+')):
        href = a.get('href', '')
        mid_m = re.search(r'nv_mid=(\d+)', href)
        if not mid_m:
            continue
        nv_mid = mid_m.group(1)
        if nv_mid in seen_mids:
            continue
        seen_mids.add(nv_mid)

        container = a.find_parent('li') or a.find_parent('div') or a.find_parent('article')
        if not container:
            continue

        all_text = container.get_text(separator=' ', strip=True)
        price_m = re.search(r'(\d{1,3}(?:,\d{3})+|\d{4,})\s*원', all_text)
        price = price_m.group(1).replace(',', '') if price_m else '0'

        title = ''
        for child_a in container.find_all('a'):
            t = child_a.get_text(strip=True)
            if len(t) > len(title) and 5 <= len(t) <= 150:
                title = t
        if not title:
            title = re.sub(r'\s+', ' ', all_text).strip()[:80]

        thumb = ''
        img = container.find('img')
        if img:
            thumb = img.get('src') or img.get('data-src') or ''
            if thumb.startswith('//'):
                thumb = 'https:' + thumb

        products.append({
            'nv_mid': nv_mid, 'title': title[:120], 'price': price,
            'product_url': f'https://search.shopping.naver.com/catalog/{nv_mid}',
            'thumbnail_url': thumb,
        })
        if len(products) >= max_count:
            break

    return products


def _scrape_with_requests(query: str, max_count: int) -> List[ProductResult]:
    """requests 기반 네이버 통합검색 폴백."""
    url = f'https://search.naver.com/search.naver?query={quote_plus(query)}&where=nexearch&sm=top_hty'
    logger.info(f'[네이버쇼핑] requests 폴백 검색: "{query}"')

    session = requests.Session()
    session.headers.update(_HEADERS)
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            return []

        items = _parse_naver_integrated(resp.text, max_count)
        if not items:
            dump_path = os.path.join(LOG_DIR, _safe_log_name('naver_fail', query))
            with open(dump_path, 'w', encoding='utf-8') as f:
                f.write(resp.text)
            return []

        return [ProductResult(
            id=f'naver_{item["nv_mid"]}',
            platform='네이버쇼핑',
            title=item['title'],
            price=item['price'],
            product_url=item['product_url'],
            thumbnail_url=item['thumbnail_url'],
        ) for item in items]

    except Exception as e:
        logger.error(f'[네이버쇼핑] requests 오류: {e}')
        return []


# ─── 메인 스크래퍼 클래스 ─────────────────────────────────────

class NaverShoppingScraper(BaseScraper):
    def __init__(self):
        super().__init__()
        self.platform_name = '네이버쇼핑'

    def _scrape_sync(self, keyword: str) -> List[ProductResult]:
        queries = _build_queries(keyword)
        logger.info(f'[네이버쇼핑] 쿼리 목록: {queries}')

        seen_ids = set()
        all_results: List[ProductResult] = []

        for query in queries:
            if len(all_results) >= config.MAX_CANDIDATES:
                break

            # 1순위: SmartSession (빠름, 브라우저 불필요)
            results = _scrape_with_smart_session(query, config.MAX_CANDIDATES)

            # 2순위: Playwright + 스텔스 (JS 렌더링, SmartSession 실패 시)
            if not results:
                logger.info('[네이버쇼핑] SmartSession 실패 → Playwright 폴백')
                results = _scrape_with_playwright(query, config.MAX_CANDIDATES)

            # 3순위 폴백: requests (빠르지만 제한적)
            if not results:
                logger.info('[네이버쇼핑] Playwright 실패 → requests 폴백')
                results = _scrape_with_requests(query, config.MAX_CANDIDATES)

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
