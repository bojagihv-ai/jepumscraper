import asyncio
import logging
import random
import re
import time
from difflib import SequenceMatcher
from typing import Any, Dict, List

import config
import progress_store
from engines.similarity_scorer import SimilarityScorer
from scrapers.auction_scraper import AuctionScraper
from scrapers.base_scraper import ProductResult
from scrapers.coupang_scraper import CoupangScraper
from scrapers.elevenst_scraper import ElevenstScraper
from scrapers.gmarket_scraper import GmarketScraper
from scrapers.naver_scraper import NaverScraper
from scrapers.naver_shopping_scraper import NaverShoppingScraper
from services import adaptive_learning
from engine.ip_manager import get_manager as get_proxy_manager, use_proxy_for

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    "success": "성공",
    "zero_result": "결과 없음",
    "blocked": "차단/보안 확인",
    "captcha": "보안 확인 필요",
    "login_required": "로그인 필요",
    "throttled": "요청 제한",
    "timeout": "시간 초과",
    "cooldown_skip": "대기",
    "manual_required": "사용자 검색 필요",
    "error": "실패",
}

PLATFORM_PRIORITY = {
    "coupang": 1,
    "naver": 2,
    "elevenst": 3,
    "gmarket": 4,
    "auction": 5,
}

PLATFORM_SCRAPERS: Dict[str, Dict[str, Any]] = {
    "naver": {
        "api": NaverScraper,
        "scraping": NaverShoppingScraper,
        "label": "Naver",
    },
    "coupang": {
        "api": None,
        "scraping": CoupangScraper,
        "label": "Coupang",
    },
    "gmarket": {
        "api": None,
        "scraping": GmarketScraper,
        "label": "Gmarket",
    },
    "auction": {
        "api": None,
        "scraping": AuctionScraper,
        "label": "Auction",
    },
    "elevenst": {
        "api": None,
        "scraping": ElevenstScraper,
        "label": "11st",
    },
}


def _extract_seller(title: str) -> str:
    cleaned = re.sub(r"[^\w\s]", " ", title or "").strip()
    parts = cleaned.split()
    return parts[0].lower() if parts else "unknown"


def _title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def _deduplicate_by_platform(products: List[ProductResult]) -> List[ProductResult]:
    seen_urls = set()
    platform_counts: Dict[str, int] = {}
    unique: List[ProductResult] = []

    for product in products:
        platform_key = adaptive_learning.normalize_platform(product.platform, product.product_url)
        normalized_url = adaptive_learning.normalize_url(product.product_url)
        if not normalized_url or normalized_url in seen_urls:
            continue

        duplicate_title = any(
            adaptive_learning.normalize_platform(p.platform, p.product_url) == platform_key
            and _title_similarity(p.title, product.title) > 0.96
            for p in unique
        )
        if duplicate_title:
            continue

        seen_urls.add(normalized_url)
        platform_counts[platform_key] = platform_counts.get(platform_key, 0) + 1
        unique.append(product)

    logger.info("[Dedup] final per-platform counts: %s", platform_counts)
    return unique


def _sort_by_priority(products: List[ProductResult]) -> List[ProductResult]:
    return sorted(
        products,
        key=lambda p: PLATFORM_PRIORITY.get(
            adaptive_learning.normalize_platform(p.platform, p.product_url),
            99,
        ),
    )


def _reserve_priority_products(
    products: List[ProductResult],
    platform_key: str,
    min_count: int,
    limit: int,
) -> List[ProductResult]:
    if limit <= 0:
        return []
    if min_count <= 0:
        return products[:limit]

    platform_key = adaptive_learning.normalize_platform(platform_key)
    selected: List[ProductResult] = []
    selected_keys = set()

    def add(product: ProductResult) -> None:
        key = getattr(product, "id", "") or id(product)
        if key not in selected_keys and len(selected) < limit:
            selected.append(product)
            selected_keys.add(key)

    priority_products = [
        product for product in products
        if adaptive_learning.normalize_platform(product.platform, product.product_url) == platform_key
    ]
    for product in priority_products[:min_count]:
        add(product)
    for product in products:
        add(product)
    return selected


class SearchService:
    def __init__(self, settings: Dict = None):
        self.settings = settings or {}
        self.scrapers: List = []
        self.last_report: Dict = {}
        self._build_scrapers()

    def _build_scrapers(self):
        platforms = self.settings.get("platforms", {})
        self.scrapers = []

        sorted_platforms = sorted(
            PLATFORM_SCRAPERS.items(),
            key=lambda item: PLATFORM_PRIORITY.get(item[0], 99),
        )

        for platform_key, platform_info in sorted_platforms:
            cfg = platforms.get(platform_key, {})
            if "enabled" in cfg and "api_enabled" not in cfg:
                cfg = {
                    "api_enabled": bool(cfg.get("enabled") and platform_info.get("api")),
                    "scraping_enabled": bool(cfg.get("enabled") and platform_info.get("scraping")),
                }

            if cfg.get("api_enabled") and platform_info.get("api"):
                try:
                    scraper = platform_info["api"]()
                    self.scrapers.append(scraper)
                    logger.info("[SearchService] %s API enabled", platform_info["label"])
                except Exception as e:
                    logger.warning("[SearchService] %s API init failed: %s", platform_info["label"], e)

            if cfg.get("scraping_enabled") and platform_info.get("scraping"):
                try:
                    scraper = platform_info["scraping"]()
                    self.scrapers.append(scraper)
                    logger.info("[SearchService] %s browser enabled", platform_info["label"])
                except Exception as e:
                    logger.warning("[SearchService] %s scraper init failed: %s", platform_info["label"], e)

        if not self.scrapers:
            logger.warning("[SearchService] no enabled scrapers")
        else:
            logger.info("[SearchService] enabled scrapers: %s", ", ".join(s.platform_name for s in self.scrapers))

    async def search_all_platforms(
        self,
        keyword: str,
        settings: Dict = None,
        job_id: str = "",
    ) -> List[ProductResult]:
        if settings:
            self.settings = settings
            self._build_scrapers()

        if not self.scrapers:
            self.last_report = {"error": "활성화된 플랫폼이 없습니다."}
            return []

        report = {
            "keyword": keyword,
            "platforms": {},
            "total_raw": 0,
            "total_final": 0,
            "priority_order": "Coupang -> Naver -> 11st -> Gmarket -> Auction",
            "limits": {
                "gentle_mode": "검색 후보는 수집된 전체를 표시하고, 상세 캡처는 선택 상품만 진행",
            },
            "dedup_method": "URL 중복 제거 + 플랫폼별 거의 동일한 제목 제거",
            "adaptive": adaptive_learning.get_effective_settings(self.settings),
        }

        scraper_names = [s.platform_name for s in self.scrapers]
        logger.info("[SearchService] %r search started with %d scrapers", keyword, len(self.scrapers))
        progress_store.set_status(f"검색 시작: {', '.join(scraper_names)}", job_id)
        per_platform_timeout = float(self.settings.get("platform_timeout_sec") or 70)
        coupang_first_mode = bool(self.settings.get("coupang_first_mode", getattr(config, "COUPANG_FIRST_MODE", True)))
        coupang_min_final = max(0, min(5, int(self.settings.get("coupang_min_final") or getattr(config, "COUPANG_MIN_FINAL", 3))))
        report["limits"]["coupang_first_mode"] = (
            "쿠팡을 먼저 수집하고, 전체 후보 목록 안에서 유사도 순으로 정렬"
            if coupang_first_mode else "꺼짐"
        )
        report["limits"]["detail_queue"] = "화면 캡처 상세 수집은 브라우저 포커스 보호를 위해 1개씩 처리"

        async def safe_search(scraper) -> List[ProductResult]:
            platform = scraper.platform_name
            platform_key = adaptive_learning.normalize_platform(type(scraper).__name__, platform)
            method = adaptive_learning.method_name(scraper)
            started = time.monotonic()
            proxy_selection = None
            progress_store.set_status(f"[{platform}] 검색 중...", job_id)

            try:
                await adaptive_learning.wait_turn(platform_key, "search", method)
                with use_proxy_for(platform_key, "search") as proxy_selection:
                    results = await asyncio.wait_for(scraper.search(keyword), timeout=per_platform_timeout)
                duration_ms = int((time.monotonic() - started) * 1000)
                success = len(results) > 0
                scraper_status = getattr(scraper, "last_status", "") or ""
                scraper_error = getattr(scraper, "last_error", "") or ""
                status = "success" if success else (scraper_status if scraper_status else "zero_result")
                if status == "success" and not success:
                    status = "zero_result"
                message = f"{len(results)} results" if not scraper_error else scraper_error
                adaptive_learning.record_method_result(
                    platform=platform_key,
                    stage="search",
                    method=method,
                    success=success,
                    status=status,
                    duration_ms=duration_ms,
                    job_id=job_id,
                    message=message,
                    metadata={
                        "keyword": keyword,
                        "raw_count": len(results),
                        **proxy_selection.metadata(),
                    },
                )
                get_proxy_manager().record_result(
                    proxy_selection,
                    platform=platform_key,
                    status=status,
                    success=success,
                    duration_ms=duration_ms,
                    error="" if success else message,
                )

                has_thumb = sum(1 for r in results if getattr(r, "local_thumbnail_path", ""))
                report["platforms"][platform] = {
                    "status": STATUS_LABELS.get(status, "실패"),
                    "raw_count": len(results),
                    "with_thumbnail": has_thumb,
                    "without_thumbnail": len(results) - has_thumb,
                    "method": method,
                    "duration_ms": duration_ms,
                    "error": None if success or status == "zero_result" else message,
                    "policy": adaptive_learning.get_platform_policy(platform_key),
                    "proxy": proxy_selection.metadata(),
                }
                return results

            except adaptive_learning.CircuitOpen as e:
                report["platforms"][platform] = {
                    "status": "대기",
                    "raw_count": 0,
                    "with_thumbnail": 0,
                    "without_thumbnail": 0,
                    "method": method,
                    "error": f"cooldown until {e.until}: {e.reason}",
                    "policy": adaptive_learning.get_platform_policy(platform_key),
                }
                adaptive_learning.log_event(
                    job_id=job_id,
                    stage="search",
                    platform=platform_key,
                    method=method,
                    status="cooldown_skip",
                    success=False,
                    message=e.reason,
                    metadata={"keyword": keyword, "until": e.until},
                )
                logger.warning("[%s] skipped by cooldown: %s", platform, e.reason)
                return []

            except Exception as e:
                duration_ms = int((time.monotonic() - started) * 1000)
                status = adaptive_learning.classify_exception(e)
                proxy_metadata = proxy_selection.metadata() if proxy_selection else {"proxy_mode": "direct", "proxy_profile_id": "direct"}
                adaptive_learning.record_method_result(
                    platform=platform_key,
                    stage="search",
                    method=method,
                    success=False,
                    status=status,
                    duration_ms=duration_ms,
                    job_id=job_id,
                    message=str(e),
                    metadata={"keyword": keyword, **proxy_metadata},
                )
                get_proxy_manager().record_result(
                    proxy_selection,
                    platform=platform_key,
                    status=status,
                    success=False,
                    duration_ms=duration_ms,
                    error=str(e),
                )
                report["platforms"][platform] = {
                    "status": STATUS_LABELS.get(status, "실패"),
                    "raw_count": 0,
                    "with_thumbnail": 0,
                    "without_thumbnail": 0,
                    "method": method,
                    "duration_ms": duration_ms,
                    "error": str(e),
                    "policy": adaptive_learning.get_platform_policy(platform_key),
                    "proxy": proxy_metadata,
                }
                logger.error("[%s] search failed: %s", platform, e)
                return []

        search_concurrency = int(self.settings.get("search_concurrency") or getattr(config, "SEARCH_CONCURRENCY", 1))
        gentle_mode = bool(self.settings.get("gentle_scraping_mode", getattr(config, "GENTLE_SCRAPING_MODE", True)))
        if gentle_mode:
            search_concurrency = 1

        def scraper_platform_key(scraper) -> str:
            return adaptive_learning.normalize_platform(
                getattr(scraper, "platform_name", ""),
                type(scraper).__name__,
            )

        run_scrapers = list(self.scrapers)
        if coupang_first_mode:
            run_scrapers.sort(
                key=lambda scraper: (
                    0 if scraper_platform_key(scraper) == "coupang" else 1,
                    PLATFORM_PRIORITY.get(scraper_platform_key(scraper), 99),
                )
            )

        results_list = []
        if search_concurrency <= 1:
            for idx, scraper in enumerate(run_scrapers):
                results_list.append(await safe_search(scraper))
                if idx < len(run_scrapers) - 1:
                    if coupang_first_mode and scraper_platform_key(scraper) == "coupang":
                        progress_store.set_status("쿠팡 결과 확보. 다른 플랫폼 보강 검색으로 이어갑니다.", job_id)
                        continue
                    min_delay = float(self.settings.get("inter_platform_delay_min") or getattr(config, "INTER_PLATFORM_DELAY_MIN", 8.0))
                    max_delay = float(self.settings.get("inter_platform_delay_max") or getattr(config, "INTER_PLATFORM_DELAY_MAX", 18.0))
                    delay = random.uniform(min_delay, max(max_delay, min_delay))
                    progress_store.set_status(f"다음 플랫폼까지 {delay:.1f}초 대기 중...", job_id)
                    await asyncio.sleep(delay)
        else:
            semaphore = asyncio.Semaphore(max(1, min(search_concurrency, len(run_scrapers))))

            async def limited_search(scraper):
                async with semaphore:
                    return await safe_search(scraper)

            results_list = await asyncio.gather(*(limited_search(s) for s in run_scrapers))

        all_products: List[ProductResult] = []
        for results in results_list:
            all_products.extend(results)

        report["total_raw"] = len(all_products)
        progress_store.set_status(f"{len(all_products)}개 결과 정리 중...", job_id)

        all_products = _sort_by_priority(all_products)
        all_products = _deduplicate_by_platform(all_products)

        progress_store.set_status("유사도 점수 계산 중...", job_id)
        try:
            scorer = SimilarityScorer(source_name=keyword)
            all_products = scorer.score_all(all_products, image_analyzer=None)
        except Exception as e:
            logger.warning("[SearchService] similarity scoring skipped: %s", e)

        report["total_final"] = len(all_products)

        final_counts: Dict[str, int] = {}
        for product in all_products:
            final_counts[product.platform] = final_counts.get(product.platform, 0) + 1
        for platform_name, count in final_counts.items():
            report["platforms"].setdefault(platform_name, {})["final_count"] = count

        report["top10"] = [
            {
                "rank": i + 1,
                "id": product.id,
                "title": product.title[:60],
                "platform": product.platform,
                "price": product.price,
                "score": product.similarity_score,
            }
            for i, product in enumerate(all_products[:10])
        ]
        report["learning"] = adaptive_learning.get_learning_summary(limit=20)
        self.last_report = report

        logger.info("[SearchService] final %d products", len(all_products))
        return all_products
