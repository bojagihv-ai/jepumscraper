"""
옥션 스크래퍼 - 쿠팡 방식 참조 (CDP Assisted Capture)
────────────────────────────────────────────────────────
동작 순서:
  1. AUCTION_DEBUG_PORT로 이미 열린 Chrome에 CDP 연결
  2. 옥션 검색 탭 찾아 HTML 파싱
  3. 탭 없으면 전용 프로필로 Chrome 열어 검색 URL 이동
  4. 다시 파싱 (최대 3회 재시도)
  Cloudflare 감지 시 AUCTION_HUMAN_CHECK_WAIT_SEC 동안 사용자 확인 대기
"""
import asyncio
import contextvars
import hashlib
import logging
import os
import random
import re
import subprocess
import time
from pathlib import Path
from typing import List
from urllib.parse import quote_plus, unquote_plus, urljoin

from bs4 import BeautifulSoup

import config
from engines.text_matcher import extract_keywords
from scrapers.base_scraper import BaseScraper, ProductResult, download_image_sync

logger = logging.getLogger(__name__)


# ─── 유틸 ─────────────────────────────────────────────────────────────────────

def _auction_debug_port() -> int:
    return int(getattr(config, "AUCTION_DEBUG_PORT", 9224) or 9224)


def _safe_log_name(prefix: str, query: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', query or '').strip(' ._')
    cleaned = re.sub(r'\s+', '_', cleaned)[:40] or 'query'
    digest = hashlib.sha1((query or '').encode('utf-8', errors='ignore')).hexdigest()[:8]
    return f'{prefix}_{cleaned}_{digest}.html'


def _looks_blocked(html: str) -> bool:
    if not html:
        return False
    lowered = html.lower()
    return (
        'cloudflare' in lowered
        or 'just a moment' in lowered
        or 'attention required' in lowered
        or 'access denied' in lowered
        or 'captcha' in lowered
        or 'verify you are human' in lowered
        or '원활한 서비스 이용을 위한' in html
        or '사람인지 확인' in html
        or '봇 확인' in html
        or '검토번호' in html
        or '잠시만 기다려' in html
        or '보안 확인' in html
    )


def _looks_like_results(html: str) -> bool:
    if not html or _looks_blocked(html):
        return False
    soup = BeautifulSoup(html, 'html.parser')
    return bool(
        soup.select_one('.itemcard')
        or soup.select_one('a[href*="itemno"], a[href*="ItemNo"], a[href*="DetailView"]')
        or soup.select_one('div[class*="section--item"], div[class*="box__item"]')
    )


def _text_first(node, selectors: list) -> str:
    for selector in selectors:
        found = node.select_one(selector)
        if found:
            text = found.get_text(" ", strip=True)
            if text:
                return text
    return ""


def _remove_stale_locks(profile_dir: Path) -> None:
    """비정상 종료로 남은 Chrome lockfile/LOCK 제거"""
    lock_files = [
        Path(profile_dir) / "lockfile",
        Path(profile_dir) / "Default" / "LOCK",
    ]
    for lf in lock_files:
        if lf.exists():
            try:
                lf.unlink()
                logger.info("[Auction] stale lock 제거: %s", lf.name)
            except Exception as exc:
                logger.debug("[Auction] lock 제거 실패 %s: %s", lf.name, exc)


def _parse_auction_html(html: str, max_count: int) -> List[ProductResult]:
    soup = BeautifulSoup(html, 'html.parser')
    results: List[ProductResult] = []

    containers = []
    for selector in [
        '.itemcard',
        'div[class*="itemcard"]',
        'li[class*="item"]',
        'div[class*="section--item"]',
        'div[class*="box__item"]',
        'article',
    ]:
        found = soup.select(selector)
        useful = [
            node for node in found
            if node.select_one('a[href*="itemno"], a[href*="ItemNo"], a[href*="DetailView"]')
        ]
        if useful:
            containers = useful
            break

    if not containers:
        seen = set()
        for link in soup.select('a[href*="itemno"], a[href*="ItemNo"], a[href*="DetailView"]'):
            container = link.find_parent(['li', 'article', 'div']) or link
            ident = id(container)
            if ident not in seen:
                containers.append(container)
                seen.add(ident)
            if len(containers) >= max_count * 2:
                break

    for idx, node in enumerate(containers):
        if len(results) >= max_count:
            break
        try:
            link = (
                node.select_one('a[href*="itemno"], a[href*="ItemNo"], a[href*="DetailView"]')
                or node.select_one('a[href]')
            )
            href = link.get('href', '') if link else ''
            if not href:
                continue
            href = urljoin('https://browse.auction.co.kr', href)
            if 'auction.co.kr' not in href.lower():
                continue

            title = _text_first(node, [
                '.text--title', '.text__item-title',
                '[class*="title"]', '[class*="item-title"]',
            ])
            if not title and link:
                title = link.get_text(" ", strip=True)
            title = re.sub(r'\s+', ' ', title or '').strip()
            if len(title) < 3:
                continue
            if any(skip in title.lower() for skip in ['광고', 'ad ', '먼저 둘러보세요', '파워클릭']):
                continue

            price_text = _text_first(node, [
                '.text--price_seller', '.price_seller',
                '.text__value', '[class*="price"]',
            ])
            price_digits = re.sub(r'[^0-9]', '', price_text or '')
            if not price_digits:
                match = re.search(r'([0-9]{1,3}(?:,[0-9]{3})+)\s*원?', node.get_text(" ", strip=True))
                price_digits = match.group(1).replace(',', '') if match else '0'

            img = node.select_one('img[src], img[data-src], img[data-original], img[data-lazy]')
            thumb_url = ''
            if img:
                thumb_url = (
                    img.get('src') or img.get('data-src')
                    or img.get('data-original') or img.get('data-lazy') or ''
                )
                if thumb_url.startswith('//'):
                    thumb_url = 'https:' + thumb_url
                elif thumb_url.startswith('/'):
                    thumb_url = urljoin('https://browse.auction.co.kr', thumb_url)
            if not thumb_url or thumb_url.startswith('data:'):
                continue

            code_match = re.search(r'(?:itemno|ItemNo)[=_]?([A-Za-z0-9]+)', href)
            product_id = f"auction_{code_match.group(1) if code_match else idx}"
            results.append(ProductResult(
                id=product_id,
                platform='옥션',
                title=title[:160],
                price=price_digits or '0',
                product_url=href,
                thumbnail_url=thumb_url,
            ))
        except Exception as exc:
            logger.debug("[Auction] parse skipped: %s", exc)

    return results


# ─── Chrome CDP 관리 ───────────────────────────────────────────────────────────

def _has_auction_debug_page() -> bool:
    """AUCTION_DEBUG_PORT로 열린 Chrome에 옥션 탭이 있는지 확인."""
    port = _auction_debug_port()
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            found = any(
                "auction.co.kr" in (page.url or "")
                for ctx in browser.contexts
                for page in ctx.pages
            )
            browser.close()
            return found
    except Exception:
        return False


def _open_auction_search_page(query: str) -> bool:
    """검색 URL로 이동. 이미 Chrome 있으면 새 탭, 없으면 새 프로세스 실행."""
    search_url = f"https://browse.auction.co.kr/search?keyword={quote_plus(query)}"
    port = _auction_debug_port()

    # 이미 Chrome이 열려있으면 새 탭으로 이동
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0] if browser.contexts else None
            if ctx is not None:
                page = ctx.new_page()
                try:
                    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                browser.close()
                logger.info("[Auction] 기존 Chrome에 새 탭으로 검색 이동: %s", query)
                return True
            browser.close()
    except Exception:
        pass

    # Chrome이 없으면 전용 프로필로 새로 실행
    try:
        from engine.browser_profile import chrome_browser_path, auction_browser_profile_dir
        chrome = chrome_browser_path()
        if not chrome:
            logger.warning("[Auction] Chrome 실행파일을 찾을 수 없습니다")
            return False
        profile_dir = auction_browser_profile_dir()
        _remove_stale_locks(Path(profile_dir))
        subprocess.Popen([
            chrome,
            f"--user-data-dir={profile_dir}",
            "--profile-directory=Default",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            search_url,
        ], close_fds=True)
        logger.info("[Auction] Chrome 새로 실행: %s", search_url)
    except Exception as exc:
        logger.warning("[Auction] Chrome 실행 실패: %s", exc)
        return False

    for _ in range(24):
        time.sleep(0.5)
        if _has_auction_debug_page():
            return True
    return False


def _safe_page_content(page, retries: int = 6, delay: float = 0.7) -> str:
    """탐색 중인 페이지도 재시도하며 content 읽기."""
    for _ in range(max(1, retries)):
        try:
            return page.content()
        except Exception as exc:
            message = str(exc).lower()
            if "navigating" not in message and "changing the content" not in message:
                logger.debug("[Auction] page.content 스킵: %s", exc)
                break
            time.sleep(delay)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=2500)
            except Exception:
                pass
    return ""


def _scrape_auction_assisted_current_page(query: str, max_count: int) -> List[ProductResult]:
    """현재 열린 옥션 검색 탭에서 결과 파싱."""
    port = _auction_debug_port()
    query_norm = re.sub(r"\s+", "", query).lower()
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            pages_list = []
            for ctx in browser.contexts:
                pages_list.extend(ctx.pages)

            for page in pages_list:
                url = page.url or ""
                if "auction.co.kr" not in url:
                    continue
                if "search" not in url.lower() and "keyword" not in url.lower():
                    continue
                decoded_url = unquote_plus(url)
                if query_norm and query_norm not in re.sub(r"\s+", "", decoded_url).lower():
                    continue

                try:
                    page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass

                for attempt in range(5):
                    if attempt:
                        try:
                            page.wait_for_load_state("networkidle", timeout=3500)
                        except Exception:
                            pass
                        try:
                            page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.75)); true")
                        except Exception:
                            pass
                        time.sleep(0.8 + attempt * 0.25)

                    html = _safe_page_content(page, retries=8, delay=0.75)
                    if not html:
                        logger.info("[Auction] 페이지 로딩 중; retry %d", attempt + 1)
                        continue

                    if _looks_blocked(html):
                        wait_sec = max(0, int(getattr(config, "AUCTION_HUMAN_CHECK_WAIT_SEC", 180) or 0))
                        logger.warning("[Auction] Cloudflare/보안 감지 - 최대 %ds 사용자 확인 대기", wait_sec)
                        try:
                            page.bring_to_front()
                        except Exception:
                            pass
                        deadline = time.time() + wait_sec
                        cleared = False
                        while time.time() < deadline:
                            time.sleep(2.0)
                            try:
                                html = page.content()
                            except Exception:
                                continue
                            if _looks_like_results(html) or not _looks_blocked(html):
                                logger.info("[Auction] 보안 확인 통과")
                                cleared = True
                                break
                        if not cleared:
                            logger.warning("[Auction] 보안 확인 시간 초과")
                            break

                    results = _parse_auction_html(html, max_count)
                    logger.info("[Auction] 현재 탭 파싱 → %d개 (시도 %d)", len(results), attempt + 1)
                    if results:
                        # lazy-load 이미지 트리거 후 재파싱
                        if attempt == 0:
                            try:
                                for _ in range(3):
                                    page.evaluate("window.scrollBy(0, window.innerHeight)")
                                    time.sleep(0.5)
                                html2 = _safe_page_content(page, retries=4, delay=0.5)
                                results2 = _parse_auction_html(html2, max_count) if html2 else []
                                if len(results2) > len(results):
                                    results = results2
                            except Exception:
                                pass
                        browser.close()
                        return results

            browser.close()
    except Exception as exc:
        logger.info("[Auction] CDP 연결 실패: %s", exc)
    return []


# ─── 스크래퍼 클래스 ───────────────────────────────────────────────────────────

class AuctionScraper(BaseScraper):
    def __init__(self):
        super().__init__()
        self.platform_name = "옥션"

    def _scrape_sync(self, keyword: str) -> List[ProductResult]:
        kws = extract_keywords(keyword)
        q = ' '.join(kws) if kws else keyword

        # 1순위: 이미 열린 검색 탭 파싱
        results = _scrape_auction_assisted_current_page(q, config.MAX_CANDIDATES)
        if results:
            logger.info("[옥션] 기존 탭에서 %d개 수집", len(results))
            return results

        # 2순위: Chrome 열어서 검색 URL로 이동
        logger.info("[옥션] Chrome 실행 또는 새 탭으로 검색 이동: %s", q)
        if not _open_auction_search_page(q):
            self.last_status = "manual_required"
            self.last_error = "옥션 Chrome을 열 수 없습니다. 수동으로 확인해주세요."
            return []

        time.sleep(2.5)
        for attempt in range(3):
            if attempt:
                time.sleep(random.uniform(2.0, 3.5))
            results = _scrape_auction_assisted_current_page(q, config.MAX_CANDIDATES)
            if results:
                return results
            logger.warning("[옥션] 0개 추출됨 (retry %d/3)", attempt + 1)

        return []

    async def search(self, keyword: str) -> List[ProductResult]:
        loop = asyncio.get_running_loop()
        self.last_status = ""
        self.last_error = ""
        ctx = contextvars.copy_context()
        results = await loop.run_in_executor(None, ctx.run, self._scrape_sync, keyword)

        for res in results:
            if res.thumbnail_url and not res.local_thumbnail_path:
                local_path = os.path.join(str(config.THUMBNAIL_DIR), f"{res.id}.jpg")
                ok = await loop.run_in_executor(
                    None, download_image_sync, res.thumbnail_url, local_path
                )
                if ok:
                    res.local_thumbnail_path = local_path

        if results:
            self.last_status = "success"
        elif not self.last_status:
            self.last_status = "zero_result"
        logger.info("[옥션] 최종 수집: %d개", len(results))
        return results

    async def get_detail_page(self, product_url: str) -> List[str]:
        return []
