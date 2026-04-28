"""
SearchService - API/스크래핑 이중 모드 + 중복 제거 + 판매자 다양성 + 플랫폼 우선순위
+ 검색 보고서 생성 + 실시간 진행 상황 전송
+ SimilarityScorer 연동: top-10 유사도 점수 정렬
"""
import asyncio
import logging
import re
import os
from typing import List, Dict, Any
from difflib import SequenceMatcher

from scrapers.base_scraper import ProductResult
from engines.similarity_scorer import SimilarityScorer
import progress_store

# API 기반 스크래퍼
from scrapers.naver_scraper import NaverScraper

# Playwright 스크래핑 기반 스크래퍼
from scrapers.naver_shopping_scraper import NaverShoppingScraper
from scrapers.coupang_scraper import CoupangScraper
from scrapers.gmarket_scraper import GmarketScraper
from scrapers.auction_scraper import AuctionScraper
from scrapers.elevenst_scraper import ElevenstScraper

logger = logging.getLogger(__name__)

# ── 플랫폼 우선순위 (낮을수록 먼저) ──────────────────────────────
PLATFORM_PRIORITY = {
    'coupang': 1,    # 쿠팡 최우선
    'naver':   2,    # 네이버 스마트스토어 2순위
    'elevenst': 3,   # 11번가
    'gmarket': 4,    # G마켓
    'auction': 5,    # 옥션 마지막
}

# 플랫폼별 스크래퍼 팩토리
PLATFORM_SCRAPERS: Dict[str, Dict[str, Any]] = {
    'naver': {
        'api':      NaverScraper,
        'scraping': NaverShoppingScraper,
        'label':    '네이버',
    },
    'coupang': {
        'api':      None,
        'scraping': CoupangScraper,
        'label':    '쿠팡',
    },
    'gmarket': {
        'api':      None,
        'scraping': GmarketScraper,
        'label':    'G마켓',
    },
    'auction': {
        'api':      None,
        'scraping': AuctionScraper,
        'label':    '옥션',
    },
    'elevenst': {
        'api':      None,
        'scraping': ElevenstScraper,
        'label':    '11번가',
    },
}

# 플랫폼 이름 한글 → 영문 매핑 (역방향)
PLATFORM_NAME_MAP = {
    '쿠팡': 'coupang', '네이버': 'naver', '네이버쇼핑': 'naver',
    '11번가': 'elevenst', 'G마켓': 'gmarket', '옥션': 'auction',
}

# ── 유틸리티 함수들 ──────────────────────────────────────────

def _extract_seller(title: str) -> str:
    """제목에서 브랜드/판매자 키워드 추출 (첫 단어를 사용)"""
    cleaned = re.sub(r'[^\w\s]', ' ', title).strip()
    parts = cleaned.split()
    if parts:
        return parts[0]
    return 'unknown'


def _deduplicate_by_platform(products: List[ProductResult]) -> List[ProductResult]:
    """
    플랫폼별로 다른 제한 적용:
    - 네이버, 쿠팡: 총 15개 제한, 동일 판매자(브랜드) 최대 3개
    - G마켓, 11번가, 옥션: 총 5개 제한, 동일 판매자 최대 1개
    - (URL 중복 제거)
    """
    seen_urls = set()
    platform_counts: Dict[str, int] = {}
    seller_counts: Dict[str, int] = {}
    unique: List[ProductResult] = []

    for p in products:
        platform = p.platform
        
        # 모든 플랫폼: 무조건 플랫폼별 5개, 동일 업체(브랜드) 제한 완화(최대 5개)
        max_platform = 5
        max_seller = 5

        # 플랫폼 한도 체크
        if platform_counts.get(platform, 0) >= max_platform:
            continue

        # 1) URL 중복 체크
        normalized_url = p.product_url.split('?')[0].rstrip('/')
        if normalized_url in seen_urls:
            continue

        # 2) 판매자 다양성 체크
        seller = _extract_seller(p.title)
        seller_key = f"{platform}_{seller}"
        current_seller_count = seller_counts.get(seller_key, 0)
        if current_seller_count >= max_seller:
            continue

        # 통과!
        seen_urls.add(normalized_url)
        platform_counts[platform] = platform_counts.get(platform, 0) + 1
        seller_counts[seller_key] = current_seller_count + 1
        unique.append(p)

    logger.info(f"[Dedup] 플랫폼별 통과 결과:")
    for pt, cnt in platform_counts.items():
        logger.info(f"  - {pt}: {cnt}개")
        
    return unique


def _sort_by_priority(products: List[ProductResult]) -> List[ProductResult]:
    """플랫폼 우선순위: 쿠팡 → 네이버 → 11번가 → G마켓 → 옥션"""
    platform_map = {
        '쿠팡': 1, '네이버': 2, '네이버쇼핑': 2,
        '11번가': 3, 'G마켓': 4, '옥션': 5,
    }
    return sorted(products, key=lambda p: platform_map.get(p.platform, 99))


class SearchService:
    def __init__(self, settings: Dict = None):
        self.settings = settings or {}
        self.scrapers: List = []
        self._build_scrapers()
        # 검색 보고서 저장
        self.last_report: Dict = {}

    def _build_scrapers(self):
        """설정에 따라 활성화된 스크래퍼 목록을 구성합니다."""
        platforms = self.settings.get('platforms', {})
        self.scrapers = []

        # 우선순위대로 스크래퍼 빌드
        sorted_platforms = sorted(PLATFORM_SCRAPERS.items(),
                                  key=lambda x: PLATFORM_PRIORITY.get(x[0], 99))

        for pkey, pinfo in sorted_platforms:
            cfg = platforms.get(pkey, {})

            # ── 하위 호환: 구 포맷 'enabled' 지원 ──
            if 'enabled' in cfg and 'api_enabled' not in cfg:
                if cfg.get('enabled', False):
                    if pinfo['api'] and not pinfo['scraping']:
                        cfg = {'api_enabled': True, 'scraping_enabled': False}
                    else:
                        cfg = {'api_enabled': False, 'scraping_enabled': True}
                else:
                    cfg = {'api_enabled': False, 'scraping_enabled': False}

            api_on      = cfg.get('api_enabled',      False)
            scraping_on = cfg.get('scraping_enabled', False)

            if api_on and pinfo.get('api'):
                try:
                    scraper = pinfo['api']()
                    self.scrapers.append(scraper)
                    logger.info(f"[SearchService] {pinfo['label']} → API 모드 활성화")
                except Exception as e:
                    logger.warning(f"[SearchService] {pinfo['label']} API 초기화 실패: {e}")

            if scraping_on and pinfo.get('scraping'):
                try:
                    scraper = pinfo['scraping']()
                    self.scrapers.append(scraper)
                    logger.info(f"[SearchService] {pinfo['label']} → 🤖 스크래핑 모드 활성화")
                except Exception as e:
                    logger.warning(f"[SearchService] {pinfo['label']} 스크래핑 초기화 실패: {e}")

        if not self.scrapers:
            logger.warning("[SearchService] 활성화된 플랫폼 없음. 설정을 확인하세요.")

        names = [s.platform_name for s in self.scrapers]
        if names:
            logger.info(f"[SearchService] 활성 스크래퍼: {', '.join(names)}")

    async def search_all_platforms(self, keyword: str, settings: Dict = None) -> List[ProductResult]:
        """
        활성화된 모든 플랫폼에서 동시에 검색합니다.
        중복 제거 + 판매자 다양성 + 플랫폼 우선순위 적용
        검색 보고서도 함께 생성합니다.
        """
        if settings:
            self.settings = settings
            self._build_scrapers()

        if not self.scrapers:
            logger.error("[SearchService] 활성화된 스크래퍼가 없습니다.")
            self.last_report = {"error": "활성화된 스크래퍼가 없습니다."}
            return []

        # 보고서 초기화
        report = {
            "keyword": keyword,
            "platforms": {},
            "total_raw": 0,
            "total_final": 0,
            "priority_order": "쿠팡 → 네이버 → 11번가 → G마켓 → 옥션",
            "limits": {
                "모든 플랫폼": "플랫폼당 무조건 5개씩, 업체는 최고 각기 다른 5군데 업체"
            },
            "dedup_method": "URL 중복 제거 + 판매자(브랜드) 다양성 확보"
        }

        logger.info(f"[SearchService] '{keyword}' 검색 시작 — {len(self.scrapers)}개 스크래퍼 병렬 실행")
        scraper_names = [s.platform_name for s in self.scrapers]
        progress_store.set_status(f"🔍 검색 시작: {', '.join(scraper_names)}")
        per_platform_timeout = float(self.settings.get('platform_timeout_sec', 70))

        async def safe_search(scraper) -> List[ProductResult]:
            platform = scraper.platform_name
            progress_store.set_status(f"🔍 [{platform}] 검색 중...")
            try:
                results = await asyncio.wait_for(
                    scraper.search(keyword),
                    timeout=per_platform_timeout,
                )
                logger.info(f"[{platform}] ✅ {len(results)}개 수집")
                
                # 보고서에 기록
                has_thumb = sum(1 for r in results if r.local_thumbnail_path)
                report["platforms"][platform] = {
                    "status": "✅ 성공",
                    "raw_count": len(results),
                    "with_thumbnail": has_thumb,
                    "without_thumbnail": len(results) - has_thumb,
                    "method": "API" if "API" in type(scraper).__name__ else "웹 스크래핑 (Playwright)",
                    "error": None
                }
                return results
            except Exception as e:
                logger.error(f"[{platform}] ❌ 오류 (건너뜀): {e}")
                report["platforms"][platform] = {
                    "status": "❌ 실패",
                    "raw_count": 0,
                    "with_thumbnail": 0,
                    "without_thumbnail": 0,
                    "method": "웹 스크래핑 (Playwright)",
                    "error": str(e)
                }
                return []

        results_list = await asyncio.gather(
            *(safe_search(s) for s in self.scrapers),
            return_exceptions=False,
        )

        all_products: List[ProductResult] = []
        for results in results_list:
            all_products.extend(results)

        report["total_raw"] = len(all_products)
        logger.info(f"[SearchService] 총 {len(all_products)}개 수집 (중복 제거 전)")

        progress_store.set_status(f"📊 {len(all_products)}개 결과를 정리하는 중...")

        # ── 후처리 파이프라인 ──
        # 1. 플랫폼 우선순위 정렬 (쿠팡 → 네이버 → 11번가 → ...)
        all_products = _sort_by_priority(all_products)

        # 2. 중복 제거 & 플랫폼/판매자별 리밋 적용
        all_products = _deduplicate_by_platform(all_products)

        # 3. 유사도 점수 계산 (SimilarityScorer)
        progress_store.set_status("🧮 유사도 점수 계산 중...")
        try:
            scorer = SimilarityScorer(source_name=keyword)
            all_products = scorer.score_all(all_products, image_analyzer=None)
            logger.info(f"[SearchService] 유사도 점수 계산 완료")
        except Exception as e:
            logger.warning(f"[SearchService] 유사도 점수 계산 실패 (건너뜀): {e}")

        # 4. 상위 20개로 제한 (UI에서 top-10 표시하지만 match_service 분류용으로 넉넉하게)
        TOP_POOL = 20
        all_products = all_products[:TOP_POOL]

        report["total_final"] = len(all_products)

        # 최종 플랫폼별 카운트
        final_counts: Dict[str, int] = {}
        for p in all_products:
            final_counts[p.platform] = final_counts.get(p.platform, 0) + 1

        for platform_name, count in final_counts.items():
            if platform_name in report["platforms"]:
                report["platforms"][platform_name]["final_count"] = count
            else:
                report["platforms"][platform_name] = {"final_count": count}

        # 점수 상위 10개 정보를 보고서에 추가
        report["top10"] = [
            {
                "rank": i + 1,
                "id": p.id,
                "title": p.title[:60],
                "platform": p.platform,
                "price": p.price,
                "score": p.similarity_score,
            }
            for i, p in enumerate(all_products[:10])
        ]

        self.last_report = report

        logger.info(f"[SearchService] 최종 {len(all_products)}개 (중복 제거 + 유사도 정렬)")
        return all_products
