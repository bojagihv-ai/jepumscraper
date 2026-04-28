"""
engine/ - JepumScraper 전문 우회 엔진 패키지
──────────────────────────────────────────────────────────────────────
8가지 봇 탐지 신호 대응 모듈:

  stealth          — Playwright 브라우저 지문 위장 (26점 스텔스 스크립트)
  human_behavior   — 베지어 곡선 마우스 + 관성 스크롤 + 읽기 정지 시뮬레이션
  bypass_engine    — Akamai / DataDome / PerimeterX / Cloudflare 쿠키 우회
  smart_session    — TLS 지문 위장 HTTP 세션 (curl_cffi / tls-client)
  captcha          — CAPTCHA 자동 해결 (2captcha / Capsolver / Anti-Captcha)
  session_manager  — 세션 나이 + 쿠키 이력 영속화 (SessionPool)
  rate_limiter     — 적응형 요청 속도 제어 (도메인별 토큰 버킷)
  navigation       — 플랫폼별 현실적 탐색 경로 (홈→카테고리→검색→상품)
  regional         — 계정/지역 신호 일관성 (한국 로케일 + timezone)
  fingerprint_suite — TLS/HTTP2 완전 지문 (JA3/JA4 + H2 SETTINGS)
  ip_manager       — IP 평판 관리 + 프록시 로테이션
  pro_crawler      — 마스터 오케스트레이터 (8신호 통합)
"""
