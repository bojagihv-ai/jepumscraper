import asyncio
import threading
import uuid
import logging
import os
import shutil
from services.history_db import create_job, update_job_status, add_result
from services.search_service import SearchService
from services.match_service import MatchService
from services.detail_scraper import DetailScraper
import config

logger = logging.getLogger(__name__)

class BackgroundJobQueue:
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance
        
    def _init(self):
        self._loop = asyncio.new_event_loop()
        self._queue = asyncio.Queue()
        self._thread = threading.Thread(target=self._start_loop, daemon=True)
        self._thread.start()
        
    def _start_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._worker())
        
    async def _worker(self):
        # 작업자에 필요한 서비스 각각 인스턴스화
        match_service = MatchService()
        
        while True:
            job = await self._queue.get()
            job_id = job['id']
            try:
                logger.info(f"[JobQueue] 처리 시작 - JobID: {job_id}")
                update_job_status(job_id, 'processing')
                await self._process_job(job, match_service)
                logger.info(f"[JobQueue] 처리 완료 - JobID: {job_id}")
            except Exception as e:
                logger.error(f"[JobQueue] 작업 오류 (JobID: {job_id}): {e}", exc_info=True)
                update_job_status(job_id, 'failed')
            finally:
                self._queue.task_done()

    async def _process_job(self, job, match_service):
        job_id = job['id']
        keyword = job['keyword']
        image_path = job['image_path']
        settings = job['settings']
        
        # 1. 스크래퍼 초기화
        search_service = SearchService(settings)
        detail_scraper = DetailScraper()
        
        # 2. 검색 실행
        raw_results = await search_service.search_all_platforms(keyword, settings)
        if not raw_results:
            update_job_status(job_id, 'completed', total_found=0, total_saved=0)
            return
            
        # 3. 매칭 실행
        categorized = match_service.classify_matches(image_path, keyword, raw_results)
        
        total_found = sum(len(lst) for lst in categorized.values())
        total_saved = 0
        
        # 4. 상세 캡처 동기화 실행 (자동 캡처 조건: 티어 1~2단계)
        slice_height = settings.get('slice_height', 0) # 0이면 한장으로
        
        for tier, tier_name in [(1, 'tier1'), (2, 'tier2')]:
            for p in categorized.get(tier_name, []):
                # 상세페이지 캡처
                res = await detail_scraper.capture_detail_page(p.product_url, p.id, slice_height=slice_height)
                detail_path = ''
                if slice_height > 0 and "screenshots" in res and res["screenshots"]:
                    # 분할 모드라면 첫 번째 샷 경로, 추후 리스트를 DB에 문자열로 통째 저장할 수 있도록 수정
                    detail_path = ";".join(res["screenshots"])
                elif res.get("screenshots") and len(res["screenshots"]) > 0:
                    detail_path = res["screenshots"][0] # fullpage fallback
                    
                result_data = {
                    'platform': p.platform,
                    'id': p.id,
                    'title': p.title,
                    'price': p.price,
                    'product_url': p.product_url,
                    'thumbnail_path': p.local_thumbnail_path,
                    'detail_path': detail_path,
                    'match_tier': tier
                }
                add_result(job_id, result_data)
                total_saved += 1
                
        # 5. 작업 상태 업데이트
        update_job_status(job_id, 'completed', total_found=total_found, total_saved=total_saved)

    def add_job(self, keyword, image_path, settings):
        """ 메인 스레드(Flask)에서 호출하는 작업 추가 메서드 """
        job_id = str(uuid.uuid4())
        
        # 원본 이미지 복사 (작업 전용 디렉토리)
        safe_ext = os.path.splitext(image_path)[1]
        save_img_path = os.path.join(config.DATA_DIR, 'input', f"job_{job_id}{safe_ext}")
        shutil.copy(image_path, save_img_path)
        
        create_job(job_id, keyword, save_img_path, settings)
        asyncio.run_coroutine_threadsafe(self._queue.put({
            'id': job_id,
            'keyword': keyword,
            'image_path': save_img_path,
            'settings': settings
        }), self._loop)
        
        return job_id

job_queue = BackgroundJobQueue()
