"""
engine/human_behavior.py — 고급 인간형 행동 시뮬레이션
────────────────────────────────────────────────────────────────────────────
봇 탐지 우회 대상:
  • 행동 패턴(Behavior Pattern)
      - 완벽히 직선적인 마우스 이동 → 베지어 곡선 + 엔트로피
      - 일정한 스크롤 속도 → 관성 기반 감속 + 읽기 정지
      - 기계적인 클릭 타이밍 → 가우시안 지연 + 더블클릭 실수
      - 0ms 이벤트 간격 → 자연스러운 이벤트 체인

동기(sync) + 비동기(async) 모두 지원.
"""

from __future__ import annotations

import math
import random
import time
from typing import Optional, Tuple


# ─── 수학 유틸 ───────────────────────────────────────────────────────────────

def _bezier(t: float, p0, p1, p2, p3) -> Tuple[float, float]:
    """3차 베지어 곡선 위의 점."""
    u = 1 - t
    x = u**3*p0[0] + 3*u**2*t*p1[0] + 3*u*t**2*p2[0] + t**3*p3[0]
    y = u**3*p0[1] + 3*u**2*t*p1[1] + 3*u*t**2*p2[1] + t**3*p3[1]
    return x, y


def _ease_in_out(t: float) -> float:
    """Ease in-out (자연스러운 가속-감속)."""
    return t * t * (3 - 2 * t)


def _gaussian_delay(mean: float, std: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, random.gauss(mean, std)))


def _generate_mouse_path(
    start: Tuple[float, float],
    end:   Tuple[float, float],
    steps: int   = 30,
    wobble: float = 0.25,
) -> list:
    """
    베지어 곡선 기반 마우스 경로 생성.
    - wobble: 중간 제어점의 흔들림 정도 (0=직선, 1=크게 흔들)
    - 작은 가우시안 노이즈 추가 → 완벽히 매끄럽지 않게
    """
    dx   = end[0] - start[0]
    dy   = end[1] - start[1]
    dist = math.hypot(dx, dy)

    # 이동 거리가 작으면 단순 직선
    if dist < 5:
        return [end]

    cp1 = (
        start[0] + dx * random.uniform(0.15, 0.45)
        + random.gauss(0, max(3, dist * wobble * 0.08)),
        start[1] + dy * random.uniform(0.15, 0.45)
        + random.gauss(0, max(3, dist * wobble * 0.08)),
    )
    cp2 = (
        start[0] + dx * random.uniform(0.55, 0.85)
        + random.gauss(0, max(3, dist * wobble * 0.08)),
        start[1] + dy * random.uniform(0.55, 0.85)
        + random.gauss(0, max(3, dist * wobble * 0.08)),
    )

    path = []
    for i in range(steps + 1):
        t       = i / steps
        t_eased = _ease_in_out(t)
        x, y    = _bezier(t_eased, start, cp1, cp2, end)
        # 미세 떨림 (인간 손 떨림 모사)
        x += random.gauss(0, 0.4)
        y += random.gauss(0, 0.4)
        path.append((x, y))

    return path


# ─── 동기 버전 ───────────────────────────────────────────────────────────────

class SyncHumanBehavior:
    """
    Playwright sync_playwright 기반 인간형 행동.
    스크래퍼의 동기 코드에서 직접 사용한다.
    """

    def __init__(
        self,
        typing_speed_wpm: int   = 65,
        typo_rate:        float = 0.03,
        scroll_speed:     str   = "medium",
        micro_tremor:     bool  = True,
    ):
        self._char_delay_mean = 60000 / (typing_speed_wpm * 5)   # ms
        self._char_delay_std  = self._char_delay_mean * 0.35
        self._typo_rate       = typo_rate
        self._micro_tremor    = micro_tremor
        self._scroll_speeds   = {
            "slow":   (90,  220),
            "medium": (45,  110),
            "fast":   (18,  55),
        }
        self._scroll_range = self._scroll_speeds.get(scroll_speed, (45, 110))
        self._mouse_pos    = (
            random.randint(300, 900),
            random.randint(200, 600),
        )

    # ── 마우스 이동 ───────────────────────────────────────────

    def move_to(self, page, x: float, y: float, click: bool = False) -> None:
        dist  = math.hypot(x - self._mouse_pos[0], y - self._mouse_pos[1])
        steps = max(12, int(dist / 6))
        path  = _generate_mouse_path(self._mouse_pos, (x, y), steps=steps)

        for px, py in path:
            page.mouse.move(px, py)
            time.sleep(random.uniform(0.003, 0.014))

        # 도착 후 짧은 정지 (목표물 확인하는 시간)
        time.sleep(_gaussian_delay(0.07, 0.04, 0.02, 0.25))
        self._mouse_pos = (x, y)

        if click:
            self.click(page, x, y)

    def click(self, page, x: float, y: float) -> None:
        # 실제 클릭 지점 약간 흔들기
        ax = x + random.gauss(0, 1.8)
        ay = y + random.gauss(0, 1.8)
        page.mouse.move(ax, ay)
        page.mouse.down()
        time.sleep(_gaussian_delay(0.13, 0.04, 0.08, 0.28))
        page.mouse.up()
        # 클릭 후 미세 떨림 (손 반동)
        if self._micro_tremor:
            for _ in range(random.randint(2, 4)):
                page.mouse.move(
                    ax + random.gauss(0, 1.2),
                    ay + random.gauss(0, 1.2),
                )
                time.sleep(random.uniform(0.01, 0.03))

    def click_element(self, page, selector: str) -> bool:
        try:
            el  = page.query_selector(selector)
            if not el:
                return False
            box = el.bounding_box()
            if not box:
                return False
            x = box['x'] + box['width']  * random.uniform(0.25, 0.75)
            y = box['y'] + box['height'] * random.uniform(0.25, 0.75)
            self.move_to(page, x, y, click=True)
            return True
        except Exception:
            return False

    def idle_tremor(self, page, duration: float = 0.5) -> None:
        """마우스 커서 미세 떨림 (정지해 있어도 움직이는 척)."""
        end = time.time() + duration
        while time.time() < end:
            dx = random.gauss(0, 2)
            dy = random.gauss(0, 2)
            nx = max(0, self._mouse_pos[0] + dx)
            ny = max(0, self._mouse_pos[1] + dy)
            page.mouse.move(nx, ny)
            self._mouse_pos = (nx, ny)
            time.sleep(random.uniform(0.04, 0.12))

    # ── 스크롤 ────────────────────────────────────────────────

    def scroll(
        self,
        page,
        direction: str    = "down",
        amount:    Optional[int] = None,
        natural:   bool   = True,
    ) -> None:
        if amount is None:
            amount = random.randint(250, 750)
        sign = 1 if direction == "down" else -1

        if natural:
            chunks     = random.randint(4, 12)
            base_chunk = amount / chunks
            velocity   = random.uniform(0.8, 1.3)   # 초기 속도 계수

            for i in range(chunks):
                # 관성 감속: 후반부로 갈수록 느려짐
                decel     = 1.0 - (i / chunks) * random.uniform(0.5, 0.9)
                chunk_px  = int(base_chunk * velocity * decel * random.uniform(0.7, 1.4))
                chunk_px  = max(10, chunk_px)
                page.mouse.wheel(0, sign * chunk_px)

                # 스크롤 속도: 초반 빠름 → 후반 느림
                delay_ms  = self._scroll_range[0] + (
                    (self._scroll_range[1] - self._scroll_range[0]) * (i / chunks)
                )
                time.sleep(delay_ms / 1000 * random.uniform(0.7, 1.4))
        else:
            page.mouse.wheel(0, sign * amount)
            time.sleep(random.uniform(0.08, 0.25))

    def scroll_to_element(self, page, selector: str) -> bool:
        """지정 요소까지 자연스럽게 스크롤한다."""
        try:
            el  = page.query_selector(selector)
            if not el:
                return False
            box = el.bounding_box()
            if not box:
                return False
            viewport_h = page.viewport_size['height'] if page.viewport_size else 1080
            target_y   = box['y']
            current_y  = page.evaluate("window.scrollY") or 0
            diff        = target_y - current_y - viewport_h * 0.3

            chunks = random.randint(5, 12)
            per    = diff / chunks
            for i in range(chunks):
                decel = 1.0 - (i / chunks) * 0.6
                page.mouse.wheel(0, int(per * decel * random.uniform(0.8, 1.2)))
                time.sleep(random.uniform(0.08, 0.18))
            return True
        except Exception:
            return False

    # ── 읽기 세션 시뮬레이션 ─────────────────────────────────

    def reading_session(
        self,
        page,
        duration:   float = 4.0,
        read_pauses: bool  = True,
    ) -> None:
        """
        사람이 페이지를 읽는 것처럼 스크롤한다.
        - 읽기 정지 (1~3초 멈춤)
        - 위로 올라가 다시 읽기 (10% 확률)
        - 화면 중앙 근처에서 마우스 미세 이동
        """
        end_t       = time.time() + duration
        scrolled    = 0
        max_scroll  = 4000
        vp          = page.viewport_size or {'width': 1920, 'height': 1080}

        while time.time() < end_t and scrolled < max_scroll:
            # 읽기 정지
            if read_pauses and random.random() < 0.18:
                pause = _gaussian_delay(1.8, 0.7, 0.8, 4.0)
                # 정지 중 마우스 미세 움직임 (텍스트 따라가는 것처럼)
                self.idle_tremor(page, duration=pause * 0.3)
                time.sleep(pause * 0.7)

            # 위로 올라가기 (재독)
            if random.random() < 0.08 and scrolled > 300:
                up_px = random.randint(60, 250)
                self.scroll(page, "up", up_px)
                scrolled -= up_px
                time.sleep(_gaussian_delay(0.8, 0.3, 0.4, 1.5))
                continue

            # 아래로 스크롤
            down_px = random.randint(80, 350)
            self.scroll(page, "down", down_px)
            scrolled += down_px

            # 마우스 가끔 이동 (관심 콘텐츠 포인팅)
            if random.random() < 0.25:
                tx = vp['width']  * random.uniform(0.2, 0.8)
                ty = vp['height'] * random.uniform(0.3, 0.7)
                self.move_to(page, tx, ty)

            time.sleep(random.uniform(0.08, 0.35))

    def natural_page_entry(self, page) -> None:
        """페이지 진입 직후 자연스러운 초기 행동."""
        time.sleep(_gaussian_delay(2.2, 0.7, 1.2, 4.0))
        vp = page.viewport_size or {'width': 1920, 'height': 1080}

        # 마우스를 화면 상단 1/3 영역으로 이동 (주소 표시줄 근처 → 페이지로 이동)
        self.move_to(
            page,
            vp['width']  * random.uniform(0.25, 0.75),
            vp['height'] * random.uniform(0.10, 0.35),
        )
        time.sleep(_gaussian_delay(0.4, 0.15, 0.2, 0.8))

        # 페이지 내용 쪽으로 이동
        self.move_to(
            page,
            vp['width']  * random.uniform(0.3, 0.7),
            vp['height'] * random.uniform(0.3, 0.6),
        )

        # 첫 스크롤 (페이지 확인)
        self.scroll(page, "down", random.randint(80, 250))
        time.sleep(random.uniform(0.4, 1.0))

        # 10% 확률로 위로 다시 올라감 (첫 화면 확인)
        if random.random() < 0.10:
            self.scroll(page, "up", random.randint(40, 120))
            time.sleep(random.uniform(0.2, 0.6))


# ─── 비동기 버전 (async 스크래퍼용) ────────────────────────────────────────────

import asyncio


class HumanBehavior:
    """
    Playwright async 기반 인간형 행동 (비동기 버전).
    기존 코드 호환성을 위해 유지.
    """

    def __init__(
        self,
        typing_speed_wpm: int   = 65,
        typo_rate:        float = 0.03,
        scroll_speed:     str   = "medium",
    ):
        self._char_delay_mean = 60000 / (typing_speed_wpm * 5)
        self._char_delay_std  = self._char_delay_mean * 0.35
        self._typo_rate       = typo_rate
        self._scroll_speeds   = {
            "slow":   (90,  220),
            "medium": (45,  110),
            "fast":   (18,  55),
        }
        self._scroll_range = self._scroll_speeds.get(scroll_speed, (45, 110))

    async def move_to(self, page, x: float, y: float, click: bool = False) -> None:
        current = getattr(page, '_mouse_pos', (
            random.randint(200, 800), random.randint(200, 600)
        ))
        dist  = math.hypot(x - current[0], y - current[1])
        steps = max(12, int(dist / 7))
        path  = _generate_mouse_path(current, (x, y), steps=steps)

        for px, py in path:
            await page.mouse.move(px, py)
            await asyncio.sleep(random.uniform(0.003, 0.013))

        await asyncio.sleep(_gaussian_delay(0.07, 0.04, 0.02, 0.25))
        page._mouse_pos = (x, y)
        if click:
            await self.click(page, x, y)

    async def click(self, page, x: float, y: float) -> None:
        ax = x + random.gauss(0, 1.8)
        ay = y + random.gauss(0, 1.8)
        await page.mouse.move(ax, ay)
        await page.mouse.down()
        await asyncio.sleep(_gaussian_delay(0.13, 0.04, 0.08, 0.28))
        await page.mouse.up()

    async def click_element(self, page, selector: str) -> bool:
        el = await page.query_selector(selector)
        if not el:
            return False
        box = await el.bounding_box()
        if not box:
            return False
        x = box['x'] + box['width']  * random.uniform(0.25, 0.75)
        y = box['y'] + box['height'] * random.uniform(0.25, 0.75)
        await self.move_to(page, x, y, click=True)
        return True

    async def scroll(
        self,
        page,
        direction: str    = "down",
        amount:    Optional[int] = None,
        natural:   bool   = True,
    ) -> None:
        if amount is None:
            amount = random.randint(250, 750)
        sign = 1 if direction == "down" else -1

        if natural:
            chunks    = random.randint(4, 12)
            base      = amount / chunks
            for i in range(chunks):
                factor   = 1.0 - (i / chunks) * random.uniform(0.5, 0.9)
                chunk_px = int(base * factor * random.uniform(0.7, 1.4))
                await page.mouse.wheel(0, sign * chunk_px)
                delay_ms = self._scroll_range[0] + (
                    (self._scroll_range[1] - self._scroll_range[0]) * (i / chunks)
                )
                await asyncio.sleep(delay_ms / 1000 * random.uniform(0.7, 1.4))
        else:
            await page.mouse.wheel(0, sign * amount)
            await asyncio.sleep(random.uniform(0.08, 0.25))

    async def random_scroll_session(
        self,
        page,
        duration: float = 3.0,
        read_pauses: bool = True,
    ) -> None:
        end_t    = asyncio.get_event_loop().time() + duration
        scrolled = 0
        max_sc   = 3500

        while asyncio.get_event_loop().time() < end_t and scrolled < max_sc:
            if read_pauses and random.random() < 0.18:
                await asyncio.sleep(_gaussian_delay(1.5, 0.6, 0.7, 3.5))

            if random.random() < 0.08 and scrolled > 200:
                up = random.randint(60, 220)
                await self.scroll(page, "up", up)
                scrolled -= up
            else:
                down = random.randint(80, 380)
                await self.scroll(page, "down", down)
                scrolled += down

            await asyncio.sleep(random.uniform(0.08, 0.40))

    async def natural_page_entry(self, page) -> None:
        await asyncio.sleep(_gaussian_delay(2.2, 0.7, 1.2, 4.0))
        vp = page.viewport_size or {'width': 1920, 'height': 1080}
        await self.move_to(
            page,
            vp['width']  * random.uniform(0.3, 0.7),
            vp['height'] * random.uniform(0.2, 0.5),
        )
        await self.scroll(page, "down", random.randint(80, 280))
        await asyncio.sleep(random.uniform(0.4, 1.2))
        if random.random() < 0.10:
            await self.scroll(page, "up", random.randint(40, 140))
            await asyncio.sleep(random.uniform(0.2, 0.6))
