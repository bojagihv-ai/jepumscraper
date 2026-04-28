import os
import aiohttp
from typing import List
from scrapers.base_scraper import BaseScraper, ProductResult, download_image_sync
import config
from engines.text_matcher import extract_keywords

class NaverScraper(BaseScraper):
    def __init__(self):
        super().__init__()
        self.platform_name = "Naver"

    async def search(self, keyword: str) -> List[ProductResult]:
        """네이버 쇼핑 API를 사용하여 검색합니다."""
        if not config.NAVER_CLIENT_ID or not config.NAVER_CLIENT_SECRET:
            print("Warning: Naver API keys are not configured.")
            return []

        # 정확도를 위해 키워드 정제
        refined_keywords = extract_keywords(keyword)
        search_query = ' '.join(refined_keywords) if refined_keywords else keyword
        
        search_url = "https://openapi.naver.com/v1/search/shop.json"
        headers = {
            "X-Naver-Client-Id": config.NAVER_CLIENT_ID,
            "X-Naver-Client-Secret": config.NAVER_CLIENT_SECRET
        }
        params = {
            "query": search_query,
            "display": max(15, config.MAX_CANDIDATES),
            "start": 1,
            "sort": "sim"
        }

        results = []
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(search_url, headers=headers, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        items = data.get('items', [])
                        
                        for i, item in enumerate(items):
                            # 네이버 쇼핑 결과에서 태그 등 제거
                            title = item.get('title', '').replace('<b>', '').replace('</b>', '')
                            price = item.get('lprice', '0')
                            link = item.get('link', '')
                            image = item.get('image', '')
                            product_id = item.get('productId', str(i))
                            
                            pid = f"naver_{product_id}"
                            
                            # 썸네일 다운로드 동기 처리 (TODO: 비동기로 개선 가능)
                            local_thumb = os.path.join(config.THUMBNAIL_DIR, f"{pid}.jpg")
                            if download_image_sync(image, local_thumb):
                                result = ProductResult(
                                    id=pid,
                                    platform="Naver",
                                    title=title,
                                    price=price,
                                    product_url=link,
                                    thumbnail_url=image,
                                    local_thumbnail_path=local_thumb
                                )
                                results.append(result)
                    else:
                        print(f"Naver API error: {response.status}")
        except Exception as e:
            print(f"Error searching Naver: {e}")
            
        return results

    async def get_detail_page(self, product_url: str) -> List[str]:
        """
        네이버 스마트스토어 등 상세페이지 이미지 캡처
        playwright가 필요하지만, 상세 스크래핑은 detail_scraper에서 일괄 처리합니다.
        여기서는 예비로 남겨둠.
        """
        # detail_scraper.py에서 공통 처리하도록 하므로 빈 목록 반환
        return []
