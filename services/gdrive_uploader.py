"""
Google Drive 자동 업로드 서비스
- credentials.json: Google Cloud Console에서 발급 (최초 1회)
- data/gdrive_token.json: 최초 인증 후 자동 저장, 이후 자동 갱신
"""
import os
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/drive.file']
BASE_DIR = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = BASE_DIR / 'credentials.json'
TOKEN_FILE = BASE_DIR / 'data' / 'gdrive_token.json'
FOLDER_CACHE_FILE = BASE_DIR / 'data' / 'gdrive_folder_cache.json'

_service = None
_folder_cache: dict = {}


def _load_folder_cache() -> dict:
    try:
        if FOLDER_CACHE_FILE.exists():
            return json.loads(FOLDER_CACHE_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}


def _save_folder_cache(cache: dict):
    try:
        FOLDER_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


def get_service():
    """Drive API 서비스 객체 반환. credentials.json 없으면 None."""
    global _service
    if _service is not None:
        return _service

    if not CREDENTIALS_FILE.exists():
        logger.debug("[GDrive] credentials.json 없음 — setup_gdrive.py 실행 필요")
        return None

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = None
        if TOKEN_FILE.exists():
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                TOKEN_FILE.write_text(creds.to_json(), encoding='utf-8')
            else:
                logger.warning("[GDrive] 인증 토큰 없음 — setup_gdrive.py 실행 필요")
                return None

        _service = build('drive', 'v3', credentials=creds)
        logger.info("[GDrive] Drive API 연결 성공")
        return _service

    except ImportError:
        logger.debug("[GDrive] google-api-python-client 미설치 — pip install 필요")
        return None
    except Exception as e:
        logger.warning(f"[GDrive] 서비스 초기화 실패: {e}")
        _service = None
        return None


def _get_or_create_folder(service, name: str, parent_id: Optional[str] = None) -> Optional[str]:
    """Drive에 폴더 생성 또는 기존 폴더 ID 반환."""
    cache_key = f"{parent_id or 'root'}/{name}"
    global _folder_cache
    if cache_key in _folder_cache:
        return _folder_cache[cache_key]

    try:
        q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent_id:
            q += f" and '{parent_id}' in parents"
        else:
            q += " and 'root' in parents"

        resp = service.files().list(q=q, fields='files(id,name)', pageSize=5).execute()
        files = resp.get('files', [])
        if files:
            folder_id = files[0]['id']
        else:
            meta = {
                'name': name,
                'mimeType': 'application/vnd.google-apps.folder',
            }
            if parent_id:
                meta['parents'] = [parent_id]
            folder = service.files().create(body=meta, fields='id').execute()
            folder_id = folder['id']

        _folder_cache[cache_key] = folder_id
        _save_folder_cache(_folder_cache)
        return folder_id

    except Exception as e:
        logger.warning(f"[GDrive] 폴더 생성/조회 실패 ({name}): {e}")
        return None


def _sanitize(text: str, max_len: int = 30) -> str:
    """파일명에 사용할 수 없는 문자 제거 후 max_len으로 자름."""
    import re
    # Windows/Drive 금지 문자 제거
    text = re.sub(r'[\\/:*?"<>|]', '', text)
    # 앞뒤 공백·점 제거
    text = text.strip(' .')
    return text[:max_len]


def normalize_platform_label(platform: str = "", product_url: str = "") -> str:
    """Return the filename label for the marketplace."""
    raw = f"{platform or ''} {product_url or ''}".lower()
    if "gmarket" in raw or "g-market" in raw or "g마켓" in raw or "지마켓" in raw:
        return "gmarket"
    if "auction" in raw or "옥션" in raw:
        return "auction"
    if "coupang" in raw or "쿠팡" in raw:
        return "coupang"
    if "11st" in raw or "11번가" in raw or "eleven" in raw:
        return "11st"
    if "naver" in raw or "네이버" in raw or "smartstore" in raw:
        return "naver"
    return _sanitize(platform or "기타", 12) or "기타"


def build_detail_filename(
    source_path: str,
    keyword: str = "",
    title: str = "",
    platform: str = "",
    seller_name: str = "",
    product_id: str = "",
    product_url: str = "",
    index: int = 1,
    total: int = 1,
    timestamp: str = "",
) -> str:
    """Build a consistent detail-image filename for local, Drive, and batch saves."""
    ext = os.path.splitext(source_path or "")[1] or ".jpg"
    if ext.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".jpg"

    platform_part = _sanitize(normalize_platform_label(platform, product_url), 12)
    keyword_part = _sanitize(keyword or "기타", 20)
    title_part = _sanitize(title or product_id or "detail", 50)
    time_part = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    seller_part = _sanitize(seller_name or "", 24)

    parts = [platform_part, keyword_part, title_part, time_part]
    if total > 1:
        parts.append(f"part{max(index, 1):02d}")
    if seller_part:
        parts.append(seller_part)
    return "_".join(part for part in parts if part) + ext


def upload_detail_image(
    file_path: str,
    keyword: str,
    product_id: str,
    root_folder_name: str = "JepumScraper 상세이미지",
    title: str = "",
    platform: str = "",
    seller_name: str = "",
    product_url: str = "",
    index: int = 1,
    total: int = 1,
    timestamp: str = "",
) -> Optional[str]:
    """
    상세페이지 이미지를 Google Drive에 업로드.
    반환값: 업로드된 파일의 Drive file ID (실패 시 None)

    Drive 폴더 구조:
      JepumScraper 상세이미지/
        └── {keyword}/
              └── {keyword}_{title}_{YYYYMMDD_HHMMSS}_{원본파일명}
    """
    if not file_path or not os.path.exists(file_path):
        return None

    service = get_service()
    if service is None:
        return None

    global _folder_cache
    if not _folder_cache:
        _folder_cache = _load_folder_cache()

    try:
        from googleapiclient.http import MediaFileUpload
        from datetime import datetime

        # 루트 폴더
        root_id = _get_or_create_folder(service, root_folder_name)
        if not root_id:
            return None

        # 키워드 하위 폴더 (키워드가 없으면 "기타")
        safe_keyword = (keyword or "기타").strip()[:50]
        kw_id = _get_or_create_folder(service, safe_keyword, parent_id=root_id)
        if not kw_id:
            return None

        # 파일명: 플랫폼_검색어_상품명_저장일시_partXX_업체명
        dt_str = timestamp or datetime.now().strftime('%Y%m%d_%H%M%S')
        upload_name = build_detail_filename(
            file_path,
            keyword=keyword,
            title=title,
            platform=platform,
            seller_name=seller_name,
            product_id=product_id,
            product_url=product_url,
            index=index,
            total=total,
            timestamp=dt_str,
        )

        # 파일 업로드
        mime = 'image/jpeg' if upload_name.lower().endswith(('.jpg', '.jpeg')) else 'image/png'
        media = MediaFileUpload(file_path, mimetype=mime, resumable=False)
        file_meta = {'name': upload_name, 'parents': [kw_id]}
        uploaded = service.files().create(
            body=file_meta,
            media_body=media,
            fields='id,name,webViewLink',
        ).execute()

        link = uploaded.get('webViewLink', '')
        logger.info(f"[GDrive] 업로드 완료: {upload_name} → {root_folder_name}/{safe_keyword}/ ({link})")
        return uploaded.get('id')

    except Exception as e:
        logger.warning(f"[GDrive] 업로드 실패 ({file_path}): {e}")
        return None


def upload_multiple(
    file_paths: list,
    keyword: str,
    product_id: str,
    root_folder_name: str = "JepumScraper 상세이미지",
    title: str = "",
    platform: str = "",
    seller_name: str = "",
    product_url: str = "",
) -> list:
    """여러 파일(분할 캡처 등) 한꺼번에 업로드. 성공한 file ID 목록 반환."""
    results = []
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    total = len(file_paths or [])
    for index, path in enumerate(file_paths or [], start=1):
        fid = upload_detail_image(
            path,
            keyword,
            product_id,
            root_folder_name,
            title=title,
            platform=platform,
            seller_name=seller_name,
            product_url=product_url,
            index=index,
            total=total,
            timestamp=timestamp,
        )
        if fid:
            results.append(fid)
    return results


def is_ready() -> bool:
    """Drive 업로드 가능 여부 (credentials + token 모두 있을 때 True)."""
    return CREDENTIALS_FILE.exists() and TOKEN_FILE.exists()


def reset_service():
    """서비스 캐시 초기화 (재인증 후 호출)."""
    global _service, _folder_cache
    _service = None
    _folder_cache = {}
