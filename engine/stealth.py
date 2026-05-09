"""
engine/stealth.py - ProScraper 통합 스텔스 엔진
────────────────────────────────────────────────
ProScraper의 fingerprint.py + advanced_fingerprint.py 통합 버전.
Playwright 페이지에 주입하여 봇 탐지를 최대한 우회한다.

포함된 위장 항목:
  기본: webdriver 제거, 플러그인, 언어, 화면해상도, platform,
        WebGL 렌더러/벤더, Canvas 노이즈, chrome 객체, permissions
  고급: AudioContext, Font Enumeration, Battery Status, CPU코어,
        Device Memory, Connection API, Speech Synthesis, Media Devices,
        Math 지문 노이즈
"""

import random
from typing import Optional

# 실제 사용자 화면 해상도 분포 (StatCounter 2024 기준)
_RESOLUTIONS = [
    (1920, 1080),
    (1366, 768),
    (1536, 864),
    (1440, 900),
    (2560, 1440),
    (1280, 720),
]

# 실제 브라우저가 보고하는 GPU 렌더러 목록
_WEBGL_RENDERERS = [
    "ANGLE (NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0)",
    "ANGLE (Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0)",
    "ANGLE (AMD Radeon RX 6600 XT Direct3D11 vs_5_0 ps_5_0)",
    "ANGLE (NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0)",
]
_WEBGL_VENDORS = [
    "Google Inc. (NVIDIA)",
    "Google Inc. (Intel)",
    "Google Inc. (AMD)",
]

_WINDOWS_FONTS = [
    "Arial", "Arial Black", "Calibri", "Cambria", "Comic Sans MS",
    "Consolas", "Courier New", "Georgia", "Impact", "Lucida Console",
    "Microsoft Sans Serif", "Palatino Linotype", "Segoe UI", "Symbol",
    "Tahoma", "Times New Roman", "Trebuchet MS", "Verdana", "Webdings",
    "Wingdings", "맑은 고딕", "나눔고딕", "바탕", "굴림", "돋움",
]


def get_stealth_script() -> str:
    """
    Playwright page.add_init_script()에 주입할 기본 스텔스 JS.
    호출마다 다른 파라미터로 고유한 지문을 생성한다.
    """
    w, h = random.choice(_RESOLUTIONS)
    renderer = random.choice(_WEBGL_RENDERERS)
    vendor = random.choice(_WEBGL_VENDORS)
    noise = random.uniform(0.00001, 0.0001)
    platform = random.choice(["Win32", "Win64"])

    return f"""
    // 1. webdriver 플래그 제거
    Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});

    // 2. 플러그인 목록 — 실제 Chrome처럼 보이게
    Object.defineProperty(navigator, 'plugins', {{
        get: () => {{
            const arr = [
                {{ name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' }},
                {{ name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' }},
                {{ name: 'Native Client', filename: 'internal-nacl-plugin' }},
            ];
            arr.__proto__ = PluginArray.prototype;
            return arr;
        }}
    }});

    // 3. 언어 설정
    Object.defineProperty(navigator, 'languages', {{
        get: () => ['ko-KR', 'ko', 'en-US', 'en']
    }});

    // 4. 화면 해상도
    Object.defineProperty(screen, 'width', {{ get: () => {w} }});
    Object.defineProperty(screen, 'height', {{ get: () => {h} }});
    Object.defineProperty(screen, 'availWidth', {{ get: () => {w} }});
    Object.defineProperty(screen, 'availHeight', {{ get: () => {h - 40} }});
    Object.defineProperty(window, 'innerWidth', {{ get: () => {w} }});
    Object.defineProperty(window, 'innerHeight', {{ get: () => {h - 100} }});

    // 5. platform
    Object.defineProperty(navigator, 'platform', {{ get: () => '{platform}' }});

    // 6. WebGL 렌더러/벤더 위장
    const origGetParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {{
        if (param === 37446) return '{renderer}';  // UNMASKED_RENDERER_WEBGL
        if (param === 37445) return '{vendor}';    // UNMASKED_VENDOR_WEBGL
        return origGetParam.call(this, param);
    }};

    // 7. Canvas fingerprint 노이즈 주입
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type) {{
        const ctx = this.getContext('2d');
        if (ctx) {{
            const imageData = ctx.getImageData(0, 0, this.width, this.height);
            for (let i = 0; i < imageData.data.length; i += 4) {{
                imageData.data[i] = imageData.data[i] + Math.floor(Math.random() * {noise} * 255);
            }}
            ctx.putImageData(imageData, 0, 0);
        }}
        return origToDataURL.apply(this, arguments);
    }};

    // 8. chrome 객체 복원
    window.chrome = {{
        runtime: {{
            PlatformOs: {{ MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' }},
            PlatformArch: {{ ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' }},
            connect: function() {{}},
            sendMessage: function() {{}}
        }}
    }};

    // 9. permissions API
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (params) => (
        params.name === 'notifications'
            ? Promise.resolve({{ state: Notification.permission }})
            : origQuery(params)
    );
    """


def get_advanced_stealth_script(
    timezone: str = "Asia/Seoul",
    language: str = "ko-KR",
) -> str:
    """
    고급 스텔스 JS — AudioContext/Font/Battery/Hardware 등 위장.
    """
    battery_level   = round(random.uniform(0.45, 0.97), 2)
    is_charging     = random.choice([True, False])
    hw_concurrency  = random.choice([4, 6, 8, 12, 16])
    device_memory   = random.choice([4, 8, 16])
    connection_type = random.choice(["4g", "wifi"])
    downlink        = round(random.uniform(5.0, 50.0), 1)
    audio_noise     = round(random.uniform(0.00001, 0.0001), 7)
    math_noise      = round(random.uniform(-0.000001, 0.000001), 9)
    rtt             = random.randint(20, 80)
    discharging_t   = random.randint(3600, 14400) if not is_charging else "Infinity"

    active_fonts = random.sample(_WINDOWS_FONTS, k=random.randint(18, len(_WINDOWS_FONTS)))
    fonts_js = "[" + ",".join(f'"{f}"' for f in active_fonts) + "]"

    charging_str = "true" if is_charging else "false"
    charging_time_str = "0" if is_charging else "Infinity"
    discharging_time_str = "Infinity" if is_charging else str(discharging_t)

    return f"""
    (() => {{
    // 1. AudioContext 지문 위장
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (AudioCtx) {{
        const origGetChannelData = AudioBuffer.prototype.getChannelData;
        AudioBuffer.prototype.getChannelData = function(channel) {{
            const data = origGetChannelData.call(this, channel);
            for (let i = 0; i < data.length; i += 100) {{
                data[i] += {audio_noise} * (Math.random() - 0.5);
            }}
            return data;
        }};
    }}

    // 2. Font Enumeration 위장
    const FAKE_FONTS = {fonts_js};
    if (document.fonts) {{
        const origCheck = document.fonts.check.bind(document.fonts);
        document.fonts.check = function(font, text) {{
            const fontName = font.replace(/['"0-9a-z ]/gi, '').trim();
            if (fontName && FAKE_FONTS.some(f => f.toLowerCase() === fontName.toLowerCase())) {{
                return true;
            }}
            return origCheck(font, text);
        }};
    }}

    // 3. Battery Status API 위장
    Object.defineProperty(navigator, 'getBattery', {{
        get: () => () => Promise.resolve({{
            level:           {battery_level},
            charging:        {charging_str},
            chargingTime:    {charging_time_str},
            dischargingTime: {discharging_time_str},
            addEventListener: () => {{}},
            removeEventListener: () => {{}},
        }})
    }});

    // 4. Hardware Concurrency
    Object.defineProperty(navigator, 'hardwareConcurrency', {{
        get: () => {hw_concurrency}
    }});

    // 5. Device Memory
    Object.defineProperty(navigator, 'deviceMemory', {{
        get: () => {device_memory}
    }});

    // 6. Network Information API
    const fakeConnection = {{
        effectiveType: '{connection_type}',
        downlink:      {downlink},
        rtt:           {rtt},
        saveData:      false,
        addEventListener: () => {{}},
        removeEventListener: () => {{}},
    }};
    Object.defineProperty(navigator, 'connection', {{ get: () => fakeConnection }});
    Object.defineProperty(navigator, 'mozConnection', {{ get: () => fakeConnection }});
    Object.defineProperty(navigator, 'webkitConnection', {{ get: () => fakeConnection }});

    // 7. Speech Synthesis
    const fakeVoices = [
        {{ voiceURI: 'Microsoft Heami - Korean (Korea)', name: 'Microsoft Heami - Korean (Korea)', lang: 'ko-KR', localService: true, default: true }},
        {{ voiceURI: 'Microsoft David - English (United States)', name: 'Microsoft David - English (United States)', lang: 'en-US', localService: true, default: false }},
    ];
    if (window.speechSynthesis) {{
        window.speechSynthesis.getVoices = () => fakeVoices;
    }}

    // 8. Media Devices
    if (navigator.mediaDevices) {{
        navigator.mediaDevices.enumerateDevices = () => Promise.resolve([
            {{ deviceId: 'default', kind: 'audioinput',  label: 'Default Microphone', groupId: 'group1' }},
            {{ deviceId: 'default', kind: 'audiooutput', label: 'Default Speaker',    groupId: 'group1' }},
            {{ deviceId: 'cam001', kind: 'videoinput',   label: 'Integrated Webcam',  groupId: 'group2' }},
        ]);
    }}

    // 9. Math 지문 위장
    const _origSin = Math.sin;
    const _origCos = Math.cos;
    const _origTan = Math.tan;
    Math.sin = (x) => _origSin(x) + {math_noise};
    Math.cos = (x) => _origCos(x) + {math_noise};
    Math.tan = (x) => _origTan(x) + {math_noise};

    }})();
    """


def get_full_stealth_script(timezone: str = "Asia/Seoul") -> str:
    """기본 + 고급 스텔스를 합친 완전한 JS 스크립트."""
    return get_stealth_script() + "\n\n" + get_advanced_stealth_script(timezone=timezone)


def apply_stealth_to_page(page) -> None:
    """
    Playwright 페이지에 playwright-stealth 라이브러리 + 자체 JS 스텔스를 동시에 적용한다.

    playwright-stealth: navigator.webdriver, chrome runtime, permissions 등 40+ 항목 패치
    자체 stealth.py:    WebGL 렌더러, Canvas 노이즈, AudioContext, Font 등 추가 위장
    두 레이어를 함께 쓰면 탐지 우회 확률이 크게 높아진다.
    """
    # 1) playwright-stealth 라이브러리 패치 (우선 적용)
    try:
        from playwright_stealth import stealth_sync
        stealth_sync(page)
    except ImportError:
        pass  # 미설치 시 자체 JS만으로 동작

    # 2) 자체 스텔스 JS 추가 주입 (WebGL·Canvas·Audio 위장)
    try:
        page.add_init_script(get_full_stealth_script())
    except Exception:
        pass  # 이미 초기화된 페이지면 skip


def apply_stealth_to_context(context) -> None:
    """
    Playwright BrowserContext 단위로 스텔스를 적용한다.
    context.new_page() 로 생성되는 모든 페이지에 자동 적용된다.
    """
    # 자체 JS는 context 레벨로 주입 (모든 페이지에 자동 적용)
    try:
        context.add_init_script(get_full_stealth_script())
    except Exception:
        pass
