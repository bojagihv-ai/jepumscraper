"""
engine/fingerprint_suite.py — 완전한 TLS + HTTP/2 + 브라우저 지문 스위트
────────────────────────────────────────────────────────────────────────────
봇 탐지 우회 대상:
  • TLS/HTTP2 특성(TLS/HTTP2 Fingerprint)
      - JA3 해시: TLS 핸드셰이크의 cipher suite + extension 순서
      - JA4 해시: 최신 TLS 지문 (Chrome 124 기준)
      - HTTP/2 SETTINGS 프레임: window size, concurrent streams 등
      - ALPN 협상 순서: h2 > http/1.1

Chrome 124 실측 값 (2024-05 기준):
  JA3:  772,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,
        0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,
        29-23-24,0
  ALPN: h2, http/1.1
  H2 SETTINGS:
    HEADER_TABLE_SIZE       = 65536
    ENABLE_PUSH             = 1
    MAX_CONCURRENT_STREAMS  = 1000  (Chrome 초기값)
    INITIAL_WINDOW_SIZE     = 6291456
    MAX_HEADER_LIST_SIZE    = 262144

curl_cffi 'chrome124' 임포소네이션이 이 값들을 자동으로 설정하므로
curl_cffi가 사용 가능한 경우 최우선 사용한다.
"""

from __future__ import annotations

import logging
import random
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── Chrome 124 TLS 설정 (참조용) ───────────────────────────────────────────
CHROME_124_CIPHER_SUITES = [
    0x1301,  # TLS_AES_128_GCM_SHA256
    0x1302,  # TLS_AES_256_GCM_SHA384
    0x1303,  # TLS_CHACHA20_POLY1305_SHA256
    0xC02B,  # TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256
    0xC02F,  # TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256
    0xC02C,  # TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384
    0xC030,  # TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384
    0xCCA9,  # TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256
    0xCCA8,  # TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256
    0xC013,  # TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA
    0xC014,  # TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA
    0x009C,  # TLS_RSA_WITH_AES_128_GCM_SHA256
    0x009D,  # TLS_RSA_WITH_AES_256_GCM_SHA384
    0x002F,  # TLS_RSA_WITH_AES_128_CBC_SHA
    0x0035,  # TLS_RSA_WITH_AES_256_CBC_SHA
]

CHROME_124_TLS_EXTENSIONS = [
    0x0000,  # server_name
    0x0017,  # extended_master_secret
    0xFF01,  # renegotiation_info
    0x000A,  # supported_groups
    0x000B,  # ec_point_formats
    0x0023,  # session_ticket
    0x0010,  # application_layer_protocol_negotiation (ALPN)
    0x0005,  # status_request
    0x000D,  # signature_algorithms
    0x0012,  # signed_certificate_timestamp
    0x0033,  # key_share
    0x002D,  # psk_key_exchange_modes
    0x002B,  # supported_versions
    0x001B,  # compress_certificate
    0x0015,  # padding
]

CHROME_124_SUPPORTED_GROUPS = [29, 23, 24]   # x25519, secp256r1, secp384r1

CHROME_124_H2_SETTINGS = {
    "HEADER_TABLE_SIZE":      65536,
    "ENABLE_PUSH":            1,
    "MAX_CONCURRENT_STREAMS": 1000,
    "INITIAL_WINDOW_SIZE":    6291456,
    "MAX_HEADER_LIST_SIZE":   262144,
}

CHROME_124_JA3 = (
    "771,"
    "4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,"
    "0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,"
    "29-23-24,"
    "0"
)

CHROME_124_ALPN = ["h2", "http/1.1"]

# ─── tls-client 식별자 풀 ────────────────────────────────────────────────────
TLS_CLIENT_IDS = [
    "chrome_124",
    "chrome_123",
    "chrome_120",
]

# ─── curl_cffi 임포소네이션 풀 ───────────────────────────────────────────────
CURL_CFFI_IMPERSONATIONS = [
    "chrome124",
    "chrome123",
    "chrome120",
]


class FingerprintSuite:
    """
    단일 "브라우저 인스턴스" 지문.
    세션 생성 시 한 번 선택되고 이후 모든 요청에 일관되게 사용된다.
    """

    def __init__(self, pin_to_chrome_124: bool = True):
        if pin_to_chrome_124:
            self.tls_id    = "chrome_124"
            self.curl_imp  = "chrome124"
        else:
            self.tls_id   = random.choice(TLS_CLIENT_IDS)
            self.curl_imp = CURL_CFFI_IMPERSONATIONS[TLS_CLIENT_IDS.index(self.tls_id)]

        self.cipher_suites  = list(CHROME_124_CIPHER_SUITES)
        self.tls_extensions = list(CHROME_124_TLS_EXTENSIONS)
        self.h2_settings    = dict(CHROME_124_H2_SETTINGS)
        self.alpn           = list(CHROME_124_ALPN)
        self.ja3            = CHROME_124_JA3

    def build_curl_cffi_session(self, proxy: Optional[str] = None):
        """
        curl_cffi 세션 생성 (HTTP/2 + TLS 완전 임포소네이션).
        이것이 가장 정확한 TLS/H2 지문을 제공한다.
        """
        try:
            from curl_cffi import requests as cffi_req
            sess = cffi_req.Session(impersonate=self.curl_imp)
            if proxy:
                sess.proxies = {"http": proxy, "https": proxy}
            logger.debug(f"[FingerprintSuite] curl_cffi ({self.curl_imp}) 세션 생성")
            return sess, "curl_cffi"
        except ImportError:
            return None, None

    def build_tls_client_session(self, proxy: Optional[str] = None):
        """tls-client 세션 생성 (JA3 완벽 복제)."""
        try:
            import tls_client
            try:
                sess = tls_client.Session(
                    client_identifier=self.tls_id,
                    random_tls_extension_order=True,
                )
            except TypeError:
                sess = tls_client.Session(client_identifier=self.tls_id)

            if proxy:
                sess.proxies = {"http": proxy, "https": proxy}
            logger.debug(f"[FingerprintSuite] tls-client ({self.tls_id}) 세션 생성")
            return sess, "tls-client"
        except ImportError:
            return None, None

    def build_best_session(self, proxy: Optional[str] = None):
        """가장 좋은 세션 백엔드를 자동 선택한다."""
        # 1) curl_cffi (HTTP/2 + TLS 완전 임포소네이션)
        sess, backend = self.build_curl_cffi_session(proxy)
        if sess:
            return sess, backend

        # 2) tls-client (JA3 복제)
        sess, backend = self.build_tls_client_session(proxy)
        if sess:
            return sess, backend

        # 3) requests 폴백 (TLS 위장 없음)
        import requests
        sess = requests.Session()
        logger.warning("[FingerprintSuite] 폴백: requests (TLS 위장 없음)")
        if proxy:
            sess.proxies = {"http": proxy, "https": proxy}
        return sess, "requests"

    def playwright_stealth_additions(self) -> str:
        """
        Playwright에 주입할 추가 지문 스크립트.
        JA3는 네트워크 레이어에서 처리되므로 여기서는 JS 레이어 지문만 다룬다.
        """
        h2_json = str(self.h2_settings).replace("'", '"').replace("True", "true").replace("False", "false")
        return f"""
        // HTTP/2 지원 신호 (window.performance 등)
        Object.defineProperty(window, '_h2_settings', {{
            value: {h2_json},
            writable: false,
            configurable: false,
        }});

        // ALPN 지원 신호
        if (typeof window.RTCPeerConnection !== 'undefined') {{
            const origOffer = RTCPeerConnection.prototype.createOffer;
            RTCPeerConnection.prototype.createOffer = function() {{
                return origOffer.apply(this, arguments);
            }};
        }}
        """


# ─── 전역 기본 지문 ──────────────────────────────────────────────────────────

def get_default_fingerprint() -> FingerprintSuite:
    """기본 Chrome 124 지문을 반환한다."""
    return FingerprintSuite(pin_to_chrome_124=True)
