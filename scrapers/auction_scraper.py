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
        or '잠시만 기다려' in html
        or '보안 확인' in html
    )


def _text_first(node, selectors: list[str]) -> str:
    for selector in selectors:
        found = node.select_one(selector)
        if found:
            text = found.get_text(" ", strip=True)
            if text:
                return text
    return ""


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
                # 옥션은 보안 확인이 자주 떠서 사용자가 볼 수 있는 브라우저 세션을 사용합니다.
                # 보안 확인이 유지되면 반복 요청하지 않고 중단해 적응 학습의 cooldown에 맡깁니다.
                browser = None
                tmp_profiles = []
                pw_proxy = None
                try:
                    from engine.ip_manager import get_playwright_proxy
                    pw_proxy = get_playwright_proxy("auction")
                except Exception as e:
                    logger.debug(f"[Auction] proxy skipped: {e}")

                async def open_context():
                    nonlocal browser
                    try:
                        from engine.browser_profile import (
                            chrome_profile_name,
                            copy_chrome_profile_tmp,
                            use_user_browser_session,
                        )
                        if use_user_browser_session():
                            tmp_profile = copy_chrome_profile_tmp()
                            if tmp_profile:
                                tmp_profiles.append(tmp_profile)
                                return await p.chromium.launch_persistent_context(
                                    user_data_dir=tmp_profile,
                                    channel="chrome",
                                    headless=False,
                                    **({"proxy": pw_proxy} if pw_proxy else {}),
                                    args=[
                                        '--disable-blink-features=AutomationControlled',
                                        f"--profile-directory={chrome_profile_name()}",
                                    ],
                                    viewport={"width": 1920, "height": 1080},
                                    locale='ko-KR',
                                    timezone_id='Asia/Seoul',
                                )
                    except Exception as e:
                        logger.debug(f"[Auction] Chrome profile context skipped: {e}")

                    if browser is None:
                        browser = await p.chromium.launch(
                            headless=False,
                            **({"proxy": pw_proxy} if pw_proxy else {}),
                        )
                    return await browser.new_context(
                        viewport={"width": 1920, "height": 1080},
                        locale='ko-KR',
                        timezone_id='Asia/Seoul',
                    )

                max_retries = 3
                for attempt in range(max_retries):
                    ctx = await open_context()
                    page = await ctx.new_page()

                    logger.info(f"[옥션] 검색 (시도 {attempt+1}/{max_retries}): {url}")
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                        await page.wait_for_timeout(random.uniform(2500, 4000))

                        page_html = await page.content()
                        if "기다리십시오" in page_html or "잠시만" in page_html or "Just a moment" in page_html:
                            logger.warning("[옥션] ⛔ 봇 차단 감지 (Cloudflare/방화벽). 브라우저에서 직접 캡차를 해제할 시간을 15초간 부여합니다.")
                            # 사용자가 클릭할 수 있도록 시간을 넉넉히 주고 대기합니다 (창이 중앙에 뜹니다)
                            await page.wait_for_timeout(15000)
                            page_html = await page.content()
                            if "기다리십시오" in page_html or "잠시만" in page_html or "Just a moment" in page_html:
                                self.last_status = "blocked"
                                self.last_error = "Cloudflare/방화벽 보안 확인"
                                logger.warning("[옥션] 보안 확인이 유지되어 이번 수집을 중단합니다.")
                                await ctx.close()
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
                            await ctx.close()
                            break
                        else:
                            logger.warning(f"[옥션] 0개 추출됨. 재시도합니다... (시도 {attempt+1})")
                            await ctx.close()
                            await asyncio.sleep(random.uniform(2.0, 4.0))

                    except Exception as e:
                        logger.warning(f"[옥션] 탐색 중 에러 (재시도): {e}")
                        await ctx.close()
                        await asyncio.sleep(random.uniform(2.0, 4.0))

                if browser:
                    await browser.close()
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
