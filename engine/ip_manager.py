"""
engine/ip_manager.py — IP 평판 + 프록시 로테이션
────────────────────────────────────────────────────────────────────────────
봇 탐지 우회 대상:
  • IP 평판(IP Reputation)
      - 데이터센터 IP = 봇 신호 → 주거용 프록시(residential) 우선
      - 동일 IP 과다 요청 → IP 로테이션
      - IP-국가 불일치 → 한국 IP 유지
      - ASN 평판 점수 → 알려진 봇 ASN 회피

기능:
  ProxyManager  : 프록시 풀 관리 + 실패 블랙리스트
  DirectMode    : 프록시 없이 직접 연결 (로컬 IP 사용)

프록시 우선순위:
  1. Residential (주거용) — 가장 신뢰도 높음
  2. ISP (인터넷 서비스 제공업체 프록시)
  3. Mobile (모바일 데이터)
  4. Datacenter (데이터센터) — 최후 수단

설정:
  .env 파일에 PROXY_LIST=socks5://...,http://... 또는
  PROXY_FILE=proxies.txt 로 프록시 목록 제공
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ─── 프록시 실패 임계값 ───────────────────────────────────────────────────────
MAX_FAILS   = 3          # 이 횟수 실패 시 블랙리스트
BLACKLIST_TTL = 600      # 블랙리스트 유지 시간 (초)
COOLDOWN    = 30         # 동일 프록시 재사용 쿨다운 (초)

# ─── ASN 블랙리스트 (알려진 데이터센터/봇 ASN) ──────────────────────────────
# 이 ASN에서 오는 IP는 봇 점수가 높아 차단 위험
DATACENTER_ASNS = {
    "AS14061",  # DigitalOcean
    "AS16276",  # OVH
    "AS14618",  # Amazon AWS
    "AS15169",  # Google Cloud
    "AS8075",   # Microsoft Azure
    "AS13335",  # Cloudflare
    "AS20473",  # Choopa/Vultr
    "AS63949",  # Linode
    "AS396982", # Google Cloud
}


@dataclass
class Proxy:
    """단일 프록시 항목."""
    url: str                         # 예: socks5://user:pass@host:port
    proxy_type: str = "unknown"      # residential|isp|mobile|datacenter|unknown
    country: str    = "KR"
    fail_count: int = 0
    blacklisted_until: float = 0.0
    last_used: float = 0.0
    success_count: int = 0

    @property
    def host(self) -> str:
        return urlparse(self.url).hostname or ""

    @property
    def is_blacklisted(self) -> bool:
        return self.fail_count >= MAX_FAILS and time.time() < self.blacklisted_until

    @property
    def is_cooling(self) -> bool:
        return time.time() - self.last_used < COOLDOWN

    @property
    def score(self) -> float:
        """높을수록 좋은 프록시."""
        type_score = {
            "residential": 10.0,
            "mobile":      8.0,
            "isp":         6.0,
            "datacenter":  2.0,
            "unknown":     4.0,
        }.get(self.proxy_type, 4.0)
        fail_penalty  = self.fail_count * 2.0
        success_bonus = min(self.success_count * 0.3, 5.0)
        return type_score - fail_penalty + success_bonus

    def on_fail(self):
        self.fail_count += 1
        if self.fail_count >= MAX_FAILS:
            self.blacklisted_until = time.time() + BLACKLIST_TTL
            logger.warning(f"[ProxyManager] 블랙리스트: {self.host} ({BLACKLIST_TTL}s)")

    def on_success(self):
        self.fail_count = max(0, self.fail_count - 1)
        self.success_count += 1
        self.last_used = time.time()


class ProxyManager:
    """
    프록시 풀 관리 + 자동 로테이션.

    사용법:
        pm = ProxyManager()
        proxy_url = pm.get()          # 최적 프록시 반환
        pm.on_fail(proxy_url)         # 실패 보고
        pm.on_success(proxy_url)      # 성공 보고
    """

    def __init__(self):
        self._proxies: List[Proxy] = []
        self._lock    = Lock()
        self._load_from_env()
        self._direct_mode = len(self._proxies) == 0

    # ── 외부 API ──────────────────────────────────────────────

    def get(self, require_type: Optional[str] = None) -> Optional[str]:
        """
        사용 가능한 최적 프록시 URL을 반환한다.
        프록시가 없으면 None (직접 연결).
        """
        if self._direct_mode:
            return None

        with self._lock:
            candidates = [
                p for p in self._proxies
                if not p.is_blacklisted and not p.is_cooling
            ]
            if require_type:
                typed = [p for p in candidates if p.proxy_type == require_type]
                if typed:
                    candidates = typed

            if not candidates:
                # 쿨다운 무시하고 블랙리스트 아닌 것
                candidates = [p for p in self._proxies if not p.is_blacklisted]

            if not candidates:
                logger.warning("[ProxyManager] 사용 가능한 프록시 없음")
                return None

            # 점수 기반 가중치 랜덤 선택
            scores  = [p.score for p in candidates]
            total   = sum(scores)
            weights = [s / total for s in scores]
            chosen  = random.choices(candidates, weights=weights, k=1)[0]
            chosen.last_used = time.time()
            return chosen.url

    def get_residential(self) -> Optional[str]:
        """주거용 프록시를 우선 반환."""
        return self.get(require_type="residential") or self.get()

    def on_fail(self, proxy_url: str):
        """프록시 실패 보고."""
        with self._lock:
            for p in self._proxies:
                if p.url == proxy_url:
                    p.on_fail()
                    break

    def on_success(self, proxy_url: str):
        """프록시 성공 보고."""
        with self._lock:
            for p in self._proxies:
                if p.url == proxy_url:
                    p.on_success()
                    break

    def add(self, url: str, proxy_type: str = "unknown", country: str = "KR"):
        """프록시를 풀에 추가한다."""
        with self._lock:
            if not any(p.url == url for p in self._proxies):
                self._proxies.append(Proxy(url=url, proxy_type=proxy_type, country=country))
                self._direct_mode = False
                logger.debug(f"[ProxyManager] 프록시 추가: {urlparse(url).hostname}")

    @property
    def is_direct(self) -> bool:
        return self._direct_mode

    @property
    def available_count(self) -> int:
        return len([p for p in self._proxies if not p.is_blacklisted])

    # ── 환경 변수에서 로드 ────────────────────────────────────

    def _load_from_env(self):
        # PROXY_LIST=socks5://...,http://...
        proxy_list = os.environ.get("PROXY_LIST", "")
        if proxy_list:
            for url in proxy_list.split(","):
                url = url.strip()
                if url:
                    ptype = self._infer_type(url)
                    self.add(url, proxy_type=ptype)

        # PROXY_FILE=proxies.txt (한 줄에 URL 하나, # 주석 지원)
        proxy_file = os.environ.get("PROXY_FILE", "")
        if proxy_file and Path(proxy_file).exists():
            for line in Path(proxy_file).read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split()
                    url   = parts[0]
                    ptype = parts[1] if len(parts) > 1 else self._infer_type(url)
                    self.add(url, proxy_type=ptype)

        if self._proxies:
            logger.info(f"[ProxyManager] {len(self._proxies)}개 프록시 로드됨")
        else:
            logger.info("[ProxyManager] 프록시 없음 → 직접 연결 모드")

    @staticmethod
    def _infer_type(url: str) -> str:
        lower = url.lower()
        if "resi" in lower or "residential" in lower:
            return "residential"
        if "mobile" in lower:
            return "mobile"
        if "isp" in lower:
            return "isp"
        if "dc" in lower or "datacenter" in lower:
            return "datacenter"
        return "unknown"


# ─── IP 평판 체크 (간단 버전) ────────────────────────────────────────────────

def check_ip_is_datacenter(ip: str) -> bool:
    """
    IP가 데이터센터 IP인지 간단히 확인한다.
    실제 환경에서는 ipinfo.io나 ip-api.com API 사용 권장.
    """
    try:
        import requests
        resp = requests.get(
            f"http://ip-api.com/json/{ip}?fields=org,hosting",
            timeout=5,
        )
        data = resp.json()
        return data.get("hosting", False)
    except Exception:
        return False


# ─── 전역 싱글톤 ─────────────────────────────────────────────────────────────

_MANAGER: Optional[ProxyManager] = None


def get_manager() -> ProxyManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ProxyManager()
    return _MANAGER


def get_proxy() -> Optional[str]:
    """간편 API: 최적 프록시 URL 반환 (없으면 None)."""
    return get_manager().get()
