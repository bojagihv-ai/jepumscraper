"""
services/detail_scraper.py - 상세페이지 캡처 (ProScraper 스텔스 강화)
──────────────────────────────────────────────────────────────────────
3단계 폴백 전략:
  네이버:    화면 캡처 (로그인 세션 활용)
  쿠팡/기타: 1) Playwright + ProScraper 스텔스  →  2) DrissionPage

ProScraper 스텔스 통합:
  - fingerprint.py + advanced_fingerprint.py 통합 스크립트 주입
  - 쿠팡: 메인 페이지 방문 후 상세 페이지 (Akamai 세션 확립)
"""
import asyncio
import contextvars
import glob
import logging
import os
import random
import re
import shutil
import sys
import tempfile
import time
import subprocess
from pathlib import Path
from typing import Dict, List
from urllib.parse import parse_qs, urlparse, unquote

# 상위 디렉토리 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import numpy as np
from PIL import Image, ImageDraw, ImageFile, ImageFont
from services import adaptive_learning

logger = logging.getLogger(__name__)

ImageFile.LOAD_TRUNCATED_IMAGES = True

DETAIL_CAPTURE_VERSION = 82

DETAIL_EXPAND_JS = r"""
(() => {
  const keywords = [
    "\uc0c1\uc138\uc815\ubcf4 \ub354\ubcf4\uae30",
    "\uc0c1\uc138\uc815\ubcf4\ub354\ubcf4\uae30",
    "\uc0c1\ud488\uc815\ubcf4 \ub354\ubcf4\uae30",
    "\uc0c1\ud488\uc0c1\uc138 \ub354\ubcf4\uae30",
    "\ud3bc\uce58\uae30",
    "\ud3bc\uccd0\ubcf4\uae30",
    "\uc0c1\uc138\uc815\ubcf4 \ud3bc\uccd0\ubcf4\uae30",
    "\uc0c1\uc138\uc815\ubcf4\ud3bc\uccd0\ubcf4\uae30"
  ];
  const clicked = [];
  const isSafeDetailButton = (el, text) => {
    const compact = (text || '').replace(/\s+/g, '');
    if (!compact) return false;
    const isExpandWord = keywords.some(kw => compact.toLowerCase().includes(String(kw).toLowerCase().replace(/\s+/g,'')));
    if (!isExpandWord && (/[>›]/.test(text) || compact.includes('\ud648') || compact.includes('\uce74\ud14c\uace0\ub9ac'))) return false;
    if (compact.includes('\ubb38\uad6c') || compact.includes('\ud3ec\uc7a5\uc6a9\ud488') || compact.includes('\ud3ec\uc7a5\uc9c0')) return false;
    if (el.closest('header,nav,[class*="breadcrumb"],[class*="location"],[class*="category"],[class*="gnb"],[class*="lnb"]')) return false;
    const href = (el.getAttribute && (el.getAttribute('href') || '')) || '';
    if (href && !href.startsWith('#') && !href.toLowerCase().startsWith('javascript') && !href.toLowerCase().includes('detail')) return false;
    const rect = el.getBoundingClientRect();
    if (rect.top + window.scrollY < 450) return false;
    return true;
  };
  const candidates = Array.from(document.querySelectorAll('button,a,[role="button"],.button,div,span'));
  for (const el of candidates) {
    const raw = (el.innerText || el.textContent || el.getAttribute('aria-label') || '') + '';
    const text = raw.replace(/\s+/g, ' ').trim();
    if (!text || text.length > 90) continue;
    const normalized = text.toLowerCase().replace(/\s+/g, '');
    const matched = keywords.some((kw) => normalized.includes(String(kw).toLowerCase().replace(/\s+/g, '')));
    if (!matched) continue;
    if (!isSafeDetailButton(el, text)) continue;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (rect.width < 24 || rect.height < 12 || style.visibility === 'hidden' || style.display === 'none') continue;
    try {
      el.scrollIntoView({block: 'center', inline: 'center'});
      el.click();
      clicked.push(text);
    } catch (err) {}
    if (clicked.length >= 8) break;
  }
  const selectors = [
    '[class*="detail"][class*="more"]',
    '[class*="more"][class*="button"]',
    '[class*="button"][class*="more"]',
    '[id*="detail"][id*="more"]',
    '[id*="more"][id*="detail"]',
    '.button__detail-more',
    '.box__detail-more button',
    '.section__detail-more button',
    '.detail-more button'
  ];
  for (const selector of selectors) {
    for (const el of Array.from(document.querySelectorAll(selector))) {
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      if (rect.width < 20 || rect.height < 10 || style.visibility === 'hidden' || style.display === 'none') continue;
      const raw = (el.innerText || el.textContent || el.getAttribute('aria-label') || '') + '';
      const text = raw.replace(/\s+/g, ' ').trim();
      if (!isSafeDetailButton(el, text || selector)) continue;
      try {
        el.scrollIntoView({block: 'center', inline: 'center'});
        el.click();
        clicked.push(`selector:${selector}`);
      } catch (err) {}
      if (clicked.length >= 12) break;
    }
    if (clicked.length >= 12) break;
  }
  if (!document.getElementById('codex-detail-expand-style')) {
    const style = document.createElement('style');
    style.id = 'codex-detail-expand-style';
    style.textContent = `
      [class*="detail"], [id*="detail"], [class*="vip"], [class*="item"], [class*="product"] {
        max-height: none !important;
      }
      [class*="detail"][style*="overflow"], [id*="detail"][style*="overflow"],
      [class*="vip"][style*="overflow"], [class*="item"][style*="overflow"],
      [class*="product"][style*="overflow"] {
        overflow: visible !important;
      }
    `;
    document.documentElement.appendChild(style);
  }
  for (const el of Array.from(document.querySelectorAll('[class*="detail"],[id*="detail"],[class*="vip"],[class*="item"],[class*="product"]')).slice(0, 300)) {
    try {
      const cs = window.getComputedStyle(el);
      const maxHeight = parseFloat(cs.maxHeight || '0');
      if (maxHeight > 200 && maxHeight < 5000) {
        el.style.setProperty('max-height', 'none', 'important');
        el.style.setProperty('height', 'auto', 'important');
        el.style.setProperty('overflow', 'visible', 'important');
      }
    } catch (err) {}
  }
  return {clicked, total: clicked.length};
})()
"""

NAVER_BTN_JS = r"""
(() => {
  try {
    // 네이버 스마트스토어 "상세정보 펼쳐보기" 버튼 직접 탐색
    // 우측 사이드바(선물하기/구매하기 등) 제외, 좌측 콘텐츠 영역만
    const HALF_W = window.innerWidth * 0.54;
    const allEls = Array.from(document.querySelectorAll('button,[role="button"],a,span,div,p'));
    for (const el of allEls) {
      const raw = (el.innerText || el.textContent || '').trim();
      const t = raw.replace(/\s+/g, '');
      if (t.length < 3 || t.length > 25) continue;
      if (!/펼쳐보기|펼치기/.test(t)) continue;
      // 리뷰/찜/구매 등 오탐 필터
      if (/리뷰|찜|담기|구매|선물|장바구니|독독|문의|홈|카테고리/.test(t)) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width < 20 || rect.height < 10) continue;
      if (rect.left > HALF_W) continue;  // 우측 사이드바 제외
      const absY = rect.top + (window.scrollY || 0);
      if (absY < 250) continue;  // 헤더 영역 제외
      const cs = window.getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      el.scrollIntoView({block: 'center', inline: 'nearest'});
      el.click();
      return {ok: true, text: t, x: Math.round(rect.left), y: Math.round(absY)};
    }
    return {ok: false};
  } catch(e) {
    return {ok: false, err: String(e)};
  }
})()
"""

# 쿠팡 '더보기' 버튼 클릭 JS
EXPAND_JS = """
(function() {
    const keywords = ['상품정보 더보기', '상세정보 더보기', '상품상세 더보기'];
    const allBtns = Array.from(document.querySelectorAll('button, a'));
    for (const kw of keywords) {
        for (const btn of allBtns) {
            const txt = (btn.innerText || btn.textContent || '').trim();
            if (txt === kw) {
                btn.scrollIntoView({behavior: 'instant', block: 'center'});
                btn.click();
                return 'clicked: ' + txt;
            }
        }
    }
    for (const btn of allBtns) {
        const txt = (btn.innerText || btn.textContent || '').trim();
        if (txt.includes('정보 더보기')) {
            btn.scrollIntoView({behavior: 'instant', block: 'center'});
            btn.click();
            return 'clicked (partial): ' + txt;
        }
    }
    return 'not found';
})();
"""

BLOCKED_KEYWORDS = [
    "Access Denied",
    "쇼핑 서비스 접속이 일시적으로 제한",
    "비정상적인 접근",
    "자동화된 요청",
    "로봇이 아닙니다",
    "captcha",
    "Cloudflare",
    "cf-turnstile",
    "사람인지 확인",
    "사용자 활동 검토",          # Gmarket Cloudflare
    "원활한 서비스 이용을 위한 간단한 확인",  # Auction Cloudflare
    "봇(Bot)이 아님을 아래 확인",# Gmarket CF 본문
    "Just a moment...",          # Cloudflare 기본 제목
    "잠시만 기다려주십시오",      # Cloudflare 한글 제목 1
    "잠시만 기다리십시오",        # Cloudflare 한글 제목 2

    "원활한 서비스 이용을 위한 간단한 확인 안내",
    "봇(Bot)이란",
    "보안 확인을 완료해 주세요",
    "영수증 번호를 입력",
    "상품을 찾을 수 없습니다",
    "판매가 중지된 상품",
    "페이지를 찾을 수 없습니다",
]


def _get_drission_page():
    from DrissionPage import ChromiumOptions, ChromiumPage
    co = ChromiumOptions()
    co.set_argument("--window-position=0,0")
    co.set_argument("--window-size=1920,1080")
    co.set_argument("--mute-audio")
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--lang=ko-KR")
    co.set_argument("--force-color-profile=srgb")
    co.set_argument("--force-device-scale-factor=1")
    co.set_argument("--run-all-compositor-stages-before-draw")

    paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    for path in paths:
        if os.path.exists(path):
            co.set_paths(browser_path=path)
            break

    return ChromiumPage(co)


def _is_blocked(html: str) -> bool:
    lower = html.lower()
    return any(kw.lower() in lower for kw in BLOCKED_KEYWORDS) or len(html) < 1000


def _clean_coupang_url(url: str) -> str:
    if "coupang.com/vp/products" not in (url or "").lower():
        return url
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        query = []
        for key in ("itemId", "vendorItemId"):
            value = params.get(key, [""])[0]
            if value:
                query.append(f"{key}={value}")
        suffix = f"?{'&'.join(query)}" if query else ""
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}{suffix}"
    except Exception:
        return url


def _copy_chrome_profile_tmp(chrome_profile: str, profile_directory: str | None = None) -> str:
    try:
        from engine.browser_profile import copy_chrome_profile_tmp
        return copy_chrome_profile_tmp(chrome_profile, profile_directory=profile_directory)
    except Exception as e:
        logger.warning(f"Chrome 프로필 복사 실패: {e}")
        return ""


def _find_autohotkey_exe() -> str:
    configured = getattr(config, "AUTOHOTKEY_EXE", "") or os.getenv("AUTOHOTKEY_EXE", "")
    candidates = [configured] if configured else []
    candidates.extend([
        r"C:\Users\kua\AppData\Local\Programs\AutoHotkey\v2\AutoHotkey64.exe",
        r"C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe",
        r"C:\Program Files\AutoHotkey\v2\AutoHotkey.exe",
        r"C:\Program Files\AutoHotkey\AutoHotkey64.exe",
        r"C:\Program Files\AutoHotkey\AutoHotkey.exe",
        r"C:\Program Files (x86)\AutoHotkey\AutoHotkey.exe",
    ])
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return ""


def _screenshot_progress_stats(images: List[Image.Image]) -> Dict:
    if len(images) < 3:
        return {
            "ok": True,
            "frames": len(images),
            "changed_frames": max(0, len(images) - 1),
            "required_changed_frames": 0,
            "avg_frame_diff": 0.0,
        }
    diffs = []
    prev = None
    for image in images:
        sample = image.convert("L").resize((96, 96))
        arr = np.asarray(sample, dtype=np.int16)
        if prev is not None:
            diffs.append(float(np.abs(arr - prev).mean()))
        prev = arr
    changed = sum(1 for diff in diffs if diff > 2.0)
    required = max(2, len(diffs) // 3)
    avg_diff = round(sum(diffs) / len(diffs), 2) if diffs else 0.0
    return {
        "ok": changed >= required,
        "frames": len(images),
        "changed_frames": changed,
        "required_changed_frames": required,
        "avg_frame_diff": avg_diff,
    }


def _screen_frame_diff(previous: Image.Image, current: Image.Image) -> float:
    try:
        width, height = current.size
        left = int(width * 0.06)
        right = max(left + 1, int(width * 0.84))
        top = int(height * 0.08)
        bottom = max(top + 1, int(height * 0.92))
        prev_sample = previous.crop((left, top, right, bottom)).convert("L").resize((96, 96))
        curr_sample = current.crop((left, top, right, bottom)).convert("L").resize((96, 96))
        prev_arr = np.asarray(prev_sample, dtype=np.int16)
        curr_arr = np.asarray(curr_sample, dtype=np.int16)
        return float(np.abs(curr_arr - prev_arr).mean())
    except Exception:
        return 999.0


def _best_stitch_cut(
    previous: Image.Image,
    current: Image.Image,
    match_left_ratio: float = 0.16,
    match_right_ratio: float = 0.84,
    max_overlap_limit: int = 360,
) -> tuple[int, int, float]:
    """Find how much of the current frame is already covered by the previous tail."""
    try:
        prev = previous.convert("RGB")
        curr = current.convert("RGB")
        width = min(prev.width, curr.width)
        if width <= 10 or prev.height <= 120 or curr.height <= 120:
            return 0, 0, 999.0

        left_ratio = max(0.0, min(float(match_left_ratio or 0.16), 0.45))
        right_ratio = max(left_ratio + 0.20, min(float(match_right_ratio or 0.84), 1.0))
        x0 = int(width * left_ratio)
        x1 = max(x0 + 1, int(width * right_ratio))
        crop_candidates = (0, 30, 60, 90)
        best: tuple[float, int, int, float] | None = None

        def evaluate(crop_top: int, overlap: int) -> tuple[float, int, int, float]:
            prev_band = prev.crop((x0, prev.height - overlap, x1, prev.height)).convert("L")
            curr_band = curr.crop((x0, crop_top, x1, crop_top + overlap)).convert("L")
            sample_w = min(520, x1 - x0)
            sample_h = min(220, overlap)
            if prev_band.size != (sample_w, sample_h):
                prev_band = prev_band.resize((sample_w, sample_h))
                curr_band = curr_band.resize((sample_w, sample_h))
            prev_arr = np.asarray(prev_band, dtype=np.int16)
            curr_arr = np.asarray(curr_band, dtype=np.int16)
            diff = float(np.abs(prev_arr - curr_arr).mean())
            texture = min(float(prev_arr.std()), float(curr_arr.std()))
            blank_penalty = 120.0 if texture < 12.0 else 0.0
            score = diff + blank_penalty + (crop_top * 0.018) - (min(overlap, 260) * 0.002)
            return score, crop_top, overlap, diff

        for crop_top in crop_candidates:
            if crop_top >= curr.height - 140:
                continue
            max_overlap = min(max(180, int(max_overlap_limit or 360)), prev.height - 20, curr.height - crop_top - 20)
            if max_overlap < 90:
                continue
            min_overlap = min(140, max_overlap)
            for overlap in range(int(max_overlap), int(min_overlap) - 1, -40):
                candidate = evaluate(crop_top, overlap)
                if best is None or candidate[0] < best[0]:
                    best = candidate
        if best is None:
            return 0, 0, 999.0
        coarse = best
        crop_top = coarse[1]
        max_overlap = min(max(180, int(max_overlap_limit or 360)), prev.height - 20, curr.height - crop_top - 20)
        min_overlap = min(90, max_overlap)
        low = max(int(min_overlap), int(coarse[2]) - 70)
        high = min(int(max_overlap), int(coarse[2]) + 70)
        for overlap in range(low, high + 1, 10):
            candidate = evaluate(crop_top, overlap)
            if candidate[0] < best[0]:
                best = candidate
        _score, crop_top, overlap, diff = best
        return int(crop_top), int(overlap), float(diff)
    except Exception:
        return 0, 0, 999.0


def _stitch_screen_frames(
    images: List[Image.Image],
    crop_right_ratio: float = 0.84,
    crop_left_ratio: float = 0.0,
    crop_top_after_first: int = 0,
    crop_bottom_each_frame: int = 6,
    soften_boundaries: bool = False,
    boundary_blend_px: int = 3,
    force_crop_top_after_first: bool = False,
    max_overlap_diff: float = 14.0,
    match_left_ratio: float = 0.16,
    match_right_ratio: float = 0.84,
    max_overlap_limit: int = 360,
    remove_boundary_px: int = 0,
) -> tuple[Image.Image, Dict]:
    if not images:
        return Image.new("RGB", (1, 1)), {"stitch_strategy": "empty"}

    frames = []
    top_cropped_flags = []
    for idx, image in enumerate(images):
        frame = image.convert("RGB")
        if frame.width >= 1500:
            left_ratio = max(0.0, min(float(crop_left_ratio or 0.0), 0.35))
            right_ratio = max(left_ratio + 0.35, min(float(crop_right_ratio or 0.84), 1.0))
            left = int(frame.width * left_ratio)
            right = int(frame.width * right_ratio)
            frame = frame.crop((left, 0, right, frame.height))
        top_was_cropped = False
        if idx > 0 and force_crop_top_after_first and crop_top_after_first > 0 and frame.height > crop_top_after_first + 160:
            frame = frame.crop((0, int(crop_top_after_first), frame.width, frame.height))
            top_was_cropped = True
        bottom_crop = max(0, min(int(crop_bottom_each_frame or 0), 48))
        if frame.height > 160 + bottom_crop and bottom_crop:
            frame = frame.crop((0, 0, frame.width, frame.height - bottom_crop))
        frames.append(frame)
        top_cropped_flags.append(top_was_cropped)
    width = min(frame.width for frame in frames)
    if any(frame.width != width for frame in frames):
        frames = [frame.crop((0, 0, width, frame.height)) for frame in frames]

    merged = frames[0].copy()
    overlaps: List[int] = []
    crops: List[int] = []
    diffs: List[float] = []
    appended_heights: List[int] = [merged.height]
    boundaries: List[int] = []

    for index, frame in enumerate(frames[1:], start=1):
        crop_top, overlap, diff = _best_stitch_cut(
            merged,
            frame,
            match_left_ratio=match_left_ratio,
            match_right_ratio=match_right_ratio,
            max_overlap_limit=max_overlap_limit,
        )
        if overlap > 0 and diff <= float(max_overlap_diff or 14.0):
            append_from = min(frame.height - 1, crop_top + overlap)
        else:
            already_top_cropped = bool(top_cropped_flags[index]) if index < len(top_cropped_flags) else False
            default_crop = 12 if already_top_cropped else max(0, min(int(crop_top_after_first or 0), frame.height - 120))
            append_from = min(frame.height - 1, default_crop)
            crop_top = default_crop
            overlap = 0
        tail = frame.crop((0, append_from, width, frame.height))
        if tail.height <= 8:
            overlaps.append(overlap)
            crops.append(crop_top)
            diffs.append(round(diff, 2))
            appended_heights.append(0)
            tail.close()
            continue
        boundaries.append(merged.height)
        next_merged = Image.new("RGB", (width, merged.height + tail.height), "white")
        next_merged.paste(merged, (0, 0))
        next_merged.paste(tail, (0, merged.height))
        merged.close()
        tail.close()
        merged = next_merged
        overlaps.append(overlap)
        crops.append(crop_top)
        diffs.append(round(diff, 2))
        appended_heights.append(frame.height - append_from)

    for frame in frames:
        try:
            frame.close()
        except Exception:
            pass

    if soften_boundaries and boundaries:
        try:
            band = max(1, min(int(boundary_blend_px or 3), 8))
            arr = np.asarray(merged, dtype=np.uint8).copy()
            height, _width = arr.shape[:2]
            for y in boundaries:
                if y <= band or y >= height - band - 1:
                    continue
                top = arr[y - band - 1].astype(np.float32)
                bottom = arr[y + band + 1].astype(np.float32)
                for offset, row in enumerate(range(y - band, y + band + 1), start=1):
                    alpha = offset / float((band * 2) + 2)
                    arr[row] = np.clip((top * (1.0 - alpha)) + (bottom * alpha), 0, 255).astype(np.uint8)
            merged.close()
            merged = Image.fromarray(arr, "RGB")
        except Exception as exc:
            logger.debug("[Detail] stitch boundary smoothing skipped: %s", exc)

    boundary_rows_removed = 0
    if remove_boundary_px and boundaries:
        try:
            cut = max(1, min(int(remove_boundary_px or 0), 32))
            arr = np.asarray(merged, dtype=np.uint8)
            remove = np.zeros(arr.shape[0], dtype=bool)
            half = max(1, cut // 2)
            for y in boundaries:
                start = max(0, int(y) - half)
                end = min(arr.shape[0], int(y) + (cut - half))
                remove[start:end] = True
            boundary_rows_removed = int(remove.sum())
            if boundary_rows_removed:
                cleaned = Image.fromarray(arr[~remove], "RGB")
                merged.close()
                merged = cleaned
        except Exception as exc:
            logger.debug("[Detail] stitch boundary row cleanup skipped: %s", exc)

    return merged, {
            "stitch_strategy": "overlap",
        "stitch_crop_right_ratio": crop_right_ratio,
        "stitch_crop_left_ratio": crop_left_ratio,
        "stitch_crop_top_after_first": crop_top_after_first,
        "stitch_crop_bottom_each_frame": crop_bottom_each_frame,
        "stitch_soften_boundaries": soften_boundaries,
        "stitch_boundary_blend_px": boundary_blend_px,
        "stitch_force_crop_top_after_first": force_crop_top_after_first,
        "stitch_max_overlap_diff": max_overlap_diff,
        "stitch_match_left_ratio": match_left_ratio,
        "stitch_match_right_ratio": match_right_ratio,
        "stitch_max_overlap_limit": max_overlap_limit,
        "stitch_remove_boundary_px": remove_boundary_px,
        "stitch_boundary_rows_removed": boundary_rows_removed,
        "stitch_overlaps": overlaps,
        "stitch_top_crops": crops,
        "stitch_diffs": diffs,
        "stitch_appended_heights": appended_heights,
    }


def _stitch_screen_frames_by_scroll_positions(
    images: List[Image.Image],
    scroll_positions: List[int],
    page_height: int = 0,
    crop_right_ratio: float = 1.0,
    crop_left_ratio: float = 0.0,
    crop_top_after_first: int = 96,
    crop_bottom_each_frame: int = 2,
) -> tuple[Image.Image, Dict]:
    """Stitch viewport screenshots by their real browser scrollY positions."""
    if not images:
        return Image.new("RGB", (1, 1)), {"stitch_strategy": "empty"}

    frames = []
    positions: List[int] = []
    last_y = -1
    for idx, image in enumerate(images):
        frame = image.convert("RGB")
        if frame.width >= 1500:
            left_ratio = max(0.0, min(float(crop_left_ratio or 0.0), 0.35))
            right_ratio = max(left_ratio + 0.35, min(float(crop_right_ratio or 1.0), 1.0))
            left = int(frame.width * left_ratio)
            right = int(frame.width * right_ratio)
            frame = frame.crop((left, 0, right, frame.height))

        y = int(scroll_positions[idx]) if idx < len(scroll_positions) else 0
        y = max(0, y)
        if y <= last_y and idx > 0:
            frame.close()
            continue

        top_crop = 0
        if idx > 0 and crop_top_after_first > 0 and frame.height > crop_top_after_first + 180:
            top_crop = min(int(crop_top_after_first), frame.height - 180)
        bottom_crop = max(0, min(int(crop_bottom_each_frame or 0), 16))
        bottom = frame.height - bottom_crop if frame.height > 180 + bottom_crop else frame.height
        if top_crop or bottom < frame.height:
            cropped = frame.crop((0, top_crop, frame.width, bottom))
            frame.close()
            frame = cropped
            y += top_crop

        frames.append(frame)
        positions.append(y)
        last_y = y

    if not frames:
        return Image.new("RGB", (1, 1)), {"stitch_strategy": "absolute_empty"}

    width = min(frame.width for frame in frames)
    if any(frame.width != width for frame in frames):
        frames = [frame.crop((0, 0, width, frame.height)) for frame in frames]

    target_height = max(y + frame.height for y, frame in zip(positions, frames))
    if page_height and page_height > 1000:
        target_height = min(max(target_height, int(page_height)), int(page_height) + 1200)
    target_height = max(1, int(target_height))

    merged = Image.new("RGB", (width, target_height), "white")
    for y, frame in zip(positions, frames):
        if y >= target_height:
            continue
        usable_height = min(frame.height, target_height - y)
        if usable_height <= 0:
            continue
        piece = frame.crop((0, 0, width, usable_height))
        merged.paste(piece, (0, y))
        piece.close()

    for frame in frames:
        try:
            frame.close()
        except Exception:
            pass

    return merged, {
        "stitch_strategy": "absolute_scroll",
        "stitch_crop_right_ratio": crop_right_ratio,
        "stitch_crop_left_ratio": crop_left_ratio,
        "stitch_crop_top_after_first": crop_top_after_first,
        "stitch_crop_bottom_each_frame": crop_bottom_each_frame,
        "stitch_scroll_positions": positions[:80],
        "stitch_absolute_page_height": int(page_height or 0),
        "stitch_absolute_output_height": int(target_height),
    }


def _screenshots_have_progress(images: List[Image.Image]) -> bool:
    return bool(_screenshot_progress_stats(images).get("ok"))


def _run_ahk_scroll(hwnd: int, steps: int = 1, delay_ms: int = 700, home_first: bool = False) -> bool:
    if not getattr(config, "ENABLE_AHK_FALLBACK", True):
        return False
    exe = _find_autohotkey_exe()
    if not exe:
        return False
    tools_dir = Path(config.BASE_DIR) / "tools"
    v1_script = tools_dir / "coupang_scroll_v1.ahk"
    v2_script = tools_dir / "coupang_scroll_v2.ahk"
    script = v2_script if "\\v2\\" in exe.lower() else v1_script
    if not script.exists():
        logger.debug("[AHK] version-compatible script missing: %s", script.name)
        return False
    timeout = max(8, int((max(steps, 1) * max(delay_ms, 100)) / 1000) + 8)
    try:
        completed = subprocess.run(
            [
                exe,
                str(script),
                str(hwnd),
                str(max(0, steps)),
                str(max(0, delay_ms)),
                "1" if home_first else "0",
            ],
            timeout=timeout,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            return True
        logger.debug("[AHK] %s failed rc=%s stderr=%s", script.name, completed.returncode, completed.stderr)
    except Exception as exc:
        logger.debug("[AHK] %s failed: %s", script.name, exc)
    return False





def _log_security_check_event(product_id: str, platform: str, method: str, status: str, url: str = ""):
    """보안 확인 이벤트를 전용 로그 파일에 기록"""
    try:
        from datetime import datetime
        log_file = r'C:\JepumScraper\logs\security_check.log'
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(log_file, 'a', encoding='utf-8') as lf:
            lf.write(f"{ts} | {platform} | {product_id} | {method} | {status} | {url}\n")
    except Exception:
        pass

def _show_captcha_success_toast(product_id: str):
    try:
        import subprocess
        import os
        from datetime import datetime
        
        # 1. Toast Notification
        ps_script = f"""
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $textNodes = $template.GetElementsByTagName('text')
        $textNodes.Item(0).AppendChild($template.CreateTextNode('🎉 캡챠 자동 해제 성공!')) | Out-Null
        $textNodes.Item(1).AppendChild($template.CreateTextNode('[{product_id}] 봇이 Cloudflare 캡챠를 뚫었습니다!')) | Out-Null
        $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('JepumScraper')
        $notifier.Show($toast)
        """
        subprocess.run(['powershell', '-Command', ps_script], creationflags=subprocess.CREATE_NO_WINDOW)
        
        # 2. Write to success log file
        log_file = r'C:\JepumScraper\logs\captcha_success.log'
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        with open(log_file, 'a', encoding='utf-8') as lf:
            lf.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - [{product_id}] CAPTCHA Auto-Resolved Successfully\n")
    except Exception as e:
        logger.error(f'Toast notification failed: {e}')

def _find_captcha_hwnd():
    """현재 화면에 떠 있는 Chrome 창 중 CAPTCHA 관련 제목을 가진 창의 hwnd를 찾습니다."""
    found_hwnd = 0
    def callback(hwnd, extra):
        nonlocal found_hwnd
        if found_hwnd != 0:
            return
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if "Chrome" in title and any(cw in title for cw in [
                "Just a moment", "보안 확인", "잠시만 기다려주십시오", "잠시만 기다리십시오",
                "원활한 서비스", "사용자 활동 검토", "간단한 확인 안내", "Security Check"
            ]):
                found_hwnd = hwnd
    win32gui.EnumWindows(callback, None)
    return found_hwnd


def _run_ahk_cf_captcha_click(hwnd: int = 0, max_wait_sec: int = 15, check_interval_ms: int = 400) -> bool:
    """Cloudflare '사람인지 확인하십시오' 체크박스를 AHK로 자동 클릭한다.

    스크래핑 중 Cloudflare Turnstile 캡챠가 감지되면 이 함수를 호출하여
    체크박스를 클릭할 수 있다.

    Args:
        hwnd: 브라우저 창 핸들 (0이면 현재 활성 창 대상)
        max_wait_sec: 캡챠가 나타날 때까지 기다리는 최대 시간(초)
        check_interval_ms: 탐색 반복 간격(ms)

    Returns:
        True = 클릭 성공, False = 타임아웃 또는 AHK 없음
    """
    if not getattr(config, "ENABLE_AHK_FALLBACK", True):
        return False
    if not hwnd:
        logger.debug("[AHK-CF] target hwnd missing; skip active-window captcha helper")
        return False
    exe = _find_autohotkey_exe()
    if not exe:
        logger.debug("[AHK-CF] AutoHotkey 실행파일 없음, 캡챠 클릭 스킵")
        return False

    tools_dir = Path(config.BASE_DIR) / "tools"
    v1_script = tools_dir / "cf_captcha_click_v1.ahk"
    v2_script = tools_dir / "cf_captcha_click_v2.ahk"
    is_v2_exe = "\\v2\\" in exe.lower()
    scripts = [v2_script] if is_v2_exe else [v1_script]
    if not scripts[0].exists():
        logger.debug("[AHK-CF] version-compatible script missing: %s", scripts[0].name)
        return False

    timeout = max_wait_sec + 5  # AHK 자체 대기 + 여유

    for script in scripts:
        if not script.exists():
            continue
        try:
            completed = subprocess.run(
                [
                    exe,
                    str(script),
                    str(hwnd) if hwnd else "",
                    str(max(1, max_wait_sec)),
                    str(max(100, check_interval_ms)),
                ],
                timeout=timeout,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode == 0:
                logger.info("[AHK-CF] Cloudflare 체크박스 클릭 성공 (hwnd=%s)", hwnd)
                return True
            elif completed.returncode == 1:
                logger.debug("[AHK-CF] 타임아웃: 캡챠 미감지 (hwnd=%s)", hwnd)
                return False
            elif completed.returncode == 2:
                logger.debug("[AHK-CF] 실행 불가: 창 핸들/인자 오류 (hwnd=%s)", hwnd)
                return False
            else:
                logger.debug("[AHK-CF] %s rc=%s stderr=%s", script.name, completed.returncode, completed.stderr)
        except subprocess.TimeoutExpired:
            logger.debug("[AHK-CF] AHK 스크립트 자체 타임아웃")
        except Exception as exc:
            logger.debug("[AHK-CF] 실행 실패: %s", exc)

    return False


def _focus_chrome_window_for_captcha(hwnd: int, product_id: str = "", reason: str = "captcha") -> bool:
    """Bring the detected Chrome window to the foreground and maximize it."""
    if not hwnd:
        return False
    try:
        import ctypes
        import win32api
        import win32con
        import win32gui

        if not win32gui.IsWindow(hwnd):
            logger.debug("[CAPTCHA-Focus] invalid hwnd=%s product=%s reason=%s", hwnd, product_id, reason)
            return False
        if not win32gui.IsWindowVisible(hwnd):
            logger.debug("[CAPTCHA-Focus] hidden hwnd=%s product=%s reason=%s", hwnd, product_id, reason)
            return False

        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.15)

        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        time.sleep(0.15)

        try:
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.05)
        except Exception:
            pass

        try:
            ctypes.windll.user32.AllowSetForegroundWindow(-1)
        except Exception:
            pass

        foreground_ok = False
        for _ in range(3):
            try:
                win32gui.BringWindowToTop(hwnd)
            except Exception:
                pass
            try:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
            time.sleep(0.15)
            if win32gui.GetForegroundWindow() == hwnd:
                foreground_ok = True
                break

        if not foreground_ok:
            try:
                ctypes.windll.user32.SwitchToThisWindow(hwnd, True)
                time.sleep(0.15)
                foreground_ok = win32gui.GetForegroundWindow() == hwnd
            except Exception:
                pass

        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        except Exception:
            pass

        logger.info(
            "[CAPTCHA-Focus] Chrome window foreground/maximize %s hwnd=%s product=%s reason=%s title=%s",
            "ok" if foreground_ok else "partial",
            hwnd,
            product_id,
            reason,
            win32gui.GetWindowText(hwnd),
        )
        return True
    except Exception as exc:
        logger.debug("[CAPTCHA-Focus] failed hwnd=%s product=%s reason=%s: %s", hwnd, product_id, reason, exc)
        return False


def _run_ahk_naver_login_click(hwnd: int = 0, max_wait_sec: int = 5, check_interval_ms: int = 400) -> bool:
    if not getattr(config, "ENABLE_AHK_FALLBACK", True):
        return False
    exe = _find_autohotkey_exe()
    if not exe:
        logger.debug("[AHK-Naver] AutoHotkey 실행파일 없음, 로그인 클릭 스킵")
        return False

    tools_dir = Path(config.BASE_DIR) / "tools"
    v1_script = tools_dir / "naver_login_click_v1.ahk"
    v2_script = tools_dir / "naver_login_click_v2.ahk"
    scripts = [v2_script] if "\\v2\\" in exe.lower() else [v1_script]
    if not scripts[0].exists():
        logger.debug("[AHK-Naver] version-compatible script missing: %s", scripts[0].name)
        return False

    timeout = max_wait_sec + 5

    for script in scripts:
        if not script.exists():
            continue
        try:
            completed = subprocess.run(
                [
                    exe,
                    str(script),
                    str(hwnd) if hwnd else "",
                    str(max(1, max_wait_sec)),
                    str(max(100, check_interval_ms)),
                ],
                timeout=timeout,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode == 0:
                logger.info("[AHK-Naver] 네이버 로그인 버튼 클릭 성공 (hwnd=%s)", hwnd)
                return True
            else:
                logger.debug("[AHK-Naver] 타임아웃 또는 실패: rc=%s", completed.returncode)
        except subprocess.TimeoutExpired:
            logger.debug("[AHK-Naver] AHK 스크립트 타임아웃")
        except Exception as exc:
            logger.debug("[AHK-Naver] 실행 실패: %s", exc)
    return False

def _normalize_detail_url(url: str) -> str:
    """Unwrap marketplace redirect/ad URLs before opening the product page."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        redirect_values = (
            params.get("redirect")
            or params.get("returnUrl")
            or params.get("returnURL")
            or params.get("url")
        )
        if redirect_values:
            candidate = unquote(redirect_values[0])
            if candidate.startswith("http"):
                logger.info("[Detail] normalized redirect URL: %s -> %s", url, candidate)
                return candidate
    except Exception as exc:
        logger.debug("[Detail] redirect URL normalization skipped: %s", exc)
    return url


def get_detail_capture_method_order(platform_key: str, product_url: str) -> List[str]:
    if platform_key == "auction":
        # Auction must stay on the dedicated visible Chrome path. Falling back to
        # Playwright/Drission can move unrelated browser windows and break the UX.
        return ["chrome_screen"]
    method_order = adaptive_learning.get_detail_method_order(platform_key, product_url)
    if platform_key == "naver":
        method_order = ["chrome_screen"] + [method for method in method_order if method != "chrome_screen"]
    if platform_key in {"coupang", "elevenst", "gmarket", "auction"}:
        preferred = ["chrome_screen"]
        if platform_key == "coupang":
            preferred.append("ahk_screen")
        method_order = preferred + [method for method in method_order if method not in preferred]
    if platform_key in {"naver", "elevenst", "gmarket"}:
        for fallback in ("chrome_screen", "playwright_user_profile", "drission"):
            if fallback not in method_order:
                method_order.append(fallback)
    return method_order


def requires_screen_capture(platform_key: str, product_url: str) -> bool:
    return any(method in {"chrome_screen", "ahk_screen"} for method in get_detail_capture_method_order(platform_key, product_url))


def _image_file_quality(path: str) -> Dict:
    info = {
        "ok": False,
        "width": 0,
        "height": 0,
        "brightness": 0.0,
        "dark_ratio": 1.0,
    }
    try:
        with Image.open(path) as img:
            rgb = img.convert("RGB")
            arr = np.asarray(rgb, dtype=np.uint8)
            info["width"], info["height"] = rgb.size
            info["brightness"] = round(float(arr.mean()), 2)
            info["dark_ratio"] = round(float(np.mean(np.all(arr < 12, axis=2))), 4)
            info["ok"] = (
                info["width"] >= 320
                and info["height"] >= 320
                and info["brightness"] >= 20
                and info["dark_ratio"] < 0.92
            )
    except Exception as exc:
        info["error"] = str(exc)
    return info


def _set_result_quality(result: Dict, path: str, extra=None) -> Dict:
    quality = _image_file_quality(path)
    diagnostics = result.setdefault("diagnostics", {})
    diagnostics.update(quality)
    diagnostics["capture_version"] = DETAIL_CAPTURE_VERSION
    if extra:
        diagnostics.update(extra)
    return quality


def _trim_auction_repeated_header(path: str) -> Dict:
    """Auction sometimes appends the top product area again after the detail body."""
    info = {"auction_trimmed_repeat": False, "auction_trim_strategy": "logo_tail_or_top_similarity"}
    try:
        with Image.open(path) as img:
            rgb = img.convert("RGB")
            arr = np.asarray(rgb, dtype=np.uint8)
            h, w = arr.shape[:2]
            if h < 6000 or w < 900:
                return info

            red = (arr[:, :, 0] > 145) & (arr[:, :, 1] < 95) & (arr[:, :, 2] < 105)
            logo_x1 = int(w * 0.08)
            logo_x2 = min(int(w * 0.60), logo_x1 + 920)
            logo_score = red[:, logo_x1:logo_x2].mean(axis=1)
            logo_start = max(3600, int(h * 0.32))
            logo_rows = np.where(logo_score[logo_start:] > 0.25)[0] + logo_start
            if logo_rows.size:
                logo_groups = np.split(logo_rows, np.where(np.diff(logo_rows) > 4)[0] + 1)
                for group in logo_groups:
                    logo_peak = float(logo_score[group].max())
                    if len(group) < 4 or logo_peak < 0.45:
                        continue
                    logo_y = int(group[0])
                    crop_y = max(1200, logo_y - 35)
                    removed_height = h - crop_y
                    if removed_height < 1200:
                        continue
                    crop_ratio = crop_y / float(max(h, 1))
                    removed_ratio = removed_height / float(max(h, 1))
                    info.update({
                        "auction_trim_candidate_y": int(crop_y),
                        "auction_trim_candidate_removed": int(removed_height),
                        "auction_trim_candidate_ratio": round(crop_ratio, 4),
                        "auction_trim_candidate_removed_ratio": round(removed_ratio, 4),
                        "auction_trim_logo_score": round(logo_peak, 4),
                    })
                    if crop_ratio < 0.82 or removed_ratio > 0.18 or removed_height > 7000:
                        info["auction_trim_rejected_reason"] = "logo_candidate_not_safe_tail"
                        continue
                    trimmed = rgb.crop((0, 0, w, crop_y))
                    tmp_path = f"{path}.auction-logo-trim.tmp.jpg"
                    trimmed.save(tmp_path, "JPEG", quality=86)
                    trimmed.close()
                    os.replace(tmp_path, path)
                    info.update({
                        "auction_trimmed_repeat": True,
                        "auction_trim_reason": "repeated_auction_logo",
                        "auction_original_height": int(h),
                        "auction_trim_y": int(crop_y),
                        "auction_removed_height": int(removed_height),
                    })
                    logger.info("[Detail] trimmed repeated Auction logo tail at y=%s from %s", crop_y, path)
                    return info

            dark = (arr[:, :, 0] < 90) & (arr[:, :, 1] < 90) & (arr[:, :, 2] < 90)
            row_dark = dark[:, int(w * 0.05):int(w * 0.95)].mean(axis=1)
            base_h = min(1100, h // 5)
            base_sample = rgb.crop((0, 0, w, base_h)).convert("L").resize((240, 120))
            base_arr = np.asarray(base_sample, dtype=np.int16)

            start = int(h * 0.55)
            ys = np.where(row_dark[start:] > 0.85)[0] + start
            if ys.size == 0:
                return info

            groups = np.split(ys, np.where(np.diff(ys) > 2)[0] + 1)
            for group in groups:
                dark_y = int(group[0])
                band_top = max(start, dark_y - 260)
                band_bottom = max(band_top + 1, dark_y - 35)
                red_count = int(red[band_top:band_bottom, int(w * 0.08):int(w * 0.55)].sum())
                if red_count < 900:
                    continue

                crop_y = max(1200, dark_y - 190)
                if crop_y <= int(h * 0.55) or h - crop_y < 1200 or crop_y + base_h >= h:
                    continue
                repeat_sample = rgb.crop((0, crop_y, w, crop_y + base_h)).convert("L").resize((240, 120))
                repeat_arr = np.asarray(repeat_sample, dtype=np.int16)
                similarity_diff = float(np.abs(base_arr - repeat_arr).mean())
                removed_height = h - crop_y
                crop_ratio = crop_y / float(max(h, 1))
                removed_ratio = removed_height / float(max(h, 1))
                info.update({
                    "auction_trim_candidate_y": int(crop_y),
                    "auction_trim_candidate_removed": int(removed_height),
                    "auction_trim_candidate_ratio": round(crop_ratio, 4),
                    "auction_trim_candidate_removed_ratio": round(removed_ratio, 4),
                    "auction_trim_candidate_diff": round(similarity_diff, 2),
                })
                if similarity_diff > 28.0:
                    info["auction_trim_rejected_reason"] = "candidate_not_similar_enough"
                    continue
                if crop_ratio < 0.80 or removed_ratio > 0.22 or removed_height > 9000:
                    info["auction_trim_rejected_reason"] = "candidate_not_safe_tail"
                    continue

                trimmed = rgb.crop((0, 0, w, crop_y))
                tmp_path = f"{path}.trim.tmp.jpg"
                trimmed.save(tmp_path, "JPEG", quality=86)
                trimmed.close()
                os.replace(tmp_path, path)
                info.update({
                    "auction_trimmed_repeat": True,
                    "auction_original_height": int(h),
                    "auction_trim_y": int(crop_y),
                    "auction_removed_height": int(h - crop_y),
                    "auction_trim_similarity_diff": round(similarity_diff, 2),
                })
                logger.info("[Detail] trimmed repeated Auction header at y=%s from %s", crop_y, path)
                return info
    except Exception as exc:
        info["auction_trim_error"] = str(exc)
        logger.debug("[Detail] auction repeat trim skipped: %s", exc)
    return info


def _trim_elevenst_repeated_tail(path: str) -> Dict:
    """Trim only a true 11st tail repeat; never cut a mid-page repeat or live detail content."""
    info = {"elevenst_trimmed_repeat": False, "elevenst_trim_strategy": "guarded_tail_similarity_with_header"}
    try:
        with Image.open(path) as img:
            rgb = img.convert("RGB")
            w, h = rgb.size
            if h < 7000 or w < 900:
                return info

            sample_h = min(1300, h // 5)
            base = rgb.crop((0, 0, w, sample_h)).convert("L").resize((240, 140))
            base_arr = np.asarray(base, dtype=np.int16)
            start = int(h * 0.55)
            stop = h - sample_h - 1
            if stop <= start:
                return info

            best_diff = 999.0
            best_y = 0
            for y in range(start, stop, 80):
                sample = rgb.crop((0, y, w, y + sample_h)).convert("L").resize((240, 140))
                diff = float(np.abs(base_arr - np.asarray(sample, dtype=np.int16)).mean())
                if diff < best_diff:
                    best_diff = diff
                    best_y = y

            if not best_y or best_diff > 18.0 or h - best_y < 1800:
                info["elevenst_trim_candidate_diff"] = round(best_diff, 2)
                return info

            refine_start = max(start, best_y - 120)
            refine_stop = min(stop, best_y + 121)
            for y in range(refine_start, refine_stop, 10):
                sample = rgb.crop((0, y, w, y + sample_h)).convert("L").resize((240, 140))
                diff = float(np.abs(base_arr - np.asarray(sample, dtype=np.int16)).mean())
                if diff < best_diff:
                    best_diff = diff
                    best_y = y

            crop_y = max(1200, best_y - 20)
            removed_height = h - crop_y
            removed_ratio = removed_height / float(max(h, 1))
            crop_ratio = crop_y / float(max(h, 1))
            info.update({
                "elevenst_trim_candidate_y": int(crop_y),
                "elevenst_trim_candidate_removed": int(removed_height),
                "elevenst_trim_candidate_ratio": round(crop_ratio, 4),
                "elevenst_trim_candidate_removed_ratio": round(removed_ratio, 4),
                "elevenst_trim_candidate_diff": round(best_diff, 2),
            })
            if crop_y <= int(h * 0.50) or removed_height < 1400:
                return info

            header_top = max(0, crop_y - 80)
            header_bottom = min(h, crop_y + 1100)
            header_arr = arr = np.asarray(rgb.crop((0, header_top, w, header_bottom)), dtype=np.uint8)
            left_band = header_arr[:, : max(260, int(w * 0.42)), :]
            red_mask = (
                (left_band[:, :, 0] > 170)
                & (left_band[:, :, 1] < 95)
                & (left_band[:, :, 2] < 120)
            )
            dark_mask = np.all(header_arr < 75, axis=2)
            red_rows = np.where(red_mask.mean(axis=1) > 0.004)[0]
            dark_rows = np.where(dark_mask[:, : max(500, int(w * 0.55))].mean(axis=1) > 0.08)[0]
            header_signal = red_rows.size >= 4 and dark_rows.size >= 2
            info.update({
                "elevenst_trim_header_signal": bool(header_signal),
                "elevenst_trim_header_red_rows": int(red_rows.size),
                "elevenst_trim_header_dark_rows": int(dark_rows.size),
            })

            if not header_signal:
                info["elevenst_trim_rejected_reason"] = "missing_repeated_11st_header"
                return info
            if crop_ratio < 0.80 or removed_height > 9000 or removed_ratio > 0.22:
                info["elevenst_trim_rejected_reason"] = "candidate_not_safe_tail"
                return info

            trimmed = rgb.crop((0, 0, w, crop_y))
            tmp_path = f"{path}.trim.tmp.jpg"
            trimmed.save(tmp_path, "JPEG", quality=86)
            trimmed.close()
            os.replace(tmp_path, path)
            info.update({
                "elevenst_trimmed_repeat": True,
                "elevenst_original_height": int(h),
                "elevenst_trim_y": int(crop_y),
                "elevenst_removed_height": int(removed_height),
                "elevenst_removed_ratio": round(removed_ratio, 4),
                "elevenst_trim_similarity_diff": round(best_diff, 2),
            })
            logger.info("[Detail] trimmed repeated 11st tail at y=%s from %s", crop_y, path)
    except Exception as exc:
        info["elevenst_trim_error"] = str(exc)
        logger.debug("[Detail] 11st repeat trim skipped: %s", exc)
    return info


def _trim_coupang_repeated_tail(path: str) -> Dict:
    """Trim the repeated Coupang product header that can get appended after the detail body."""
    info = {"coupang_trimmed_repeat": False, "coupang_trim_strategy": "bottom_header_color"}
    try:
        with Image.open(path) as img:
            rgb = img.convert("RGB")
            w, h = rgb.size
            if h < 9000 or w < 900:
                rgb.close()
                return info

            arr = np.asarray(rgb, dtype=np.uint8)
            start_y = int(h * 0.55)
            broad_w = min(420, max(180, w // 3))
            menu_x1 = min(max(70, w // 18), broad_w - 1)
            menu_x2 = min(max(menu_x1 + 90, w // 7), broad_w)
            broad = arr[start_y:, :broad_w, :]
            menu = arr[start_y:, menu_x1:menu_x2, :]
            broad_blue = (
                (broad[:, :, 0] < 95)
                & (broad[:, :, 1] > 70)
                & (broad[:, :, 1] < 185)
                & (broad[:, :, 2] > 135)
            )
            menu_blue = (
                (menu[:, :, 0] < 95)
                & (menu[:, :, 1] > 70)
                & (menu[:, :, 1] < 185)
                & (menu[:, :, 2] > 135)
            )
            broad_score = broad_blue.mean(axis=1)
            menu_score = menu_blue.mean(axis=1) if menu.size else broad_score
            candidates = np.where((broad_score > 0.10) & (menu_score > 0.22))[0]
            if not candidates.size:
                return info

            groups = np.split(candidates, np.where(np.diff(candidates) > 3)[0] + 1)
            for group in groups:
                if len(group) < 42:
                    continue
                group_start = int(group[0] + start_y)
                group_end = int(group[-1] + start_y)
                if group_start < int(h * 0.58):
                    continue
                if h - group_start < 1300:
                    continue
                max_broad = float(np.max(broad_score[group]))
                max_menu = float(np.max(menu_score[group]))
                if max_broad < 0.14 or max_menu < 0.28:
                    continue
                crop_y = max(1200, group_start - 220)
                if crop_y <= int(h * 0.54) or h - crop_y < 1300:
                    continue
                removed_height = h - crop_y
                crop_ratio = crop_y / float(max(h, 1))
                removed_ratio = removed_height / float(max(h, 1))
                info.update({
                    "coupang_trim_candidate_y": int(crop_y),
                    "coupang_trim_candidate_removed": int(removed_height),
                    "coupang_trim_candidate_ratio": round(crop_ratio, 4),
                    "coupang_trim_candidate_removed_ratio": round(removed_ratio, 4),
                })
                if crop_ratio < 0.80 or removed_ratio > 0.22 or removed_height > 9000:
                    info["coupang_trim_rejected_reason"] = "candidate_not_safe_tail"
                    continue
                trimmed = rgb.crop((0, 0, w, crop_y))
                tmp_path = f"{path}.coupang-trim.tmp.jpg"
                trimmed.save(tmp_path, "JPEG", quality=86)
                trimmed.close()
                rgb.close()
                os.replace(tmp_path, path)
                info.update({
                    "coupang_trimmed_repeat": True,
                    "coupang_original_height": int(h),
                    "coupang_trim_y": int(crop_y),
                    "coupang_removed_height": int(h - crop_y),
                    "coupang_repeat_group_start": int(group_start),
                    "coupang_repeat_group_end": int(group_end),
                    "coupang_repeat_blue_score": round(max_broad, 3),
                })
                logger.info("[Detail] trimmed repeated Coupang tail at y=%s from %s", crop_y, path)
                return info
    except Exception as exc:
        info["coupang_trim_error"] = str(exc)
        logger.debug("[Detail] Coupang repeat trim skipped: %s", exc)
    return info


def _remove_naver_stitch_seams(path: str) -> Dict:
    """Remove very thin white bands introduced at screen-stitch boundaries."""
    info = {"naver_stitch_seams_removed": 0}
    try:
        with Image.open(path) as img:
            rgb = img.convert("RGB")
            w, h = rgb.size
            if w < 900 or h < 3000:
                rgb.close()
                return info

            arr = np.asarray(rgb, dtype=np.uint8)
            x1 = int(w * 0.02)
            x2 = max(x1 + 1, int(w * 0.78))
            band = arr[:, x1:x2, :]
            light_ratio = np.mean(np.all(band > 244, axis=2), axis=1)
            row_mean = band.mean(axis=(1, 2))
            prev_mean = np.array([
                float(np.median(row_mean[max(0, i - 10):max(0, i - 2)])) if i > 3 else float(row_mean[i])
                for i in range(h)
            ])
            next_mean = np.array([
                float(np.median(row_mean[min(h, i + 3):min(h, i + 11)])) if i < h - 4 else float(row_mean[i])
                for i in range(h)
            ])
            neighbor_mean = np.minimum(prev_mean, next_mean)
            candidates = (
                (light_ratio > 0.988)
                | ((light_ratio > 0.965) & (row_mean > 250))
            )
            candidates[:260] = False
            candidates[-260:] = False

            remove = np.zeros(h, dtype=bool)
            ys = np.where(candidates)[0]
            if ys.size:
                groups = np.split(ys, np.where(np.diff(ys) > 1)[0] + 1)
                for group in groups:
                    run_len = int(len(group))
                    if run_len < 1 or run_len > 6:
                        continue
                    start = int(group[0])
                    end = int(group[-1])
                    before = light_ratio[max(0, start - 10):max(0, start - 2)]
                    after = light_ratio[min(h, end + 3):min(h, end + 11)]
                    before_med = float(np.median(before)) if before.size else 1.0
                    after_med = float(np.median(after)) if after.size else 1.0
                    before_mean = float(np.median(row_mean[max(0, start - 10):max(0, start - 2)])) if start > 3 else 255.0
                    after_mean = float(np.median(row_mean[min(h, end + 3):min(h, end + 11)])) if end < h - 4 else 255.0
                    if before_med > 0.955 and after_med > 0.955 and abs(before_mean - after_mean) < 8.0:
                        continue
                    remove[start:end + 1] = True

            removed = int(remove.sum())
            if not removed:
                rgb.close()
                return info
            if removed > 180:
                rgb.close()
                info.update({
                    "naver_stitch_seams_skipped_excessive": removed,
                    "naver_stitch_seam_strategy": "conservative_skip",
                })
                logger.info(
                    "[Detail] skipped Naver seam cleanup because %s candidate rows looked excessive for %s",
                    removed,
                    path,
                )
                return info

            cleaned = Image.fromarray(arr[~remove], "RGB")
            tmp_path = f"{path}.seam.tmp.jpg"
            cleaned.save(tmp_path, "JPEG", quality=86)
            cleaned.close()
            rgb.close()
            os.replace(tmp_path, path)
            info.update({
                "naver_stitch_seams_removed": removed,
                "naver_stitch_original_height": int(h),
                "naver_stitch_cleaned_height": int(h - removed),
            })
            logger.info("[Detail] removed %s thin Naver stitch seam rows from %s", removed, path)
            return info
    except Exception as exc:
        info["naver_stitch_seam_error"] = str(exc)
        logger.debug("[Detail] Naver stitch seam cleanup skipped: %s", exc)
    return info


def _postprocess_naver_capture(path: str) -> Dict:
    """Trim Naver SmartStore's right-side option rail from captured detail images."""
    info = {"naver_right_rail_cropped": False}
    if not getattr(config, "ENABLE_NAVER_RIGHT_RAIL_CROP", False):
        info.update(_remove_naver_stitch_seams(path))
        return info
    try:
        with Image.open(path) as img:
            rgb = img.convert("RGB")
            w, h = rgb.size
            if w < 1500 or h < 700:
                rgb.close()
                info.update(_remove_naver_stitch_seams(path))
                return info

            preferred_width = int(getattr(config, "NAVER_RIGHT_RAIL_CROP_WIDTH", 1240) or 1240)
            if w >= 1800:
                right = preferred_width
            elif w >= 1500:
                right = min(preferred_width, int(w * 0.72))
            else:
                right = w

            right = max(980, min(w, right))
            if right >= w - 32:
                rgb.close()
                info.update(_remove_naver_stitch_seams(path))
                return info

            tmp_path = path + ".tmp.jpg"
            cropped = rgb.crop((0, 0, right, h))
            cropped.save(tmp_path, "JPEG", quality=86)
            cropped.close()
            rgb.close()
            os.replace(tmp_path, path)
            info.update({
                "naver_right_rail_cropped": True,
                "naver_original_width": int(w),
                "naver_crop_width": int(right),
            })
            info.update(_remove_naver_stitch_seams(path))
            logger.info("[Detail] cropped Naver right rail: %s -> %s (%s)", w, right, path)
            return info
    except Exception as exc:
        info["naver_crop_error"] = str(exc)
        logger.debug("[Detail] Naver postprocess skipped: %s", exc)
    info.update(_remove_naver_stitch_seams(path))
    return info


def is_detail_result_usable(detail) -> bool:
    if not detail:
        return False
    screenshots = detail.get("screenshots") or []
    if not screenshots:
        return False
    diagnostics = detail.get("diagnostics") or {}
    if diagnostics.get("capture_version") != DETAIL_CAPTURE_VERSION:
        return False
    if diagnostics.get("ok") is False:
        return False
    target_title = str(diagnostics.get("target_title") or "").lower()
    if any(word in target_title for word in ("login", "sign in", "\ub85c\uadf8\uc778")):
        return False
    if any(word in target_title for word in ("잠시만 기다", "just a moment", "보안 확인", "security check")):
        return False
    for screenshot in screenshots:
        if not screenshot or not os.path.exists(screenshot):
            return False
        if not _image_file_quality(screenshot).get("ok"):
            return False
    return True


class DetailScraper:
    def __init__(self):
        self.output_dir = config.DETAIL_DIR

    # ──────────────────────────────────────────────────────────────────────
    #  【1순위】 DrissionPage 캡처 (쿠팡·기타)
    # ──────────────────────────────────────────────────────────────────────
    def _capture_drission(self, product_url: str, product_id: str,
                           output_dir: str, slice_height: int = 0) -> Dict:
        result = {"screenshots": [], "mhtml_path": "", "method": "drission", "status": "started"}
        product_url = _normalize_detail_url(product_url)
        platform_key = adaptive_learning.normalize_platform("", product_url)
        result["diagnostics"] = {"capture_version": DETAIL_CAPTURE_VERSION, "platform": platform_key}
        os.makedirs(output_dir, exist_ok=True)

        page = _get_drission_page()
        try:
            is_coupang = "coupang.com" in product_url

            if is_coupang:
                logger.info(f"[{product_id}] 쿠팡 메인 선방문...")
                page.get("https://www.coupang.com", retry=1, interval=1, timeout=30)
                time.sleep(1.5)

            logger.info(f"[{product_id}] 상세 페이지 이동: {product_url}")
            page.get(product_url, retry=1, interval=1, timeout=45)
            time.sleep(3.5)

            html = page.html
            if _is_blocked(html):
                logger.warning(f"[{product_id}] DrissionPage 봇 차단 감지 → AHK CF 캡챠 클릭 시도")
                if _run_ahk_cf_captcha_click(hwnd=_find_captcha_hwnd(), max_wait_sec=12, check_interval_ms=400):
                    logger.info(f"[{product_id}] AHK CF 클릭 완료 → 최대 15초 대기 후 재확인")
                    resolved = False
                    for _ in range(15):
                        time.sleep(1.0)
                        html = page.html
                        if not _is_blocked(html):
                            resolved = True
                            break
                    if resolved:
                        logger.info(f"[{product_id}] CF 캡챠 해제 성공, DrissionPage 캡처 계속")
                        _show_captcha_success_toast(product_id)
                    else:
                        logger.warning(f"[{product_id}] CF 캡챠 해제 실패 → blocked 처리")
                        result["status"] = "blocked"
                        page.quit()
                        return result
                else:
                    logger.warning(f"[{product_id}] AHK 클릭 실패 → blocked 처리")
                    result["status"] = "blocked"
                    page.quit()
                    return result

            expanded = self._expand_market_details_drission(page, product_id, platform_key)

            for _ in range(15):
                page.run_js("window.scrollBy(0, window.innerHeight * 0.8)")
                time.sleep(0.3)
            page.run_js("window.scrollTo(0, 0)")
            time.sleep(1)

            if is_coupang:
                self._expand_coupang_details(page, product_id)

            body_height = page.run_js("return document.body.scrollHeight") or 1080
            fullpage_path = os.path.join(output_dir, f"{product_id}_fullpage.jpg")
            raw_png = os.path.join(output_dir, f"{product_id}_fullpage.png")

            page.run_js("window.scrollTo(0, 0)")
            time.sleep(2.0)

            captured = False
            try:
                page.get_screenshot(path=output_dir, name=f"{product_id}_fullpage.png", full_page=True)
                if os.path.exists(raw_png):
                    img = Image.open(raw_png)
                    if img.mode in ("RGBA", "P"):
                        img = img.convert("RGB")
                    brightness = np.array(img).mean()
                    if brightness < 15:
                        img.close(); os.remove(raw_png)
                    else:
                        img.save(fullpage_path, "JPEG", quality=85)
                        img.close(); os.remove(raw_png)
                        captured = True
            except Exception as e:
                logger.warning(f"[{product_id}] DrissionPage 캡처 실패: {e}")

            if not captured:
                fullpage_path = self._scroll_capture_fallback(page, output_dir, product_id, int(body_height))

            page.quit()

            if not fullpage_path or not os.path.exists(fullpage_path):
                return result

            quality = _set_result_quality(result, fullpage_path, {"expanded": expanded})
            if not quality.get("ok"):
                logger.warning("[Detail] Drission screenshot rejected for %s: %s", product_id, quality)
                result["status"] = "bad_screenshot"
                return result

            if slice_height > 0:
                result["screenshots"] = self._slice_image(fullpage_path, output_dir, slice_height)
            else:
                result["screenshots"] = [fullpage_path]
            result["status"] = "success"

            return result

        except Exception as e:
            logger.error(f"[{product_id}] DrissionPage 오류: {e}")
            try: page.quit()
            except Exception: pass
            return result

    def _expand_market_details_drission(self, page, product_id: str, platform_key: str) -> List[str]:
        clicked: List[str] = []
        for attempt in range(3):
            try:
                result = page.run_js(DETAIL_EXPAND_JS) or []
                if isinstance(result, list):
                    clicked.extend(str(item) for item in result)
                elif isinstance(result, dict):
                    clicked.extend(str(item) for item in (result.get("clicked") or []))
                time.sleep(0.8 + attempt * 0.4)
            except Exception as exc:
                logger.debug("[Detail] Drission detail expand skipped for %s: %s", product_id, exc)
                break
        if clicked:
            logger.info("[Detail] expanded %s detail via Drission: %s", platform_key, clicked[:3])
        return clicked

    def _expand_market_details_playwright(self, page, product_id: str, platform_key: str) -> List[str]:
        clicked: List[str] = []
        for attempt in range(3):
            try:
                result = page.evaluate(DETAIL_EXPAND_JS) or []
                if isinstance(result, list):
                    clicked.extend(str(item) for item in result)
                elif isinstance(result, dict):
                    clicked.extend(str(item) for item in (result.get("clicked") or []))
                page.wait_for_timeout(900 + attempt * 350)
            except Exception as exc:
                logger.debug("[Detail] Playwright detail expand skipped for %s: %s", product_id, exc)
                break
        if clicked:
            logger.info("[Detail] expanded %s detail via Playwright: %s", platform_key, clicked[:3])
        return clicked

    def _playwright_scroll_capture_fallback(self, page, output_dir: str, product_id: str, body_height: int) -> str:
        try:
            viewport_h = int(page.evaluate("window.innerHeight") or 1080)
            viewport_w = int(page.evaluate("window.innerWidth") or 1920)
        except Exception:
            viewport_h = 1080
            viewport_w = 1920

        step = max(420, int(viewport_h * 0.82))
        max_y = max(0, int(body_height) - viewport_h)
        y_positions = list(range(0, max_y + 1, step))
        if not y_positions or y_positions[-1] != max_y:
            y_positions.append(max_y)
        y_positions = y_positions[:36]

        chunk_paths = []
        for index, y in enumerate(y_positions):
            try:
                page.evaluate(f"window.scrollTo(0, {int(y)})")
                page.wait_for_timeout(450)
                chunk_path = os.path.join(output_dir, f"_pw_chunk_{index:03d}.png")
                page.screenshot(path=chunk_path, full_page=False, timeout=15000)
                if os.path.exists(chunk_path):
                    chunk_paths.append(chunk_path)
            except Exception as exc:
                logger.warning("[Detail] Playwright viewport chunk %s failed for %s: %s", index, product_id, exc)

        if not chunk_paths:
            return ""

        pil_imgs = []
        try:
            for path in chunk_paths:
                pil_imgs.append(Image.open(path).convert("RGB"))
            width = min((img.width for img in pil_imgs), default=viewport_w)
            total_h = sum(img.height for img in pil_imgs)
            merged = Image.new("RGB", (width, total_h), "white")
            y_offset = 0
            for img in pil_imgs:
                crop = img.crop((0, 0, width, img.height))
                merged.paste(crop, (0, y_offset))
                y_offset += img.height
                crop.close()
            fullpage_path = os.path.join(output_dir, f"{product_id}_fullpage.jpg")
            merged.save(fullpage_path, "JPEG", quality=85)
            merged.close()
            return fullpage_path
        finally:
            for img in pil_imgs:
                try:
                    img.close()
                except Exception:
                    pass
            for path in chunk_paths:
                try:
                    os.remove(path)
                except Exception:
                    pass

    def _expand_coupang_details(self, page, product_id: str) -> None:
        clicked = False
        for kw in ["상품정보 더보기", "상세정보 더보기"]:
            try:
                btn = page.ele(f"text:{kw}", timeout=2)
                if btn:
                    btn.click()
                    clicked = True
                    break
            except Exception:
                pass

        if not clicked:
            result = page.run_js(EXPAND_JS)
            time.sleep(1.5)
            old_h = page.run_js("return document.body.scrollHeight") or 0
            time.sleep(2)
            new_h = page.run_js("return document.body.scrollHeight") or 0
            if int(new_h) > int(old_h) + 100:
                clicked = True

        if clicked:
            time.sleep(2)
            for _ in range(10):
                page.run_js("window.scrollBy(0, window.innerHeight)")
                time.sleep(0.4)
            time.sleep(1)
            body_h = page.run_js("return document.body.scrollHeight") or 10000
            steps = max(30, int(body_h / 300))
            for i in range(steps):
                y = int(i * body_h / steps)
                page.run_js(f"window.scrollTo(0, {y})")
                time.sleep(0.2)
            page.run_js("window.scrollTo(0, 0)")
            time.sleep(2)

    # ──────────────────────────────────────────────────────────────────────
    #  【2순위】 Playwright + ProScraper 스텔스 캡처 (쿠팡·기타 1순위)
    # ──────────────────────────────────────────────────────────────────────
    def _capture_playwright(self, product_url: str, product_id: str,
                             output_dir: str, is_naver: bool = False) -> Dict:
        """
        Playwright + ProScraper 완전 스텔스 캡처.
        쿠팡: 메인 페이지 먼저 방문 (Akamai 세션 확립) 후 상세 페이지.
        """
        result = {"screenshots": [], "mhtml_path": "", "method": "playwright_user_profile", "status": "started"}
        product_url = _normalize_detail_url(product_url)
        platform_key = adaptive_learning.normalize_platform("", product_url)
        result["diagnostics"] = {"capture_version": DETAIL_CAPTURE_VERSION, "platform": platform_key}
        os.makedirs(output_dir, exist_ok=True)

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("[Playwright] 미설치 → 차선책 불가")
            return result

        try:
            from engine.stealth import get_full_stealth_script
            stealth_script = get_full_stealth_script()
        except ImportError:
            # 폴백: 기본 스텔스만
            stealth_script = """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR','ko','en-US']});
            """

        is_coupang = "coupang.com" in product_url
        logger.info(f"[{product_id}] Playwright 스텔스 캡처 시작: {product_url}")

        # ★ bypass_engine으로 Akamai 쿠키 선획득 (쿠팡)
        bypass_cookies_list = []
        if is_coupang:
            try:
                from engine.bypass_engine import get_bypass_cookies
                bypass_cookies = get_bypass_cookies(
                    'https://www.coupang.com', protection='akamai'
                )
                for name, value in bypass_cookies.items():
                    bypass_cookies_list.append({
                        'name': name, 'value': value,
                        'domain': '.coupang.com', 'path': '/',
                    })
                logger.debug(f"[{product_id}] bypass 쿠키 {len(bypass_cookies_list)}개 준비")
            except Exception as _be:
                logger.debug(f"[{product_id}] bypass_engine 생략: {_be}")

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--lang=ko-KR",
            "--window-size=1920,1080",
            "--force-device-scale-factor=1",
            "--disable-web-security",
        ]
        pw_proxy = None
        try:
            from engine.ip_manager import get_playwright_proxy
            pw_proxy = get_playwright_proxy("naver" if is_naver else product_url)
        except Exception as e:
            logger.debug(f"[{product_id}] proxy skipped: {e}")

        try:
            with sync_playwright() as pw:
                context = None
                tmp_profile = None

                # 네이버: Chrome 프로필 임시복사로 로그인 세션 활용
                if getattr(config, 'USE_USER_BROWSER_SESSION', True):
                    chrome_profile = getattr(config, 'CHROME_USER_DATA_DIR', '') or os.path.join(
                        os.environ.get('LOCALAPPDATA', ''),
                        r"Google\Chrome\User Data"
                    )
                    profile_directory = None
                    try:
                        from engine.browser_profile import chrome_profile_name, naver_chrome_profile_name
                        profile_directory = naver_chrome_profile_name() if is_naver else chrome_profile_name()
                    except Exception:
                        profile_directory = getattr(config, "CHROME_PROFILE_DIRECTORY", "Default") or "Default"
                    tmp_profile = _copy_chrome_profile_tmp(chrome_profile, profile_directory=profile_directory)
                    if tmp_profile:
                        try:
                            result.setdefault("diagnostics", {}).update({"chrome_profile_directory": profile_directory})
                            context = pw.chromium.launch_persistent_context(
                                user_data_dir=tmp_profile,
                                channel="chrome",
                                headless=False,
                                **({"proxy": pw_proxy} if pw_proxy else {}),
                                args=launch_args + [f"--profile-directory={profile_directory}"],
                                viewport={"width": 1920, "height": 1080},
                                no_viewport=False,
                            )
                        except Exception as e:
                            logger.warning(f"[{product_id}] 프로필 컨텍스트 실패: {e}")
                            context = None

                # 프로필 없으면 일반 브라우저
                if context is None:
                    result["method"] = "playwright_headless"
                    browser = pw.chromium.launch(
                        headless=True,
                        args=launch_args,
                        **({"proxy": pw_proxy} if pw_proxy else {}),
                    )
                    context = browser.new_context(
                        viewport={"width": 1920, "height": 1080},
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        locale="ko-KR",
                        timezone_id="Asia/Seoul",
                        extra_http_headers={
                            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
                            "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
                            "sec-ch-ua-mobile": "?0",
                            "sec-ch-ua-platform": '"Windows"',
                        },
                    )

                # bypass_engine 쿠키 주입 (쿠팡 Akamai 우회)
                if bypass_cookies_list:
                    try:
                        context.add_cookies(bypass_cookies_list)
                        logger.debug(f"[{product_id}] bypass 쿠키 Playwright context 주입 완료")
                    except Exception as _ce:
                        logger.debug(f"[{product_id}] 쿠키 주입 실패: {_ce}")

                page = context.new_page()

                # ★ ProScraper 풀 스텔스 주입
                page.add_init_script(stealth_script)

                # 쿠팡: 메인 페이지 먼저 방문 (Akamai 세션 확립)
                if is_coupang:
                    logger.info(f"[{product_id}] 쿠팡 메인 방문 (Akamai 세션 확립)...")
                    try:
                        page.goto("https://www.coupang.com", wait_until="domcontentloaded", timeout=20000)
                        time.sleep(random.uniform(2.0, 3.5))
                        # 자연스러운 스크롤
                        for _ in range(random.randint(2, 3)):
                            page.mouse.wheel(0, random.randint(200, 400))
                            time.sleep(random.uniform(0.3, 0.7))
                        time.sleep(1.0)
                    except Exception:
                        pass

                # 상세 페이지 이동
                try:
                    page.goto(product_url, wait_until="domcontentloaded", timeout=35000)
                except Exception as eg:
                    logger.warning(f"[{product_id}] goto 실패: {eg}")

                time.sleep(random.uniform(3.0, 4.5))

                # CAPTCHA 자동 해결 시도
                try:
                    from engine.captcha import get_solver
                    solver = get_solver()
                    if solver.available():
                        solved = solver.auto_solve_page(page)
                        if solved:
                            logger.info(f"[{product_id}] CAPTCHA 자동 해결 완료")
                            time.sleep(2.0)
                except Exception:
                    pass

                content = page.content()
                if _is_blocked(content):
                    logger.warning(f"[{product_id}] Playwright 봇 차단 감지 → AHK CF 캡챠 클릭 시도")
                    # AHK로 Cloudflare 체크박스 클릭 시도 (gmarket/auction 공통)
                    if _run_ahk_cf_captcha_click(hwnd=_find_captcha_hwnd(), max_wait_sec=12, check_interval_ms=400):
                        logger.info(f"[{product_id}] AHK CF 클릭 완료 → 최대 15초 대기 후 재확인")
                        resolved = False
                        for _ in range(15):
                            time.sleep(1.0)
                            content = page.content()
                            if not _is_blocked(content):
                                resolved = True
                                break
                        if resolved:
                            logger.info(f"[{product_id}] CF 캡챠 해제 성공, Playwright 캡처 계속")
                            _show_captcha_success_toast(product_id)
                        else:
                            logger.warning(f"[{product_id}] CF 캡챠 해제 실패 → blocked 처리")
                            result["status"] = "blocked"
                            context.close()
                            if tmp_profile:
                                shutil.rmtree(tmp_profile, ignore_errors=True)
                            return result
                    else:
                        logger.warning(f"[{product_id}] AHK 클릭 실패 → blocked 처리")
                        result["status"] = "blocked"
                        context.close()
                        if tmp_profile:
                            shutil.rmtree(tmp_profile, ignore_errors=True)
                        return result

                expanded = self._expand_market_details_playwright(page, product_id, platform_key)

                # lazy-load 스크롤 (1차)
                body_h = page.evaluate("document.body.scrollHeight") or 5000
                steps = max(20, int(body_h / 400))
                for i in range(steps):
                    y = int(i * body_h / steps)
                    page.evaluate(f"window.scrollTo(0, {y})")
                    time.sleep(0.15)
                time.sleep(1)

                # 쿠팡: 더보기 클릭
                if is_coupang:
                    for kw in ["상품정보 더보기", "상세정보 더보기", "상품상세 더보기"]:
                        try:
                            btn = page.locator(f"text={kw}").first
                            if btn.count() > 0 and btn.is_visible(timeout=2000):
                                btn.scroll_into_view_if_needed()
                                btn.click()
                                logger.info(f"[{product_id}] 더보기 클릭: '{kw}'")
                                time.sleep(2)
                                break
                        except Exception:
                            pass

                # 최종 느린 스크롤 (lazy-load 이미지 완전 로드)
                final_h = page.evaluate("document.body.scrollHeight") or body_h
                slow_steps = max(30, min(80, int(final_h / 250)))
                for i in range(slow_steps):
                    y = int(i * final_h / slow_steps)
                    page.evaluate(f"window.scrollTo(0, {y})")
                    time.sleep(0.2)
                page.evaluate(f"window.scrollTo(0, {final_h})")
                time.sleep(2.5)
                page.evaluate("window.scrollTo(0, 0)")
                time.sleep(1.5)

                # 풀페이지 스크린샷
                expanded.extend(self._expand_market_details_playwright(page, product_id, platform_key))

                png_path = os.path.join(output_dir, f"{product_id}_pw.png")
                fullpage_path = os.path.join(output_dir, f"{product_id}_fullpage.jpg")
                used_viewport_fallback = False
                try:
                    page.screenshot(path=png_path, full_page=True, timeout=30000)
                except Exception as shot_exc:
                    logger.warning(
                        "[Detail] Playwright full-page screenshot failed for %s; using viewport chunks: %s",
                        product_id,
                        shot_exc,
                    )
                    fullpage_path = self._playwright_scroll_capture_fallback(
                        page,
                        output_dir,
                        product_id,
                        int(final_h or body_h or 1080),
                    )
                    used_viewport_fallback = True
                finally:
                    context.close()
                    if tmp_profile:
                        shutil.rmtree(tmp_profile, ignore_errors=True)

                if not used_viewport_fallback and not os.path.exists(png_path):
                    return result
                if used_viewport_fallback and (not fullpage_path or not os.path.exists(fullpage_path)):
                    return result

                img = Image.open(fullpage_path if used_viewport_fallback else png_path)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                brightness = np.array(img).mean()
                if brightness < 15:
                    logger.warning(f"[{product_id}] 검은 이미지 (밝기={brightness:.1f})")
                    img.close()
                    try:
                        os.remove(fullpage_path if used_viewport_fallback else png_path)
                    except Exception:
                        pass
                    return result

                if not used_viewport_fallback:
                    img.save(fullpage_path, "JPEG", quality=85)
                    img.close()
                    try:
                        os.remove(png_path)
                    except Exception:
                        pass
                else:
                    img.close()
                quality = _set_result_quality(result, fullpage_path, {"expanded": expanded})
                if not quality.get("ok"):
                    logger.warning("[Detail] Playwright screenshot rejected for %s: %s", product_id, quality)
                    result["status"] = "bad_screenshot"
                    return result

                logger.info(f"[{product_id}] Playwright 스텔스 캡처 완료: 밝기={brightness:.1f}")
                result["screenshots"] = [fullpage_path]
                result["status"] = "success"
                return result

        except Exception as e:
            logger.error(f"[{product_id}] Playwright 오류: {e}")
            if tmp_profile:
                shutil.rmtree(tmp_profile, ignore_errors=True)
            return result

    # ──────────────────────────────────────────────────────────────────────
    #  【3순위】 화면 캡처 (사용자 브라우저 세션 활용)
    # ──────────────────────────────────────────────────────────────────────
    def _capture_naver_via_screen(self, product_url: str, product_id: str,
                                   output_dir: str, scroll_driver: str = "win32",
                                   job_id: str = "") -> Dict:
        """
        네이버 화면 캡처 v2.
        Chrome subprocess 열기 → PrintWindow API 캡처 → 이어붙이기.
        """
        import subprocess
        import ctypes

        method_name = "ahk_screen" if scroll_driver == "ahk" else "chrome_screen"
        result = {"screenshots": [], "mhtml_path": "", "method": method_name, "status": "started"}
        product_url = _normalize_detail_url(product_url)
        result["diagnostics"] = {"capture_version": DETAIL_CAPTURE_VERSION}
        os.makedirs(output_dir, exist_ok=True)
        if scroll_driver == "ahk" and not _find_autohotkey_exe():
            result["status"] = "ahk_unavailable"
            return result

        chrome_exe = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        if not os.path.exists(chrome_exe):
            logger.error("[화면캡처] Chrome 없음")
            return result

        product_url_lower = product_url.lower()
        is_coupang = "coupang.com" in product_url_lower
        is_elevenst = "11st.co.kr" in product_url_lower
        is_gmarket = "gmarket" in product_url_lower
        is_auction = "auction" in product_url_lower
        is_naver = "naver.com" in product_url_lower or "smartstore" in product_url_lower
        is_naver_catalog = "search.shopping.naver.com/catalog" in product_url_lower
        nav_url = _clean_coupang_url(product_url) if is_coupang else product_url
        logger.info(f"[{product_id}] 화면 캡처 v2 시작: {product_url}")

        hwnd_target = None
        tab_opened = False
        new_window_opened = False
        keep_window_open = False
        debug_port = None
        NAVER_HINTS = ['naver', '스마트스토어', 'smartstore', '네이버']

        def _get_chrome_wins():
            wins = []
            def _cb(hwnd, _):
                if (win32gui.IsWindowVisible(hwnd)
                        and win32gui.GetClassName(hwnd) == 'Chrome_WidgetWin_1'):
                    t = win32gui.GetWindowText(hwnd)
                    if t:
                        wins.append((hwnd, t))
            win32gui.EnumWindows(_cb, None)
            return wins

        try:
            import win32gui, win32con, win32api

            chrome_before = _get_chrome_wins()
            before_hwnds = {hwnd for hwnd, _title in chrome_before}
            chrome_args = [chrome_exe, "--no-first-run", "--no-default-browser-check"]
            if is_coupang:
                try:
                    from engine.browser_profile import coupang_browser_profile_dir
                    try:
                        debug_port = int(getattr(config, "COUPANG_DEBUG_PORT", 9223) or 9223)
                    except Exception:
                        debug_port = None
                    chrome_args.extend([
                        f"--user-data-dir={coupang_browser_profile_dir()}",
                        "--profile-directory=Default",
                    ])
                    if debug_port:
                        chrome_args.append(f"--remote-debugging-port={debug_port}")
                        chrome_args.append("--remote-allow-origins=*")
                except Exception as exc:
                    logger.debug(f"[{product_id}] 쿠팡 전용 프로필 생략: {exc}")
            elif is_naver:
                try:
                    import socket
                    from engine.browser_profile import chrome_profiles_for_domain, chrome_user_data_dir
                    naver_profiles = chrome_profiles_for_domain("naver.com", limit=3)
                    naver_profile = naver_profiles[0] if naver_profiles else getattr(config, "CHROME_PROFILE_DIRECTORY", "Default")
                    try:
                        configured_port = int(getattr(config, "NAVER_DEBUG_PORT", 0) or 0)
                    except Exception:
                        configured_port = 0
                    if configured_port:
                        debug_port = configured_port
                    else:
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                            sock.bind(("127.0.0.1", 0))
                            debug_port = int(sock.getsockname()[1])
                    result["diagnostics"].update({
                        "chrome_user_data_dir": str(chrome_user_data_dir()),
                        "chrome_profile_directory": naver_profile,
                        "chrome_profile_candidates": naver_profiles,
                        "naver_debug_port_requested": debug_port,
                    })
                    chrome_args.extend([
                        f"--user-data-dir={chrome_user_data_dir()}",
                        f"--profile-directory={naver_profile}",
                        f"--remote-debugging-port={debug_port}",
                        "--remote-allow-origins=*",
                    ])
                    logger.info("[Detail] Naver screen capture uses Chrome profile: %s", naver_profile)
                except Exception as exc:
                    debug_port = None
                    logger.warning(f"[{product_id}] 네이버 프로필 자동 선택 실패: {exc}")
            elif is_elevenst or is_gmarket or is_auction:
                try:
                    import socket
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                        sock.bind(("127.0.0.1", 0))
                        debug_port = int(sock.getsockname()[1])
                    if is_elevenst:
                        profile_name = "screen_elevenst"
                    else:
                        profile_name = "screen_gmarket" if is_gmarket else "screen_auction"
                    screen_profile_dir = os.path.join(config.DATA_DIR, "browser_profiles", profile_name)
                    os.makedirs(screen_profile_dir, exist_ok=True)
                    chrome_args.extend([
                        f"--user-data-dir={screen_profile_dir}",
                        f"--remote-debugging-port={debug_port}",
                        "--remote-allow-origins=*",
                    ])
                except Exception as exc:
                    debug_port = None
                    logger.debug(f"[{product_id}] screen CDP profile skipped: {exc}")
            chrome_args.append(nav_url)

            if is_coupang:
                launch_args = chrome_args[:-1] + ['--new-window', product_url]
                subprocess.Popen(launch_args)
                new_window_opened = True
            elif chrome_before:
                subprocess.Popen([chrome_exe, '--new-window', *chrome_args[1:]])
                new_window_opened = True
            else:
                subprocess.Popen([chrome_exe, '--new-window', *chrome_args[1:]])
                new_window_opened = True

            time.sleep(4)
            if is_coupang:
                title_hints = ['coupang', '\ucfe0\ud321']
            elif is_elevenst:
                title_hints = ['11st', '11st.co.kr', '11\ubc88\uac00']
            elif "auction" in product_url.lower():
                title_hints = ['auction', '\uc625\uc158']
            elif "gmarket" in product_url.lower():
                title_hints = ['gmarket', 'g\ub9c8\ucf13']
            else:
                title_hints = ['naver', 'smartstore', '\ub124\uc774\ubc84']
            last_new_wins = []
            def _looks_like_coupang_product(title: str) -> bool:
                if not is_coupang:
                    return False
                title_lower = (title or "").lower()
                if "coupang" not in title_lower and "쿠팡" not in title:
                    return False
                reject = ["추천하는", "글로벌 스토어", "페이지를 복원", "chrome에 로그인", "translate"]
                return not any(word.lower() in title_lower for word in reject)

            for _wait in range(24):
                wins_now = _get_chrome_wins()
                if is_coupang:
                    new_wins = [(hwnd, title) for hwnd, title in wins_now if hwnd not in before_hwnds]
                    if new_wins:
                        last_new_wins = new_wins
                        product_wins = [(hwnd, title) for hwnd, title in new_wins if _looks_like_coupang_product(title)]
                    else:
                        product_wins = [(hwnd, title) for hwnd, title in wins_now if _looks_like_coupang_product(title)]
                    if product_wins:
                        hwnd_target = product_wins[0][0]
                        break
                if not is_coupang:
                    new_wins = [(hwnd, title) for hwnd, title in wins_now if hwnd not in before_hwnds]
                    if new_wins:
                        last_new_wins = new_wins
                    candidate_wins = new_wins if is_auction else (new_wins or wins_now)
                    for hwnd, title in candidate_wins:
                        title_lower = title.lower()
                        if any(hint.lower() in title_lower for hint in title_hints):
                            hwnd_target = hwnd
                            break
                    if not hwnd_target and new_wins and not (is_auction or is_gmarket):
                        hwnd_target = new_wins[-1][0]
                if hwnd_target:
                    break
                time.sleep(0.5)

            if not hwnd_target and last_new_wins:
                if is_auction:
                    auction_wins = [
                        (hwnd, title)
                        for hwnd, title in last_new_wins
                        if any(hint.lower() in title.lower() for hint in title_hints)
                    ]
                    if len(auction_wins) == 1:
                        hwnd_target = auction_wins[0][0]
                    elif len(last_new_wins) == 1:
                        hwnd_target = last_new_wins[0][0]
                    else:
                        logger.warning(
                            "[Detail] Auction screen capture refused: ambiguous new windows %s",
                            [title for _, title in last_new_wins],
                        )
                else:
                    hwnd_target = last_new_wins[-1][0]

            if not hwnd_target:
                if new_window_opened and not is_coupang:
                    logger.warning(
                        "[Detail] Screen capture refused for %s: opened window was not found",
                        product_id,
                    )
                    result["status"] = "wrong_window"
                    return result
                wins_now = _get_chrome_wins()
                if wins_now:
                    hwnd_target = wins_now[0][0]

            if not hwnd_target:
                logger.error(f"[{product_id}] Chrome 창 확보 실패")
                return result

            page_title = win32gui.GetWindowText(hwnd_target)
            bad_screen_titles = ["login", "sign in", "\ub85c\uadf8\uc778", "제목 없음", "untitled", "about:blank"]
            title_lower = (page_title or "").lower()
            if is_gmarket and any(word in title_lower for word in ("제목 없음", "untitled", "about:blank")):
                for _ in range(32):
                    time.sleep(0.5)
                    page_title = win32gui.GetWindowText(hwnd_target)
                    title_lower = (page_title or "").lower()
                    if not any(word in title_lower for word in ("제목 없음", "untitled", "about:blank")):
                        break
            if not is_coupang and any(word in title_lower for word in bad_screen_titles):
                if is_naver:
                    login_wait_sec = max(0, int(getattr(config, "NAVER_LOGIN_WAIT_SEC", 240) or 0))
                    result["diagnostics"].update({
                        "target_title": page_title,
                        "manual_required": True,
                        "login_wait_sec": login_wait_sec,
                    })
                    logger.warning(
                        "[Detail] Naver login required for %s. Waiting up to %ss for manual login.",
                        product_id,
                        login_wait_sec,
                    )
                    _run_ahk_naver_login_click(hwnd=hwnd_target, max_wait_sec=5)
                    try:
                        if job_id:
                            import progress_store as _progress_store
                            _progress_store.set_status(
                                f"네이버 로그인 대기 중: 열린 Chrome 창에서 로그인해 주세요. 최대 {login_wait_sec}초 대기합니다.",
                                job_id,
                                metadata={
                                    "stage": "detail_capture",
                                    "product_id": product_id,
                                    "platform": "naver",
                                    "status": "login_wait",
                                    "wait_sec": login_wait_sec,
                                },
                            )
                    except Exception:
                        pass

                    def _go_product_again() -> None:
                        try:
                            import pyautogui as _pag
                            try:
                                win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                                win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
                                time.sleep(0.05)
                                ctypes.windll.user32.SetForegroundWindow(hwnd_target)
                            except Exception:
                                pass
                            _pag.hotkey("ctrl", "l")
                            try:
                                import pyperclip
                                pyperclip.copy(product_url)
                                _pag.hotkey("ctrl", "v")
                            except Exception:
                                _pag.write(product_url, interval=0)
                            _pag.press("enter")
                        except Exception as exc:
                            logger.debug("[Detail] Naver post-login navigation skipped: %s", exc)

                    deadline = time.time() + login_wait_sec
                    relogin_tried = False
                    while time.time() < deadline:
                        time.sleep(2.0)
                        page_title = win32gui.GetWindowText(hwnd_target)
                        title_lower = (page_title or "").lower()
                        if any(word in title_lower for word in bad_screen_titles):
                            continue
                        if not relogin_tried:
                            relogin_tried = True
                            _go_product_again()
                            time.sleep(4.0)
                            page_title = win32gui.GetWindowText(hwnd_target)
                            title_lower = (page_title or "").lower()
                            if any(word in title_lower for word in bad_screen_titles):
                                continue
                        result["diagnostics"].update({
                            "target_title": page_title,
                            "manual_login_completed": True,
                        })
                        try:
                            if job_id:
                                import progress_store as _progress_store
                                _progress_store.set_status(
                                    "네이버 로그인 확인됨. 상세페이지 캡처를 계속합니다.",
                                    job_id,
                                    metadata={
                                        "stage": "detail_capture",
                                        "product_id": product_id,
                                        "platform": "naver",
                                        "status": "login_resumed",
                                    },
                                )
                        except Exception:
                            pass
                        logger.info("[Detail] Naver login completed for %s; continuing capture: %s", product_id, page_title)
                        break
                    else:
                        keep_window_open = True
                        result["status"] = "login_required"
                        result["reason"] = "Naver login required"
                        result["diagnostics"].update({
                            "target_title": page_title,
                            "manual_required": True,
                            "manual_login_timeout": True,
                        })
                        logger.warning("[Detail] Naver manual login timed out for %s", product_id)
                        return result

                    title_lower = (page_title or "").lower()
                    if any(word in title_lower for word in bad_screen_titles):
                        keep_window_open = True
                        result["status"] = "login_required"
                        result["reason"] = "Naver login required"
                        result["diagnostics"].update({"target_title": page_title, "manual_required": True})
                        return result
                else:
                    logger.warning("[Detail] Screen capture rejected for %s: wrong/login window %s", product_id, page_title)
                    result["status"] = "wrong_window"
                    result["diagnostics"].update({
                        "target_title": page_title,
                        "manual_required": False,
                    })
                    return result
            if not is_coupang and any(word in title_lower for word in bad_screen_titles):
                logger.warning("[Detail] Screen capture rejected for %s: wrong/login window %s", product_id, page_title)
                result["status"] = "login_required" if is_naver else "wrong_window"
                if is_naver:
                    result["reason"] = "Naver login required"
                result["diagnostics"].update({
                    "target_title": page_title,
                    "manual_required": bool(is_naver),
                })
                return result
            if is_elevenst and any(word in title_lower for word in ("naver", "smartstore", "\ub124\uc774\ubc84")):
                logger.warning("[Detail] Screen capture rejected for %s: expected 11st but got %s", product_id, page_title)
                result["status"] = "wrong_window"
                result["diagnostics"].update({"target_title": page_title})
                return result
            logger.info(f"[{product_id}] 화면 캡처 대상 창: {hwnd_target} {page_title}")
            CAPTCHA_TITLES = [
                "보안 확인",           # 일반
                "Security Check",      # 영문
                "로봇이 아닙니다",     # 일반
                "사용자 활동 검토",    # Gmarket Cloudflare
                "원활한 서비스",       # Auction Cloudflare
                "간단한 확인 안내",    # Auction Cloudflare 2
                "Just a moment",       # Cloudflare 기본
                "잠시만 기다려주십시오",# Cloudflare 한글 1
                "잠시만 기다리십시오", # Cloudflare 한글 2 (실제 확인된 제목)
                "봇 확인",             # 기타
            ]

            # Chrome 이탈 확인 다이얼로그 감지 (Gmarket/Auction 캡챠 페이지에서 자주 발생)
            CHROME_DIALOG_TITLES = [
                "다른 페이지를 방문하시겠습니까",   # Gmarket CF beforeunload dialog
                "이 사이트를 벗어나시겠습니까",      # Chrome Leave site dialog
                "Leave site",                         # 영문
                "사이트를 떠나시겠습니까",
            ]
            if any(d in page_title for d in CHROME_DIALOG_TITLES):
                _focus_chrome_window_for_captcha(hwnd_target, product_id, "chrome_dialog")
                logger.warning(f"[{product_id}] Chrome 이탈 다이얼로그 감지 '{page_title}' → Esc 후 AHK 시도")
                try:
                    import pyautogui as _pag
                    _pag.press("escape")
                    time.sleep(0.5)
                except Exception:
                    pass
                _run_ahk_cf_captcha_click(hwnd=hwnd_target, max_wait_sec=10, check_interval_ms=350)
                time.sleep(2.0)
                page_title = win32gui.GetWindowText(hwnd_target)

            # Gmarket/Auction 전용: 제목이 단순 브랜드명만 있으면 캡챠 의심 → 선제 AHK
            _GMARKET_BRAND_ONLY = ("G마켓", "지마켓", "Gmarket")
            _AUCTION_BRAND_ONLY = ("옥션", "Auction")
            _is_brand_only_title = (
                (is_gmarket and any(page_title.strip().startswith(b) and len(page_title.strip()) < 20
                                    for b in _GMARKET_BRAND_ONLY))
                or
                (is_auction and any(page_title.strip().startswith(b) and len(page_title.strip()) < 15
                                    for b in _AUCTION_BRAND_ONLY))
            )
            if _is_brand_only_title:
                _focus_chrome_window_for_captcha(hwnd_target, product_id, "brand_only_title")
                logger.warning(f"[{product_id}] 브랜드명만 있는 창 제목 '{page_title}' → 캡챠 의심, AHK 선제 스캔")
                if _run_ahk_cf_captcha_click(hwnd=hwnd_target, max_wait_sec=5, check_interval_ms=400):
                    logger.info(f"[{product_id}] AHK 선제 클릭 성공 → 3초 대기")
                    time.sleep(3.0)
                    page_title = win32gui.GetWindowText(hwnd_target)

            if any(cw in page_title for cw in CAPTCHA_TITLES):
                _focus_chrome_window_for_captcha(hwnd_target, product_id, "captcha_title")

                logger.warning(f"[{product_id}] CAPTCHA 감지 → AHK로 체크박스 클릭 시도")

                cf_clicked = _run_ahk_cf_captcha_click(

                    hwnd=hwnd_target,

                    max_wait_sec=12,

                    check_interval_ms=350,

                )

                if cf_clicked:
                    logger.info(f"[{product_id}] CAPTCHA 1차 클릭 완료 → 10초간 상태 확인")
                    resolved = False
                    for _wait in range(10):
                        time.sleep(1.0)
                        page_title = win32gui.GetWindowText(hwnd_target)
                        if not any(cw in page_title for cw in CAPTCHA_TITLES):
                            resolved = True
                            break

                    if not resolved:
                        # 2차 시도: 위치가 미세하게 다를 수 있으므로 재클릭
                        logger.warning(f"[{product_id}] CAPTCHA 1차 미해제 → 2차 AHK 클릭 시도")
                        _run_ahk_cf_captcha_click(hwnd=hwnd_target, max_wait_sec=8, check_interval_ms=300)
                        for _wait2 in range(10):
                            time.sleep(1.0)
                            page_title = win32gui.GetWindowText(hwnd_target)
                            if not any(cw in page_title for cw in CAPTCHA_TITLES):
                                resolved = True
                                break

                    if not resolved:
                        logger.warning(f"[{product_id}] CAPTCHA 2차까지 미해제 (20초) → security_check로 분류")
                        _log_security_check_event(product_id, "gmarket" if is_gmarket else "auction", "chrome_screen", "captcha_blocked", product_url)
                        result["status"] = "captcha_blocked"
                        return result

                    logger.info(f"[{product_id}] CAPTCHA 해제 성공! 캡처 계속 진행")
                    _show_captcha_success_toast(product_id)

                else:
                    logger.warning(f"[{product_id}] AHK 클릭 실패 → security_check 분류")
                    _log_security_check_event(product_id, "gmarket" if is_gmarket else "auction", "chrome_screen", "ahk_click_failed", product_url)
                    result["status"] = "captcha_blocked"
                    return result


            win32gui.ShowWindow(hwnd_target, win32con.SW_MAXIMIZE)
            time.sleep(0.3)
            try:
                win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(0.05)
                ctypes.windll.user32.SetForegroundWindow(hwnd_target)
                time.sleep(0.5)
            except Exception as _fe:
                try:
                    ctypes.windll.user32.SwitchToThisWindow(hwnd_target, True)
                    time.sleep(0.3)
                except Exception:
                    pass

            time.sleep(1.5)
            if is_coupang:
                try:
                    import pyautogui as _pag
                    left, top, right, bottom = win32gui.GetWindowRect(hwnd_target)
                    if not _looks_like_coupang_product(page_title):
                        _pag.click(left + ((right - left) // 2), top + 80)
                        _pag.hotkey('ctrl', 'l')
                        try:
                            import pyperclip
                            pyperclip.copy(product_url)
                        except Exception:
                            import subprocess as _subprocess
                            _subprocess.run(
                                ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $args[0]", product_url],
                                check=False,
                                stdout=_subprocess.DEVNULL,
                                stderr=_subprocess.DEVNULL,
                            )
                        _pag.hotkey('ctrl', 'v')
                        _pag.press('enter')
                        time.sleep(5.0)
                        page_title = win32gui.GetWindowText(hwnd_target)
                    try:
                        ctypes.windll.user32.SetForegroundWindow(hwnd_target)
                    except Exception:
                        pass
                    if scroll_driver == "ahk":
                        _run_ahk_scroll(hwnd_target, steps=0, delay_ms=0, home_first=True)
                    else:
                        _pag.moveTo(left + ((right - left) // 2), top + ((bottom - top) // 2))
                        _pag.hotkey('ctrl', 'home')
                    time.sleep(0.7)
                except Exception as exc:
                    logger.debug(f"[{product_id}] initial screen scroll reset skipped: {exc}")
            else:
                try:
                    import pyautogui as _pag
                    left, top, right, bottom = win32gui.GetWindowRect(hwnd_target)
                    try:
                        ctypes.windll.user32.SetForegroundWindow(hwnd_target)
                    except Exception:
                        pass
                    # 11번가/네이버는 옵션 드롭다운이나 상단 고정 영역에 포커스가 남으면
                    # 본문이 아니라 같은 영역만 반복 캡처된다. 본문을 명시적으로 잡고 시작한다.
                    for _ in range(2):
                        _pag.press("esc")
                        time.sleep(0.12)
                    if is_naver:
                        _pag.press("esc")
                        time.sleep(0.1)
                    else:
                        main_x = left + int((right - left) * (0.36 if is_elevenst else 0.50))
                        main_y = top + 130 + min(420, max(180, (bottom - top - 130) // 3))
                        _pag.click(main_x, main_y)
                        time.sleep(0.2)
                    time.sleep(0.2)
                    _pag.hotkey("ctrl", "home")
                    time.sleep(0.8)
                except Exception as exc:
                    logger.debug(f"[{product_id}] initial page focus reset skipped: {exc}")

        except Exception as e:
            logger.error(f"[{product_id}] Chrome 초기화 실패: {e}")
            return result

        try:
            def _print_window_capture(hwnd) -> Image.Image:
                import win32gui as _wg, win32ui, win32con as _wc, ctypes as _ct
                rect = _wg.GetWindowRect(hwnd)
                w = rect[2] - rect[0]; h = rect[3] - rect[1]
                if w <= 0 or h <= 0:
                    raise ValueError(f"Invalid size: {w}x{h}")
                hwnd_dc = _wg.GetWindowDC(hwnd)
                mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
                save_dc = mfc_dc.CreateCompatibleDC()
                bmp = win32ui.CreateBitmap()
                bmp.CreateCompatibleBitmap(mfc_dc, w, h)
                save_dc.SelectObject(bmp)
                ok = _ct.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
                if not ok:
                    save_dc.BitBlt((0, 0), (w, h), mfc_dc, (0, 0), _wc.SRCCOPY)
                bmp_info = bmp.GetInfo()
                bmp_bits = bmp.GetBitmapBits(True)
                img = Image.frombuffer('RGB',
                    (bmp_info['bmWidth'], bmp_info['bmHeight']),
                    bmp_bits, 'raw', 'BGRX', 0, 1)
                save_dc.DeleteDC(); mfc_dc.DeleteDC()
                _wg.ReleaseDC(hwnd, hwnd_dc)
                _wg.DeleteObject(bmp.GetHandle())
                return img

            screen_detail_expanded = False
            naver_rebuilt_detail_page = False

            def _click_detail_more_button_by_color() -> bool:
                if is_auction:
                    logger.info("[Detail] skip generic color detail-more fallback for Auction")
                    return False
                try:
                    full_img = _print_window_capture(hwnd_target).convert("RGB")
                    arr = np.array(full_img)
                    full_img.close()
                    h, w = arr.shape[:2]
                    x0 = int(w * 0.12)
                    x1 = int(w * 0.72)
                    y0 = min(h - 1, CHROME_UI + 180)
                    y1 = max(y0 + 1, h - 60)
                    crop = arr[y0:y1, x0:x1]
                    if crop.size == 0:
                        return False

                    red = crop[:, :, 0].astype(np.int16)
                    green = crop[:, :, 1].astype(np.int16)
                    blue = crop[:, :, 2].astype(np.int16)
                    green_button = (green > 120) & (red < 110) & (blue < 130) & ((green - red) > 45)
                    red_button = (red > 160) & (green < 120) & (blue < 130) & ((red - green) > 45)
                    blue_button = (blue > 145) & (red < 135) & (green < 190) & ((blue - red) > 35)
                    mask = green_button | red_button | blue_button
                    if int(mask.sum()) < 700:
                        return False

                    row_counts = mask.sum(axis=1)
                    candidate_rows = np.where(row_counts > 70)[0]
                    if candidate_rows.size == 0:
                        return False
                    row_groups = np.split(candidate_rows, np.where(np.diff(candidate_rows) > 1)[0] + 1)
                    row_groups = [group for group in row_groups if 18 <= len(group) <= 90]
                    if not row_groups:
                        return False
                    candidates = []
                    for row_group in row_groups:
                        col_counts = mask[row_group, :].sum(axis=0)
                        candidate_cols = np.where(col_counts > max(6, len(row_group) * 0.18))[0]
                        if candidate_cols.size == 0:
                            continue
                        col_groups = np.split(candidate_cols, np.where(np.diff(candidate_cols) > 1)[0] + 1)
                        col_groups = [group for group in col_groups if 80 <= len(group) <= 420]
                        col_sets = []
                        union_width = int(candidate_cols[-1] - candidate_cols[0] + 1)
                        if 130 <= union_width <= 420:
                            col_sets.append(candidate_cols)
                        col_sets.extend(col_groups)
                        for col_group in col_sets:
                            ry0 = int(row_group[0])
                            ry1 = int(row_group[-1]) + 1
                            cx0 = int(col_group[0])
                            cx1 = int(col_group[-1]) + 1
                            cand_width = max(1, cx1 - cx0)
                            cand_height = max(1, ry1 - ry0)
                            button_pixels = int(mask[ry0:ry1, cx0:cx1].sum())
                            fill_density = button_pixels / float(cand_width * cand_height)
                            if is_gmarket and (cand_width < 180 or cand_height < 32 or fill_density < 0.38):
                                continue
                            click_x = x0 + int((cx0 + cx1) / 2)
                            click_y = y0 + int((ry0 + ry1) / 2)
                            if is_gmarket and not (w * 0.22 <= click_x <= w * 0.58):
                                continue
                            by0 = max(0, ry0 - 55)
                            by1 = min(crop.shape[0], ry1 + 55)
                            bx0 = max(0, cx0 - 90)
                            bx1 = min(crop.shape[1], cx1 + 90)
                            region = crop[by0:by1, bx0:bx1]
                            region_mask = mask[by0:by1, bx0:bx1]
                            if region.size == 0:
                                continue
                            background = region[~region_mask]
                            background_mean = float(background.mean()) if background.size else float(region.mean())
                            if is_gmarket and background_mean < 205:
                                continue
                            center_score = 1.0 - min(1.0, abs((click_x / max(1, w)) - 0.40) / 0.30)
                            score = button_pixels + (fill_density * 600.0) + (background_mean * 2.0) + (center_score * 400.0)
                            candidates.append((score, click_x, click_y, background_mean))
                    if not candidates:
                        return False

                    _score, click_x, click_y, background_mean = max(candidates, key=lambda item: item[0])
                    _win_click(click_x, click_y)
                    logger.info(
                        "[Detail] clicked screen detail-more button at %s,%s bg=%.1f",
                        click_x,
                        click_y,
                        background_mean,
                    )
                    time.sleep(1.6)
                    return True
                except Exception as exc:
                    logger.debug(f"[{product_id}] screen detail-more color scan skipped: {exc}")
                    return False

            def _click_naver_detail_more_button_by_outline() -> bool:
                try:
                    full_img = _print_window_capture(hwnd_target).convert("RGB")
                    arr = np.asarray(full_img, dtype=np.uint8)
                    full_img.close()
                    h, w = arr.shape[:2]
                    x0 = int(w * 0.10)
                    # 우측 사이드바(선물하기/구매하기 등) 제외, 좌측 컨텐츠 영역만 스캔
                    x1 = int(w * 0.52)
                    y0 = min(h - 1, CHROME_UI + 220)
                    y1 = max(y0 + 1, h - 80)
                    crop = arr[y0:y1, x0:x1]
                    if crop.size == 0:
                        return False

                    gray = crop.astype(np.int16).mean(axis=2)
                    dark = gray < 125
                    row_counts = dark.sum(axis=1)
                    candidate_rows = np.where((row_counts > 130) & (row_counts < 1100))[0]
                    if candidate_rows.size == 0:
                        return False

                    row_groups = np.split(candidate_rows, np.where(np.diff(candidate_rows) > 2)[0] + 1)
                    lines = []
                    for group in row_groups:
                        if not (1 <= len(group) <= 9):
                            continue
                        band = dark[int(group[0]):int(group[-1]) + 1, :]
                        col_counts = band.sum(axis=0)
                        candidate_cols = np.where(col_counts > 0)[0]
                        if candidate_cols.size == 0:
                            continue
                        # Fill tiny antialiasing gaps in the button border, then find the widest run.
                        filled = np.convolve((col_counts > 0).astype(np.int16), np.ones(9, dtype=np.int16), mode="same") > 0
                        cols = np.where(filled)[0]
                        if cols.size == 0:
                            continue
                        col_groups = np.split(cols, np.where(np.diff(cols) > 1)[0] + 1)
                        widest = max(col_groups, key=len)
                        width = int(widest[-1] - widest[0] + 1)
                        # "상세정보 펼쳐보기"는 좌측 콘텐츠 전체 폭(~630px+)이라 상한 950으로 확장
                        if not (210 <= width <= 950):
                            continue
                        lines.append({
                            "y": int((group[0] + group[-1]) / 2),
                            "x0": int(widest[0]),
                            "x1": int(widest[-1]),
                            "width": width,
                        })

                    candidates = []
                    for i, top_line in enumerate(lines):
                        for bottom_line in lines[i + 1:]:
                            distance = bottom_line["y"] - top_line["y"]
                            if not (30 <= distance <= 72):
                                continue
                            left_delta = abs(top_line["x0"] - bottom_line["x0"])
                            right_delta = abs(top_line["x1"] - bottom_line["x1"])
                            width_delta = abs(top_line["width"] - bottom_line["width"])
                            if left_delta > 55 or right_delta > 55 or width_delta > 80:
                                continue
                            bx0 = max(0, min(top_line["x0"], bottom_line["x0"]) - 8)
                            bx1 = min(crop.shape[1], max(top_line["x1"], bottom_line["x1"]) + 8)
                            by0 = max(0, top_line["y"] - 6)
                            by1 = min(crop.shape[0], bottom_line["y"] + 8)
                            region = crop[by0:by1, bx0:bx1]
                            if region.size == 0:
                                continue
                            brightness = float(region.mean())
                            dark_density = float(dark[by0:by1, bx0:bx1].mean())
                            if brightness < 170 or not (0.015 <= dark_density <= 0.22):
                                continue
                            click_x = x0 + int((bx0 + bx1) / 2)
                            click_y = y0 + int((by0 + by1) / 2)
                            # "상세정보 펼쳐보기" 버튼은 좌측 컨텐츠 영역 중앙(~31%)에 위치
                            center_score = 1.0 - min(1.0, abs((click_x / max(1, w)) - 0.31) / 0.25)
                            score = (top_line["width"] + bottom_line["width"]) + center_score * 280 - abs(distance - 44) * 4
                            candidates.append((score, click_x, click_y, brightness, dark_density, distance))

                    if not candidates:
                        return False
                    _score, click_x, click_y, brightness, dark_density, distance = max(candidates, key=lambda item: item[0])
                    _win_click(click_x, click_y)
                    logger.info(
                        "[Detail] clicked Naver detail expand button at %s,%s bg=%.1f density=%.3f h=%s",
                        click_x,
                        click_y,
                        brightness,
                        dark_density,
                        distance,
                    )
                    time.sleep(1.4)
                    return True
                except Exception as exc:
                    logger.debug(f"[{product_id}] Naver detail-more outline scan skipped: {exc}")
                    return False

            def _cdp_call(method: str, params: Dict | None = None, timeout: float = 8):
                nonlocal debug_port
                if not debug_port:
                    return None
                try:
                    import json
                    import urllib.request
                    import websocket
                    tabs = None
                    for _ in range(3):
                        try:
                            with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json", timeout=0.7) as response:
                                tabs = json.loads(response.read().decode("utf-8", errors="ignore"))
                            break
                        except Exception:
                            time.sleep(0.18)
                    if not tabs:
                        debug_port = None
                        return None
                    page_tabs = [
                        tab for tab in tabs
                        if tab.get("type") == "page"
                        and tab.get("webSocketDebuggerUrl")
                        and not str(tab.get("url") or "").startswith("chrome://")
                    ]
                    if not page_tabs:
                        debug_port = None
                        return None
                    target_base = product_url_lower.split("?", 1)[0]
                    matching_tabs = [
                        tab for tab in page_tabs
                        if target_base and target_base in str(tab.get("url") or "").lower()
                    ]
                    tab = (matching_tabs or page_tabs)[-1]
                    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=timeout)
                    try:
                        payload = {
                            "id": 1,
                            "method": method,
                            "params": params or {},
                        }
                        ws.send(json.dumps(payload))
                        while True:
                            message = json.loads(ws.recv())
                            if message.get("id") == 1:
                                return message
                    finally:
                        ws.close()
                except Exception as exc:
                    logger.debug(f"[{product_id}] CDP {method} skipped: {exc}")
                    return None

            def _cdp_eval(expression: str, timeout: float = 8):
                return _cdp_call(
                    "Runtime.evaluate",
                    {
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                    timeout=timeout,
                )

            def _decode_cdp_image(data: str) -> Image.Image:
                import base64
                import io
                return Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")

            def _hide_auction_detail_more_residue_via_cdp(stage: str = "capture") -> Dict:
                """Auction only: remove a leftover detail-more button after the detail body is already expanded."""
                if not (debug_port and is_auction and screen_detail_expanded):
                    return {}
                script = r"""
(() => {
  const compact = (text) => String(text || '').replace(/\s+/g, '').trim();
  const keywords = ['상세정보더보기', '상세정보 더보기', '상품상세더보기', '상품상세 더보기', '상품정보더보기', '상품정보 더보기'];
  const badWords = ['필수표기정보', '쿠폰', '구매', '장바구니', '주문', '결제'];
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 80 || rect.height < 24 || rect.width > 620 || rect.height > 140) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && Number(cs.opacity || 1) > 0.05;
  };
  if (!document.getElementById('codex-auction-detail-more-hide-style')) {
    const style = document.createElement('style');
    style.id = 'codex-auction-detail-more-hide-style';
    style.textContent = `
      .codex-auction-detail-more-hidden {
        display: none !important;
        visibility: hidden !important;
        pointer-events: none !important;
      }
    `;
    document.documentElement.appendChild(style);
  }
  const selectors = 'button,a,[role="button"],input[type="button"],input[type="submit"],div,span,p';
  const hidden = [];
  for (const el of Array.from(document.querySelectorAll(selectors)).slice(0, 7000)) {
    const text = compact(el.innerText || el.textContent || el.getAttribute('aria-label') || el.value || '');
    if (!text || text.length > 70) continue;
    if (badWords.some((word) => text.includes(word))) continue;
    if (!keywords.some((word) => text === compact(word) || text.includes(compact(word)))) continue;
    if (!visible(el)) continue;
    const rect = el.getBoundingClientRect();
    const absoluteTop = Math.round(rect.top + window.scrollY);
    if (absoluteTop < 260) continue;
    const cs = getComputedStyle(el);
    const redButton = (
      (cs.backgroundColor || '').includes('239') ||
      (cs.backgroundColor || '').includes('238') ||
      /btn|button|more|detail/i.test(String(el.className || '') + ' ' + String(el.id || ''))
    );
    if (!redButton && rect.width < 140) continue;
    el.classList.add('codex-auction-detail-more-hidden');
    hidden.push({text, top: absoluteTop, width: Math.round(rect.width), height: Math.round(rect.height)});
  }
  return {hidden: hidden.length, items: hidden.slice(0, 8)};
})()
"""
                try:
                    message = _cdp_eval(script, timeout=8)
                    value = (((message or {}).get("result") or {}).get("result") or {}).get("value") or {}
                    if not isinstance(value, dict):
                        return {}
                    hidden = int(value.get("hidden") or 0)
                    if hidden:
                        logger.info("[Detail] hid leftover Auction detail-more button(s) before capture: %s stage=%s", hidden, stage)
                    return {
                        f"auction_detail_more_hidden_{stage}": hidden,
                        f"auction_detail_more_hidden_items_{stage}": value.get("items") or [],
                    }
                except Exception as exc:
                    logger.debug("[Detail] auction detail-more residue hide skipped: %s", exc)
                    return {f"auction_detail_more_hide_error_{stage}": str(exc)}

            def _prime_lazy_images_via_cdp() -> Dict:
                lazy_hard_limit = 240000 if is_naver else 260000
                lazy_max_passes = 5 if is_naver else 14
                lazy_stable_rounds = 1 if is_naver else 3
                lazy_delay_ms = 80 if is_naver else 180
                lazy_timeout = 75 if is_naver else 170
                lazy_script = r"""
(() => new Promise(async (resolve) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const pageHeight = () => Math.max(
    document.body ? document.body.scrollHeight : 0,
    document.documentElement ? document.documentElement.scrollHeight : 0
  );
  const viewportHeight = () => Math.max(600, Math.round(window.innerHeight || 900));
  const step = Math.max(560, Math.floor(viewportHeight() * 0.72));
  let previousHeight = 0;
  let stableRounds = 0;
  let passes = 0;
  let totalScrolls = 0;
  const hardLimit = __HARD_LIMIT__;

  while (passes < __MAX_PASSES__ && stableRounds < __STABLE_ROUNDS__) {
    const startHeight = Math.min(pageHeight(), hardLimit);
    for (let y = 0; y <= startHeight + step; y += step) {
      window.scrollTo(0, y);
      totalScrolls += 1;
      await sleep(__DELAY_MS__);
    }
    await sleep(700);
    const currentHeight = Math.min(pageHeight(), hardLimit);
    if (Math.abs(currentHeight - previousHeight) < 80) {
      stableRounds += 1;
    } else {
      stableRounds = 0;
    }
    previousHeight = currentHeight;
    passes += 1;
  }
  window.scrollTo(0, pageHeight());
  await sleep(900);
  const finalHeight = Math.min(pageHeight(), hardLimit);
  window.scrollTo(0, 0);
  await sleep(450);
  resolve({
    scrollHeight: Math.round(finalHeight),
    viewportHeight: Math.round(window.innerHeight),
    viewportWidth: Math.round(window.innerWidth),
    lazyPasses: passes,
    lazyStableRounds: stableRounds,
    lazyScrolls: totalScrolls
  });
})())
"""
                lazy_script = (
                    lazy_script
                    .replace("__HARD_LIMIT__", str(lazy_hard_limit))
                    .replace("__MAX_PASSES__", str(lazy_max_passes))
                    .replace("__STABLE_ROUNDS__", str(lazy_stable_rounds))
                    .replace("__DELAY_MS__", str(lazy_delay_ms))
                )
                message = _cdp_eval(lazy_script, timeout=lazy_timeout)
                value = (((message or {}).get("result") or {}).get("result") or {}).get("value") or {}
                if isinstance(value, dict) and value:
                    return value
                try:
                    def _read_scroll_height() -> int:
                        height_message = _cdp_eval(
                            "Math.max(document.body?document.body.scrollHeight:0,document.documentElement?document.documentElement.scrollHeight:0)",
                            timeout=4,
                        )
                        raw_height = (((height_message or {}).get("result") or {}).get("result") or {}).get("value") or 0
                        return int(raw_height or 0)

                    viewport_message = _cdp_eval(
                        "({w:Math.round(window.innerWidth||1600),h:Math.round(window.innerHeight||900)})",
                        timeout=4,
                    )
                    viewport_value = (((viewport_message or {}).get("result") or {}).get("result") or {}).get("value") or {}
                    viewport_width = int((viewport_value or {}).get("w") or 1600)
                    viewport_height = int((viewport_value or {}).get("h") or 900)
                    step = max(560, int(max(viewport_height, 600) * 0.72))
                    previous_height = 0
                    stable_rounds = 0
                    passes = 0
                    total_scrolls = 0
                    hard_limit = lazy_hard_limit
                    while passes < (5 if is_naver else 14) and stable_rounds < lazy_stable_rounds:
                        current_limit = min(max(_read_scroll_height(), viewport_height), hard_limit)
                        for y in range(0, current_limit + step, step):
                            _cdp_eval(f"window.scrollTo(0,{int(y)}); true", timeout=3)
                            total_scrolls += 1
                            time.sleep(0.08)
                        time.sleep(0.45)
                        current_height = min(_read_scroll_height(), hard_limit)
                        if abs(current_height - previous_height) < 80:
                            stable_rounds += 1
                        else:
                            stable_rounds = 0
                        previous_height = current_height
                        passes += 1
                    final_height = min(_read_scroll_height(), hard_limit)
                    _cdp_eval(f"window.scrollTo(0,{int(final_height)}); true", timeout=3)
                    time.sleep(0.4)
                    _cdp_eval("window.scrollTo(0,0); true", timeout=3)
                    time.sleep(0.25)
                    return {
                        "scrollHeight": int(final_height),
                        "viewportHeight": int(viewport_height),
                        "viewportWidth": int(viewport_width),
                        "lazyPasses": int(passes),
                        "lazyStableRounds": int(stable_rounds),
                        "lazyScrolls": int(total_scrolls),
                        "lazyFallback": True,
                    }
                except Exception as exc:
                    result.setdefault("diagnostics", {})["lazy_prime_error"] = str(exc)
                    return {}

            def _save_cdp_pieces(
                base_path: str,
                pieces: List[Image.Image],
                quality: int,
                mode_prefix: str,
            ) -> tuple[List[str], Dict]:
                """Save CDP tiles without creating oversized JPEG files."""
                total_height = sum(piece.height for piece in pieces)
                width = pieces[0].width if pieces else 0
                max_part_height = 60000
                for old_path in glob.glob(os.path.join(output_dir, "cdp_part_*.jpg")):
                    try:
                        os.remove(old_path)
                    except Exception:
                        pass

                def _save_chunk(path: str, chunk: List[Image.Image]) -> None:
                    canvas = Image.new("RGB", (width, sum(piece.height for piece in chunk)), "white")
                    y_offset = 0
                    for piece in chunk:
                        canvas.paste(piece, (0, y_offset))
                        y_offset += piece.height
                    canvas.save(path, "JPEG", quality=quality)
                    canvas.close()

                try:
                    if total_height <= max_part_height:
                        _save_chunk(base_path, pieces)
                        return [base_path], {
                            "capture_mode": f"{mode_prefix}_tiled_exact" if len(pieces) > 1 else f"{mode_prefix}_single",
                            "cdp_tiles": len(pieces),
                            "cdp_chunked": False,
                            "cdp_chunk_count": 1,
                            "cdp_max_part_height": max_part_height,
                        }

                    if os.path.exists(base_path):
                        try:
                            os.remove(base_path)
                        except Exception:
                            pass

                    saved_paths: List[str] = []
                    current: List[Image.Image] = []
                    current_height = 0
                    part_index = 1
                    for piece in pieces:
                        if current and current_height + piece.height > max_part_height:
                            part_path = os.path.join(output_dir, f"cdp_part_{part_index:03d}.jpg")
                            _save_chunk(part_path, current)
                            saved_paths.append(part_path)
                            part_index += 1
                            current = []
                            current_height = 0
                        current.append(piece)
                        current_height += piece.height
                    if current:
                        part_path = os.path.join(output_dir, f"cdp_part_{part_index:03d}.jpg")
                        _save_chunk(part_path, current)
                        saved_paths.append(part_path)
                    return saved_paths, {
                        "capture_mode": f"{mode_prefix}_chunked",
                        "cdp_tiles": len(pieces),
                        "cdp_chunked": True,
                        "cdp_chunk_count": len(saved_paths),
                        "cdp_max_part_height": max_part_height,
                    }
                finally:
                    for piece in pieces:
                        try:
                            piece.close()
                        except Exception:
                            pass

            def _capture_auction_detail_scroller_via_cdp() -> bool:
                if not (debug_port and is_auction):
                    return False
                try:
                    _cdp_call("Page.enable", {}, timeout=5)
                    init_msg = _cdp_eval(r"""
(() => {
  const compact = (text) => String(text || '').replace(/\s+/g, '').trim();
  const moreWords = ['상세정보더보기', '상세정보 더보기', '상품상세더보기', '상품상세 더보기', '상품정보더보기', '상품정보 더보기']
    .map(compact);
  for (const el of Array.from(document.querySelectorAll('button,a,[role="button"],input[type="button"],div,span')).slice(0, 8000)) {
    const text = compact(el.innerText || el.textContent || el.getAttribute('aria-label') || el.value || '');
    if (!text || text.length > 70) continue;
    if (moreWords.some((word) => text === word || text.includes(word))) {
      const rect = el.getBoundingClientRect();
      if (rect.width >= 80 && rect.height >= 24) {
        el.style.setProperty('display', 'none', 'important');
        el.style.setProperty('visibility', 'hidden', 'important');
        el.style.setProperty('pointer-events', 'none', 'important');
      }
    }
  }
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return rect.width > 240 && rect.height > 120 && cs.display !== 'none' && cs.visibility !== 'hidden';
  };
  const scoreTarget = (el, explicit) => {
    const rect = el.getBoundingClientRect();
    const scrollHeight = Math.round(el.scrollHeight || 0);
    const clientHeight = Math.round(el.clientHeight || rect.height || 0);
    const overflow = Math.max(0, scrollHeight - clientHeight);
    return {
      el,
      rect,
      explicit,
      scrollHeight,
      clientHeight,
      width: Math.round(rect.width || 0),
      height: Math.round(rect.height || 0),
      score: overflow + scrollHeight + (explicit ? 25000 : 0) + Math.max(0, rect.height || 0)
    };
  };
  const explicitCandidates = Array.from(document.querySelectorAll(
    '#item_detail_view_js, .box__detail-view, ' +
    '[id*="detail_view"], [class*="detail-view"], ' +
    '[id*="itemDetail"], [class*="itemDetail"], ' +
    '[id*="item_detail"], [class*="item_detail"], ' +
    '[id*="item-detail"], [class*="item-detail"], ' +
    '[id*="productDetail"], [class*="productDetail"], ' +
    '[id*="product_detail"], [class*="product_detail"]'
  ))
    .filter(visible)
    .map((el) => scoreTarget(el, true));
  const seen = new Set(explicitCandidates.map((item) => item.el));
  const scrollCandidates = Array.from(document.querySelectorAll('main,section,article,div')).slice(0, 12000)
    .filter((el) => !seen.has(el) && visible(el) && (el.scrollHeight - el.clientHeight) > 240)
    .map((el) => scoreTarget(el, false));
  const candidates = explicitCandidates.concat(scrollCandidates)
    .filter((item) => item.scrollHeight > 800 && item.width > 240)
    .sort((a, b) => b.score - a.score);
  const target = candidates[0] && candidates[0].el;
  if (!target) return {ok: false, reason: 'detail_target_not_found', candidates: candidates.length};
  target.setAttribute('data-jepum-auction-capture-target', '1');
  target.scrollTop = 0;
  target.scrollIntoView({block: 'start', inline: 'nearest'});
  const rect = target.getBoundingClientRect();
  const pageX = Math.max(0, Math.round((window.scrollX || 0) + rect.left));
  const pageY = Math.max(0, Math.round((window.scrollY || 0) + rect.top));
  const width = Math.max(320, Math.min(2200, Math.round(rect.width || target.clientWidth || window.innerWidth || 1200)));
  const clientHeight = Math.max(320, Math.round(target.clientHeight || rect.height || window.innerHeight || 900));
  const scrollHeight = Math.max(clientHeight, Math.round(target.scrollHeight || clientHeight));
  const viewportH = Math.max(320, Math.round(window.innerHeight || 900));
  const innerScrollable = (Math.round(target.scrollHeight || 0) - Math.round(target.clientHeight || 0)) > 240;
  return {ok: true, pageX, pageY, width, clientHeight, scrollHeight, viewportH, innerScrollable, candidates: candidates.length};
})()
""", timeout=10)
                    init = (((init_msg or {}).get("result") or {}).get("result") or {}).get("value") or {}
                    if not isinstance(init, dict) or not init.get("ok"):
                        logger.info("[Detail] Auction CDP scroller capture skipped: %s", init)
                        return False

                    page_x = int(init.get("pageX") or 0)
                    page_y = int(init.get("pageY") or 0)
                    width = int(init.get("width") or 0)
                    viewport_h = int(init.get("clientHeight") or 0)
                    scroll_h = int(init.get("scrollHeight") or 0)
                    window_viewport_h = int(init.get("viewportH") or viewport_h or 900)
                    inner_scrollable = bool(init.get("innerScrollable"))
                    if width < 320 or scroll_h < 500:
                        return False
                    if inner_scrollable and viewport_h < 320:
                        return False

                    pieces: List[Image.Image] = []
                    fullpage_path = os.path.join(output_dir, f"{product_id}_fullpage.jpg")
                    if inner_scrollable:
                        _raw_step = max(320, viewport_h)
                        max_top = max(0, scroll_h - viewport_h)
                        if max_top == 0 or _raw_step >= scroll_h:
                            # clientHeight ≈ scrollHeight → 실제로 스크롤 안 됨
                            # 한 번에 전체를 캡처하면 26000px+ 대형 스크린샷 → 잘림 위험
                            # window-scroll 모드로 전환해서 타일링
                            inner_scrollable = False
                            step = max(320, window_viewport_h)
                            max_top = 0
                            logger.info(
                                "[Detail] Auction CDP scroller: max_top=0 → window-scroll mode el_h=%s win_viewport=%s step=%s pageY=%s",
                                scroll_h, window_viewport_h, step, page_y,
                            )
                        else:
                            step = _raw_step
                            logger.info(
                                "[Detail] Auction CDP scroller: inner-scroll mode el_h=%s viewport_h=%s step=%s",
                                scroll_h, viewport_h, step,
                            )
                    else:
                        # Non-scrollable element: tile using window viewport height
                        step = max(320, window_viewport_h)
                        max_top = 0  # unused for non-scrollable
                        logger.info(
                            "[Detail] Auction CDP scroller: window-scroll mode el_h=%s win_viewport=%s step=%s pageY=%s",
                            scroll_h, window_viewport_h, step, page_y,
                        )
                    last_end = 0
                    y = 0
                    tile_index = 0
                    while last_end < scroll_h and tile_index < 120:
                        if inner_scrollable:
                            actual_y = min(y, max_top)
                            if tile_index and pieces and actual_y <= 0 and last_end > 0:
                                break
                            set_msg = _cdp_eval(
                                f"""
(() => {{
  const visible = (el) => {{
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return rect.width > 240 && rect.height > 120 && cs.display !== 'none' && cs.visibility !== 'hidden';
  }};
  const explicit = Array.from(document.querySelectorAll(
    '[data-jepum-auction-capture-target="1"], #item_detail_view_js, .box__detail-view, ' +
    '[id*="detail_view"], [class*="detail-view"], ' +
    '[id*="itemDetail"], [class*="itemDetail"], [id*="item_detail"], [class*="item_detail"], ' +
    '[id*="item-detail"], [class*="item-detail"], ' +
    '[id*="productDetail"], [class*="productDetail"], [id*="product_detail"], [class*="product_detail"]'
  ))
    .filter((el) => visible(el) && (el.scrollHeight || 0) > 800);
  const seen = new Set(explicit);
  const fallback = Array.from(document.querySelectorAll('main,section,article,div')).slice(0, 12000)
    .filter((el) => !seen.has(el) && visible(el) && ((el.scrollHeight || 0) - (el.clientHeight || 0)) > 240);
  const candidates = explicit.concat(fallback)
    .map((el) => {{
      const rect = el.getBoundingClientRect();
      const scrollHeight = Math.round(el.scrollHeight || 0);
      const clientHeight = Math.round(el.clientHeight || rect.height || 0);
      const explicitScore = el.hasAttribute('data-jepum-auction-capture-target') ? 50000 : 0;
      return {{el, score: explicitScore + scrollHeight + Math.max(0, scrollHeight - clientHeight) + Math.max(0, rect.height || 0)}};
    }})
    .sort((a, b) => b.score - a.score);
  const target = candidates[0] && candidates[0].el;
  if (!target) return {{ok: false}};
  target.scrollTop = {int(actual_y)};
  target.scrollIntoView({{block: 'start', inline: 'nearest'}});
  const rect = target.getBoundingClientRect();
  return {{
    ok: true,
    scrollTop: Math.round(target.scrollTop || 0),
    pageX: Math.max(0, Math.round((window.scrollX || 0) + rect.left)),
    pageY: Math.max(0, Math.round((window.scrollY || 0) + rect.top)),
    width: Math.max(320, Math.min(2200, Math.round(rect.width || target.clientWidth || window.innerWidth || 1200))),
    clientHeight: Math.max(320, Math.round(target.clientHeight || rect.height || window.innerHeight || 900))
  }};
}})()
""",
                                timeout=8,
                            )
                            set_value = (((set_msg or {}).get("result") or {}).get("result") or {}).get("value") or {}
                            if isinstance(set_value, dict) and set_value.get("ok"):
                                page_x = int(set_value.get("pageX") or page_x)
                                page_y = int(set_value.get("pageY") or page_y)
                                width = int(set_value.get("width") or width)
                                viewport_h = int(set_value.get("clientHeight") or viewport_h)
                            time.sleep(0.28)
                            capture_h = max(320, min(viewport_h, scroll_h - actual_y))
                            clip_y = page_y
                        else:
                            # Non-scrollable element: use absolute page-Y coordinates per tile
                            actual_y = y
                            if actual_y >= scroll_h:
                                break
                            # Scroll window to bring this slice into viewport (triggers lazy loading)
                            prime_y = max(0, page_y + actual_y - 80)
                            _cdp_eval(f"window.scrollTo(0, {prime_y}); true;", timeout=4)
                            time.sleep(0.35)
                            capture_h = max(320, min(step, scroll_h - actual_y))
                            clip_y = page_y + actual_y
                        shot = _cdp_call(
                            "Page.captureScreenshot",
                            {
                                "format": "jpeg",
                                "quality": 88,
                                "fromSurface": True,
                                "captureBeyondViewport": True,
                                "clip": {
                                    "x": page_x,
                                    "y": clip_y,
                                    "width": width,
                                    "height": capture_h,
                                    "scale": 1,
                                },
                            },
                            timeout=20,
                        )
                        data = (((shot or {}).get("result") or {}).get("data") or "")
                        if not data:
                            break
                        image = _decode_cdp_image(data)
                        if inner_scrollable:
                            overlap_top = max(0, last_end - actual_y)
                            usable_h = min(image.height - overlap_top, scroll_h - last_end)
                        else:
                            overlap_top = 0
                            usable_h = min(image.height, scroll_h - last_end)
                        if usable_h <= 24:
                            image.close()
                            break
                        if overlap_top:
                            piece = image.crop((0, overlap_top, image.width, overlap_top + usable_h))
                            image.close()
                        elif usable_h < image.height:
                            piece = image.crop((0, 0, image.width, usable_h))
                            image.close()
                        else:
                            piece = image
                        pieces.append(piece)
                        last_end += usable_h
                        tile_index += 1
                        y += step

                    if len(pieces) < 1:
                        for piece in pieces:
                            piece.close()
                        return False
                    saved_paths, capture_stats = _save_cdp_pieces(fullpage_path, pieces, 88, "auction_detail_scroller")
                    if not saved_paths:
                        return False
                    fullpage_path = saved_paths[0]
                    postprocess_info = {}
                    if os.path.exists(fullpage_path):
                        postprocess_info.update(_trim_auction_repeated_header(fullpage_path))
                    quality = _set_result_quality(
                        result,
                        fullpage_path,
                        {
                            "capture_version": DETAIL_CAPTURE_VERSION,
                            "capture_mode": "auction_detail_scroller_cdp",
                            "auction_detail_scroller": True,
                            "auction_detail_scroll_height": scroll_h,
                            "auction_detail_tiles": tile_index,
                            **capture_stats,
                            **postprocess_info,
                        },
                    )
                    if not quality.get("ok"):
                        result["screenshots"] = []
                        result["status"] = quality.get("reason", "quality_failed")
                        return False
                    result["screenshots"] = saved_paths
                    result["method"] = method_name
                    result["status"] = "success"
                    logger.info("[Detail] Auction CDP scroller capture complete: tiles=%s height=%s", tile_index, scroll_h)
                    return True
                except Exception as exc:
                    logger.debug("[Detail] Auction CDP scroller capture failed: %s", exc)
                    return False

            def _capture_fullpage_via_cdp() -> bool:
                if not debug_port:
                    return False
                try:
                    _cdp_call("Page.enable", {}, timeout=5)
                    # 옥션도 fullpage CDP 캡처 허용 (상단~하단 전체 페이지)
                    # screen_detail_expanded 체크 제거 — "더보기"는 이미 _preexpand_screen_detail() 에서 처리됨
                    auction_hide_info = {}
                    if is_auction:
                        auction_hide_info.update(_hide_auction_detail_more_residue_via_cdp("before_lazy"))
                    lazy_stats = _prime_lazy_images_via_cdp()
                    if is_auction:
                        auction_hide_info.update(_hide_auction_detail_more_residue_via_cdp("after_lazy"))
                    metrics = _cdp_call("Page.getLayoutMetrics", {}, timeout=5) or {}
                    result_obj = metrics.get("result") or {}
                    content_size = result_obj.get("cssContentSize") or result_obj.get("contentSize") or {}
                    layout_viewport = result_obj.get("cssLayoutViewport") or result_obj.get("layoutViewport") or {}
                    viewport_width = int(layout_viewport.get("clientWidth") or lazy_stats.get("viewportWidth") or 1600)
                    content_width = int(content_size.get("width") or viewport_width)
                    content_height = max(
                        int(content_size.get("height") or 0),
                        int(lazy_stats.get("scrollHeight") or 0),
                        int(layout_viewport.get("pageY", 0) or 0) + int(layout_viewport.get("clientHeight") or 0),
                    )
                    width = max(320, min(content_width, viewport_width, 2200))
                    height_limit = 240000 if is_naver else 260000
                    capture_y = 0
                    capture_region_info = {"cdp_detail_start_strategy": "full_document"}
                    height = max(320, min(content_height, height_limit))
                    if height < 700:
                        return False

                    fullpage_path = os.path.join(output_dir, f"{product_id}_fullpage.jpg")
                    # Very tall single CDP screenshots can repeat the top product area
                    # on some commerce pages. Keep the stable Naver/11st paths as-is,
                    # but tile the three flaky CDP commerce pages more aggressively.
                    use_commerce_tiles = bool(is_coupang or is_gmarket or is_auction)
                    max_single_height = 6500 if use_commerce_tiles else 28000
                    if height <= max_single_height:
                        screenshot = _cdp_call(
                            "Page.captureScreenshot",
                            {
                                "format": "jpeg",
                                "quality": 86,
                                "fromSurface": True,
                                "captureBeyondViewport": True,
                                "clip": {
                                    "x": 0,
                                    "y": capture_y,
                                    "width": width,
                                    "height": height,
                                    "scale": 1,
                                },
                            },
                            timeout=25,
                        )
                        data = ((screenshot or {}).get("result") or {}).get("data")
                        if not data:
                            return False
                        image = _decode_cdp_image(data)
                        image.save(fullpage_path, "JPEG", quality=86)
                        image.close()
                        saved_paths = [fullpage_path]
                        capture_stats = {
                            "capture_mode": "cdp_fullpage",
                            "cdp_tiles": 1,
                            "cdp_chunked": False,
                            "cdp_chunk_count": 1,
                        }
                    else:
                        tile_height = 6000 if use_commerce_tiles else 9000
                        pieces = []
                        y = 0
                        while y < height:
                            h = min(tile_height, height - y)
                            screenshot = _cdp_call(
                                "Page.captureScreenshot",
                                {
                                    "format": "jpeg",
                                    "quality": 86,
                                    "fromSurface": True,
                                    "captureBeyondViewport": True,
                                    "clip": {
                                        "x": 0,
                                        "y": capture_y + y,
                                        "width": width,
                                        "height": h,
                                        "scale": 1,
                                    },
                                },
                                timeout=20,
                            )
                            data = ((screenshot or {}).get("result") or {}).get("data")
                            if not data:
                                for piece in pieces:
                                    piece.close()
                                return False
                            pieces.append(_decode_cdp_image(data))
                            y += h
                        saved_paths, capture_stats = _save_cdp_pieces(fullpage_path, pieces, 86, "cdp")

                    postprocess_info = {}
                    if len(saved_paths) == 1:
                        fullpage_path = saved_paths[0]
                        if is_coupang and getattr(config, "ENABLE_COUPANG_REPEAT_TRIM", True):
                            postprocess_info.update(_trim_coupang_repeated_tail(fullpage_path))
                        if is_elevenst and getattr(config, "ENABLE_ELEVENST_REPEAT_TRIM", True):
                            postprocess_info.update(_trim_elevenst_repeated_tail(fullpage_path))
                        if is_auction and getattr(config, "ENABLE_AUCTION_REPEAT_TRIM", True):
                            postprocess_info.update(_trim_auction_repeated_header(fullpage_path))
                        if is_naver:
                            postprocess_info.update(_postprocess_naver_capture(fullpage_path))
                    else:
                        postprocess_info["postprocess_skipped_reason"] = "chunked_cdp_capture"

                    quality = _set_result_quality(
                        result,
                        saved_paths[0],
                        {
                            "scroll_driver": scroll_driver,
                            "target_title": page_title,
                            "detail_expanded": bool(screen_detail_expanded),
                            "cdp_width": width,
                            "cdp_height": height,
                            "cdp_capture_y": int(capture_y),
                            "lazy_scroll_height": int(lazy_stats.get("scrollHeight") or 0),
                            "lazy_passes": int(lazy_stats.get("lazyPasses") or 0),
                            "lazy_stable_rounds": int(lazy_stats.get("lazyStableRounds") or 0),
                            "lazy_scrolls": int(lazy_stats.get("lazyScrolls") or 0),
                            **capture_region_info,
                            **capture_stats,
                            **postprocess_info,
                            **auction_hide_info,
                        },
                    )
                    if not quality.get("ok"):
                        result["screenshots"] = []
                        result["diagnostics"]["cdp_fullpage_rejected"] = quality
                        return False
                    result["screenshots"] = saved_paths
                    result["status"] = "success"
                    logger.info(
                        "[Detail] CDP fullpage capture success for %s: %sx%s mode=%s chunks=%s",
                        product_id,
                        width,
                        height,
                        capture_stats.get("capture_mode"),
                        len(saved_paths),
                    )
                    return True
                except Exception as exc:
                    result.setdefault("diagnostics", {})["cdp_fullpage_error"] = str(exc)
                    logger.debug(f"[{product_id}] CDP fullpage capture skipped: {exc}")
                    return False

            def _capture_cdp_clip_to_file(
                fullpage_path: str,
                x: int,
                y: int,
                width: int,
                height: int,
                mode_prefix: str,
            ) -> Dict | None:
                try:
                    x = max(0, int(x))
                    y = max(0, int(y))
                    width = max(320, min(int(width), 2200))
                    height_limit = 240000 if is_naver else 260000
                    height = max(320, min(int(height), height_limit))
                    max_single_height = 28000
                    if height <= max_single_height:
                        screenshot = _cdp_call(
                            "Page.captureScreenshot",
                            {
                                "format": "jpeg",
                                "quality": 88,
                                "fromSurface": True,
                                "captureBeyondViewport": True,
                                "clip": {"x": x, "y": y, "width": width, "height": height, "scale": 1},
                            },
                            timeout=25,
                        )
                        data = ((screenshot or {}).get("result") or {}).get("data")
                        if not data:
                            return None
                        image = _decode_cdp_image(data)
                        image.save(fullpage_path, "JPEG", quality=88)
                        image.close()
                        return {
                            "capture_mode": f"{mode_prefix}_single",
                            "cdp_tiles": 1,
                            "cdp_chunked": False,
                            "cdp_chunk_count": 1,
                            "screenshots": [fullpage_path],
                            "cdp_x": x,
                            "cdp_y": y,
                            "cdp_width": width,
                            "cdp_height": height,
                        }

                    tile_height = 9000
                    pieces = []
                    tile_y = 0
                    while tile_y < height:
                        tile_h = min(tile_height, height - tile_y)
                        screenshot = _cdp_call(
                            "Page.captureScreenshot",
                            {
                                "format": "jpeg",
                                "quality": 88,
                                "fromSurface": True,
                                "captureBeyondViewport": True,
                                "clip": {
                                    "x": x,
                                    "y": y + tile_y,
                                    "width": width,
                                    "height": tile_h,
                                    "scale": 1,
                                },
                            },
                            timeout=22,
                        )
                        data = ((screenshot or {}).get("result") or {}).get("data")
                        if not data:
                            for piece in pieces:
                                piece.close()
                            return None
                        pieces.append(_decode_cdp_image(data))
                        tile_y += tile_h
                    saved_paths, stats = _save_cdp_pieces(fullpage_path, pieces, 88, mode_prefix)
                    return {
                        **stats,
                        "screenshots": saved_paths,
                        "cdp_x": x,
                        "cdp_y": y,
                        "cdp_width": width,
                        "cdp_height": height,
                    }
                except Exception as exc:
                    result.setdefault("diagnostics", {})[f"{mode_prefix}_clip_error"] = str(exc)
                    logger.debug(f"[{product_id}] CDP clip capture skipped: {exc}")
                    return None

            def _capture_naver_detail_region_via_cdp() -> bool:
                if not (debug_port and is_naver):
                    return False
                try:
                    _cdp_call("Page.enable", {}, timeout=5)
                    _expand_detail_more_via_cdp()
                    lazy_stats = _prime_lazy_images_via_cdp()
                    _prepare_screen_capture_layout()
                    message = _cdp_eval(r"""
(() => {
  const doc = document.documentElement;
  const body = document.body;
  const docH = Math.max(
    body ? body.scrollHeight : 0,
    doc ? doc.scrollHeight : 0,
    body ? body.offsetHeight : 0,
    doc ? doc.offsetHeight : 0
  );
  const docW = Math.max(
    body ? body.scrollWidth : 0,
    doc ? doc.scrollWidth : 0,
    window.innerWidth || 0
  );
  const visible = (el, rect) => {
    if (!el || !rect || rect.width <= 1 || rect.height <= 1) return false;
    const style = window.getComputedStyle(el);
    return style && style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0.03;
  };
  const percentile = (values, ratio) => {
    if (!values.length) return 0;
    const sorted = values.slice().sort((a, b) => a - b);
    const idx = Math.max(0, Math.min(sorted.length - 1, Math.round((sorted.length - 1) * ratio)));
    return sorted[idx];
  };
  const images = Array.from(document.images).map((img) => {
    const rect = img.getBoundingClientRect();
    if (!visible(img, rect)) return null;
    const w = Math.round(rect.width);
    const h = Math.round(rect.height);
    const top = Math.round(rect.top + window.scrollY);
    const left = Math.round(rect.left + window.scrollX);
    const naturalW = img.naturalWidth || 0;
    const naturalH = img.naturalHeight || 0;
    const src = img.currentSrc || img.src || '';
    if (!src || w < 160 || h < 90 || w * h < 25000) return null;
    if (top < 110) return null;
    if (left > window.innerWidth * 0.86) return null;
    if (w <= 260 && h <= 260 && top < 1400) return null;
    if (naturalW <= 80 || naturalH <= 80) return null;
    return {left, top, right: left + w, bottom: top + h, width: w, height: h, src};
  }).filter(Boolean);

  const large = images.filter((img) => img.width >= 260 && img.height >= 140);
  if (!large.length) {
    return {ok: false, reason: 'no_large_images', imageCount: images.length, scrollHeight: docH};
  }

  const lefts = large.map((img) => img.left);
  const rights = large.map((img) => img.right);
  const tops = large.map((img) => img.top);
  const bottoms = large.map((img) => img.bottom);
  let left = Math.max(0, Math.floor(percentile(lefts, 0.08) - 36));
  let right = Math.min(docW, Math.ceil(percentile(rights, 0.94) + 36));
  let center = (left + right) / 2;
  if (right - left > 1520) {
    left = Math.max(0, Math.floor(center - 740));
    right = Math.min(docW, left + 1480);
  }
  if (right - left < 780) {
    left = Math.max(0, Math.floor(center - 440));
    right = Math.min(docW, left + 880);
  }
  const top = Math.max(0, Math.floor(Math.min(...tops) - 120));
  const bottom = Math.min(docH, Math.ceil(Math.max(...bottoms) + 360));
  const height = Math.max(0, bottom - top);
  const width = Math.max(0, right - left);
  return {
    ok: width >= 320 && height >= 700,
    x: left,
    y: top,
    width,
    height,
    imageCount: images.length,
    largeImageCount: large.length,
    scrollHeight: docH,
    scrollWidth: docW
  };
})()
""")
                    clip = (((message or {}).get("result") or {}).get("result") or {}).get("value") or {}
                    if not isinstance(clip, dict) or not clip.get("ok"):
                        result.setdefault("diagnostics", {})["naver_cdp_region_rejected"] = clip
                        return False

                    fullpage_path = os.path.join(output_dir, f"{product_id}_fullpage.jpg")
                    capture_stats = _capture_cdp_clip_to_file(
                        fullpage_path,
                        int(clip.get("x") or 0),
                        int(clip.get("y") or 0),
                        int(clip.get("width") or 0),
                        int(clip.get("height") or 0),
                        "naver_detail_region_cdp",
                    )
                    if not capture_stats:
                        return False
                    capture_paths = capture_stats.pop("screenshots", [fullpage_path])
                    postprocess_info = {}
                    if len(capture_paths) == 1:
                        postprocess_info = _postprocess_naver_capture(capture_paths[0])
                    else:
                        postprocess_info["postprocess_skipped_reason"] = "chunked_cdp_capture"
                    quality = _set_result_quality(
                        result,
                        capture_paths[0],
                        {
                            "scroll_driver": scroll_driver,
                            "target_title": page_title,
                            "detail_expanded": bool(screen_detail_expanded),
                            "naver_capture_strategy": "detail_region_cdp",
                            "lazy_scroll_height": int(lazy_stats.get("scrollHeight") or 0),
                            "lazy_passes": int(lazy_stats.get("lazyPasses") or 0),
                            "lazy_stable_rounds": int(lazy_stats.get("lazyStableRounds") or 0),
                            "lazy_scrolls": int(lazy_stats.get("lazyScrolls") or 0),
                            "naver_region_image_count": int(clip.get("imageCount") or 0),
                            "naver_region_large_image_count": int(clip.get("largeImageCount") or 0),
                            "naver_region_scroll_height": int(clip.get("scrollHeight") or 0),
                            **capture_stats,
                            **postprocess_info,
                        },
                    )
                    if not quality.get("ok"):
                        result["screenshots"] = []
                        result["diagnostics"]["naver_cdp_region_quality_rejected"] = quality
                        return False
                    result["screenshots"] = capture_paths
                    result["status"] = "success"
                    logger.info(
                        "[Detail] Naver detail-region CDP capture success for %s: %sx%s images=%s mode=%s",
                        product_id,
                        capture_stats.get("cdp_width"),
                        capture_stats.get("cdp_height"),
                        clip.get("largeImageCount"),
                        capture_stats.get("capture_mode"),
                    )
                    return True
                except Exception as exc:
                    result.setdefault("diagnostics", {})["naver_cdp_region_error"] = str(exc)
                    logger.debug(f"[{product_id}] Naver detail-region CDP capture skipped: {exc}")
                    return False


            def _expand_naver_detail_button_via_cdp() -> bool:
                """Naver "상세정보 펼쳐보기" 버튼 전용 CDP 클릭 — 우측 사이드바 제외"""
                if not debug_port:
                    return False
                try:
                    for scroll_y in (0, 500, 1000, 1800, 2700, 4000):
                        _cdp_eval(f"window.scrollTo(0, {scroll_y}); true;")
                        import time as _t; _t.sleep(0.3)
                        msg = _cdp_eval(NAVER_BTN_JS)
                        val = (((msg or {}).get("result") or {}).get("result") or {}).get("value") or {}
                        if isinstance(val, dict) and val.get("ok"):
                            logger.info("[Detail] Naver 펼쳐보기 btn clicked via CDP: %s", val)
                            _cdp_eval("window.scrollTo(0, 0); true;")
                            import time as _t2; _t2.sleep(0.5)
                            return True
                    return False
                except Exception as exc:
                    logger.debug(f"[{product_id}] _expand_naver_detail_button_via_cdp skipped: {exc}")
                    return False

            def _expand_detail_more_via_cdp() -> bool:
                if not debug_port:
                    return False
                try:
                    expanded = False
                    for y in (0, 900, 1800, 2700, 3600, 4800):
                        _cdp_eval(f"window.scrollTo(0, {int(y)}); true;")
                        time.sleep(0.25)
                        message = _cdp_eval(DETAIL_EXPAND_JS)
                        value = (((message or {}).get("result") or {}).get("result") or {}).get("value") or {}
                        if isinstance(value, list):
                            clicked = value
                            total = len(value)
                        elif isinstance(value, dict):
                            clicked = value.get("clicked") or []
                            total = int(value.get("total") or 0)
                        else:
                            clicked = []
                            total = 0
                        if total > 0 or clicked:
                            expanded = True
                            break
                    _cdp_eval("window.scrollTo(0, 0); document.scrollingElement && (document.scrollingElement.scrollTop = 0); true;")
                    time.sleep(0.5)
                    if expanded:
                        logger.info("[Detail] expanded screen detail via CDP")
                    return expanded
                except Exception as exc:
                    logger.debug(f"[{product_id}] CDP detail expand skipped: {exc}")
                    return False

            def _scroll_page_via_cdp() -> bool:
                if not debug_port:
                    return False
                try:
                    if is_auction:
                        message = _cdp_eval(
                            f"""
(() => {{
  const step = Math.max(620, Math.floor((window.innerHeight || {max(1, win_h - CHROME_UI)}) * 0.78));
  const visible = (el) => {{
    if (!el) return false;
    if (el === document.documentElement || el === document.body || el === document.scrollingElement) return true;
    const rect = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return rect.width > 120 && rect.height > 120 && cs.display !== 'none' && cs.visibility !== 'hidden';
  }};
  const uniq = [];
  const seen = new Set();
  const add = (el) => {{
    if (!el || seen.has(el)) return;
    seen.add(el);
    uniq.push(el);
  }};
  add(document.scrollingElement);
  add(document.documentElement);
  add(document.body);
  for (const el of Array.from(document.querySelectorAll('main,section,article,div,iframe')).slice(0, 9000)) add(el);
  const candidates = uniq
    .filter((el) => visible(el) && (el.scrollHeight - el.clientHeight) > 80)
    .map((el) => {{
      const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : {{top: 0, left: 0, width: window.innerWidth, height: window.innerHeight}};
      const isDoc = el === document.scrollingElement || el === document.documentElement || el === document.body;
      const scrollTop = isDoc ? Math.round(window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0) : Math.round(el.scrollTop || 0);
      const clientHeight = isDoc ? Math.round(window.innerHeight || el.clientHeight || 0) : Math.round(el.clientHeight || 0);
      const scrollHeight = Math.round(el.scrollHeight || 0);
      return {{
        el,
        isDoc,
        scrollTop,
        clientHeight,
        scrollHeight,
        maxTop: Math.max(0, scrollHeight - clientHeight),
        score: Math.max(0, scrollHeight - clientHeight) + (isDoc ? 1000 : 0) + Math.max(0, rect.height)
      }};
    }})
    .filter((item) => item.maxTop > item.scrollTop + 8)
    .sort((a, b) => b.score - a.score);
  const target = candidates[0];
  if (!target) {{
    const scrollHeight = Math.round(Math.max(document.body ? document.body.scrollHeight : 0, document.documentElement ? document.documentElement.scrollHeight : 0));
    const innerHeight = Math.round(window.innerHeight || 0);
    return {{moved: false, atBottom: true, before: Math.round(window.scrollY || 0), after: Math.round(window.scrollY || 0), scrollHeight, innerHeight, target: 'none'}};
  }}
  const before = target.scrollTop;
  const next = Math.min(target.maxTop, before + step);
  if (target.isDoc) {{
    window.scrollTo(0, next);
    document.documentElement.scrollTop = next;
    document.body.scrollTop = next;
  }} else {{
    target.el.scrollTop = next;
  }}
  const after = target.isDoc
    ? Math.round(window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0)
    : Math.round(target.el.scrollTop || 0);
  return {{
    moved: Math.abs(after - before) > 2,
    atBottom: after >= target.maxTop - 8,
    before,
    after,
    step,
    scrollHeight: target.scrollHeight,
    innerHeight: target.clientHeight,
    maxTop: target.maxTop,
    target: target.isDoc ? 'document' : ((target.el.tagName || 'EL') + '#' + (target.el.id || '') + '.' + String(target.el.className || '').slice(0, 50))
  }};
}})()
"""
                        )
                        value = (((message or {}).get("result") or {}).get("result") or {}).get("value") or {}
                        if not isinstance(value, dict):
                            return False
                        before_y = int(value.get("before") or 0)
                        after_y = int(value.get("after") or before_y)
                        scroll_h = int(value.get("scrollHeight") or 0)
                        moved = bool(value.get("moved"))
                        at_bottom = bool(value.get("atBottom"))
                        if not moved and not at_bottom:
                            logger.info(
                                "[Detail] Auction DOM scroll did not move; falling back to physical scroll target=%s y=%s height=%s",
                                value.get("target"),
                                before_y,
                                scroll_h,
                            )
                            return False
                        if moved:
                            logger.info(
                                "[Detail] Auction DOM scroll moved target=%s y=%s->%s height=%s",
                                value.get("target"),
                                before_y,
                                after_y,
                                scroll_h,
                            )
                        return True
                    min_step = 420 if is_naver else 520
                    step_ratio = 0.68 if is_naver else 0.82
                    message = _cdp_eval(
                        f"(() => {{"
                        f"const beforeY = Math.round(window.scrollY || 0);"
                        f"const doc = document.documentElement;"
                        f"const body = document.body;"
                        f"const innerHeight = Math.round(window.innerHeight || 0);"
                        f"const scrollHeight = Math.round(Math.max("
                        f"body ? body.scrollHeight : 0,"
                        f"doc ? doc.scrollHeight : 0"
                        f"));"
                        f"const maxY = Math.max(0, scrollHeight - innerHeight);"
                        f"const step = Math.max({min_step}, Math.floor(innerHeight * {step_ratio}));"
                        f"window.scrollBy(0, step);"
                        f"const afterY = Math.round(window.scrollY || 0);"
                        f"return {{"
                        f"beforeY,"
                        f"scrollY: afterY,"
                        f"innerHeight,"
                        f"scrollHeight,"
                        f"maxY,"
                        f"step,"
                        f"moved: Math.abs(afterY - beforeY) > 2,"
                        f"atBottom: afterY >= maxY - 8"
                        f"}};"
                        f"}})()"
                    )
                    value = (((message or {}).get("result") or {}).get("result") or {}).get("value") or {}
                    if is_gmarket and isinstance(value, dict):
                        moved = bool(value.get("moved"))
                        at_bottom = bool(value.get("atBottom"))
                        if not moved and not at_bottom:
                            logger.info(
                                "[Detail] Gmarket CDP scroll did not move; falling back to physical scroll y=%s height=%s",
                                value.get("scrollY"),
                                value.get("scrollHeight"),
                            )
                            return False
                        return moved or at_bottom
                    if is_auction and isinstance(value, dict):
                        moved = bool(value.get("moved"))
                        at_bottom = bool(value.get("atBottom"))
                        if not moved and not at_bottom:
                            logger.info(
                                "[Detail] Auction CDP scroll did not move; falling back to physical scroll y=%s height=%s",
                                value.get("scrollY"),
                                value.get("scrollHeight"),
                            )
                            return False
                        return True
                    return isinstance(value, dict) and int(value.get("step") or 0) > 0
                except Exception as exc:
                    logger.debug(f"[{product_id}] CDP scroll skipped: {exc}")
                    return False

            def _scroll_metrics_via_cdp() -> Dict:
                if not debug_port:
                    return {}
                try:
                    message = _cdp_eval(
                        "(() => ({"
                        "scrollY: Math.round(window.scrollY || 0),"
                        "innerHeight: Math.round(window.innerHeight || 0),"
                        "scrollHeight: Math.round(Math.max("
                        "document.body ? document.body.scrollHeight : 0,"
                        "document.documentElement ? document.documentElement.scrollHeight : 0"
                        "))"
                        "}))()"
                    )
                    value = (((message or {}).get("result") or {}).get("result") or {}).get("value") or {}
                    return value if isinstance(value, dict) else {}
                except Exception as exc:
                    logger.debug(f"[{product_id}] CDP scroll metrics skipped: {exc}")
                    return {}

            def _prepare_screen_capture_layout() -> bool:
                if not debug_port:
                    return False
                try:
                    message = _cdp_eval(r"""
(() => {
  if (!document.getElementById('codex-screen-capture-style')) {
    const style = document.createElement('style');
    style.id = 'codex-screen-capture-style';
    style.textContent = `
      html { scroll-behavior: auto !important; }
      body { overflow-anchor: none !important; }
      .codex-screen-hidden {
        display: none !important;
        visibility: hidden !important;
        pointer-events: none !important;
      }
    `;
    document.documentElement.appendChild(style);
  }
  document.documentElement.classList.add('codex-screen-capture-active');

  const hidden = [];
  const host = String(location.hostname || '').toLowerCase();
  const isNaver = host.includes('naver');
  const shouldHide = (el) => {
    if (!el || el.id === 'codex-screen-capture-style') return false;
    const cs = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const z = Number.parseInt(cs.zIndex || '0', 10) || 0;
    const fixedOrSticky = ['fixed', 'sticky'].includes(cs.position);
    const text = ((el.innerText || el.textContent || el.getAttribute('aria-label') || '') + '')
      .replace(/\s+/g, ' ')
      .trim();
    if (isNaver && /(상세정보\s*(펼쳐보기|더보기)|상세정보펼쳐보기|상세정보더보기)/.test(text)) {
      return false;
    }
    const topBar = fixedOrSticky && rect.top <= 160 && rect.height <= Math.min(220, window.innerHeight * 0.24);
    const bottomBar = fixedOrSticky && rect.bottom >= window.innerHeight - 120 && rect.height <= 240;
    const rightRail = fixedOrSticky && rect.left >= window.innerWidth * 0.58 && rect.width <= window.innerWidth * 0.42;
    const floatingButton = fixedOrSticky && rect.width <= 180 && rect.height <= 180 && z >= 10;
    const stickyNaverRail = isNaver && rect.left >= window.innerWidth * 0.66 && rect.height >= 120;
    const stickyNaverTabs = isNaver
      && rect.top <= Math.min(460, window.innerHeight * 0.55)
      && rect.height <= 150
      && rect.width >= window.innerWidth * 0.30
      && /(상세정보|상세설명|리뷰|Q&A|문의|판매자정보|추천)/.test(text);
    const naverOptionRail = isNaver
      && rect.left >= window.innerWidth * 0.52
      && rect.height >= 80
      && /(옵션 선택|배송방법|배송정보|선물하기|찜하기|장바구니|구매하기)/.test(text);
    const naverFloatingControl = isNaver
      && z >= 5
      && rect.left >= window.innerWidth * 0.55
      && /(맨위|TOP|접기|선물하기|찜하기)/i.test(text);
    return topBar || bottomBar || rightRail || floatingButton
      || stickyNaverRail || stickyNaverTabs || naverOptionRail || naverFloatingControl;
  };

  for (const el of Array.from(document.body.querySelectorAll('*')).slice(0, 9000)) {
    if (shouldHide(el)) {
      el.classList.add('codex-screen-hidden');
      hidden.push({
        tag: el.tagName,
        id: el.id || '',
        className: String(el.className || '').slice(0, 80)
      });
    }
  }
  return {total: hidden.length, hidden: hidden.slice(0, 20)};
})()
""")
                    value = (((message or {}).get("result") or {}).get("result") or {}).get("value") or {}
                    count = int(value.get("total") or 0) if isinstance(value, dict) else 0
                    if count:
                        logger.info("[Detail] hidden fixed/sticky overlays before capture: %s", count)
                    return count > 0
                except Exception as exc:
                    logger.debug(f"[{product_id}] capture layout prep skipped: {exc}")
                    return False

            def _preexpand_screen_detail() -> None:
                nonlocal screen_detail_expanded
                if not (is_coupang or is_gmarket or is_auction or is_naver):
                    return
                try:
                    import pyautogui as _pag
                    try:
                        ctypes.windll.user32.SetForegroundWindow(hwnd_target)
                    except Exception:
                            pass
                    _pag.hotkey("ctrl", "home")
                    if render_hwnd:
                        try:
                            win32api.SendMessage(render_hwnd, win32con.WM_KEYDOWN, VK_HOME, HOME_DN)
                            time.sleep(0.04)
                            win32api.SendMessage(render_hwnd, win32con.WM_KEYUP, VK_HOME, HOME_UP)
                        except Exception:
                            pass
                    time.sleep(0.5)

                    if is_naver:
                        _dismiss_naver_popup()
                        if _expand_naver_detail_button_via_cdp() or _expand_detail_more_via_cdp():
                            screen_detail_expanded = True
                            _pag.hotkey("ctrl", "home")
                            if render_hwnd:
                                try:
                                    win32api.SendMessage(render_hwnd, win32con.WM_KEYDOWN, VK_HOME, HOME_DN)
                                    time.sleep(0.04)
                                    win32api.SendMessage(render_hwnd, win32con.WM_KEYUP, VK_HOME, HOME_UP)
                                except Exception:
                                    pass
                            time.sleep(0.5)
                            return
                        def _naver_dismiss_js_dialog() -> bool:
                            """JS alert 다이얼로그 자동 dismiss. 다이얼로그가 있으면 True 반환."""
                            if not debug_port:
                                return False
                            try:
                                resp = _cdp_call("Page.handleJavaScriptDialog", {"accept": True}, timeout=2)
                                if resp and "error" not in resp:
                                    logger.info("[Detail] Naver JS dialog auto-dismissed (확인 클릭)")
                                    time.sleep(0.25)
                                    return True
                            except Exception:
                                pass
                            return False

                        for _ in range(18):
                            if _click_naver_detail_more_button_by_outline():
                                # 클릭 후 JS 다이얼로그 확인 — "옵션 선택" 등 잘못된 버튼이면 dismiss 후 재시도
                                if _naver_dismiss_js_dialog():
                                    # 잘못된 버튼 클릭 → 다이얼로그 dismiss 후 계속 탐색
                                    logger.info("[Detail] Naver wrong button dismissed, continue searching expand btn")
                                    if render_hwnd:
                                        try:
                                            win32api.SendMessage(render_hwnd, win32con.WM_KEYDOWN, VK_NEXT, NEXT_DN)
                                            time.sleep(0.04)
                                            win32api.SendMessage(render_hwnd, win32con.WM_KEYUP, VK_NEXT, NEXT_UP)
                                        except Exception:
                                            _pag.press("pagedown")
                                    else:
                                        _pag.press("pagedown")
                                    time.sleep(0.55)
                                    continue
                                screen_detail_expanded = True
                                break
                            # 매 스크롤마다도 혹시 떠있는 다이얼로그 정리
                            _naver_dismiss_js_dialog()
                            if render_hwnd:
                                try:
                                    win32api.SendMessage(render_hwnd, win32con.WM_KEYDOWN, VK_NEXT, NEXT_DN)
                                    time.sleep(0.04)
                                    win32api.SendMessage(render_hwnd, win32con.WM_KEYUP, VK_NEXT, NEXT_UP)
                                except Exception:
                                    _pag.press("pagedown")
                            else:
                                _pag.press("pagedown")
                            time.sleep(0.55)
                        if not screen_detail_expanded and _expand_detail_more_via_cdp():
                            screen_detail_expanded = True
                        _pag.hotkey("ctrl", "home")
                        if render_hwnd:
                            try:
                                win32api.SendMessage(render_hwnd, win32con.WM_KEYDOWN, VK_HOME, HOME_DN)
                                time.sleep(0.04)
                                win32api.SendMessage(render_hwnd, win32con.WM_KEYUP, VK_HOME, HOME_UP)
                            except Exception:
                                pass
                        time.sleep(0.7)
                        return

                    def _auction_remaining_detail_more_count_via_cdp() -> int:
                        if not (debug_port and is_auction):
                            return 0
                        script = r"""
(() => {
  const keywords = ['상세정보더보기', '상세정보 더보기', '상품상세더보기', '상품상세 더보기', '상품정보더보기', '상품정보 더보기']
    .map((text) => String(text || '').replace(/\s+/g, '').trim());
  const compact = (text) => String(text || '').replace(/\s+/g, '').trim();
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 80 || rect.height < 24 || rect.width > 620 || rect.height > 140) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && Number(cs.opacity || 1) > 0.05;
  };
  let count = 0;
  const selectors = 'button,a,[role="button"],input[type="button"],input[type="submit"]';
  for (const el of Array.from(document.querySelectorAll(selectors)).slice(0, 3000)) {
    const text = compact(el.innerText || el.textContent || el.getAttribute('aria-label') || el.value || '');
    if (!keywords.includes(text) || !visible(el)) continue;
    const top = Math.round(el.getBoundingClientRect().top + window.scrollY);
    if (top >= 260) count++;
  }
  return count;
})()
"""
                        try:
                            msg = _cdp_eval(script, timeout=6)
                            raw = (((msg or {}).get("result") or {}).get("result") or {}).get("value") or 0
                            return int(raw or 0)
                        except Exception:
                            return 0

                    def _dispatch_mouse_click_via_cdp(x: int, y: int) -> bool:
                        if not debug_port:
                            return False
                        try:
                            x = int(x)
                            y = int(y)
                            if x <= 0 or y <= 0:
                                return False
                            _cdp_call("Page.bringToFront", {}, timeout=3)
                            _cdp_call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, timeout=3)
                            _cdp_call(
                                "Input.dispatchMouseEvent",
                                {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
                                timeout=3,
                            )
                            time.sleep(0.05)
                            _cdp_call(
                                "Input.dispatchMouseEvent",
                                {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
                                timeout=3,
                            )
                            return True
                        except Exception as exc:
                            logger.debug("[Detail] CDP mouse click skipped: %s", exc)
                            return False

                    def _dismiss_js_dialog_via_cdp(reason: str = "") -> bool:
                        if not debug_port:
                            return False
                        try:
                            _cdp_call("Page.handleJavaScriptDialog", {"accept": True}, timeout=2)
                            logger.info("[Detail] accepted JS dialog via CDP%s", f" ({reason})" if reason else "")
                            time.sleep(0.25)
                            return True
                        except Exception:
                            return False

                    def _expand_coupang_auction_detail_more_via_cdp() -> bool:
                        """Coupang/Auction: click only the real detail-more button, not generic accordions."""
                        if not (debug_port and (is_coupang or is_auction)):
                            return False
                        keywords = (
                            ["상품정보더보기", "상세정보더보기", "상품상세더보기"]
                            if is_coupang
                            else ["상세정보더보기", "상품상세더보기", "상품정보더보기"]
                        )
                        script = r"""
(() => {
  const keywords = __KEYWORDS__;
  const compact = (text) => String(text || '').replace(/\s+/g, '').trim();
  const textOf = (el) => compact((el.innerText || el.textContent || el.getAttribute('aria-label') || ''));
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 30 || rect.height < 14) return false;
    const cs = el.ownerDocument.defaultView.getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && Number(cs.opacity || 1) > 0.05;
  };
  const safeButtonText = (t) => {
    if (!t || t.length > 80) return false;
    if (t.includes('필수표기정보더보기') || t.includes('쿠폰') || t.includes('구매') || t.includes('장바구니')) return false;
    return keywords.some((kw) => t.includes(kw));
  };
  const docs = [document];
  for (const frame of Array.from(document.querySelectorAll('iframe'))) {
    try {
      if (frame.contentDocument) docs.push(frame.contentDocument);
    } catch (_) {}
  }
  const selectors = [
    'button', 'a', '[role="button"]', 'input[type="button"]', 'input[type="submit"]',
    '[class*="detail"][class*="more"]', '[class*="more"][class*="detail"]',
    '[id*="detail"][id*="more"]', '[id*="more"][id*="detail"]'
  ];
  const candidates = [];
  for (const doc of docs) {
    const win = doc.defaultView || window;
    for (const selector of selectors) {
      for (const el of Array.from(doc.querySelectorAll(selector)).slice(0, 2000)) {
        const t = textOf(el) || compact(el.value || '');
        if (!safeButtonText(t) || !visible(el)) continue;
        const rect = el.getBoundingClientRect();
        const frameEl = win.frameElement;
        const frameRect = frameEl ? frameEl.getBoundingClientRect() : {top: 0, left: 0};
        const absoluteTop = rect.top + (win.scrollY || 0) + frameRect.top + window.scrollY;
        if (absoluteTop < 260) continue;
        const area = rect.width * rect.height;
        const score = (keywords.findIndex((kw) => t.includes(kw)) + 1) * -1000 + absoluteTop - Math.min(area, 80000) / 120;
        candidates.push({el, win, text: t, top: absoluteTop, score});
      }
    }
  }
  candidates.sort((a, b) => a.score - b.score);
  const best = candidates[0];
  if (!best) return {clicked: false, reason: 'not_found'};
  try {
    best.el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
    const rect = best.el.getBoundingClientRect();
    const frameEl = best.win && best.win.frameElement;
    const frameRect = frameEl ? frameEl.getBoundingClientRect() : {top: 0, left: 0};
    const x = Math.round(frameRect.left + rect.left + rect.width / 2);
    const y = Math.round(frameRect.top + rect.top + rect.height / 2);
    if (String(location.hostname || '').toLowerCase().includes('auction')) {
      try { best.el.click(); } catch (_) {}
      return {clicked: true, needsMouse: true, x, y, text: best.text, top: Math.round(best.top)};
    }
    best.el.click();
    return {clicked: true, text: best.text, top: Math.round(best.top)};
  } catch (err) {
    return {clicked: false, reason: String(err && err.message || err)};
  }
})()
"""
                        try:
                            import json
                            for scroll_y in (0, 900, 1800, 3000, 4500, 6500):
                                _cdp_eval(f"window.scrollTo(0,{int(scroll_y)}); true;", timeout=4)
                                time.sleep(0.35)
                                before_height = int((_scroll_metrics_via_cdp() or {}).get("scrollHeight") or 0)
                                msg = _cdp_eval(script.replace("__KEYWORDS__", json.dumps(keywords, ensure_ascii=False)), timeout=8)
                                val = (((msg or {}).get("result") or {}).get("result") or {}).get("value") or {}
                                if isinstance(val, dict) and val.get("clicked"):
                                    if is_auction:
                                        if val.get("needsMouse"):
                                            _dispatch_mouse_click_via_cdp(int(val.get("x") or 0), int(val.get("y") or 0))
                                            _dismiss_js_dialog_via_cdp("auction detail-more mouse click")
                                        time.sleep(2.6)
                                        after_height = int((_scroll_metrics_via_cdp() or {}).get("scrollHeight") or 0)
                                        remaining = _auction_remaining_detail_more_count_via_cdp()
                                        if after_height > before_height + 300 or remaining == 0:
                                            logger.info(
                                                "[Detail] auction detail-more click verified: %s at y=%s height=%s->%s remaining=%s mouse=%s",
                                                val.get("text"),
                                                val.get("top"),
                                                before_height,
                                                after_height,
                                                remaining,
                                                bool(val.get("needsMouse")),
                                            )
                                            return True
                                        logger.info(
                                            "[Detail] auction detail-more click not verified yet: %s at y=%s height=%s->%s remaining=%s mouse=%s",
                                            val.get("text"),
                                            val.get("top"),
                                            before_height,
                                            after_height,
                                            remaining,
                                            bool(val.get("needsMouse")),
                                        )
                                        continue
                                    time.sleep(2.0)
                                    logger.info(
                                        "[Detail] %s exact detail-more clicked via CDP: %s at y=%s",
                                        "coupang" if is_coupang else "auction",
                                        val.get("text"),
                                        val.get("top"),
                                    )
                                    return True
                            return False
                        except Exception as exc:
                            logger.debug("[Detail] exact detail-more CDP skipped: %s", exc)
                            return False

                    def _expand_auction_detail_more() -> bool:
                        """옥션 전용: CDP로 버튼 위치 찾은 후 실제 마우스 클릭."""
                        if not (debug_port and is_auction):
                            return False
                        try:
                            _AUCTION_FIND_JS = """(() => {
  const kwds = ['상세정보더보기','상세정보 더보기','상품상세더보기'];
  for (const el of Array.from(document.querySelectorAll('button,a,div,span,p,input'))) {
    const t = (el.innerText||el.textContent||'').replace(/\\s+/g,'').trim();
    if (!kwds.some(k=>t.includes(k))) continue;
    const cs = window.getComputedStyle(el);
    if (cs.display==='none'||cs.visibility==='hidden') continue;
    const rect = el.getBoundingClientRect();
    if (rect.width < 20 || rect.height < 10) continue;
    if (rect.top + window.scrollY < 200) continue;
    el.scrollIntoView({block:'center',behavior:'instant'});
    const r2 = el.getBoundingClientRect();
    return {x:Math.round(r2.left+r2.width/2), y:Math.round(r2.top+r2.height/2), found:true};
  }
  return {found:false};
})()"""
                            for scroll_y in (0, 1500, 3000, 5000):
                                _cdp_eval(f"window.scrollTo(0,{scroll_y}); true;")
                                time.sleep(0.8)
                                before_height = int((_scroll_metrics_via_cdp() or {}).get("scrollHeight") or 0)
                                msg = _cdp_eval(_AUCTION_FIND_JS)
                                val = (((msg or {}).get("result") or {}).get("result") or {}).get("value") or {}
                                if isinstance(val, dict) and val.get("found"):
                                    vx = int(val.get("x") or 0)
                                    vy = int(val.get("y") or 0)
                                    wl, wt, _, _ = win32gui.GetWindowRect(hwnd_target)
                                    _win_click(wl + vx, wt + CHROME_UI + vy)
                                    logger.info("[Detail] auction detail-more clicked via CDP pos (%s,%s)", wl + vx, wt + CHROME_UI + vy)
                                    _dismiss_js_dialog_via_cdp("auction fallback click")
                                    time.sleep(2.4)
                                    after_height = int((_scroll_metrics_via_cdp() or {}).get("scrollHeight") or 0)
                                    remaining = _auction_remaining_detail_more_count_via_cdp()
                                    if after_height > before_height + 300 or remaining == 0:
                                        logger.info(
                                            "[Detail] auction fallback detail-more verified: height=%s->%s remaining=%s",
                                            before_height,
                                            after_height,
                                            remaining,
                                        )
                                        return True
                                    logger.info(
                                        "[Detail] auction fallback detail-more not verified: height=%s->%s remaining=%s",
                                        before_height,
                                        after_height,
                                        remaining,
                                    )
                        except Exception as exc:
                            logger.debug("[Detail] auction expand skipped: %s", exc)
                        return False

                    def _click_remaining_auction_detail_more_after_cdp() -> bool:
                        """Auction only: if CDP reported a click but the button remains, do one real click."""
                        if not (debug_port and is_auction):
                            return False
                        script = r"""
(() => {
  const keywords = [
    '\uc0c1\uc138\uc815\ubcf4\ub354\ubcf4\uae30',
    '\uc0c1\ud488\uc0c1\uc138\ub354\ubcf4\uae30',
    '\uc0c1\ud488\uc815\ubcf4\ub354\ubcf4\uae30'
  ];
  const badWords = [
    '\ud544\uc218\ud45c\uae30\uc815\ubcf4\ub354\ubcf4\uae30',
    '\ucfe0\ud3f0',
    '\uad6c\ub9e4',
    '\uc7a5\ubc14\uad6c\ub2c8'
  ];
  const compact = (text) => String(text || '').replace(/\s+/g, '').trim();
  const normalizedKeywords = keywords.map((word) => compact(word));
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 80 || rect.height < 24 || rect.width > 620 || rect.height > 140) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && Number(cs.opacity || 1) > 0.05;
  };
  const candidates = [];
  const selectors = 'button,a,[role="button"],input[type="button"],input[type="submit"],div,span,p';
  for (const el of Array.from(document.querySelectorAll(selectors)).slice(0, 5000)) {
    const text = compact(el.innerText || el.textContent || el.getAttribute('aria-label') || el.value || '');
    if (!text || text.length > 90) continue;
    if (badWords.some((word) => text.includes(word))) continue;
    if (!normalizedKeywords.includes(text)) continue;
    if (!visible(el)) continue;
    const rect = el.getBoundingClientRect();
    const absoluteTop = Math.round(rect.top + window.scrollY);
    if (absoluteTop < 260) continue;
    candidates.push({el, text, absoluteTop, area: rect.width * rect.height});
  }
  candidates.sort((a, b) => a.absoluteTop - b.absoluteTop || b.area - a.area);
  const best = candidates[0];
  if (!best) return {found: false};
  best.el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const rect = best.el.getBoundingClientRect();
  return {
    found: true,
    x: Math.round(rect.left + rect.width / 2),
    y: Math.round(rect.top + rect.height / 2),
    text: best.text,
    top: best.absoluteTop
  };
})()
"""
                        try:
                            for scroll_y in (0, 1800, 3600, 5600, 7600, 9800):
                                _cdp_eval(f"window.scrollTo(0,{int(scroll_y)}); true;", timeout=4)
                                time.sleep(0.35)
                                before_height = int((_scroll_metrics_via_cdp() or {}).get("scrollHeight") or 0)
                                msg = _cdp_eval(script, timeout=8)
                                val = (((msg or {}).get("result") or {}).get("result") or {}).get("value") or {}
                                if not (isinstance(val, dict) and val.get("found")):
                                    continue
                                vx = int(val.get("x") or 0)
                                vy = int(val.get("y") or 0)
                                if vx <= 0 or vy <= 0:
                                    continue
                                wl, wt, _, _ = win32gui.GetWindowRect(hwnd_target)
                                _win_click(wl + vx, wt + CHROME_UI + vy)
                                logger.info(
                                    "[Detail] auction remaining detail-more clicked physically: %s at y=%s",
                                    val.get("text"),
                                    val.get("top"),
                                )
                                _dismiss_js_dialog_via_cdp("auction remaining detail-more")
                                time.sleep(2.4)
                                after_height = int((_scroll_metrics_via_cdp() or {}).get("scrollHeight") or 0)
                                remaining = _auction_remaining_detail_more_count_via_cdp()
                                if after_height > before_height + 300 or remaining == 0:
                                    logger.info(
                                        "[Detail] auction physical detail-more click verified: height=%s->%s remaining=%s",
                                        before_height,
                                        after_height,
                                        remaining,
                                    )
                                    return True
                                logger.info(
                                    "[Detail] auction physical detail-more click not verified: height=%s->%s remaining=%s",
                                    before_height,
                                    after_height,
                                    remaining,
                                )
                            return False
                        except Exception as exc:
                            logger.debug("[Detail] auction remaining detail-more check skipped: %s", exc)
                            return False

                    if is_coupang or is_auction:
                        cdp_expanded = _expand_coupang_auction_detail_more_via_cdp()
                        if is_auction:
                            if not cdp_expanded:
                                if _auction_remaining_detail_more_count_via_cdp() == 0:
                                    cdp_expanded = True
                                else:
                                    cdp_expanded = _click_remaining_auction_detail_more_after_cdp() or _expand_auction_detail_more()
                    else:
                        cdp_expanded = _expand_detail_more_via_cdp()
                    if cdp_expanded and (is_coupang or is_gmarket or is_auction):
                        screen_detail_expanded = True
                        _pag.hotkey("ctrl", "home")
                        if render_hwnd:
                            try:
                                win32api.SendMessage(render_hwnd, win32con.WM_KEYDOWN, VK_HOME, HOME_DN)
                                time.sleep(0.04)
                                win32api.SendMessage(render_hwnd, win32con.WM_KEYUP, VK_HOME, HOME_UP)
                            except Exception:
                                pass
                        time.sleep(0.5)
                        return
                    if is_auction:
                        logger.info("[Detail] Auction dedicated detail-more failed; generic color fallback disabled")
                        return
                    max_scroll_iters = 12 if is_coupang else 10 if is_gmarket else 12 if is_auction else 7
                    for _ in range(max_scroll_iters):
                        if _click_detail_more_button_by_color():
                            screen_detail_expanded = True
                            break
                        if render_hwnd:
                            try:
                                win32api.SendMessage(render_hwnd, win32con.WM_KEYDOWN, VK_NEXT, NEXT_DN)
                                time.sleep(0.04)
                                win32api.SendMessage(render_hwnd, win32con.WM_KEYUP, VK_NEXT, NEXT_UP)
                            except Exception:
                                _pag.press("pagedown")
                        else:
                            _pag.press("pagedown")
                        time.sleep(0.45)
                    _pag.hotkey("ctrl", "home")
                    if render_hwnd:
                        try:
                            win32api.SendMessage(render_hwnd, win32con.WM_KEYDOWN, VK_HOME, HOME_DN)
                            time.sleep(0.04)
                            win32api.SendMessage(render_hwnd, win32con.WM_KEYUP, VK_HOME, HOME_UP)
                        except Exception:
                            pass
                    time.sleep(0.5)
                except Exception as exc:
                    logger.debug(f"[{product_id}] screen pre-expand skipped: {exc}")

            rect = win32gui.GetWindowRect(hwnd_target)
            win_w = rect[2] - rect[0]
            win_h = rect[3] - rect[1]
            CHROME_UI = 130

            def _win_click(x: int, y: int) -> None:
                try:
                    win32api.SetCursorPos((int(x), int(y)))
                    time.sleep(0.04)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                    time.sleep(0.03)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                except Exception:
                    pass

            def _dismiss_naver_popup() -> None:
                if not is_naver:
                    return
                try:
                    import pyautogui as _pag
                    try:
                        ctypes.windll.user32.SetForegroundWindow(hwnd_target)
                    except Exception:
                        pass
                    for _ in range(3):
                        _pag.press("esc")
                        time.sleep(0.16)
                    try:
                        left, top, right, _bottom = win32gui.GetWindowRect(hwnd_target)
                        win32api.SetCursorPos((max(left + 16, right - 90), top + 18))
                        time.sleep(0.08)
                    except Exception:
                        pass
                except Exception as exc:
                    logger.debug(f"[{product_id}] naver popup dismiss skipped: {exc}")

            def _set_clipboard_text(text: str) -> bool:
                try:
                    import win32clipboard
                    win32clipboard.OpenClipboard()
                    try:
                        win32clipboard.EmptyClipboard()
                        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
                    finally:
                        win32clipboard.CloseClipboard()
                    return True
                except Exception as exc:
                    logger.debug(f"[{product_id}] clipboard write skipped: {exc}")
                    return False

            def _get_clipboard_text() -> str:
                try:
                    import win32clipboard
                    win32clipboard.OpenClipboard()
                    try:
                        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                            return str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or "")
                    finally:
                        win32clipboard.CloseClipboard()
                except Exception:
                    return ""
                return ""

            def _read_address_bar_url() -> str:
                try:
                    import pyautogui as _pag
                    try:
                        ctypes.windll.user32.SetForegroundWindow(hwnd_target)
                    except Exception:
                        pass
                    _pag.hotkey("ctrl", "l")
                    time.sleep(0.08)
                    _pag.hotkey("ctrl", "c")
                    time.sleep(0.08)
                    current = _get_clipboard_text().strip()
                    _pag.press("esc")
                    return current
                except Exception as exc:
                    logger.debug(f"[{product_id}] address-bar read skipped: {exc}")
                    return ""

            def _run_address_bar_javascript(script: str, wait_sec: float = 2.0) -> bool:
                try:
                    import pyautogui as _pag
                    try:
                        ctypes.windll.user32.SetForegroundWindow(hwnd_target)
                    except Exception:
                        pass
                    _pag.hotkey("ctrl", "l")
                    time.sleep(0.12)
                    # Chrome strips pasted javascript: URLs. Paste the prefix in two parts.
                    if not _set_clipboard_text("java"):
                        return False
                    _pag.hotkey("ctrl", "v")
                    time.sleep(0.05)
                    if not _set_clipboard_text("script:" + script):
                        return False
                    _pag.hotkey("ctrl", "v")
                    time.sleep(0.08)
                    _pag.press("enter")
                    time.sleep(wait_sec)
                    return True
                except Exception as exc:
                    logger.debug(f"[{product_id}] address-bar JS skipped: {exc}")
                    return False

            def _resolve_naver_catalog_to_seller() -> bool:
                nonlocal product_url, product_url_lower, is_naver_catalog
                if not is_naver_catalog:
                    return True
                marker = "JEPUM_NAVER_SELLER_NAV_V73"
                script = r"""
                (async()=>{
                const MARK='JEPUM_NAVER_SELLER_NAV_V73';
                document.title=MARK+' scanning';
                const sleep=m=>new Promise(r=>setTimeout(r,m));
                const text=e=>String((e&&e.innerText)||(e&&e.textContent)||'').replace(/\s+/g,' ').trim();
                const pageH=()=>Math.max(document.body?document.body.scrollHeight:0,document.documentElement?document.documentElement.scrollHeight:0);
                for(let y=0;y<Math.min(pageH(),5200);y+=680){scrollTo(0,y);await sleep(220)}
                scrollTo(0,0);await sleep(250);
                const bad=/search\.shopping\.naver\.com\/catalog|search\.naver\.com|shopping\.naver\.com\/home|shopping\.naver\.com\/search|shopping\.naver\.com\/catalog/i;
                const good=/smartstore\.naver\.com|brand\.naver\.com|shopping\.naver\.com\/window-products|shopping\.naver\.com\/outlink|cr\.shopping\.naver\.com\/adcr/i;
                const buy=/구매|최저가|판매처|쇼핑몰|바로가기|사이트|방문|사러가기|구매하러/i;
                const candidates=[];
                for(const a of Array.from(document.querySelectorAll('a[href]'))){
                let href=a.href||'';if(!href||bad.test(href))continue;
                const t=text(a);const r=a.getBoundingClientRect();const y=Math.round(r.top+scrollY);
                let score=0;
                if(good.test(href))score+=120;
                if(/smartstore\.naver\.com|brand\.naver\.com/i.test(href))score+=60;
                if(buy.test(t))score+=70;
                if(r.width>40&&r.height>14)score+=20;
                if(y>100&&y<4200)score+=Math.max(0,30-Math.floor(y/180));
                if(/adcr|outlink|window-products/i.test(href))score+=20;
                if(score>=90)candidates.push({a,href,t,score,y});
                }
                candidates.sort((x,y)=>y.score-x.score||x.y-y.y);
                const chosen=candidates[0];
                if(!chosen){document.title=MARK+' failed';return}
                document.title=MARK+' opening';
                location.href=chosen.href;
                })()
                """
                script = "".join(line.strip() for line in script.splitlines())
                result.setdefault("diagnostics", {})["naver_catalog_url"] = product_url
                if not _run_address_bar_javascript(script, wait_sec=1.0):
                    result.setdefault("diagnostics", {})["naver_catalog_resolved"] = False
                    return False
                deadline = time.time() + 35.0
                current_url = ""
                current_title = ""
                while time.time() < deadline:
                    time.sleep(1.0)
                    current_title = win32gui.GetWindowText(hwnd_target) or ""
                    current_url = _read_address_bar_url()
                    lowered = current_url.lower()
                    if lowered and "search.shopping.naver.com/catalog" not in lowered and "search.naver.com" not in lowered:
                        if any(host in lowered for host in ("smartstore.naver.com", "brand.naver.com", "shopping.naver.com/window-products")):
                            product_url = current_url
                            product_url_lower = product_url.lower()
                            is_naver_catalog = False
                            result.setdefault("diagnostics", {}).update({
                                "naver_catalog_resolved": True,
                                "naver_resolved_product_url": product_url,
                                "naver_resolved_title": current_title[:180],
                            })
                            logger.info("[Detail] resolved Naver catalog to seller page: %s -> %s", product_id, product_url)
                            return True
                    if marker not in current_title and "가격비교" not in current_title and "price" not in current_title.lower():
                        if current_url and "catalog" not in current_url.lower() and "cr.shopping.naver.com" not in current_url.lower() and "/outlink" not in current_url.lower():
                            product_url = current_url
                            product_url_lower = product_url.lower()
                            is_naver_catalog = False
                            result.setdefault("diagnostics", {}).update({
                                "naver_catalog_resolved": True,
                                "naver_resolved_product_url": product_url,
                                "naver_resolved_title": current_title[:180],
                            })
                            return True
                result.setdefault("diagnostics", {}).update({
                    "naver_catalog_resolved": False,
                    "naver_catalog_last_url": current_url,
                    "naver_catalog_last_title": current_title[:180],
                })
                logger.warning("[Detail] failed to resolve Naver catalog page for %s: %s / %s", product_id, current_title, current_url)
                return False

            def _rebuild_naver_detail_page_via_bookmarklet() -> bool:
                nonlocal naver_rebuilt_detail_page, screen_detail_expanded
                if not is_naver:
                    return False
                script = r"""(async()=>{const s=m=>new Promise(r=>setTimeout(r,m));const h=()=>Math.max(document.body?document.body.scrollHeight:0,document.documentElement?document.documentElement.scrollHeight:0);let p=0,st=0;for(let a=0;a<5&&st<2;a++){const mh=Math.min(h(),90000);const step=Math.max(520,Math.floor(innerHeight*.72));for(let y=0;y<=mh+step;y+=step){scrollTo(0,y);await s(150)}await s(550);const c=h();if(Math.abs(c-p)<90)st++;else st=0;p=c}scrollTo(0,0);await s(350);const esc=t=>String(t||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));const seen=new Set();const arr=[...document.images].map(img=>{const r=img.getBoundingClientRect();let src=img.currentSrc||img.src||img.getAttribute('data-src')||img.getAttribute('data-lazy-src')||img.getAttribute('data-original')||'';src=String(src||'');if(src.startsWith('//'))src=location.protocol+src;return{src,w:Math.round(img.naturalWidth||r.width||0),h:Math.round(img.naturalHeight||r.height||0),dw:Math.round(r.width||0),dh:Math.round(r.height||0),top:Math.round(r.top+scrollY)}}).filter(o=>{if(!o.src||o.src.startsWith('data:')||seen.has(o.src))return false;seen.add(o.src);const w=Math.max(o.w,o.dw),hh=Math.max(o.h,o.dh);if(w<160||hh<110||w*hh<22000)return false;if(o.top<80&&hh<260)return false;return true});if(!arr.length){document.title='Naver detail rebuild failed';return}const title=document.title||'Naver detail';const imgs=arr.map(o=>`<img src="${esc(o.src)}" loading="eager" decoding="sync">`).join('');document.open();document.write(`<!doctype html><html><head><meta charset="utf-8"><title>${esc(title)}</title><style>html,body{margin:0;padding:0;background:#fff;color:#111}body{font-family:Arial,'Malgun Gothic',sans-serif}.wrap{width:min(1120px,100vw);margin:0 auto;padding:24px 0 80px}.meta{font-size:14px;color:#666;margin:0 16px 18px}.meta b{color:#111}img{display:block;max-width:100%;height:auto;margin:0 auto 18px;background:#fff}hr{border:0;border-top:1px solid #eee;margin:18px 0}</style></head><body><main class="wrap"><p class="meta"><b>네이버 상세 이미지 재조립</b> · ${arr.length} images</p><hr>${imgs}</main><script>Promise.all(Array.from(document.images).map(i=>i.complete?1:new Promise(r=>{i.onload=i.onerror=r}))).then(()=>setTimeout(()=>scrollTo(0,0),500));</script></body></html>`);document.close();})()"""
                if not _run_address_bar_javascript(script, wait_sec=4.0):
                    return False
                try:
                    import pyautogui as _pag
                    _pag.hotkey("ctrl", "home")
                except Exception:
                    pass
                time.sleep(1.0)
                naver_rebuilt_detail_page = True
                screen_detail_expanded = True
                result.setdefault("diagnostics", {})["naver_rebuilt_detail_page"] = True
                logger.info("[Detail] Naver detail page rebuilt in-browser for %s", product_id)
                return True

            def _rebuild_naver_detail_page_via_bookmarklet() -> bool:
                nonlocal naver_rebuilt_detail_page, screen_detail_expanded
                if not is_naver:
                    return False
                marker = "JEPUM_NAVER_REBUILT_V75"
                safe_product_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(product_id))[:80] or "naver"
                download_name = f"jepum_naver_detail_{safe_product_id}_{int(time.time() * 1000)}.json"
                download_dir = Path(os.path.expanduser("~")) / "Downloads"
                download_path = download_dir / download_name
                script = r"""
                (async()=>{
                const MARK='JEPUM_NAVER_REBUILT_V75';
                const DOWNLOAD_NAME='__DOWNLOAD_NAME__';
                document.title=MARK+' running';
                const sleep=m=>new Promise(r=>setTimeout(r,m));
                const pageH=()=>Math.max(document.body?document.body.scrollHeight:0,document.documentElement?document.documentElement.scrollHeight:0);
                const text=e=>String((e&&e.innerText)||(e&&e.textContent)||'').replace(/\s+/g,' ').trim();
                const visible=e=>{const r=e.getBoundingClientRect();return r.width>20&&r.height>12&&r.bottom>0&&r.top<innerHeight};
                const expandRe=/(\uc0c1\uc138|\uc0c1\ud488).*(\ub354\ubcf4\uae30|\ud3bc\uccd0\ubcf4\uae30)|\ud3bc\uccd0\ubcf4\uae30/;
                let expandBottom=0;
                if(!__ALREADY_EXPANDED__){
                for(const e of Array.from(document.querySelectorAll('button,a,[role="button"]'))){
                const t=text(e);if(!t||!expandRe.test(t))continue;
                const er=e.getBoundingClientRect();if(er.width<20||er.height<12)continue;
                if(er.left>innerWidth*.55)continue;
                try{e.scrollIntoView({block:'center'});await sleep(250);const r=e.getBoundingClientRect();const y=Math.round(r.bottom+scrollY);if(y>200)expandBottom=expandBottom?Math.min(expandBottom,y):y;e.click();await sleep(900)}catch(_){}
                }
                }
                let prev=0,stable=0;
                for(let pass=0;pass<6&&stable<2;pass++){
                const maxY=Math.min(pageH(),260000),step=Math.max(680,Math.floor(innerHeight*.86));
                for(let y=0;y<=maxY+step;y+=step){scrollTo(0,y);await sleep(150)}
                await sleep(550);const now=pageH();if(Math.abs(now-prev)<80)stable++;else stable=0;prev=now;
                }
                scrollTo(0,0);await sleep(450);
                const detailRe=/\uc0c1\ud488\uc0c1\uc138|\uc0c1\uc138\uc815\ubcf4|\uc0c1\uc138\uc124\uba85/;
                let detailTop=0;
                for(const e of Array.from(document.querySelectorAll('a,button,li,span,div'))){
                const t=text(e);if(!t||!detailRe.test(t)||t.length>42)continue;
                const r=e.getBoundingClientRect(),y=Math.round(r.top+scrollY);
                if(r.width>30&&r.height>8&&y>180)detailTop=detailTop?Math.min(detailTop,y):y;
                }
                if(expandBottom)detailTop=Math.max(detailTop||0,expandBottom+40);
                if(!detailTop||detailTop>pageH()-600)detailTop=Math.min(Math.max(560,Math.floor(innerHeight*.62)),Math.floor(pageH()*.32));
                const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
                function srcOf(img){let src=img.currentSrc||img.src||img.getAttribute('data-src')||img.getAttribute('data-lazy-src')||img.getAttribute('data-original')||'';src=String(src||'');if(src.startsWith('//'))src=location.protocol+src;return src}
                function collect(loose=false){
                const seen=new Set(),out=[];
                for(const img of Array.from(document.images)){
                const r=img.getBoundingClientRect(),src=srcOf(img);if(!src||src.startsWith('data:'))continue;
                const key=src.split('?')[0];if(seen.has(key))continue;
                const top=Math.round(r.top+scrollY),left=Math.round(r.left),dw=Math.round(r.width||0),dh=Math.round(r.height||0);
                const w=Math.round(img.naturalWidth||dw||0),h=Math.round(img.naturalHeight||dh||0),mw=Math.max(w,dw),mh=Math.max(h,dh),cx=left+(dw/2);
                const minTop=Math.max(320,detailTop-80);
                if(top<minTop)continue;
                if(cx>innerWidth*.82||left>innerWidth*.76)continue;
                if(dw>0&&dw<220&&!loose)continue;
                if(mw<220||mh<140||mw*mh<36000)continue;
                if(/sprite|icon|logo|profile|adcr|banner/i.test(src)&&mh<360)continue;
                seen.add(key);out.push({src,top,w:mw,h:mh});
                }
                return out.sort((a,b)=>a.top-b.top);
                }
                function collectTexts(){
                const out=[],seen=new Set();
                const uiRe=/\uad6c\ub9e4|\uc7a5\ubc14\uad6c\ub2c8|\ucc1c\ud558\uae30|\uc120\ubb3c\ud558\uae30|\ud1a1\ud1a1|\ub9ac\ubdf0|\ubb38\uc758|\ubc30\uc1a1\uc815\ubcf4|\ubc30\uc1a1\ubc29\ubc95|\ubc30\uc1a1\ube44|\ud0dd\ubc30|\uacb0\uc81c|\uc785\ub825\ud558\uc9c0|\uc8fc\ubb38 \uc2dc|\uc0c1\ud488\ubc88\ud638|\ubaa8\ub378\uba85|\uc6d0\uc0b0\uc9c0|\uc635\uc158|\uad50\ud658|\ubc18\ud488|\uac80\uc0c9|\ub85c\uadf8\uc778|\ud310\ub9e4\uc790|\uc2a4\ub9c8\ud2b8\uc2a4\ud1a0\uc5b4|\ub124\uc774\ubc84|\ucd94\ucc9c|\uc0c1\uc138\uc815\ubcf4|\ud3bc\uccd0\ubcf4\uae30|\uac19\uc774 \ub458\ub7ec\ubcfc/;
                const skipSel='header,nav,footer,aside,[role="navigation"],[class*="floating"],[class*="sticky"],[class*="option"],[class*="purchase"],[class*="order"],[class*="review"],[class*="qna"],[class*="recommend"]';
                for(const e of Array.from(document.querySelectorAll('h1,h2,h3,h4,p,li,dt,dd,strong,b,em,span,div'))){
                if(e.closest(skipSel))continue;
                const r=e.getBoundingClientRect(),top=Math.round(r.top+scrollY),left=Math.round(r.left),cx=left+(r.width/2);
                if(top<detailTop-180||r.width<120||r.height<12||cx>innerWidth*.82||left>innerWidth*.78)continue;
                let t=text(e);if(!t||t.length<8||t.length>420)continue;
                if(uiRe.test(t))continue;
                const childText=Array.from(e.children||[]).map(c=>text(c)).filter(Boolean).join(' ');
                if(childText&&childText.length>=t.length*.82)continue;
                const key=t.replace(/\s+/g,'').slice(0,180);if(seen.has(key))continue;seen.add(key);
                out.push({type:'text',text:t,top,left,w:Math.round(r.width),h:Math.round(r.height)});
                if(out.length>=180)break;
                }
                return out.sort((a,b)=>a.top-b.top);
                }
                let arr=collect(false);if(arr.length<3)arr=collect(true);if(arr.length>520)arr=arr.slice(0,520);
                if(!arr.length){document.title=MARK+' failed 0 images';return}
                const firstImageTop=arr.length?arr[0].top:detailTop;
                const texts=collectTexts().filter(o=>o.top>=firstImageTop-40).slice(0,80);
                const blocks=arr.map(o=>Object.assign({type:'image'},o)).concat(texts).sort((a,b)=>(a.top-b.top)||((a.left||0)-(b.left||0)));
                try{const payloadObj={marker:MARK,count:arr.length,textCount:texts.length,images:arr,texts,blocks};window.__JEPUM_NAVER_DETAIL_PAYLOAD=payloadObj;sessionStorage.setItem('__JEPUM_NAVER_DETAIL_PAYLOAD',JSON.stringify(payloadObj));}catch(_){}
                document.title=MARK+' '+arr.length+' images '+texts.length+' texts';
                scrollTo(0,0);
                })()
                """
                script = "".join(line.strip() for line in script.splitlines())
                script = script.replace("__DOWNLOAD_NAME__", download_name)
                script = script.replace("__ALREADY_EXPANDED__", "true" if screen_detail_expanded else "false")
                if not _run_address_bar_javascript(script, wait_sec=1.0):
                    return False
                title_text = ""
                deadline = time.time() + 95.0
                while time.time() < deadline:
                    title_text = win32gui.GetWindowText(hwnd_target) or ""
                    if marker in title_text and (" images" in title_text or " failed" in title_text):
                        break
                    time.sleep(0.35)
                if marker not in title_text or " failed" in title_text or " images" not in title_text:
                    result.setdefault("diagnostics", {}).update({
                        "naver_rebuild_verified": False,
                        "naver_rebuild_window_title": title_text[:180],
                    })
                    logger.info("[Detail] Naver rebuild did not verify for %s: %s", product_id, title_text)
                    return False
                image_count = 0
                count_match = re.search(r"(\d+)\s+images", title_text)
                if count_match:
                    try:
                        image_count = int(count_match.group(1))
                    except Exception:
                        image_count = 0
                json_target = os.path.join(output_dir, f"{product_id}_naver_images.json")
                payload = None
                try:
                    import json
                    payload_message = _cdp_eval(
                        "(window.__JEPUM_NAVER_DETAIL_PAYLOAD || JSON.parse(sessionStorage.getItem('__JEPUM_NAVER_DETAIL_PAYLOAD') || 'null'))",
                        timeout=8,
                    )
                    payload_value = (((payload_message or {}).get("result") or {}).get("result") or {}).get("value")
                    if isinstance(payload_value, dict):
                        payload = payload_value
                        with open(json_target, "w", encoding="utf-8") as handle:
                            json.dump(payload, handle, ensure_ascii=False)
                except Exception as exc:
                    result.setdefault("diagnostics", {})["naver_payload_read_error"] = str(exc)
                if payload is None and download_path.exists():
                    try:
                        shutil.move(str(download_path), json_target)
                    except Exception:
                        json_target = str(download_path)
                    try:
                        import json
                        with open(json_target, "r", encoding="utf-8") as handle:
                            payload = json.load(handle)
                    except Exception as exc:
                        result.setdefault("diagnostics", {})["naver_legacy_json_error"] = str(exc)
                        payload = None
                if payload is not None:
                    try:
                        import io
                        import json
                        from urllib.request import Request, urlopen

                        image_items = payload.get("images") or []
                        block_items = payload.get("blocks") or []
                        if not block_items:
                            block_items = [{"type": "image", **item} for item in image_items]
                        text_items = [item for item in block_items if item.get("type") == "text"]
                        loaded_images: Dict[str, Image.Image] = {}
                        headers = {
                            "User-Agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
                            ),
                            "Referer": product_url,
                        }
                        for item in image_items[:560]:
                            src = str(item.get("src") or "").strip()
                            if not src:
                                continue
                            try:
                                request = Request(src, headers=headers)
                                with urlopen(request, timeout=20) as response:
                                    data = response.read()
                                img = Image.open(io.BytesIO(data)).convert("RGB")
                                img.load()
                                if img.width < 120 or img.height < 90:
                                    img.close()
                                    continue
                                loaded_images[src.split("?")[0]] = img
                            except Exception as exc:
                                logger.debug("[Detail] Naver rebuilt image download skipped: %s", exc)
                        if loaded_images:
                            def _font(size: int, bold: bool = False):
                                candidates = [
                                    r"C:\Windows\Fonts\malgunbd.ttf" if bold else r"C:\Windows\Fonts\malgun.ttf",
                                    r"C:\Windows\Fonts\malgun.ttf",
                                    r"C:\Windows\Fonts\arial.ttf",
                                ]
                                for font_path in candidates:
                                    if not font_path:
                                        continue
                                    try:
                                        return ImageFont.truetype(font_path, size=size)
                                    except Exception:
                                        pass
                                return ImageFont.load_default()

                            text_font = _font(28)
                            text_bold = _font(30, bold=True)
                            max_width = min(max(max(img.width for img in loaded_images.values()), 900), 1000)
                            measure = Image.new("RGB", (max_width, 80), "white")
                            measure_draw = ImageDraw.Draw(measure)

                            def _text_width(value: str, font) -> int:
                                try:
                                    box = measure_draw.textbbox((0, 0), value, font=font)
                                    return int(box[2] - box[0])
                                except Exception:
                                    return len(value) * 14

                            def _wrap_text(value: str, font, limit: int) -> List[str]:
                                value = re.sub(r"\s+", " ", str(value or "")).strip()
                                if not value:
                                    return []
                                tokens = re.split(r"(\s+)", value)
                                lines: List[str] = []
                                current = ""
                                for token in tokens:
                                    if not token:
                                        continue
                                    candidate = current + token if current else token.strip()
                                    if _text_width(candidate, font) <= limit:
                                        current = candidate
                                        continue
                                    if current:
                                        lines.append(current.strip())
                                        current = ""
                                    token = token.strip()
                                    if not token:
                                        continue
                                    if _text_width(token, font) <= limit:
                                        current = token
                                        continue
                                    chunk = ""
                                    for ch in token:
                                        if _text_width(chunk + ch, font) <= limit:
                                            chunk += ch
                                        else:
                                            if chunk:
                                                lines.append(chunk)
                                            chunk = ch
                                    current = chunk
                                if current.strip():
                                    lines.append(current.strip())
                                return lines[:18]

                            def _render_text_segment(value: str, index: int) -> Image.Image | None:
                                lines = _wrap_text(value, text_bold if index == 0 else text_font, max_width - 140)
                                if not lines:
                                    return None
                                font = text_bold if index == 0 else text_font
                                line_h = max(34, int(getattr(font, "size", 28) * 1.45))
                                seg_h = 44 + len(lines) * line_h + 34
                                seg = Image.new("RGB", (max_width, seg_h), "white")
                                draw = ImageDraw.Draw(seg)
                                y_text = 34
                                for line in lines:
                                    tw = _text_width(line, font)
                                    x = max(56, (max_width - tw) // 2)
                                    draw.text((x, y_text), line, fill=(35, 35, 35), font=font)
                                    y_text += line_h
                                return seg

                            prepared: List[Image.Image] = []
                            image_segments = 0
                            text_segments = 0
                            for block in block_items[:900]:
                                block_type = str(block.get("type") or "image")
                                if block_type == "text":
                                    segment = _render_text_segment(str(block.get("text") or ""), text_segments)
                                    if segment is not None:
                                        prepared.append(segment)
                                        text_segments += 1
                                    continue
                                src = str(block.get("src") or "").strip()
                                source = loaded_images.get(src.split("?")[0])
                                if source is None:
                                    continue
                                img = source.copy()
                                if img.width > max_width:
                                    new_height = max(1, int(img.height * (max_width / float(img.width))))
                                    img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
                                prepared.append(img)
                                image_segments += 1

                            if not prepared:
                                for source in loaded_images.values():
                                    img = source.copy()
                                    if img.width > max_width:
                                        new_height = max(1, int(img.height * (max_width / float(img.width))))
                                        img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
                                    prepared.append(img)
                                    image_segments += 1

                            margin_y = 34
                            top_pad = 36
                            bottom_pad = 90
                            chunk_max_height = 60000
                            total_height = top_pad + sum(img.height + margin_y for img in prepared) + bottom_pad
                            fullpage_path = os.path.join(output_dir, f"{product_id}_fullpage.jpg")
                            saved_paths: List[str] = []
                            brightness_values: List[float] = []

                            def _save_rebuild_chunk(path: str, segments: List[Image.Image], height: int) -> float:
                                canvas = Image.new("RGB", (max_width, height), "white")
                                y = top_pad
                                for segment in segments:
                                    x = max(0, (max_width - segment.width) // 2)
                                    canvas.paste(segment, (x, y))
                                    y += segment.height + margin_y
                                brightness_value = float(np.array(canvas).mean())
                                canvas.save(path, "JPEG", quality=90)
                                canvas.close()
                                return brightness_value

                            if total_height <= chunk_max_height:
                                brightness_values.append(_save_rebuild_chunk(fullpage_path, prepared, total_height))
                                saved_paths = [fullpage_path]
                            else:
                                try:
                                    if os.path.exists(fullpage_path):
                                        os.remove(fullpage_path)
                                    for old_path in glob.glob(os.path.join(output_dir, "part_*.jpg")):
                                        os.remove(old_path)
                                except Exception:
                                    pass
                                current_segments: List[Image.Image] = []
                                current_height = top_pad + bottom_pad
                                part_index = 1
                                for segment in prepared:
                                    segment_height = segment.height + margin_y
                                    if current_segments and current_height + segment_height > chunk_max_height:
                                        part_path = os.path.join(output_dir, f"part_{part_index:03d}.jpg")
                                        brightness_values.append(_save_rebuild_chunk(part_path, current_segments, current_height))
                                        saved_paths.append(part_path)
                                        part_index += 1
                                        current_segments = []
                                        current_height = top_pad + bottom_pad
                                    current_segments.append(segment)
                                    current_height += segment_height
                                if current_segments:
                                    part_path = os.path.join(output_dir, f"part_{part_index:03d}.jpg")
                                    brightness_values.append(_save_rebuild_chunk(part_path, current_segments, current_height))
                                    saved_paths.append(part_path)

                            for img in prepared:
                                img.close()
                            for source in loaded_images.values():
                                source.close()
                            brightness = float(np.mean(brightness_values)) if brightness_values else 255.0
                            result.setdefault("diagnostics", {}).update({
                                "naver_rebuilt_detail_page": True,
                                "naver_rebuild_verified": True,
                                "naver_rebuilt_image_count": image_count,
                                "naver_rebuilt_downloaded_images": len(loaded_images),
                                "naver_rebuilt_image_segments": image_segments,
                                "naver_rebuilt_text_segments": text_segments,
                                "naver_rebuilt_text_count": len(text_items),
                                "naver_rebuilt_block_count": len(block_items),
                                "naver_rebuild_json": json_target,
                                "stitch_strategy": "image_text_rebuild_chunked" if len(saved_paths) > 1 else "image_text_rebuild",
                                "merged_width": int(max_width),
                                "merged_height": int(total_height),
                                "naver_rebuilt_chunked": len(saved_paths) > 1,
                                "naver_rebuilt_chunks": len(saved_paths),
                                "brightness": round(float(brightness), 1),
                            })
                            quality = _set_result_quality(result, saved_paths[0])
                            if quality.get("ok"):
                                result["screenshots"] = saved_paths
                                result["status"] = "success"
                                return True
                    except Exception as exc:
                        result.setdefault("diagnostics", {})["naver_image_rebuild_error"] = str(exc)
                        logger.debug("[Detail] Naver image rebuild file processing skipped: %s", exc)
                # 이미지 다운로드 실패 — 원본 페이지(document.write 없이 보존됨)에서 screen capture 진행
                try:
                    import pyautogui as _pag
                    _pag.hotkey("ctrl", "home")
                except Exception:
                    pass
                time.sleep(0.5)
                result.setdefault("diagnostics", {}).update({
                    "naver_rebuild_verified": True,
                    "naver_rebuilt_image_count": image_count,
                    "naver_rebuild_fallback_to_screen_capture": True,
                })
                logger.info("[Detail] Naver bookmarklet payload ready, falling back to screen capture for %s", product_id)
                return False

            render_hwnd = None
            def _find_renderer(child_hwnd, _):
                nonlocal render_hwnd
                if win32gui.GetClassName(child_hwnd) == 'Chrome_RenderWidgetHostHWND':
                    render_hwnd = child_hwnd
            try:
                win32gui.EnumChildWindows(hwnd_target, _find_renderer, None)
            except Exception:
                pass

            VK_HOME  = 0x24
            HOME_DN  = 1 | (0x47 << 16) | (1 << 24)
            HOME_UP  = HOME_DN | (1 << 30) | (1 << 31)
            VK_NEXT = 0x22
            NEXT_DN = 1 | (0x51 << 16) | (1 << 24)
            NEXT_UP = NEXT_DN | (1 << 30) | (1 << 31)
            if render_hwnd:
                try:
                    win32api.SendMessage(render_hwnd, win32con.WM_KEYDOWN, VK_HOME, HOME_DN)
                    time.sleep(0.05)
                    win32api.SendMessage(render_hwnd, win32con.WM_KEYUP,   VK_HOME, HOME_UP)
                    time.sleep(0.5)
                except Exception:
                    pass

            if is_naver:
                time.sleep(0.8)
                _dismiss_naver_popup()
                if render_hwnd:
                    try:
                        win32api.SendMessage(render_hwnd, win32con.WM_KEYDOWN, VK_HOME, HOME_DN)
                        time.sleep(0.05)
                        win32api.SendMessage(render_hwnd, win32con.WM_KEYUP,   VK_HOME, HOME_UP)
                        time.sleep(0.35)
                    except Exception:
                        pass
                if is_naver_catalog and not _resolve_naver_catalog_to_seller():
                    result["status"] = "naver_catalog_unresolved"
                    result["reason"] = "Naver catalog page could not be resolved to a seller detail page"
                    return result

            _preexpand_screen_detail()
            if debug_port and is_naver:
                _prepare_screen_capture_layout()
                if _capture_naver_detail_region_via_cdp():
                    return result
                if _capture_fullpage_via_cdp():
                    return result
                _cdp_eval("window.scrollTo(0, 0); document.scrollingElement && (document.scrollingElement.scrollTop = 0); true;")
                time.sleep(0.45)
            if is_naver and _rebuild_naver_detail_page_via_bookmarklet():
                if result.get("status") == "success" and result.get("screenshots"):
                    return result
                page_title = win32gui.GetWindowText(hwnd_target) or page_title
            if debug_port and (is_coupang or is_elevenst or is_auction):
                _prepare_screen_capture_layout()
                # 옥션도 전체 페이지(상단~하단) fullpage CDP 먼저 시도
                if _capture_fullpage_via_cdp():
                    return result
                # fullpage 실패 시 옥션 전용 섹션 타일러로 fallback
                if is_auction and _capture_auction_detail_scroller_via_cdp():
                    return result
                _cdp_eval("window.scrollTo(0, 0); document.scrollingElement && (document.scrollingElement.scrollTop = 0); true;")
                time.sleep(0.35)
                if is_auction:
                    try:
                        import pyautogui as _pag
                        ctypes.windll.user32.SetForegroundWindow(hwnd_target)
                        time.sleep(0.08)
                        if render_hwnd:
                            win32api.SendMessage(render_hwnd, win32con.WM_KEYDOWN, VK_HOME, HOME_DN)
                            time.sleep(0.05)
                            win32api.SendMessage(render_hwnd, win32con.WM_KEYUP, VK_HOME, HOME_UP)
                        reset_msg = _cdp_eval(
                            "(() => {"
                            "window.scrollTo(0, 0);"
                            "document.documentElement.scrollTop = 0;"
                            "document.body && (document.body.scrollTop = 0);"
                            "for (const el of Array.from(document.querySelectorAll('main,section,article,div,iframe')).slice(0, 9000)) {"
                            "  if ((el.scrollHeight - el.clientHeight) > 80) el.scrollTop = 0;"
                            "}"
                            "const detailCandidates = Array.from(document.querySelectorAll('#item_detail_view_js, .box__detail-view, [id*=\"detail_view\"], [class*=\"detail-view\"]'))"
                            "  .map((el) => ({el, rect: el.getBoundingClientRect(), scrollHeight: Math.round(el.scrollHeight || 0), clientHeight: Math.round(el.clientHeight || 0)}))"
                            "  .filter((item) => item.scrollHeight > 500 || item.rect.height > 500)"
                            "  .sort((a, b) => (b.scrollHeight + b.rect.height) - (a.scrollHeight + a.rect.height));"
                            "const detail = detailCandidates[0] && detailCandidates[0].el;"
                            "if (detail) {"
                            "  detail.scrollTop = 0;"
                            "  const scrollables = [];"
                            "  for (let p = detail.parentElement; p; p = p.parentElement) {"
                            "    if ((p.scrollHeight - p.clientHeight) > 80) scrollables.unshift(p);"
                            "  }"
                            "  for (const p of scrollables) {"
                            "    const pr = p.getBoundingClientRect();"
                            "    const dr = detail.getBoundingClientRect();"
                            "    p.scrollTop += Math.round(dr.top - pr.top - 24);"
                            "  }"
                            "  detail.scrollIntoView({block: 'start', inline: 'nearest'});"
                            "  const afterRect = detail.getBoundingClientRect();"
                            "  if (afterRect.top > Math.max(220, window.innerHeight * 0.55)) {"
                            "    window.scrollBy(0, Math.round(afterRect.top - 120));"
                            "  } else if (afterRect.top < 0) {"
                            "    window.scrollBy(0, Math.round(afterRect.top - 16));"
                            "  }"
                            "}"
                            "return detail ? {found: true, top: Math.round(detail.getBoundingClientRect().top), scrollHeight: Math.round(detail.scrollHeight || 0), clientHeight: Math.round(detail.clientHeight || 0), candidates: detailCandidates.length} : {found: false, candidates: detailCandidates.length};"
                            "})()",
                            timeout=8,
                        )
                        reset_value = (((reset_msg or {}).get("result") or {}).get("result") or {}).get("value") or {}
                        time.sleep(0.55)
                        logger.info("[Detail] Auction visible window moved to detail before screen capture: %s", reset_value)
                    except Exception as exc:
                        logger.debug("[Detail] Auction visible top reset skipped: %s", exc)
            elif debug_port and is_gmarket:
                # G마켓은 창 스크롤 캡처가 가장 안정적이다. CDP는 스크롤/레이아웃 정리만 맡긴다.
                _prepare_screen_capture_layout()
                _cdp_eval("window.scrollTo(0, 0); document.scrollingElement && (document.scrollingElement.scrollTop = 0); true;")
                time.sleep(0.35)

            VK_SPACE = 0x20
            SPACE_DN = 1 | (0x39 << 16)
            SPACE_UP = SPACE_DN | (1 << 30) | (1 << 31)
            SCROLL_COUNT = 18
            naver_expected_scrolls = 0
            auction_expected_scrolls = 0
            auction_reached_bottom = False
            gmarket_expected_scrolls = 0
            gmarket_reached_bottom = False
            estimated_step = 420   # CDP 없을 때 스크롤량 추정용 기본값
            scroll_height = 0
            scroll_height = 0
            viewport_height = 0
            if is_naver:
                SCROLL_COUNT = 78
                metrics = _scroll_metrics_via_cdp()
                scroll_height = int(metrics.get("scrollHeight") or 0)
                viewport_height = int(metrics.get("innerHeight") or 0)
                if viewport_height:
                    # CDP 있음: CDP scroll과 동일하게 0.68 배율
                    estimated_step = max(420, int(viewport_height * 0.68))
                else:
                    # CDP 없음: PageDown ≈ viewport * 0.88 (win_h - CHROME_UI로 추정)
                    estimated_step = max(420, int((win_h - CHROME_UI) * 0.88))
                if scroll_height and estimated_step:
                    naver_expected_scrolls = int((scroll_height + estimated_step - 1) // estimated_step) + 4
                    SCROLL_COUNT = max(32, min(160, naver_expected_scrolls))
            elif is_auction:
                metrics = _scroll_metrics_via_cdp()
                scroll_height = int(metrics.get("scrollHeight") or 0)
                viewport_height = int(metrics.get("innerHeight") or 0)
                if viewport_height:
                    estimated_step = max(520, int(viewport_height * 0.82))
                else:
                    estimated_step = max(520, int((win_h - CHROME_UI) * 0.82))
                if scroll_height and estimated_step:
                    auction_expected_scrolls = int((scroll_height + estimated_step - 1) // estimated_step) + 8
                    SCROLL_COUNT = max(34, min(160, auction_expected_scrolls))
                else:
                    SCROLL_COUNT = 72
            elif is_gmarket:
                metrics = _scroll_metrics_via_cdp()
                scroll_height = int(metrics.get("scrollHeight") or 0)
                viewport_height = int(metrics.get("innerHeight") or 0)
                if viewport_height:
                    estimated_step = max(520, int(viewport_height * 0.82))
                else:
                    estimated_step = max(520, int((win_h - CHROME_UI) * 0.82))
                if scroll_height and estimated_step:
                    gmarket_expected_scrolls = int((scroll_height + estimated_step - 1) // estimated_step) + 6
                    SCROLL_COUNT = max(22, min(120, gmarket_expected_scrolls))
                else:
                    SCROLL_COUNT = 36
            screenshots = []
            duplicate_tail_frames = 0
            scroll_positions: List[int] = []
            scroll_page_heights: List[int] = []

            for _si in range(SCROLL_COUNT + 1):
                if _si == 0:
                    _dismiss_naver_popup()
                try:
                    frame_metrics = _scroll_metrics_via_cdp() if debug_port and (is_naver or is_auction or is_gmarket) else {}
                    full_img = _print_window_capture(hwnd_target)
                    content = full_img.crop((0, CHROME_UI, win_w, win_h))
                    if screenshots:
                        diff = _screen_frame_diff(screenshots[-1], content)
                        if diff < 0.8:
                            duplicate_tail_frames += 1
                            min_duplicate_frames = 6 if is_naver else (8 if is_auction else 3)
                            near_gmarket_bottom = False
                            if is_gmarket and frame_metrics:
                                current_y = int(frame_metrics.get("scrollY") or 0)
                                current_inner = int(frame_metrics.get("innerHeight") or viewport_height or 0)
                                current_height = int(frame_metrics.get("scrollHeight") or scroll_height or 0)
                                near_gmarket_bottom = bool(current_height and current_inner and current_y + current_inner >= current_height - 24)
                            if (
                                len(screenshots) >= min_duplicate_frames
                                and duplicate_tail_frames >= 3
                                and (not is_gmarket or near_gmarket_bottom)
                            ):
                                logger.info(
                                    "[Detail] stopped screen capture at duplicate tail frame %s (diff=%.2f)",
                                    _si,
                                    diff,
                                )
                                content.close()
                                full_img.close()
                                break
                        else:
                            duplicate_tail_frames = 0
                    screenshots.append(content)
                    if is_naver:
                        if frame_metrics:
                            # CDP 성공: 정확한 절대 scrollY 사용
                            scroll_positions.append(int(frame_metrics.get("scrollY") or 0))
                            scroll_page_heights.append(int(frame_metrics.get("scrollHeight") or scroll_height))
                        else:
                            # CDP 없음: estimated_step 누적으로 추정
                            scroll_positions.append(_si * estimated_step)
                            scroll_page_heights.append(scroll_height)
                    elif is_auction and frame_metrics:
                        # Auction CDP: 정확한 절대 scrollY 사용 (심리스 스티칭용)
                        scroll_positions.append(int(frame_metrics.get("scrollY") or 0))
                        scroll_page_heights.append(int(frame_metrics.get("scrollHeight") or scroll_height))
                    elif is_gmarket and frame_metrics:
                        scroll_positions.append(int(frame_metrics.get("scrollY") or 0))
                        scroll_page_heights.append(int(frame_metrics.get("scrollHeight") or scroll_height))
                    full_img.close()
                    if (is_auction or is_gmarket) and frame_metrics:
                        current_y = int(frame_metrics.get("scrollY") or 0)
                        current_inner = int(frame_metrics.get("innerHeight") or viewport_height or 0)
                        current_height = int(frame_metrics.get("scrollHeight") or scroll_height or 0)
                        if current_height and current_inner and current_y + current_inner >= current_height - 24:
                            if is_auction:
                                auction_reached_bottom = True
                            if is_gmarket:
                                gmarket_reached_bottom = True
                            logger.info(
                                "[Detail] %s screen capture reached bottom at frame %s y=%s inner=%s height=%s",
                                "Auction" if is_auction else "Gmarket",
                                _si,
                                current_y,
                                current_inner,
                                current_height,
                            )
                            break
                except Exception as e:
                    logger.warning(f"[{product_id}] 캡처 {_si} 실패: {e}")

                if _si < SCROLL_COUNT:
                    if debug_port and (is_coupang or is_naver or is_elevenst or is_gmarket or is_auction) and _scroll_page_via_cdp():
                        pass
                    elif is_coupang and scroll_driver == "ahk":
                        _run_ahk_scroll(hwnd_target, steps=1, delay_ms=0, home_first=False)
                    else:
                        try:
                            import pyautogui as _pag
                            left, top, right, bottom = win32gui.GetWindowRect(hwnd_target)
                            try:
                                ctypes.windll.user32.SetForegroundWindow(hwnd_target)
                            except Exception:
                                pass
                            if is_naver:
                                if render_hwnd:
                                    try:
                                        win32api.SendMessage(render_hwnd, win32con.WM_KEYDOWN, VK_NEXT, NEXT_DN)
                                        time.sleep(0.04)
                                        win32api.SendMessage(render_hwnd, win32con.WM_KEYUP, VK_NEXT, NEXT_UP)
                                    except Exception:
                                        _pag.press("pagedown")
                                else:
                                    _pag.press("pagedown")
                            elif is_elevenst:
                                _pag.press("esc")
                                time.sleep(0.05)
                                _pag.click(
                                    left + int((right - left) * 0.34),
                                    top + CHROME_UI + min(520, max(260, (bottom - top - CHROME_UI) // 2)),
                                )
                                time.sleep(0.05)
                                _pag.press("pagedown")
                            elif is_auction:
                                logger.info("[Detail] Auction physical scroll fallback at frame %s", _si)
                                _pag.press("esc")
                                time.sleep(0.05)
                                focus_x = left + int((right - left) * 0.42)
                                focus_y = top + CHROME_UI + min(540, max(300, (bottom - top - CHROME_UI) // 2))
                                try:
                                    win32api.SetCursorPos((int(focus_x), int(focus_y)))
                                    time.sleep(0.03)
                                    win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, -720, 0)
                                    time.sleep(0.06)
                                    win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, -720, 0)
                                except Exception:
                                    _pag.moveTo(focus_x, focus_y)
                                    _pag.scroll(-7)
                                time.sleep(0.08)
                                _pag.press("pagedown")
                            else:
                                focus_x = left + int((right - left) * 0.45)
                                focus_y = top + CHROME_UI + min(520, max(260, (bottom - top - CHROME_UI) // 2))
                                _pag.click(focus_x, focus_y)
                                time.sleep(0.05)
                                _pag.press("pagedown")
                                time.sleep(0.12)
                                _pag.press("esc")
                        except Exception:
                            try:
                                import pyautogui as _pag
                                _pag.press('pagedown')
                            except Exception:
                                pass
                    time.sleep(0.7)

            if not screenshots:
                return result
            if len(screenshots) < 3:
                for image in screenshots:
                    image.close()
                result["status"] = "incomplete_capture"
                return result
            progress_stats = _screenshot_progress_stats(screenshots)
            updated_diagnostics = dict(result.get("diagnostics") or {})
            updated_diagnostics.update({
                "capture_version": DETAIL_CAPTURE_VERSION,
                "scroll_driver": scroll_driver,
                "scroll_count_limit": SCROLL_COUNT,
                "naver_expected_scrolls": naver_expected_scrolls,
                "auction_expected_scrolls": auction_expected_scrolls,
                "auction_reached_bottom": bool(auction_reached_bottom),
                "gmarket_expected_scrolls": gmarket_expected_scrolls,
                "gmarket_reached_bottom": bool(gmarket_reached_bottom),
                "initial_scroll_height": int(scroll_height or 0),
                "initial_viewport_height": int(viewport_height or 0),
                "target_title": page_title,
                "detail_expanded": bool(screen_detail_expanded),
                "naver_rebuilt_detail_page": bool(naver_rebuilt_detail_page),
                **progress_stats,
            })
            result["diagnostics"] = updated_diagnostics
            if not progress_stats.get("ok"):
                logger.warning(f"[{product_id}] screen capture scroll stalled ({method_name})")
                # Gmarket/Auction scroll stall = 높은 확률로 CF 캡챠 페이지
                # → Playwright로 넘어가기 전에 보이는 Chrome 창에서 AHK 클릭 먼저 시도
                if (is_gmarket or is_auction) and method_name == "chrome_screen":
                    _focus_chrome_window_for_captcha(hwnd_target, product_id, "scroll_stalled")
                    logger.warning(f"[{product_id}] Gmarket/Auction stall → AHK CF 클릭 시도 (visible Chrome)")
                    _STALL_CAPTCHA_TITLES = ["다른 페이지", "방문하시겠습니까", "Just a moment", "보안 확인", "잠시만 기다려주십시오", "잠시만 기다리십시오", "원활한 서비스", "간단한 확인"]
                    _cf_ok = _run_ahk_cf_captcha_click(hwnd=hwnd_target, max_wait_sec=12, check_interval_ms=350)
                    if _cf_ok:
                        logger.info(f"[{product_id}] AHK CF 1차 클릭 → 10초간 재확인")
                        resolved = False
                        for _ in range(10):
                            time.sleep(1.0)
                            try:
                                _new_title = win32gui.GetWindowText(hwnd_target)
                                if not any(cw in _new_title for cw in _STALL_CAPTCHA_TITLES):
                                    resolved = True
                                    break
                            except Exception:
                                pass

                        if not resolved:
                            logger.warning(f"[{product_id}] CF 1차 미해제 → 2차 AHK 클릭")
                            _run_ahk_cf_captcha_click(hwnd=hwnd_target, max_wait_sec=8, check_interval_ms=300)
                            for _ in range(10):
                                time.sleep(1.0)
                                try:
                                    _new_title = win32gui.GetWindowText(hwnd_target)
                                    if not any(cw in _new_title for cw in _STALL_CAPTCHA_TITLES):
                                        resolved = True
                                        break
                                except Exception:
                                    pass

                        if resolved:
                            logger.info(f"[{product_id}] CF 해제 확인! → 캡처 재시도를 위해 stall 반환")
                            _show_captcha_success_toast(product_id)
                        else:
                            logger.warning(f"[{product_id}] CF 2차까지 미해제 → security_check 기록")
                            _log_security_check_event(product_id, "gmarket" if is_gmarket else "auction", "chrome_screen", "stall_captcha_failed", product_url)
                    else:
                        logger.warning(f"[{product_id}] AHK CF 탐지 실패 (체크박스 없음)")
                for image in screenshots:
                    image.close()
                result["status"] = "scroll_stalled"
                return result

            crop_left_ratio = 0.0
            crop_right_ratio = 1.0 if is_naver else 0.84
            if is_naver and naver_rebuilt_detail_page:
                if win_w >= 1700:
                    crop_left_ratio = 0.18
                    crop_right_ratio = 0.82
                else:
                    crop_left_ratio = 0.06
                    crop_right_ratio = 0.94
            crop_top_after_first = (48 if naver_rebuilt_detail_page else 72) if is_naver else 0
            if (is_naver or is_auction) and len(scroll_positions) == len(screenshots) and len(set(scroll_positions)) >= 3:
                merged, stitch_diagnostics = _stitch_screen_frames_by_scroll_positions(
                    screenshots,
                    scroll_positions,
                    page_height=max(scroll_page_heights or [0]),
                    crop_right_ratio=crop_right_ratio,
                    crop_left_ratio=crop_left_ratio,
                    crop_top_after_first=crop_top_after_first,
                    crop_bottom_each_frame=2,
                )
            else:
                merged, stitch_diagnostics = _stitch_screen_frames(
                    screenshots,
                    crop_right_ratio=crop_right_ratio,
                    crop_left_ratio=crop_left_ratio,
                    crop_top_after_first=crop_top_after_first,
                    crop_bottom_each_frame=0 if is_naver else 6,
                    soften_boundaries=False,
                    boundary_blend_px=0 if is_naver else 3,
                    force_crop_top_after_first=False,
                    max_overlap_diff=52.0 if is_naver else 14.0,
                    match_left_ratio=0.12 if is_naver else 0.16,
                    match_right_ratio=0.66 if is_naver else 0.84,
                    max_overlap_limit=780 if is_naver else 360,
                    remove_boundary_px=(8 if naver_rebuilt_detail_page else 18) if is_naver else 0,
                )
            result["diagnostics"].update(stitch_diagnostics)
            for image in screenshots:
                try:
                    image.close()
                except Exception:
                    pass

            brightness = np.array(merged).mean()
            if brightness < 15:
                merged.close()
                return result

            fullpage_path = os.path.join(output_dir, f"{product_id}_fullpage.jpg")
            merged.save(fullpage_path, "JPEG", quality=85)
            postprocess_info = {}
            if is_naver:
                postprocess_info.update(_postprocess_naver_capture(fullpage_path))
            result["diagnostics"].update({
                "merged_width": int(merged.width),
                "merged_height": int(merged.height),
                "brightness": round(float(brightness), 1),
                **postprocess_info,
            })
            quality = _set_result_quality(result, fullpage_path)
            if not quality.get("ok"):
                merged.close()
                logger.warning("[Detail] Screen screenshot rejected for %s: %s", product_id, quality)
                result["status"] = "bad_screenshot"
                return result
            merged.close()
            logger.info(f"[{product_id}] 화면 캡처 완료: 밝기={brightness:.1f}")
            result["screenshots"] = [fullpage_path]
            result["status"] = "success"
            return result

        except Exception as e:
            logger.error(f"[{product_id}] 화면 캡처 오류: {e}")
            return result

        finally:
            try:
                if hwnd_target:
                    try:
                        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
                        time.sleep(0.05)
                        ctypes.windll.user32.SetForegroundWindow(hwnd_target)
                        time.sleep(0.3)
                    except Exception:
                        pass
                    if keep_window_open:
                        logger.info("[Detail] keeping Chrome window open for manual follow-up: %s", product_id)
                    elif new_window_opened:
                        win32gui.PostMessage(hwnd_target, win32con.WM_CLOSE, 0, 0)
                    elif tab_opened:
                        import pyautogui as _pag
                        _pag.hotkey('ctrl', 'w')
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────────
    #  메인 진입점: 3단계 폴백 전략
    # ──────────────────────────────────────────────────────────────────────
    def _capture_sync(self, product_url: str, product_id: str,
                       slice_height: int = 0, job_id: str = "", platform: str = "") -> Dict:
        product_detail_dir = os.path.join(self.output_dir, str(product_id))
        os.makedirs(product_detail_dir, exist_ok=True)
        product_url = _normalize_detail_url(product_url)

        platform_key = adaptive_learning.normalize_platform(platform, product_url)
        method_order = get_detail_capture_method_order(platform_key, product_url)
        logger.warning(
            "[DetailRuntime] method_order product=%s platform=%s order=%s url=%s source=%s",
            product_id,
            platform_key,
            method_order,
            product_url,
            __file__,
        )
        last_result = {"screenshots": [], "mhtml_path": "", "method": "none", "status": "not_started"}

        for method_index, method in enumerate(method_order):
            started = time.monotonic()
            if method not in {"chrome_screen", "ahk_screen"}:
                try:
                    adaptive_learning.wait_turn_sync(platform_key, "detail_capture", method, product_url)
                except adaptive_learning.CircuitOpen as e:
                    logger.warning(f"[{product_id}] detail skipped by cooldown: {e.reason}")
                    last_result = {
                        "screenshots": [],
                        "mhtml_path": "",
                        "method": method,
                        "status": "cooldown",
                        "reason": e.reason,
                    }
                    adaptive_learning.log_event(
                        job_id=job_id,
                        stage="detail_capture",
                        platform=platform_key,
                        method=method,
                        status="cooldown_skip",
                        success=False,
                        url=product_url,
                        message=e.reason,
                        metadata={"product_id": product_id, "until": e.until},
                    )
                    has_screen_fallback = any(
                        fallback in {"chrome_screen", "ahk_screen"}
                        for fallback in method_order[method_index + 1:]
                    )
                    if has_screen_fallback:
                        continue
                    return last_result

            try:
                if method in {"chrome_screen", "ahk_screen"}:
                    logger.info(f"[{product_id}] chrome screen capture...")
                    scroll_driver = "ahk" if method == "ahk_screen" else "win32"
                    result = self._capture_naver_via_screen(
                        product_url,
                        product_id,
                        product_detail_dir,
                        scroll_driver=scroll_driver,
                        job_id=job_id,
                    )
                    result = self._apply_slice(result, product_detail_dir, slice_height)
                elif method == "drission":
                    logger.info(f"[{product_id}] DrissionPage capture...")
                    result = self._capture_drission(product_url, product_id, product_detail_dir, slice_height)
                else:
                    logger.info(f"[{product_id}] Playwright capture...")
                    result = self._capture_playwright(product_url, product_id, product_detail_dir, is_naver=(platform_key == "naver"))
                    result = self._apply_slice(result, product_detail_dir, slice_height)

                result["method"] = result.get("method") or method
                success = bool(result.get("screenshots"))
                if success and not is_detail_result_usable(result):
                    logger.warning("[Detail] rejecting unusable detail result for %s via %s", product_id, method)
                    result["screenshots"] = []
                    result["status"] = "bad_screenshot"
                    success = False
                status = result.get("status") or ("success" if success else "empty_or_blocked")
                if not success and status == "started":
                    status = "empty_or_blocked"
                if not success:
                    result["status"] = status
                adaptive_learning.record_method_result(
                    platform=platform_key,
                    stage="detail_capture",
                    method=result.get("method", method),
                    success=success,
                    status=status,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    job_id=job_id,
                    url=product_url,
                    metadata={
                        "product_id": product_id,
                        "screenshots": len(result.get("screenshots") or []),
                        **(result.get("diagnostics") or {}),
                    },
                )
                last_result = result
                if (
                    not success
                    and platform_key == "naver"
                    and method in {"chrome_screen", "ahk_screen"}
                    and status in {"login_required", "wrong_window", "manual_required"}
                ):
                    logger.warning(
                        "[Detail] stopping Naver detail fallbacks for %s after screen status=%s",
                        product_id,
                        status,
                    )
                    return result
                # G마켓/옥션: 보안 확인(captcha) 감지 시 fallback 엔진 낭비 방지
                # → 즉시 중단하고 재시도 큐로 보냄
                if (
                    not success
                    and platform_key in {"gmarket", "auction"}
                    and status in {"captcha_blocked", "blocked", "scroll_stalled"}
                ):
                    _log_security_check_event(product_id, platform_key, method, status, product_url)
                    logger.warning(
                        "[Detail] G마켓/옥션 보안 확인 감지 → fallback 중단: %s status=%s method=%s",
                        product_id, status, method,
                    )
                    return result
                if success:
                    return result
            except Exception as e:
                last_result = {
                    "screenshots": [],
                    "mhtml_path": "",
                    "method": method,
                    "status": adaptive_learning.classify_exception(e),
                    "reason": str(e),
                }
                adaptive_learning.record_method_result(
                    platform=platform_key,
                    stage="detail_capture",
                    method=method,
                    success=False,
                    status=last_result["status"],
                    duration_ms=int((time.monotonic() - started) * 1000),
                    job_id=job_id,
                    url=product_url,
                    message=str(e),
                    metadata={"product_id": product_id},
                )
                logger.warning(f"[{product_id}] {method} failed: {e}")

        return last_result

    def _apply_slice(self, result: Dict, output_dir: str, slice_height: int) -> Dict:
        if not result.get("screenshots") or slice_height <= 0:
            return result
        new_shots = []
        for p in result["screenshots"]:
            if os.path.exists(p):
                chunks = self._slice_image(p, output_dir, slice_height)
                new_shots.extend(chunks)
        result["screenshots"] = new_shots
        return result

    # ──────────────────────────────────────────────────────────────────────
    #  유틸리티
    # ──────────────────────────────────────────────────────────────────────
    def _scroll_capture_fallback(self, page, output_dir: str,
                                  product_id: str, body_height: int) -> str:
        viewport_h = page.run_js("return window.innerHeight") or 1080
        num_chunks = max(1, min((body_height // viewport_h) + 1, 30))

        chunk_imgs = []
        page.run_js("window.scrollTo(0, 0)")
        time.sleep(0.5)

        for i in range(num_chunks):
            y = i * viewport_h
            page.run_js(f"window.scrollTo(0, {y})")
            time.sleep(0.4)
            try:
                chunk_name = f"_chunk_{i:03d}.png"
                page.get_screenshot(path=output_dir, name=chunk_name)
                chunk_path = os.path.join(output_dir, chunk_name)
                if os.path.exists(chunk_path):
                    chunk_imgs.append(chunk_path)
            except Exception as e:
                logger.warning(f"Chunk {i} 캡처 실패: {e}")

        if not chunk_imgs:
            return ""

        pil_imgs = [Image.open(p).convert("RGB") for p in chunk_imgs]
        total_h = sum(im.height for im in pil_imgs)
        merged = Image.new("RGB", (pil_imgs[0].width, total_h))
        y_offset = 0
        for im in pil_imgs:
            merged.paste(im, (0, y_offset))
            y_offset += im.height
        for im in pil_imgs:
            im.close()

        fullpage_path = os.path.join(output_dir, f"{product_id}_fullpage.jpg")
        merged.save(fullpage_path, "JPEG", quality=85)
        merged.close()

        for p in chunk_imgs:
            try: os.remove(p)
            except Exception: pass

        return fullpage_path

    def _slice_image(self, fullpage_path: str, output_dir: str,
                      slice_height: int) -> List[str]:
        for old in glob.glob(os.path.join(output_dir, "part_*.jpg")):
            try: os.remove(old)
            except Exception: pass

        img = Image.open(fullpage_path)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        w, h = img.size
        img_arr = np.array(img)
        row_variance = np.std(img_arr, axis=(1, 2))

        search_range = 150
        current_y = 0
        part_idx = 1
        chunks = []

        while current_y < h:
            target_y = current_y + slice_height
            if target_y >= h:
                crop = img.crop((0, current_y, w, h))
                part_path = os.path.join(output_dir, f"part_{part_idx:03d}.jpg")
                crop.save(part_path, "JPEG", quality=85)
                chunks.append(part_path)
                break

            search_start = max(current_y + 50, target_y - search_range)
            search_end = min(h - 1, target_y + search_range)
            sub = row_variance[search_start:search_end]
            best_y = search_start + int(np.argmin(sub)) if len(sub) > 0 else target_y

            crop = img.crop((0, current_y, w, best_y))
            part_path = os.path.join(output_dir, f"part_{part_idx:03d}.jpg")
            crop.save(part_path, "JPEG", quality=85)
            chunks.append(part_path)

            current_y = best_y
            part_idx += 1

        img.close()
        return chunks

    async def capture_detail_page(
        self, product_url: str, product_id: str, slice_height: int = 0,
        job_id: str = "", platform: str = ""
    ) -> Dict:
        """Async wrapper for detail page capture."""
        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()
        result = await loop.run_in_executor(
            None, ctx.run, self._capture_sync, product_url, product_id, slice_height, job_id, platform
        )
        return result
