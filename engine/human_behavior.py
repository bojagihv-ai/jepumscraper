"""
engine/human_behavior.py - ProScraper 인간형 행동 시뮬레이션
────────────────────────────────────────────────────────────
Bezier 곡선 마우스 이동, Gaussian 타이핑 속도, 관성 스크롤 등
Playwright 페이지에서 봇 탐지를 우회하는 인간다운 행동을 구현한다.
"""

import asyncio
import math
import random
from typing import Optional, Tuple


def _bezier(t: float, p0, p1, p2, p3) -> Tuple[float, float]:
    u = 1 - t
    x = u**3*p0[0] + 3*u**2*t*p1[0] + 3*u*t**2*p2[0] + t**3*p3[0]
    y = u**3*p0[1] + 3*u**2*t*p1[1] + 3*u*t**2*p2[1] + t**3*p3[1]
    return x, y


def _gaussian_delay(mean: float, std: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, random.gauss(mean, std)))


def _generate_mouse_path(
    start: Tuple[float, float],
    end: Tuple[float, float],
    steps: int = 25,
    wobble: float = 0.3,
) -> list:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    dist = math.hypot(dx, dy)

    cp1 = (
        start[0] + dx * random.uniform(0.2, 0.4) + random.gauss(0, dist * wobble * 0.1),
        start[1] + dy * random.uniform(0.2, 0.4) + random.gauss(0, dist * wobble * 0.1),
    )
    cp2 = (
        start[0] + dx * random.uniform(0.6, 0.8) + random.gauss(0, dist * wobble * 0.1),
        start[1] + dy * random.uniform(0.6, 0.8) + random.gauss(0, dist * wobble * 0.1),
    )

    path = []
    for i in range(steps + 1):
        t = i / steps
        t_eased = t * t * (3 - 2 * t)
        x, y = _bezier(t_eased, start, cp1, cp2, end)
        x += random.gauss(0, 0.5)
        y += random.gauss(0, 0.5)
        path.append((x, y))

    return path


class HumanBehavior:
    """Playwright 페이지에 인간다운 행동을 수행하는 클래스."""

    def __init__(
        self,
        typing_speed_wpm: int = 60,
        typo_rate: float = 0.04,
        scroll_speed: str = "medium",
    ) -> None:
        self._char_delay_mean = 60000 / (typing_speed_wpm * 5)
        self._char_delay_std  = self._char_delay_mean * 0.4
        self._typo_rate = typo_rate
        self._scroll_speeds = {
            "slow":   (80,  200),
            "medium": (40,  100),
            "fast":   (15,  50),
        }
        self._scroll_range = self._scroll_speeds.get(scroll_speed, (40, 100))

    async def move_to(self, page, x: float, y: float, click: bool = False) -> None:
        current = getattr(page, '_mouse_pos', (
            random.randint(200, 800), random.randint(200, 600)
        ))
        dist = math.hypot(x - current[0], y - current[1])
        steps = max(10, int(dist / 8))
        path = _generate_mouse_path(current, (x, y), steps=steps)

        for px, py in path:
            await page.mouse.move(px, py)
            await asyncio.sleep(random.uniform(0.003, 0.012))

        await asyncio.sleep(_gaussian_delay(0.08, 0.04, 0.02, 0.3))
        page._mouse_pos = (x, y)

        if click:
            await self.click(page, x, y)

    async def click(self, page, x: float, y: float) -> None:
        actual_x = x + random.gauss(0, 1.5)
        actual_y = y + random.gauss(0, 1.5)
        await page.mouse.move(actual_x, actual_y)
        await page.mouse.down()
        await asyncio.sleep(_gaussian_delay(0.12, 0.04, 0.08, 0.25))
        await page.mouse.up()

    async def click_element(self, page, selector: str) -> bool:
        el = await page.query_selector(selector)
        if not el:
            return False
        box = await el.bounding_box()
        if not box:
            return False
        x = box['x'] + box['width']  * random.uniform(0.3, 0.7)
        y = box['y'] + box['height'] * random.uniform(0.3, 0.7)
        await self.move_to(page, x, y, click=True)
        return True

    async def scroll(
        self,
        page,
        direction: str = "down",
        amount: Optional[int] = None,
        natural: bool = True,
    ) -> None:
        if amount is None:
            amount = random.randint(300, 800)
        sign = 1 if direction == "down" else -1

        if natural:
            chunks = random.randint(4, 10)
            base = amount / chunks
            for i in range(chunks):
                factor = 1.0 - (i / chunks) * 0.6
                chunk_px = int(base * factor * random.uniform(0.7, 1.3))
                await page.mouse.wheel(0, sign * chunk_px)
                delay = self._scroll_range[0] + (self._scroll_range[1] - self._scroll_range[0]) * (i / chunks)
                await asyncio.sleep(delay / 1000)
        else:
            await page.mouse.wheel(0, sign * amount)
            await asyncio.sleep(random.uniform(0.1, 0.3))

    async def random_scroll_session(
        self,
        page,
        duration: float = 3.0,
        read_pauses: bool = True,
    ) -> None:
        end_time = asyncio.get_event_loop().time() + duration
        total_scrolled = 0
        max_scroll = 3000

        while asyncio.get_event_loop().time() < end_time and total_scrolled < max_scroll:
            if read_pauses and random.random() < 0.15:
                await asyncio.sleep(random.uniform(1.0, 3.0))

            if random.random() < 0.1 and total_scrolled > 200:
                scroll_up = random.randint(50, 200)
                await self.scroll(page, "up", scroll_up)
                total_scrolled -= scroll_up
            else:
                scroll_down = random.randint(100, 400)
                await self.scroll(page, "down", scroll_down)
                total_scrolled += scroll_down

            await asyncio.sleep(random.uniform(0.1, 0.5))

    async def natural_page_entry(self, page) -> None:
        await asyncio.sleep(_gaussian_delay(2.5, 0.8, 1.5, 4.0))
        vp = page.viewport_size or {'width': 1920, 'height': 1080}
        await self.move_to(
            page,
            vp['width']  * random.uniform(0.3, 0.7),
            vp['height'] * random.uniform(0.2, 0.5),
        )
        await self.scroll(page, "down", random.randint(100, 300))
        await asyncio.sleep(random.uniform(0.5, 1.5))
        if random.random() < 0.4:
            await self.scroll(page, "up", random.randint(50, 150))
            await asyncio.sleep(random.uniform(0.3, 0.8))
