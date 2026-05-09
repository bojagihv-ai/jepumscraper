"""
배치 자동화 서비스 (batch_service.py)
─────────────────────────────────────
여러 키워드를 순차적으로 검색·상세캡처하며, 키워드 간 중복 제거 + 상세 보고서를 생성한다.

사용법 (main.py):
    from services.batch_service import batch_manager, init_batch_service
    init_batch_service(detail_scraper_instance, user_sessions_dict)
"""

import asyncio
import threading
import uuid
import json
import logging
import os
import time
import shutil
from pathlib import Path
from typing import Optional

import config
import progress_store

logger = logging.getLogger(__name__)

DETAIL_RETRY_STATUSES = {
    "security_check_required",
    "captcha_blocked",
    "wrong_window",
    "scroll_stalled",
}
DETAIL_SECURITY_STATUSES = {
    "security_check_required",
    "captcha_blocked",
}
DETAIL_STATUS_LABELS = {
    "security_check_required": "보안 확인 필요",
    "captcha_blocked": "보안 확인 차단",
    "wrong_window": "잘못된 창",
    "scroll_stalled": "스크롤 정체",
    "bad_screenshot": "캡처 품질 실패",
    "login_required": "로그인 필요",
    "incomplete_capture": "불완전 캡처",
    "empty_or_blocked": "빈 캡처/차단",
    "failed": "캡처 실패",
    "success": "캡처 성공",
}


def _detail_status_label(status: str) -> str:
    status = str(status or "unknown")
    return DETAIL_STATUS_LABELS.get(status, status)


def _detail_failure_group(status: str) -> str:
    status = str(status or "")
    if status in DETAIL_SECURITY_STATUSES:
        return "security_check"
    if status == "wrong_window":
        return "wrong_window"
    if status == "scroll_stalled":
        return "scroll_stalled"
    if status == "bad_screenshot":
        return "bad_screenshot"
    return "normal_failure"


def _detail_retry_reason(status: str) -> str:
    status = str(status or "")
    if status in DETAIL_SECURITY_STATUSES:
        return "Chrome에서 보안 확인이 필요합니다. 확인 후 재시도 대상입니다."
    if status == "wrong_window":
        return "상세페이지가 아닌 창이 잡혔습니다. 창 정리 후 재시도 대상입니다."
    if status == "scroll_stalled":
        return "스크롤이 움직이지 않아 캡처를 중단했습니다. 보안 확인 또는 페이지 정체 가능성이 있어 재시도 대상입니다."
    return ""


def _empty_detail_status_counts() -> dict:
    return {
        "security_check_required": 0,
        "captcha_blocked": 0,
        "wrong_window": 0,
        "scroll_stalled": 0,
        "bad_screenshot": 0,
        "login_required": 0,
        "other_failed": 0,
    }


def _ensure_report_detail_defaults(report: dict) -> dict:
    report.setdefault("detail_retryable", 0)
    report.setdefault("detail_security", 0)
    report.setdefault("detail_wrong_window", 0)
    report.setdefault("detail_scroll_stalled", 0)
    report.setdefault("detail_status_counts", _empty_detail_status_counts())
    report.setdefault("retry_queue", [])
    return report

# 배치 저장 폴더
BATCH_DIR = Path(config.BASE_DIR) / "batch_jobs"
BATCH_DIR.mkdir(exist_ok=True)

# main.py 에서 주입받는 공유 오브젝트
_detail_scraper_instance = None
_user_sessions_ref: dict = {}


def init_batch_service(detail_scraper_instance, user_sessions: dict):
    """main.py 시작 시 1회 호출 — detail_scraper 와 user_sessions 를 주입한다."""
    global _detail_scraper_instance, _user_sessions_ref
    _detail_scraper_instance = detail_scraper_instance
    _user_sessions_ref = user_sessions


# ────────────────────────────────────────────────────────────────────────────
#  BatchJob  ─  단일 배치 작업 상태 저장소
# ────────────────────────────────────────────────────────────────────────────
class BatchJob:
    """
    하나의 배치 작업.
    - keywords: [ {"text": "보자기", "auto_select": "top5"}, ... ]
    - auto_select 옵션:
        "all"          : 중복 제거 후 전체 선택
        "top5"         : 상위 5개 (예: top<N>)
        "score0.7"     : 유사도 ≥ 0.7 인 것만 (예: score<F>)
        "3"            : top3 와 같음 (숫자 문자열)
    """

    def __init__(
        self,
        batch_id: str,
        company_name: str,
        product_name: str,
        product_type: str,
        image_path: str,
        keywords: list,
        auto_select: str = "all",
        scrape_details: bool = True,
        output_gdrive: bool = True,
        output_local_dir: str = "",
    ):
        self.batch_id = batch_id
        self.company_name = company_name
        self.product_name = product_name
        self.product_type = product_type
        self.image_path = image_path
        self.keywords = keywords          # list of str or {"text":..., "auto_select":...}
        self.auto_select = auto_select    # 기본 자동 선택 기준
        self.scrape_details = scrape_details
        self.output_gdrive = output_gdrive          # 완료 후 GDrive 업로드
        self.output_local_dir = output_local_dir    # 완료 후 추가 로컬 폴더 복사 (빈 문자열=비활성)

        self.status = "pending"           # pending | running | paused | completed | cancelled | failed
        self.current_keyword_idx = -1
        self.keyword_reports: list = []   # 키워드 별 보고서
        self.overall_report: dict = {}
        self.retry_queue: list = []

        self.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.started_at = ""
        self.ended_at = ""
        self.log: list = []              # 간략 배치 로그 (최대 200줄)

        # 키워드 간 중복 제거 상태
        self.scraped_product_ids: set = set()
        self.scraped_norm_urls: set = set()

        # 일시정지/취소 제어
        self._pause_event = threading.Event()
        self._pause_event.set()       # 처음엔 일시정지 없음
        self._cancel_flag = False
        self._current_task = None     # 현재 실행 중인 asyncio Task

        # 키워드 보고서 초기화
        for i, kw in enumerate(keywords):
            kw_text = kw["text"] if isinstance(kw, dict) else str(kw)
            kw_sel = kw.get("auto_select", auto_select) if isinstance(kw, dict) else auto_select
            self.keyword_reports.append({
                "idx": i,
                "keyword": kw_text,
                "auto_select": kw_sel,
                "status": "pending",
                "session_id": None,
                "started_at": "",
                "ended_at": "",
                "duration_sec": 0.0,
                "candidates_found": 0,
                "candidates_auto_selected": 0,
                "selected_items": [],
                "detail_items": [],
                "duplicates": [],
                "details_scraped": 0,
                "detail_success": 0,
                "detail_failed": 0,
                "detail_cached": 0,      # 캐시에서 가져온 수
                "detail_small": 0,       # 의심스럽게 작은 캡처 (< 200KB) 수
                "detail_retryable": 0,
                "detail_security": 0,
                "detail_wrong_window": 0,
                "detail_scroll_stalled": 0,
                "detail_status_counts": _empty_detail_status_counts(),
                "retry_queue": [],
                "images_collected": 0,
                "error": None,
            })

    # ── 직렬화 ───────────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "company_name": self.company_name,
            "product_name": self.product_name,
            "product_type": self.product_type,
            "image_path": self.image_path,
            "keywords": self.keywords,
            "auto_select": self.auto_select,
            "scrape_details": self.scrape_details,
            "output_gdrive": self.output_gdrive,
            "output_local_dir": self.output_local_dir,
            "status": self.status,
            "current_keyword_idx": self.current_keyword_idx,
            "keyword_reports": self.keyword_reports,
            "overall_report": self.overall_report,
            "retry_queue": self.retry_queue,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "log": self.log[-100:],
        }

    def save(self):
        """현재 상태를 JSON 파일로 영속화"""
        try:
            job_dir = BATCH_DIR / self.batch_id
            job_dir.mkdir(exist_ok=True)
            with open(job_dir / "job.json", "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[Batch] 저장 실패 {self.batch_id}: {e}")

    def add_log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.log.append(entry)
        if len(self.log) > 200:
            self.log = self.log[-200:]
        progress_store.set_status(msg, self.batch_id)


# ────────────────────────────────────────────────────────────────────────────
#  BatchManager  ─  배치 작업 생성/실행 관리자 (싱글턴)
# ────────────────────────────────────────────────────────────────────────────
class BatchManager:
    _instance: Optional["BatchManager"] = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._jobs: dict[str, BatchJob] = {}
                inst._loop = asyncio.new_event_loop()
                inst._thread = threading.Thread(target=inst._run_loop, daemon=True, name="BatchLoop")
                inst._thread.start()
                cls._instance = inst
        return cls._instance

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # ── 외부 API ─────────────────────────────────────────────────────────────

    def create_job(
        self,
        company_name: str,
        product_name: str,
        product_type: str,
        image_path: str,
        keywords: list,
        auto_select: str = "all",
        scrape_details: bool = True,
        output_gdrive: bool = True,
        output_local_dir: str = "",
    ) -> str:
        batch_id = str(uuid.uuid4())
        job = BatchJob(
            batch_id=batch_id,
            company_name=company_name,
            product_name=product_name,
            product_type=product_type,
            image_path=image_path,
            keywords=keywords,
            auto_select=auto_select,
            scrape_details=scrape_details,
            output_gdrive=output_gdrive,
            output_local_dir=output_local_dir,
        )
        self._jobs[batch_id] = job
        job.save()
        return batch_id

    def get_job(self, batch_id: str) -> Optional[BatchJob]:
        job = self._jobs.get(batch_id)
        if job is None:
            self.load_from_disk()
            job = self._jobs.get(batch_id)
        return job

    def list_jobs(self) -> list:
        self.load_from_disk()
        jobs = [j.to_dict() for j in self._jobs.values()]
        return sorted(jobs, key=lambda j: j.get("created_at", ""), reverse=True)

    def start_job(self, batch_id: str, settings: dict) -> bool:
        job = self.get_job(batch_id)
        if not job:
            return False
        if job.status == "running":
            return True
        if job.status == "completed":
            return False

        job.status = "running"
        job._pause_event.set()
        job._cancel_flag = False
        job.retry_queue = []

        fut = asyncio.run_coroutine_threadsafe(
            self._run_job(job, settings), self._loop
        )
        return True

    def pause_job(self, batch_id: str) -> bool:
        job = self.get_job(batch_id)
        if not job or job.status != "running":
            return False
        job._pause_event.clear()
        job.status = "paused"
        job.add_log("⏸ 일시정지됨")
        job.save()
        return True

    def resume_job(self, batch_id: str) -> bool:
        job = self.get_job(batch_id)
        if not job or job.status != "paused":
            return False
        job.status = "running"
        job._pause_event.set()
        job.add_log("▶ 재개됨")
        job.save()
        return True

    def cancel_job(self, batch_id: str) -> bool:
        job = self.get_job(batch_id)
        if not job:
            return False
        job._cancel_flag = True
        job._pause_event.set()  # 일시정지 상태라면 해제
        job.status = "cancelled"
        job.add_log("🛑 취소됨")
        job.save()
        return True

    def load_from_disk(self):
        """서버 재시작 시 저장된 배치 작업 목록을 복원 (메타 정보만)"""
        for job_dir in BATCH_DIR.iterdir():
            if not job_dir.is_dir():
                continue
            job_file = job_dir / "job.json"
            if not job_file.exists():
                continue
            try:
                with open(job_file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                bid = d["batch_id"]
                if bid in self._jobs:
                    continue
                # 실행 중이었던 작업은 중단됨으로 표시
                if d.get("status") in ("running", "paused"):
                    d["status"] = "cancelled"
                    d.setdefault("log", []).append("[서버 재시작으로 인해 취소됨]")

                job = BatchJob(
                    batch_id=bid,
                    company_name=d.get("company_name", ""),
                    product_name=d.get("product_name", ""),
                    product_type=d.get("product_type", ""),
                    image_path=d.get("image_path", ""),
                    keywords=d.get("keywords", []),
                    auto_select=d.get("auto_select", "all"),
                    scrape_details=d.get("scrape_details", True),
                    output_gdrive=d.get("output_gdrive", False),
                    output_local_dir=d.get("output_local_dir", ""),
                )
                job.status = d.get("status", "pending")
                job.keyword_reports = d.get("keyword_reports", job.keyword_reports)
                computed_retry_queue = []
                for report in job.keyword_reports:
                    _ensure_report_detail_defaults(report)
                    computed_retry_queue.extend(
                        self._refresh_report_detail_summary(
                            report,
                            report.get("keyword", ""),
                            int(report.get("idx", 0) or 0),
                        )
                    )
                job.overall_report = d.get("overall_report", {})
                job.retry_queue = d.get("retry_queue", []) or computed_retry_queue
                job.overall_report.setdefault("total_retryable_details", len(job.retry_queue))
                job.overall_report.setdefault(
                    "total_security_checks",
                    sum(int((r.get("detail_security") or 0)) for r in job.keyword_reports),
                )
                job.overall_report.setdefault(
                    "total_wrong_window",
                    sum(int((r.get("detail_wrong_window") or 0)) for r in job.keyword_reports),
                )
                job.overall_report.setdefault(
                    "total_scroll_stalled",
                    sum(int((r.get("detail_scroll_stalled") or 0)) for r in job.keyword_reports),
                )
                job.created_at = d.get("created_at", "")
                job.started_at = d.get("started_at", "")
                job.ended_at = d.get("ended_at", "")
                job.log = d.get("log", [])
                self._jobs[bid] = job
            except Exception as e:
                logger.warning(f"[Batch] 복원 실패 {job_dir}: {e}")

    # ── 핵심 실행 루프 ────────────────────────────────────────────────────────

    async def _run_job(self, job: BatchJob, settings: dict):
        job.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        total_kw = len(job.keywords)
        job.retry_queue = []
        job.add_log(f"🚀 배치 시작: {job.product_name} · 키워드 {total_kw}개")
        job.save()

        total_selected = 0
        total_scraped = 0
        total_images = 0
        total_duplicates = 0

        for i, kw_entry in enumerate(job.keywords):
            # ─ 취소 체크 ─
            if job._cancel_flag:
                break

            # ─ 일시정지 대기 (블로킹 없이 비동기 처리) ─
            while not job._pause_event.is_set():
                await asyncio.sleep(0.5)
            if job._cancel_flag:
                break

            kw_text = kw_entry["text"] if isinstance(kw_entry, dict) else str(kw_entry)
            kw_sel = kw_entry.get("auto_select", job.auto_select) if isinstance(kw_entry, dict) else job.auto_select

            job.current_keyword_idx = i
            report = job.keyword_reports[i]
            _ensure_report_detail_defaults(report)
            report["status"] = "running"
            report["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            report["detail_status_counts"] = _empty_detail_status_counts()
            report["retry_queue"] = []
            job.add_log(f"[{i+1}/{total_kw}] 🔍 키워드 검색 시작: {kw_text}")
            job.save()

            try:
                # ── 1. 검색 ──
                session_id, all_candidates = await self._run_search(job, kw_text, settings)
                report["session_id"] = session_id

                if all_candidates is None:
                    raise RuntimeError("검색 결과가 없습니다 (플랫폼 설정/API 키 확인)")

                report["candidates_found"] = len(all_candidates)
                job.add_log(f"[{i+1}/{total_kw}] 후보 {len(all_candidates)}개 수집됨")

                # ── 2. 중복 제거 ──
                selected, duplicates = self._dedup(job, all_candidates)
                report["duplicates"] = duplicates
                total_duplicates += len(duplicates)

                if duplicates:
                    job.add_log(
                        f"[{i+1}/{total_kw}] ⚠️ 중복 제외 {len(duplicates)}개"
                        f" (사유: {duplicates[0]['reason'][:30]}...)"
                    )

                # ── 3. 자동 선택 ──
                to_scrape = self._auto_select(selected, kw_sel)
                report["candidates_auto_selected"] = len(to_scrape)
                report["selected_items"] = [self._product_report_item(p) for p in to_scrape]
                total_selected += len(to_scrape)
                job.add_log(f"[{i+1}/{total_kw}] ✅ 자동 선택: {len(to_scrape)}개 (기준: {kw_sel})")

                # 선택된 제품의 ID/URL 을 중복 검사 풀에 등록
                from services import adaptive_learning as _al
                for p in to_scrape:
                    pid = getattr(p, "id", "")
                    n_url = _al.normalize_url(getattr(p, "product_url", ""))
                    if pid:
                        job.scraped_product_ids.add(pid)
                    if n_url:
                        job.scraped_norm_urls.add(n_url)

                # ── 4. 상세페이지 캡처 ──
                if job.scrape_details and to_scrape:
                    job.add_log(f"[{i+1}/{total_kw}] 📸 상세페이지 캡처 중 ({len(to_scrape)}개)...")
                    scraped_data, n_ok, n_fail, n_imgs, n_cached, n_small = await self._scrape_details(
                        job, session_id, to_scrape, settings
                    )
                    report["details_scraped"] = n_ok + n_fail
                    report["detail_success"] = n_ok
                    report["detail_failed"] = n_fail
                    report["detail_cached"] = n_cached
                    report["detail_small"] = n_small
                    report["images_collected"] = n_imgs
                    report["detail_items"] = self._detail_report_items(to_scrape, scraped_data)
                    retry_items = self._refresh_report_detail_summary(report, kw_text, i)
                    job.retry_queue.extend(retry_items)
                    total_scraped += n_ok
                    total_images += n_imgs
                    log_extra = []
                    if n_fail:
                        log_extra.append(f"{n_fail}개 실패")
                    if n_cached:
                        log_extra.append(f"💾 캐시 {n_cached}개")
                    if n_small:
                        log_extra.append(f"⚠️ 소형 {n_small}개")
                    if retry_items:
                        log_extra.append(f"재시도 큐 {len(retry_items)}개")
                    job.add_log(
                        f"[{i+1}/{total_kw}] 캡처 완료: {n_ok}개 성공"
                        + ((" · " + " · ".join(log_extra)) if log_extra else "")
                        + f" · 이미지 {n_imgs}장"
                    )

                    # 세션에 scraped_data 반영 (review 페이지에서 볼 수 있게)
                    if session_id and session_id in _user_sessions_ref:
                        _user_sessions_ref[session_id]["last_scraped_data"] = scraped_data

                    # ── 출력 저장 (GDrive / 로컬 추가폴더) ──
                    await self._save_outputs(job, kw_text, scraped_data, settings, i+1, total_kw)

                report["status"] = "completed"
                job.add_log(f"[{i+1}/{total_kw}] ✅ 키워드 완료: {kw_text}")

            except Exception as e:
                logger.error(f"[Batch] 키워드 처리 실패 ({kw_text}): {e}", exc_info=True)
                report["status"] = "failed"
                report["error"] = str(e)[:300]
                job.add_log(f"[{i+1}/{total_kw}] ❌ 실패: {str(e)[:80]}")

            # 소요 시간 계산
            report["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                t0 = time.mktime(time.strptime(report["started_at"], "%Y-%m-%d %H:%M:%S"))
                t1 = time.mktime(time.strptime(report["ended_at"], "%Y-%m-%d %H:%M:%S"))
                report["duration_sec"] = round(t1 - t0, 1)
            except Exception:
                pass
            job.save()

        # ── 최종 보고서 ──
        completed_kw = sum(1 for r in job.keyword_reports if r["status"] == "completed")
        job.overall_report = {
            "total_keywords": total_kw,
            "completed_keywords": completed_kw,
            "failed_keywords": sum(1 for r in job.keyword_reports if r["status"] == "failed"),
            "total_candidates_selected": total_selected,
            "total_details_scraped": total_scraped,
            "total_images": total_images,
            "total_duplicates_excluded": total_duplicates,
            "total_retryable_details": len(job.retry_queue),
            "total_security_checks": sum(
                int((r.get("detail_security") or 0)) for r in job.keyword_reports
            ),
            "total_wrong_window": sum(
                int((r.get("detail_wrong_window") or 0)) for r in job.keyword_reports
            ),
            "total_scroll_stalled": sum(
                int((r.get("detail_scroll_stalled") or 0)) for r in job.keyword_reports
            ),
        }
        job.status = "cancelled" if job._cancel_flag else "completed"
        job.ended_at = time.strftime("%Y-%m-%d %H:%M:%S")
        job.add_log(
            f"🏁 배치 {'취소' if job._cancel_flag else '완료'}"
            f": {completed_kw}/{total_kw}개 키워드"
            f", 총 이미지 {total_images}장"
        )
        job.save()

    # ── 내부 헬퍼 ────────────────────────────────────────────────────────────

    def _dedup(self, job: BatchJob, candidates: list) -> tuple[list, list]:
        """
        이미 다른 키워드에서 수집된 제품을 제외한다.
        반환: (선택 후보 list, 중복 정보 list)
        """
        from services import adaptive_learning as _al

        selected = []
        duplicates = []
        for p in candidates:
            pid = getattr(p, "id", "")
            n_url = _al.normalize_url(getattr(p, "product_url", ""))

            if pid and pid in job.scraped_product_ids:
                duplicates.append({
                    "product_id": pid,
                    "title": getattr(p, "title", "")[:60],
                    "platform": getattr(p, "platform", ""),
                    "score": float(getattr(p, "similarity_score", 0) or 0),
                    "reason": "이전 키워드에서 동일 제품 ID로 이미 수집됨",
                })
            elif n_url and n_url in job.scraped_norm_urls:
                duplicates.append({
                    "product_id": pid,
                    "title": getattr(p, "title", "")[:60],
                    "platform": getattr(p, "platform", ""),
                    "score": float(getattr(p, "similarity_score", 0) or 0),
                    "reason": "이전 키워드에서 동일 URL로 이미 수집됨",
                })
            else:
                selected.append(p)
        return selected, duplicates

    def _auto_select(self, candidates: list, criteria: str) -> list:
        """
        criteria 기준으로 후보를 자동 선택한다.
        "all" → 전체
        "top<N>" → 상위 N개
        "score<F>" → 유사도 ≥ F
        숫자 문자열 → top<N> 과 동일
        """
        if not criteria or criteria == "all":
            return candidates
        c = str(criteria).strip().lower()
        if c.startswith("top"):
            try:
                n = int(c[3:])
                return candidates[:n]
            except ValueError:
                return candidates
        if c.startswith("score"):
            try:
                min_s = float(c[5:])
                return [p for p in candidates if float(getattr(p, "similarity_score", 0) or 0) >= min_s]
            except ValueError:
                return candidates
        # 숫자만 입력된 경우
        try:
            n = int(c)
            return candidates[:n]
        except ValueError:
            return candidates

    def _product_report_item(self, product) -> dict:
        """Return a compact, JSON-safe snapshot for the batch report."""
        try:
            score = round(float(getattr(product, "similarity_score", 0) or 0), 1)
        except Exception:
            score = 0.0
        return {
            "product_id": getattr(product, "id", "") or "",
            "title": getattr(product, "title", "") or "",
            "platform": getattr(product, "platform", "") or "",
            "seller_name": getattr(product, "seller_name", "") or "",
            "price": getattr(product, "price", "") or "",
            "product_url": getattr(product, "product_url", "") or "",
            "score": score,
        }

    def _detail_status_counts(self, items: list[dict]) -> dict:
        counts = _empty_detail_status_counts()
        for item in items or []:
            if item.get("capture_success"):
                continue
            status = str(item.get("status") or "unknown")
            if status in counts:
                counts[status] += 1
            else:
                counts["other_failed"] += 1
        return counts

    def _detail_retry_items(self, keyword: str, keyword_idx: int, items: list[dict]) -> list[dict]:
        retry_items: list[dict] = []
        for item in items or []:
            status = str(item.get("status") or "")
            if status not in DETAIL_RETRY_STATUSES:
                continue
            retry_items.append({
                "keyword": keyword,
                "keyword_idx": keyword_idx,
                "product_id": item.get("product_id", ""),
                "title": item.get("title", ""),
                "platform": item.get("platform", ""),
                "seller_name": item.get("seller_name", ""),
                "product_url": item.get("product_url", ""),
                "status": status,
                "status_label": item.get("status_label") or _detail_status_label(status),
                "failure_group": item.get("failure_group") or _detail_failure_group(status),
                "reason": item.get("reason") or _detail_retry_reason(status),
                "target_title": item.get("target_title", ""),
                "method": item.get("method", ""),
            })
        return retry_items

    def _refresh_report_detail_summary(
        self,
        report: dict,
        keyword: str = "",
        keyword_idx: int = 0,
    ) -> list[dict]:
        _ensure_report_detail_defaults(report)
        items = report.get("detail_items") or []
        status_counts = self._detail_status_counts(items)
        retry_items = self._detail_retry_items(keyword, keyword_idx, items)
        report["detail_status_counts"] = status_counts
        report["detail_security"] = (
            status_counts.get("security_check_required", 0)
            + status_counts.get("captcha_blocked", 0)
        )
        report["detail_wrong_window"] = status_counts.get("wrong_window", 0)
        report["detail_scroll_stalled"] = status_counts.get("scroll_stalled", 0)
        report["detail_retryable"] = len(retry_items)
        report["retry_queue"] = retry_items
        return retry_items

    def _detail_report_items(self, products: list, scraped_data: dict) -> list[dict]:
        """Merge selected products with per-product capture diagnostics."""
        items: list[dict] = []
        scraped_data = scraped_data or {}
        for product in products:
            item = self._product_report_item(product)
            product_id = item.get("product_id", "")
            detail = scraped_data.get(product_id, {}) or {}
            screenshots = detail.get("screenshots") or []
            status = detail.get("status", "unknown")
            diagnostics = detail.get("diagnostics") or {}
            reason = detail.get("reason") or detail.get("error") or _detail_retry_reason(status) or ""
            item.update({
                "status": status,
                "status_label": _detail_status_label(status),
                "failure_group": "" if screenshots else _detail_failure_group(status),
                "retryable": (not screenshots and str(status or "") in DETAIL_RETRY_STATUSES),
                "retry_reason": _detail_retry_reason(status),
                "capture_success": bool(screenshots),
                "image_count": len(screenshots),
                "from_cache": bool(detail.get("from_cache")),
                "small_capture": bool(detail.get("small_capture")),
                "total_size_kb": detail.get("total_size_kb", 0),
                "method": detail.get("method", ""),
                "reason": reason,
                "target_title": diagnostics.get("target_title", ""),
                "diagnostics": {
                    key: diagnostics.get(key)
                    for key in (
                        "target_title",
                        "capture_version",
                        "scroll_driver",
                        "scroll_count_limit",
                        "gmarket_expected_scrolls",
                        "gmarket_reached_bottom",
                        "auction_expected_scrolls",
                        "auction_reached_bottom",
                        "tail_duplicates",
                        "unique_frames",
                    )
                    if diagnostics.get(key) not in (None, "")
                },
            })
            items.append(item)
        return items

    async def _run_search(
        self, job: BatchJob, keyword: str, settings: dict
    ) -> tuple[str, list | None]:
        """키워드 검색 → (session_id, 정렬된 후보 list | None)"""
        from services.search_service import SearchService
        from engines.similarity_scorer import SimilarityScorer
        from services.match_service import MatchService

        session_id = str(uuid.uuid4())

        try:
            s_svc = SearchService(settings)
            raw = await s_svc.search_all_platforms(keyword, settings, job_id=job.batch_id)
            if not raw:
                return session_id, None

            # 유사도 점수 계산
            scorer = SimilarityScorer(source_name=job.product_name)
            scored = scorer.score_all(raw, image_analyzer=None)
            scored.sort(key=lambda p: float(getattr(p, "similarity_score", 0) or 0), reverse=True)

            # tier 분류
            categorized = MatchService().classify_matches(job.image_path, keyword, scored)
            all_products = {p.id: p for tier_list in categorized.values() for p in tier_list}
            all_candidates = sorted(
                list(all_products.values()),
                key=lambda p: float(getattr(p, "similarity_score", 0) or 0),
                reverse=True,
            )
            top_candidates = all_candidates[:10]

            session_data = {
                "source_image": job.image_path,
                "source_name": f"{job.product_name} [{keyword}]",
                "results": categorized,
                "all_products": all_products,
                "search_report": getattr(s_svc, "last_report", {}) or {},
                "all_candidates": all_candidates,
                "top_candidates": top_candidates,
                "batch_id": job.batch_id,
                "batch_keyword": keyword,
            }
            # user_sessions 에 등록 → /review/<session_id> 에서 열 수 있음
            _user_sessions_ref[session_id] = session_data
            return session_id, all_candidates

        except Exception as e:
            logger.error(f"[Batch] 검색 실패 ({keyword}): {e}", exc_info=True)
            raise

    async def _save_outputs(
        self,
        job: BatchJob,
        keyword: str,
        scraped_data: dict,
        settings: dict,
        kw_idx: int,
        total_kw: int,
    ):
        """
        GDrive 업로드 및 추가 로컬 폴더 복사 (job.output_gdrive / job.output_local_dir 에 따라).
        scraped_data: {product_id: {screenshots:[...], title:..., ...}}
        """
        import os as _os
        from services.gdrive_uploader import build_detail_filename
        loop = asyncio.get_running_loop()
        batch_timestamp = time.strftime('%Y%m%d_%H%M%S')

        # 모든 제품의 screenshot 목록 수집
        all_shots: list[tuple[str, str, str, str, str, str, int, int]] = []
        for pid, info in scraped_data.items():
            shots = [
                shot for shot in (info.get("screenshots") or [])
                if shot and _os.path.exists(shot)
            ]
            total_shots = len(shots)
            title = info.get("title", "")
            platform = info.get("platform", "")
            seller_name = info.get("seller_name", "") or platform
            product_url = info.get("product_url", "")
            for idx, shot in enumerate(shots, start=1):
                if shot and _os.path.exists(shot):
                    all_shots.append((shot, pid, title, platform, seller_name, product_url, idx, total_shots))

        if not all_shots:
            return

        # ── Google Drive 업로드 ──
        if job.output_gdrive:
            from services import gdrive_uploader
            if gdrive_uploader.is_ready():
                gdrive_folder = settings.get("gdrive_folder", "JepumScraper 상세이미지")
                job.add_log(f"[{kw_idx}/{total_kw}] ☁️ GDrive 업로드 중 ({len(all_shots)}개)...")
                n_uploaded = 0
                for file_path, pid, title, platform, seller_name, product_url, idx, total_shots in all_shots:
                    if job._cancel_flag:
                        break
                    try:
                        fid = await loop.run_in_executor(
                            None,
                            gdrive_uploader.upload_detail_image,
                            file_path, keyword, pid, gdrive_folder, title,
                            platform, seller_name, product_url, idx, total_shots, batch_timestamp,
                        )
                        if fid:
                            n_uploaded += 1
                    except Exception as e:
                        logger.warning(f"[Batch] GDrive 업로드 실패 {file_path}: {e}")
                job.add_log(f"[{kw_idx}/{total_kw}] ☁️ GDrive 완료: {n_uploaded}/{len(all_shots)}개 업로드")
            else:
                job.add_log(f"[{kw_idx}/{total_kw}] ⚠️ GDrive 미연결 — setup_gdrive.py 실행 필요")

        # ── 로컬 폴더 추가 복사 ──
        out_dir = (job.output_local_dir or "").strip()
        if out_dir:
            import re as _re
            safe_kw = _re.sub(r'[\\/:*?"<>|]', '_', keyword)[:40]
            dest_base = Path(out_dir) / safe_kw
            try:
                dest_base.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                job.add_log(f"[{kw_idx}/{total_kw}] ❌ 로컬폴더 생성 실패: {e}")
                return

            job.add_log(f"[{kw_idx}/{total_kw}] 💾 로컬 복사 중 → {out_dir}")
            n_copied = 0

            def _do_copy():
                nonlocal n_copied
                for file_path, pid, title, platform, seller_name, product_url, idx, total_shots in all_shots:
                    try:
                        target_filename = build_detail_filename(
                            file_path,
                            keyword=keyword,
                            title=title,
                            platform=platform,
                            seller_name=seller_name,
                            product_id=pid,
                            product_url=product_url,
                            index=idx,
                            total=total_shots,
                            timestamp=batch_timestamp,
                        )
                        dest_file = dest_base / target_filename
                        # 같은 이름 충돌 방지
                        stem = dest_file.stem
                        suffix = dest_file.suffix
                        duplicate_idx = 2
                        while dest_file.exists():
                            dest_file = dest_base / f"{stem}_{duplicate_idx}{suffix}"
                            duplicate_idx += 1
                        shutil.copy2(file_path, dest_file)
                        n_copied += 1
                    except Exception as e:
                        logger.warning(f"[Batch] 로컬 복사 실패 {file_path}: {e}")

            await loop.run_in_executor(None, _do_copy)
            job.add_log(f"[{kw_idx}/{total_kw}] 💾 로컬 복사 완료: {n_copied}/{len(all_shots)}개 → {out_dir}/{safe_kw}/")

    async def _scrape_details(
        self, job: BatchJob, session_id: str, products: list, settings: dict
    ) -> tuple[dict, int, int, int, int, int]:
        """
        상세페이지 캡처.
        반환: (scraped_data_dict, n_ok, n_fail, n_images, n_cached, n_small)
        n_cached : 캐시에서 가져온 수
        n_small  : 총 파일 크기가 200KB 미만인 의심 캡처 수 (봇 탐지 가능성)
        """
        import os as _os
        from services import adaptive_learning as _al
        try:
            from services.detail_scraper import is_detail_result_usable as _detail_usable
        except Exception:
            def _detail_usable(detail):
                return bool((detail or {}).get("screenshots"))

        SMALL_THRESHOLD = 200 * 1024  # 200 KB

        if _detail_scraper_instance is None:
            logger.warning("[Batch] detail_scraper 미초기화 — 상세 캡처 건너뜀")
            return {}, 0, 0, 0, 0, 0

        slice_height = int(settings.get("slice_height", 0) or 0)
        scraped_data: dict = {}
        n_ok = 0
        n_fail = 0
        n_imgs = 0
        n_cached = 0
        n_small = 0

        for p in products:
            if job._cancel_flag:
                break
            try:
                is_cached = False
                cached = _al.get_detail_cache(p.product_url)
                if cached and not _detail_usable(cached):
                    logger.info("[Batch] stale/unusable detail cache ignored for %s", getattr(p, "id", "?"))
                    cached = None
                if cached:
                    res = cached
                    is_cached = True
                    n_cached += 1
                else:
                    res = await _detail_scraper_instance.capture_detail_page(
                        p.product_url,
                        p.id,
                        slice_height=slice_height,
                        job_id=job.batch_id,
                        platform=p.platform,
                    )
                    if res.get("screenshots") and _detail_usable(res):
                        _al.save_detail_cache(
                            p.product_url, p.platform, p.id,
                            res, res.get("status", "success")
                        )

                shots = res.get("screenshots") or []
                total_size = 0
                for shot in shots:
                    try:
                        total_size += _os.path.getsize(shot)
                    except OSError:
                        pass
                small_capture = bool(total_size > 0 and total_size < SMALL_THRESHOLD and not is_cached)

                # 의심스럽게 작은 캡처 감지 (봇 차단 / 오류 페이지일 가능성)
                if small_capture:
                    n_small += 1
                    logger.warning(
                        f"[Batch] 소형 캡처 의심 — {getattr(p, 'id', '?')}"
                        f" ({total_size//1024}KB, {len(shots)}장)"
                    )

                scraped_data[p.id] = {
                    "screenshots": shots,
                    "title": getattr(p, "title", ""),
                    "platform": getattr(p, "platform", ""),
                    "seller_name": getattr(p, "seller_name", "") or getattr(p, "platform", ""),
                    "product_url": getattr(p, "product_url", ""),
                    "status": res.get("status", "unknown"),
                    "from_cache": is_cached,
                    "image_count": len(shots),
                    "total_size_kb": round(total_size / 1024, 1) if total_size else 0,
                    "small_capture": small_capture,
                    "method": res.get("method", ""),
                    "reason": res.get("reason") or res.get("error") or "",
                    "diagnostics": res.get("diagnostics") or {},
                }
                n_imgs += len(shots)
                if shots:
                    n_ok += 1
                else:
                    n_fail += 1

            except Exception as e:
                logger.warning(f"[Batch] 상세 캡처 실패 {getattr(p, 'id', '?')}: {e}")
                n_fail += 1
                scraped_data[getattr(p, "id", str(uuid.uuid4()))] = {
                    "screenshots": [],
                    "title": getattr(p, "title", ""),
                    "platform": getattr(p, "platform", ""),
                    "seller_name": getattr(p, "seller_name", "") or getattr(p, "platform", ""),
                    "product_url": getattr(p, "product_url", ""),
                    "status": "failed",
                    "image_count": 0,
                    "total_size_kb": 0,
                    "small_capture": False,
                    "from_cache": False,
                    "method": "",
                    "error": str(e)[:120],
                }

        return scraped_data, n_ok, n_fail, n_imgs, n_cached, n_small


# ── 모듈 레벨 싱글턴 ─────────────────────────────────────────────────────────
batch_manager = BatchManager()
