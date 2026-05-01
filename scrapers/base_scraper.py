from dataclasses import dataclass
from typing import List
import os

def download_image_sync(url: str, save_path: str) -> bool:
    """간단한 동기식 이미지 다운로드 헬퍼"""
    import requests
    import config
    try:
        if url.startswith('http'):
            # headers to bypass simple blocking
            headers = {'User-Agent': config.USER_AGENT_LIST[0]}
            proxies = None
            try:
                from engine.ip_manager import get_proxy
                proxy_url = get_proxy()
                if proxy_url:
                    proxies = {"http": proxy_url, "https": proxy_url}
            except Exception:
                proxies = None
            resp = requests.get(url, stream=True, timeout=10, headers=headers, proxies=proxies)
            if resp.status_code == 200:
                with open(save_path, 'wb') as f:
                    for chunk in resp.iter_content(1024):
                        f.write(chunk)
                return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Image download failed: {url} - {e}")
    return False

@dataclass
class ProductResult:
    id: str             # 자체 생성 고유 ID (플랫폼_상품ID 형태 권장)
    platform: str       # 네이버, 쿠팡, G마켓 등
    title: str          # 제품명
    price: str          # 가격 (문자열 형태)
    product_url: str    # 상품 상세 페이지 URL
    thumbnail_url: str  # 썸네일 이미지 URL
    local_thumbnail_path: str = "" # 로컬에 다운로드된 썸네일 경로
    match_tier: int = 0 # 매칭 단계 (1=동일, 2=유사, 3=색상다름, 0=미분류)
    similarity_score: float = 0.0  # 0~100 유사도 점수 (specs/04 기준)

class BaseScraper:
    def __init__(self):
        self.platform_name = "Base"
        self.last_status = ""
        self.last_error = ""

    async def search(self, keyword: str) -> List[ProductResult]:
        """키워드로 상품을 검색하고 결과를 반환합니다."""
        raise NotImplementedError

    async def get_detail_page(self, product_url: str) -> List[str]:
        """주어진 상세 페이지 URL에서 상세 이미지 URL 목록이나 로컬 경로 목록을 반환합니다."""
        raise NotImplementedError

    def _ensure_dir(self, dir_path: str):
        if not os.path.exists(dir_path):
            os.makedirs(dir_path)
