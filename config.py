import os
from pathlib import Path
from dotenv import load_dotenv

# 로컬 .env 파일 로드 (API 키 등)
load_dotenv()

# --- 디렉토리 설정 ---
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / 'data'
INPUT_DIR = DATA_DIR / 'input'
THUMBNAIL_DIR = DATA_DIR / 'thumbnails'
DETAIL_DIR = DATA_DIR / 'detail_pages'
OUTPUT_DIR = DATA_DIR / 'output'

# 필요한 디렉토리 생성
for d in [INPUT_DIR, THUMBNAIL_DIR, DETAIL_DIR, OUTPUT_DIR]:
    os.makedirs(d, exist_ok=True)

# --- 스크래핑 설정 ---
SCRAPING_DELAY_MIN = 3.0  # 최소 대기 시간 (초)
SCRAPING_DELAY_MAX = 5.0  # 최대 대기 시간 (초)
GENTLE_SCRAPING_MODE = True
AUTO_TUNE_ENABLED = True
ENABLE_CLIP_ANALYSIS = False
SEARCH_CONCURRENCY = 1
DETAIL_CAPTURE_CONCURRENCY = 2
INTER_PLATFORM_DELAY_MIN = 8.0
INTER_PLATFORM_DELAY_MAX = 18.0
DETAIL_CACHE_TTL_DAYS = 14
COUPANG_FIRST_MODE = True
COUPANG_MIN_FINAL = 3
ENABLE_PROXY_PROFILES = False
PROXY_HEALTH_CHECK_URL = os.getenv('PROXY_HEALTH_CHECK_URL', 'https://api.ipify.org?format=json')
PROXY_HEALTH_TIMEOUT_SEC = int(os.getenv('PROXY_HEALTH_TIMEOUT_SEC', '8'))
PROXY_PROFILES = []
USE_USER_BROWSER_SESSION = True
CHROME_USER_DATA_DIR = os.getenv(
    'CHROME_USER_DATA_DIR',
    str(Path(os.getenv('LOCALAPPDATA', '')) / 'Google' / 'Chrome' / 'User Data')
)
CHROME_PROFILE_DIRECTORY = os.getenv('CHROME_PROFILE_DIRECTORY', 'Default')
NAVER_CHROME_PROFILE_DIRECTORY = os.getenv('NAVER_CHROME_PROFILE_DIRECTORY', '')
NAVER_LOGIN_WAIT_SEC = int(os.getenv('NAVER_LOGIN_WAIT_SEC', '240'))
NAVER_DEBUG_PORT = int(os.getenv('NAVER_DEBUG_PORT', '0') or '0')
COUPANG_USE_DEDICATED_PROFILE = os.getenv('COUPANG_USE_DEDICATED_PROFILE', '1') != '0'
COUPANG_BROWSER_PROFILE_DIR = os.getenv(
    'COUPANG_BROWSER_PROFILE_DIR',
    str(DATA_DIR / 'browser_profiles' / 'coupang')
)
COUPANG_ASSISTED_CAPTURE = os.getenv('COUPANG_ASSISTED_CAPTURE', '1') != '0'
COUPANG_DEBUG_PORT = int(os.getenv('COUPANG_DEBUG_PORT', '9223'))
AUCTION_BROWSER_PROFILE_DIR = os.getenv(
    'AUCTION_BROWSER_PROFILE_DIR',
    str(DATA_DIR / 'browser_profiles' / 'auction')
)
AUCTION_DEBUG_PORT = int(os.getenv('AUCTION_DEBUG_PORT', '9224'))
AUCTION_HUMAN_CHECK_WAIT_SEC = int(os.getenv('AUCTION_HUMAN_CHECK_WAIT_SEC', '180'))
ENABLE_AHK_FALLBACK = os.getenv('ENABLE_AHK_FALLBACK', '1') != '0'
AUTOHOTKEY_EXE = os.getenv('AUTOHOTKEY_EXE', '')
USER_AGENT_LIST = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
]

# --- API 설정 ---
NAVER_CLIENT_ID = os.getenv('NAVER_CLIENT_ID', '')
NAVER_CLIENT_SECRET = os.getenv('NAVER_CLIENT_SECRET', '')
API_KEYS = {}

# --- CAPTCHA 해결 서비스 API 키 ---
CAPSOLVER_API_KEY   = os.getenv('CAPSOLVER_API_KEY', '')
TWOCAPTCHA_API_KEY  = os.getenv('TWOCAPTCHA_API_KEY', '')
ANTICAPTCHA_API_KEY = os.getenv('ANTICAPTCHA_API_KEY', '')

# --- 우회 엔진 설정 ---
BYPASS_CACHE_DIR = DATA_DIR / 'bypass_cache'
os.makedirs(BYPASS_CACHE_DIR, exist_ok=True)

# --- 모델 설정 ---
CLIP_MODEL_NAME = "openai/clip-vit-base-patch32"

# --- 매칭 임계값 (Thresholds) ---
PHASH_THRESHOLD = 5         # 1단계: 완전히 동일한 이미지 (해밍 거리 <= 5)
CLIP_SIMILARITY_TIER2 = 0.82 # 2단계: 형태/모양 유사도 (CLIP 코사인 유사도)
NAME_SIMILARITY_TIER2 = 0.4  # 2단계: 이름 유사도
CLIP_SIMILARITY_TIER3 = 0.75 # 3단계: 모양은 비슷하나 색상/디테일 다름
COLOR_SIMILARITY_MAX = 0.6   # 3단계: 색상 차이 (HSV 유사도 < 0.6 이면 색상다름으로 간주)

# 한 제품당 최대 검수 개수 (중복 제거 전 수집 수)
MAX_CANDIDATES = 30
