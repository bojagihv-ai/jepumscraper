import os
import asyncio
import random
import logging
import re
from typing import List
from urllib.parse import quote_plus
from playwright.async_api import async_playwright, TimeoutError as PwTimeout
from scrapers.base_scraper import BaseScraper, ProductResult, download_image_sync
import config
from engines.text_matcher import extract_keywords

logger = logging.getLogger(__name__)

class ElevenstScraper(BaseScraper):
    def __init__(self):
        super().__init__()
        self.platform_name = "11번가"

    async def search(self, keyword: str) -> List[ProductResult]:
        kws = extract_keywords(keyword)
        q = ' '.join(kws) if kws else keyword
        url = f"https://search.11st.co.kr/pc/total-search?kwd={quote_plus(q)}&tabId=goods"
        results: List[ProductResult] = []

        try:
            async with async_playwright() as p:
                ua = random.choice(config.USER_AGENT_LIST)
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--disable-blink-features=AutomationControlled']
                )
                ctx = await browser.new_context(
                    user_agent=ua,
                    viewport={"width": 1920, "height": 1080}
                )
                await ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )
                page = await ctx.new_page()

                logger.info(f"[11번가] 검색: {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(random.uniform(2000, 3500))

                # 상품 카드: div.c-card-item — 검색결과 섹션만 타겟
                try:
                    await page.wait_for_selector('div.c-card-item, .c-card-item__anchor', timeout=8000)
                except PwTimeout:
                    logger.warning("[11번가] 상품 리스트 로드 타임아웃")
                    await browser.close()
                    return results

                # 검색결과 내 상품 카드만 추출 (광고/배너 섹션 제외)
                # 일반 검색 결과는 ul.c-list-product 또는 .search-list 내에 위치
                items = await page.query_selector_all('ul.c-list-product .c-card-item, .list-goods .c-card-item, .search-list .c-card-item')
                if not items:
                    # fallback: 모든 c-card-item
                    items = await page.query_selector_all('div.c-card-item')
                logger.info(f"[11번가] {len(items)}개 아이템 발견")

                for idx, item in enumerate(items[:config.MAX_CANDIDATES * 3]):  # 광고 포함 여분 수집
                    if len(results) >= config.MAX_CANDIDATES:
                        break
                    try:
                        # 링크
                        link_el = await item.query_selector('a.c-card-item__anchor, a[href*="/products/"]')
                        href = ''
                        if link_el:
                            href = await link_el.get_attribute('href') or ''
                        if href.startswith('//'):
                            href = 'https:' + href
                        if not href:
                            continue

                        # 제목 — .c-card-item__name dd 가 상품명 텍스트 (dt는 "상품명" 레이블)
                        title = ''
                        name_el = await item.query_selector('.c-card-item__name dd')
                        if name_el:
                            title = (await name_el.inner_text()).strip()
                        if not title:
                            img_alt_el = await item.query_selector('img[alt]')
                            if img_alt_el:
                                title = (await img_alt_el.get_attribute('alt') or '').strip()
                        if not title:
                            if link_el:
                                title = (await link_el.get_attribute('title') or '').strip()
                        if not title:
                            continue

                        # 가격 — .c-card-item__price dd .value
                        price_el = await item.query_selector('.c-card-item__price dd .value, .c-card-item__price .value')
                        price = '0'
                        if price_el:
                            price = (await price_el.inner_text()).replace(',', '').strip()

                        # 썸네일 — .c-card-item__thumb img 또는 .c-card-item__visual img
                        img_el = await item.query_selector('.c-card-item__thumb img, .c-card-item__visual img, img[src]:not([src*="spacer"])')
                        thumb_url = ''
                        if img_el:
                            thumb_url = await img_el.get_attribute('src') or await img_el.get_attribute('data-src') or ''
                        if thumb_url.startswith('//'):
                            thumb_url = 'https:' + thumb_url

                        # 상품 ID (URL에서 products/{id} 추출)
                        prod_match = re.search(r'/products/(\d+)', href)
                        pid = f"11st_{prod_match.group(1) if prod_match else idx}"

                        local_thumb = ''
                        if thumb_url:
                            local_thumb = os.path.join(config.THUMBNAIL_DIR, f"{pid}.jpg")
                            if not download_image_sync(thumb_url, local_thumb):
                                local_thumb = ''

                        if href and title:
                            results.append(ProductResult(
                                id=pid, platform='11번가', title=title.strip(),
                                price=price, product_url=href,
                                thumbnail_url=thumb_url, local_thumbnail_path=local_thumb
                            ))
                    except Exception as e:
                        logger.debug(f"[11번가] 아이템 {idx} 파싱 오류: {e}")
                        continue

                await browser.close()

        except Exception as e:
            logger.error(f"[11번가] 검색 오류: {e}")

        logger.info(f"[11번가] 최종 수집: {len(results)}개")
        return results

    async def get_detail_page(self, product_url: str) -> List[str]:
        return []
