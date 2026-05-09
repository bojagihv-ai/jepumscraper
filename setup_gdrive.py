"""
Google Drive 최초 인증 설정 스크립트
한 번만 실행하면 이후 자동 업로드됨
"""
import os
import sys
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = BASE_DIR / 'credentials.json'
TOKEN_FILE = BASE_DIR / 'data' / 'gdrive_token.json'

def main():
    print("=" * 60)
    print("  JepumScraper — Google Drive 연동 설정")
    print("=" * 60)

    # Step 1: google 패키지 설치 확인
    try:
        import google.oauth2.credentials
        import google_auth_oauthlib.flow
        import googleapiclient.discovery
        print("✅ google 패키지 설치됨")
    except ImportError:
        print("❌ google 패키지 미설치. 설치 중...")
        os.system(f'"{sys.executable}" -m pip install google-api-python-client google-auth-oauthlib google-auth-httplib2')
        print("✅ 설치 완료. 스크립트를 다시 실행하세요.")
        input("엔터 키를 누르면 종료...")
        sys.exit(0)

    # Step 2: credentials.json 확인
    if not CREDENTIALS_FILE.exists():
        print()
        print("📋 credentials.json 파일이 없습니다.")
        print()
        print("아래 단계를 따라 credentials.json을 받아주세요:")
        print()
        print("1. 브라우저에서 Google Cloud Console 열기")
        print("   → https://console.cloud.google.com/")
        print()
        print("2. 좌측 상단 프로젝트 선택 → '새 프로젝트' 클릭")
        print("   프로젝트 이름: JepumScraper (아무 이름도 OK)")
        print()
        print("3. 좌측 메뉴 → 'API 및 서비스' → '라이브러리'")
        print("   → 'Google Drive API' 검색 → '사용 설정'")
        print()
        print("4. 좌측 메뉴 → 'API 및 서비스' → '사용자 인증 정보'")
        print("   → '사용자 인증 정보 만들기' → 'OAuth 클라이언트 ID'")
        print("   → 애플리케이션 유형: '데스크톱 앱'")
        print("   → 이름: JepumScraper → '만들기'")
        print()
        print("5. 생성된 클라이언트 → 다운로드 버튼 (↓)")
        print(f"   다운로드 파일을 여기에 복사: {CREDENTIALS_FILE}")
        print()
        print("(브라우저를 열어드릴게요)")
        webbrowser.open("https://console.cloud.google.com/apis/credentials")
        input("\ncredentials.json 파일을 위 경로에 복사한 후 엔터 키를 누르세요...")

        if not CREDENTIALS_FILE.exists():
            print("❌ credentials.json 파일이 없습니다. 다시 실행하세요.")
            input("엔터 키를 누르면 종료...")
            sys.exit(1)

    print()
    print("✅ credentials.json 확인됨")
    print()
    print("📌 브라우저가 열리면 hsong7266@gmail.com 계정으로 로그인 후")
    print("   'JepumScraper이(가) Google 드라이브에 액세스하도록 허용' 클릭")
    print()
    input("준비됐으면 엔터 키를 누르세요...")

    # Step 3: OAuth 인증
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        SCOPES = ['https://www.googleapis.com/auth/drive.file']
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)

        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(creds.to_json(), encoding='utf-8')

        print()
        print("✅ 인증 성공! 토큰 저장 완료")
        print(f"   {TOKEN_FILE}")

        # Step 4: 연결 테스트
        service = build('drive', 'v3', credentials=creds)
        about = service.about().get(fields='user').execute()
        email = about.get('user', {}).get('emailAddress', '')
        print(f"   연결된 계정: {email}")
        print()
        print("🎉 설정 완료! 이제 상세페이지 캡처 시 자동으로 Drive에 저장됩니다.")
        print()
        print("   Drive 폴더: 내 드라이브 → JepumScraper 상세이미지 → {키워드}/")

    except Exception as e:
        print(f"❌ 인증 실패: {e}")
        print("   다시 시도하거나 credentials.json 파일을 확인하세요.")

    input("\n엔터 키를 누르면 종료...")

if __name__ == '__main__':
    main()
