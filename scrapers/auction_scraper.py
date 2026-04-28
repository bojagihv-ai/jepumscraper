"""
옥션 Playwright 스크래핑 - JS Evaluate 사용
"""
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

class AuctionScraper(BaseScraper):
    def __init__(self):
        super().__init__()
        self.platform_name = "옥션"

    async def search(self, keyword: str) -> List[ProductResult]:
        kws = extract_keywords(keyword)
        q = ' '.join(kws) if kws else keyword
        url = f"https://browse.auction.co.kr/search?keyword={quote_plus(q)}"
        results: List[ProductResult] = []

        try:
            async with async_playwright() as p:
                ua = random.choice(config.USER_AGENT_LIST)
                # 옥션은 Cloudflare/방화벽이 강력하여 Headless를 False(창 뜨는 모드)로 우회합니다.
                # 지문 스푸핑(AutomationControlled 제거 등)을 최소화해야 봇 차단을 덜 받습니다.
                browser = await p.chromium.launch(
                    headless=False
                )
                
                max_retries = 3
                for attempt in range(max_retries):
                    ctx = await browser.new_context(
                        viewport={"width": 1920, "height": 1080},
                        locale='ko-KR',
                        timezone_id='Asia/Seoul',
                    )
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
                                logger.warning("[옥션] 캡차 해제 실패. 재시도합니다.")
                                await ctx.close()
                                await asyncio.sleep(2.0)
                                continue
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

                await browser.close()

        except Exception as e:
            logger.error(f"[옥션] 최상위 크롬 구동 에러: {e}")

        logger.info(f"[옥션] 최종 수집: {len(results)}개")
        return results

    async def get_detail_page(self, product_url: str) -> List[str]:
        return []
