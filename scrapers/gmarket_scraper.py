import os
import asyncio
import contextvars
import logging
import random
from typing import List
from urllib.parse import quote_plus
import config
from engines.text_matcher import extract_keywords
from scrapers.base_scraper import BaseScraper, ProductResult, download_image_sync

logger = logging.getLogger(__name__)

class GmarketScraper(BaseScraper):
    def __init__(self):
        super().__init__()
        self.platform_name = "G마켓"

    def _scrape_sync(self, keyword: str) -> List[ProductResult]:
        from DrissionPage import ChromiumPage, ChromiumOptions
        import time
        from bs4 import BeautifulSoup
        import re

        kws = extract_keywords(keyword)
        q = ' '.join(kws) if kws else keyword
        url = f"https://browse.gmarket.co.kr/search?keyword={quote_plus(q)}"
        results: List[ProductResult] = []
        tmp_profile = ""

        co = ChromiumOptions()
        co.set_argument('--window-position=-32000,-32000')
        co.set_argument('--mute-audio')
        try:
            from engine.ip_manager import get_proxy
            proxy_url = get_proxy("gmarket")
            if proxy_url:
                co.set_argument(f'--proxy-server={proxy_url}')
        except Exception as e:
            logger.debug(f"[Gmarket] proxy skipped: {e}")
        try:
            from engine.browser_profile import chrome_profile_name, copy_chrome_profile_tmp
            tmp_profile = copy_chrome_profile_tmp()
            if tmp_profile:
                co.set_user_data_path(tmp_profile)
                co.set_user(chrome_profile_name())
        except Exception as e:
            logger.debug(f"[Gmarket] Chrome profile context skipped: {e}")
        paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        ]
        for path in paths:
            if os.path.exists(path):
                co.set_paths(browser_path=path)
                break

        page = None
        try:
            page = ChromiumPage(co)
            logger.info("[G마켓] 검색 시작 (DrissionPage)...")
            
            page.get(url, retry=1, interval=1, timeout=30)
            time.sleep(2.5)

            html = page.html
            
            # 스크롤 
            for _ in range(5):
                page.run_js("window.scrollBy(0, window.innerHeight * 0.9)")
                time.sleep(0.4)

            soup = BeautifulSoup(page.html, 'html.parser')
            items = soup.select('div.box__item-container')

            for item in items:
                title_tag = item.select_one('.text__item')
                if not title_tag:
                    continue
                title = title_tag.text.strip()
                
                a_tag = item.select_one('a.link__item')
                if not a_tag:
                    continue
                item_link = a_tag.get('href', '')
                
                price_str = "0"
                price_tag = item.select_one('strong.text__value')
                if price_tag:
                    price_str = re.sub(r'[^0-9]', '', price_tag.text)
                
                try:
                    price = int(price_str)
                except:
                    price = 0
                if price == 0:
                    continue
                
                thumb_url = ""
                img_tag = item.select_one('img.image__item')
                if img_tag:
                    thumb_url = img_tag.get('src') or img_tag.get('data-original') or ""
                if thumb_url and thumb_url.startswith('//'):
                    thumb_url = 'https:' + thumb_url

                prod_id = f"gmarket_{random.randint(100000, 999999)}"
                m = re.search(r'goodscode=(\d+)', item_link)
                if m:
                    prod_id = f"gmarket_{m.group(1)}"
                else:
                    parts = item_link.split('/')
                    if len(parts) > 0 and parts[-1].isdigit():
                        prod_id = f"gmarket_{parts[-1]}"

                res = ProductResult(
                    id=prod_id,
                    platform="G마켓",
                    title=title,
                    price=str(price),
                    product_url=item_link,
                    thumbnail_url=thumb_url
                )
                results.append(res)
                
                if len(results) >= config.MAX_CANDIDATES:
                    break

        except Exception as e:
            logger.error(f"[G마켓] 오류: {e}")
        finally:
            if page:
                try:
                    page.quit()
                except:
                    pass
            if tmp_profile:
                try:
                    import shutil
                    shutil.rmtree(tmp_profile, ignore_errors=True)
                except Exception:
                    pass

        return results

    async def search(self, keyword: str) -> List[ProductResult]:
        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()
        results = await loop.run_in_executor(None, ctx.run, self._scrape_sync, keyword)
        
        for res in results:
            if res.thumbnail_url:
                local_path = os.path.join(str(config.THUMBNAIL_DIR), f"{res.id}.jpg")
                ok = await loop.run_in_executor(
                    None, download_image_sync, res.thumbnail_url, local_path
                )
                if ok:
                    res.local_thumbnail_path = local_path
                
        return results
