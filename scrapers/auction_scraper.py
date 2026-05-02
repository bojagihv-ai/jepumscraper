"""
옥션 Playwright 스크래핑 - JS Evaluate 사용
"""
import os
import asyncio
import random
import logging
import re
import hashlib
from typing import List
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PwTimeout
from scrapers.base_scraper import BaseScraper, ProductResult, download_image_sync
import config
from engines.text_matcher import extract_keywords

logger = logging.getLogger(__name__)

# Cloudflare/봇 감지 우회용 stealth 초기화 스크립트
_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR','ko','en-US','en']});
window.chrome = {runtime: {}};
"""


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
        or '\uc6d0\ud65c\ud55c \uc11c\ube44\uc2a4 \uc774\uc6a9\uc744 \uc704\ud55c' in html
        or '\uc0ac\ub78c\uc778\uc9c0 \ud655\uc778' in html
        or '\ubd07 \ud655\uc778' in html
        or '\uac80\ud1a0\ubc88\ud638' in html
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


def _text_first(node, selectors: list[str]) -> str:
    for selector in selectors:
        found = node.select_one(selector)
        if found:
            text = found.get_text(" ", strip=True)
            if text:
                return text
    return ""


def _remove_stale_locks(profile_dir: "Path") -> None:
    """비정상 종료로 남은 Chrome lockfile/LOCK 제거 (Chrome 미실행 시에만)"""
    from pathlib import Path
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
        useful = [node for node in found if node.select_one('a[href*="itemno"], a[href*="ItemNo"], a[href*="DetailView"]')]
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
            link = node.select_one('a[href*="itemno"], a[href*="ItemNo"], a[href*="DetailView"]') or node.select_one('a[href]')
            href = link.get('href', '') if link else ''
            if not href:
                continue
            href = urljoin('https://browse.auction.co.kr', href)
            if 'auction.co.kr' not in href.lower():
                continue

            title = _text_first(node, [
                '.text--title',
                '.text__item-title',
                '[class*="title"]',
                '[class*="item-title"]',
            ])
            if not title and link:
                title = link.get_text(" ", strip=True)
            title = re.sub(r'\s+', ' ', title or '').strip()
            if len(title) < 3:
                continue
            lowered_title = title.lower()
            if any(skip in lowered_title for skip in ['광고', 'ad ', '먼저 둘러보세요', '파워클릭']):
                continue

            price_text = _text_first(node, [
                '.text--price_seller',
                '.price_seller',
                '.text__value',
                '[class*="price"]',
            ])
            price_digits = re.sub(r'[^0-9]', '', price_text or '')
            if not price_digits:
                match = re.search(r'([0-9]{1,3}(?:,[0-9]{3})+)\s*원?', node.get_text(" ", strip=True))
                price_digits = match.group(1).replace(',', '') if match else '0'

            img = node.select_one('img[src], img[data-src], img[data-original], img[data-lazy]')
            thumb_url = ''
            if img:
                thumb_url = (
                    img.get('src')
                    or img.get('data-src')
                    or img.get('data-original')
                    or img.get('data-lazy')
                    or ''
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
                platform='Auction',
                title=title[:160],
                price=price_digits or '0',
                product_url=href,
                thumbnail_url=thumb_url,
            ))
        except Exception as exc:
            logger.debug("[AuctionManual] parse skipped: %s", exc)

    return results

class AuctionScraper(BaseScraper):
    def __init__(self):
        super().__init__()
        self.platform_name = "옥션"

    async def search(self, keyword: str) -> List[ProductResult]:
        self.last_status = ""
        self.last_error = ""
        kws = extract_keywords(keyword)
        q = ' '.join(kws) if kws else keyword
        url = f"https://browse.auction.co.kr/search?keyword={quote_plus(q)}"
        results: List[ProductResult] = []

        try:
            async with async_playwright() as p:
                ua = random.choice(config.USER_AGENT_LIST)
                # 옥션 전용 프로필 우선, 실패 시 사용자 Chrome 임시복사본으로 폴백
                tmp_profiles = []
                pw_proxy = None
                try:
                    from engine.ip_manager import get_playwright_proxy
                    pw_proxy = get_playwright_proxy("auction")
                except Exception as e:
                    logger.debug(f"[Auction] proxy skipped: {e}")

                async def open_context():
                    from engine.browser_profile import (
                        auction_browser_profile_dir,
                        chrome_profile_name,
                        copy_chrome_profile_tmp,
                        use_user_browser_session,
                    )
                    profile_dir = auction_browser_profile_dir()
                    debug_port = int(getattr(config, "AUCTION_DEBUG_PORT", 9224) or 9224)

                    # 1단계: 이미 열려있는 전용 Chrome에 CDP 연결
                    try:
                        browser = await p.chromium.connect_over_cdp(
                            f"http://127.0.0.1:{debug_port}",
                            timeout=2500,
                        )
                        if browser.contexts:
                            logger.info("[Auction] CDP 연결 성공 (port %s)", debug_port)
                            return browser.contexts[0], "cdp"
                    except Exception as cdp_exc:
                        logger.debug("[Auction] CDP 연결 스킵: %s", cdp_exc)

                    # 2단계: 전용 프로필로 새 Chrome 실행 (stale lock 정리 후)
                    _remove_stale_locks(profile_dir)
                    try:
                        ctx = await p.chromium.launch_persistent_context(
                            user_data_dir=str(profile_dir),
                            channel="chrome",
                            headless=False,
                            **({"proxy": pw_proxy} if pw_proxy else {}),
                            ignore_default_args=['--enable-automation'],  # "자동화 소프트웨어" 배너 제거
                            args=[
                                '--disable-blink-features=AutomationControlled',
                                '--disable-infobars',
                                '--profile-directory=Default',
                            ],
                            viewport={"width": 1920, "height": 1080},
                            locale='ko-KR',
                            timezone_id='Asia/Seoul',
                        )
                        await ctx.add_init_script(_STEALTH_SCRIPT)
                        logger.info("[Auction] 전용 프로필로 실행")
                        return ctx, "persistent"
                    except Exception as e:
                        logger.warning("[Auction] 전용 프로필 실패, 임시복사본으로 폴백: %s", e)

                    # 3단계: 사용자 Chrome 쿠키 임시복사본 사용 (GitHub 원본 방식)
                    try:
                        if use_user_browser_session():
                            tmp_profile = copy_chrome_profile_tmp()
                            if tmp_profile:
                                tmp_profiles.append(tmp_profile)
                                ctx = await p.chromium.launch_persistent_context(
                                    user_data_dir=tmp_profile,
                                    channel="chrome",
                                    headless=False,
                                    **({"proxy": pw_proxy} if pw_proxy else {}),
                                    ignore_default_args=['--enable-automation'],
                                    args=[
                                        '--disable-blink-features=AutomationControlled',
                                        '--disable-infobars',
                                        f"--profile-directory={chrome_profile_name()}",
                                    ],
                                    viewport={"width": 1920, "height": 1080},
                                    locale='ko-KR',
                                    timezone_id='Asia/Seoul',
                                )
                                await ctx.add_init_script(_STEALTH_SCRIPT)
                                logger.info("[Auction] 임시복사본 프로필로 실행")
                                return ctx, "tmp_profile"
                    except Exception as e2:
                        logger.warning("[Auction] 임시복사본 폴백도 실패: %s", e2)

                    self.last_status = "manual_required"
                    self.last_error = "옥션 Chrome을 열 수 없습니다. 수동으로 확인해주세요."
                    return None, "none"

                async def close_context(ctx, mode: str, page=None):
                    try:
                        if page is not None and not page.is_closed():
                            await page.close()
                    except Exception:
                        pass
                    if mode not in ("cdp",) and ctx is not None:
                        try:
                            await ctx.close()
                        except Exception:
                            pass

                async def wait_for_manual_check(page):
                    wait_sec = max(0, int(getattr(config, "AUCTION_HUMAN_CHECK_WAIT_SEC", 180) or 0))
                    deadline = asyncio.get_running_loop().time() + wait_sec
                    logger.warning("[Auction] human check page detected. Waiting up to %ss for user confirmation.", wait_sec)
                    try:
                        await page.bring_to_front()
                    except Exception:
                        pass
                    while True:
                        await page.wait_for_timeout(2000)
                        try:
                            html = await page.content()
                        except Exception as exc:
                            logger.debug("[Auction] human-check page still navigating: %s", exc)
                            try:
                                await page.wait_for_load_state("domcontentloaded", timeout=3000)
                            except Exception:
                                pass
                            if asyncio.get_running_loop().time() >= deadline:
                                try:
                                    return await page.content()
                                except Exception:
                                    return ""
                            continue
                        if _looks_like_results(html) or not _looks_blocked(html):
                            logger.info("[Auction] human check cleared; continuing search.")
                            return html
                        if asyncio.get_running_loop().time() >= deadline:
                            return html

                max_retries = 3
                for attempt in range(max_retries):
                    ctx, ctx_mode = await open_context()
                    if ctx is None:
                        break
                    page = await ctx.new_page()

                    logger.info(f"[옥션] 검색 (시도 {attempt+1}/{max_retries}): {url}")
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                        await page.wait_for_timeout(random.uniform(2500, 4000))

                        page_html = await page.content()
                        if _looks_blocked(page_html):
                            page_html = await wait_for_manual_check(page)
                            if _looks_blocked(page_html):
                                self.last_status = "blocked"
                                self.last_error = "Auction human check is still pending"
                                logger.warning("[Auction] human check was not completed within the wait window.")
                                await close_context(ctx, ctx_mode, page)
                                break
                        if "기다리십시오" in page_html or "잠시만" in page_html or "Just a moment" in page_html:
                            logger.warning("[옥션] ⛔ 봇 차단 감지 (Cloudflare/방화벽). 브라우저에서 직접 캡차를 해제할 시간을 15초간 부여합니다.")
                            # 사용자가 클릭할 수 있도록 시간을 넉넉히 주고 대기합니다 (창이 중앙에 뜹니다)
                            await page.wait_for_timeout(15000)
                            page_html = await page.content()
                            if "기다리십시오" in page_html or "잠시만" in page_html or "Just a moment" in page_html:
                                self.last_status = "blocked"
                                self.last_error = "Cloudflare/방화벽 보안 확인"
                                logger.warning("[옥션] 보안 확인이 유지되어 이번 수집을 중단합니다.")
                                await close_context(ctx, ctx_mode, page)
                                break
                            else:
                                logger.info("[옥션] 캡차가 해제되었습니다! 진행합니다.")
                                
                        # 정상 진입 시 좀 더 렌더링되도록 대기
                        await page.wait_for_timeout(2000)

                            
                        # 스크롤해서 lazy-load 이미지 로드
                        for _ in range(3):
                            await page.evaluate("window.scrollBy(0, window.innerHeight)")
                            await page.wait_for_timeout(500)

                        product_data = await page.evaluate("""() => {
                            const items = [];
                            document.querySelectorAll('.itemcard').forEach(card => {
                                const link = card.querySelector('a');
                                if (link && link.href.includes('itemno=')) {
                                    let title = card.querySelector('.text--title');
                                    let titleTxt = title ? title.textContent.trim() : link.textContent.trim();
                                    if(titleTxt === "먼저 둘러보세요" || titleTxt === "파워클릭" || titleTxt.includes("주목할 만한") || titleTxt.includes("일반등록") || !titleTxt) {
                                        const realTitleEl = card.querySelector('.text__item-title');
                                        titleTxt = realTitleEl ? realTitleEl.textContent.trim() : titleTxt;
                                    }
                                    let price = card.querySelector('.text--price_seller');
                                    let priceTxt = price ? price.textContent.replace(/[^0-9]/g, '') : '0';
                                    if(priceTxt === '0') {
                                        const backupPrice = card.querySelector('.price_seller, .text__value');
                                        priceTxt = backupPrice ? backupPrice.textContent.replace(/[^0-9]/g, '') : '0';
                                    }
                                    let img = card.querySelector('img');
                                    let imgTxt = img ? (img.src || img.getAttribute('data-original') || '') : '';
                                    if(titleTxt && titleTxt.length > 2 && titleTxt !== "먼저 둘러보세요" && titleTxt !== "일반등록") {
                                      items.push({href: link.href, title: titleTxt, price: priceTxt, thumbUrl: imgTxt});
                                    }
                                }
                            });
                            return items.slice(0, 60);
                        }""")

                        logger.info(f"[옥션] 완료: {len(product_data)}개 추출됨")

                        if len(product_data) > 0:
                            for idx, item in enumerate(product_data):
                                try:
                                    href = item.get('href', '')
                                    title = item.get('title', '')
                                    price = item.get('price', '0')
                                    thumb_url = item.get('thumbUrl', '')
                                    
                                    if not href or not title: continue
                                    if thumb_url.startswith('//'): thumb_url = 'https:' + thumb_url

                                    code_match = re.search(r'itemno[=_](\w+)', href, re.I)
                                    pid = f"auction_{code_match.group(1) if code_match else idx}"

                                    local_thumb = ''
                                    if thumb_url:
                                        local_thumb = os.path.join(config.THUMBNAIL_DIR, f"{pid}.jpg")
                                        if not download_image_sync(thumb_url, local_thumb):
                                            local_thumb = ''

                                    results.append(ProductResult(
                                        id=pid, platform='옥션', title=title,
                                        price=price, product_url=href,
                                        thumbnail_url=thumb_url, local_thumbnail_path=local_thumb
                                    ))
                                except Exception as e:
                                    continue
                            # 성공했으므로 재시도 루프 탈출
                            await close_context(ctx, ctx_mode, page)
                            break
                        else:
                            logger.warning(f"[옥션] 0개 추출됨. 재시도합니다... (시도 {attempt+1})")
                            await close_context(ctx, ctx_mode, page)
                            await asyncio.sleep(random.uniform(2.0, 4.0))

                    except Exception as e:
                        logger.warning(f"[옥션] 탐색 중 에러 (재시도): {e}")
                        await close_context(ctx, ctx_mode, page)
                        await asyncio.sleep(random.uniform(2.0, 4.0))

                if tmp_profiles:
                    import shutil
                    for tmp_profile in tmp_profiles:
                        shutil.rmtree(tmp_profile, ignore_errors=True)

        except Exception as e:
            self.last_status = "error"
            self.last_error = str(e)
            logger.error(f"[옥션] 최상위 크롬 구동 에러: {e}")

        if results:
            self.last_status = "success"
        elif not self.last_status:
            self.last_status = "zero_result"
        logger.info(f"[옥션] 최종 수집: {len(results)}개")
        return results

    async def get_detail_page(self, product_url: str) -> List[str]:
        return []
