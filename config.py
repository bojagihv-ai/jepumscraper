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
USER_AGENT_LIST = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
]

# --- API 설정 ---
NAVER_CLIENT_ID = os.getenv('NAVER_CLIENT_ID', '')
NAVER_CLIENT_SECRET = os.getenv('NAVER_CLIENT_SECRET', '')
API_KEYS = {}

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
