import difflib
import re

def clean_product_name(name):
    """
    제품명에서 특수문자를 제거하고 소문자로 변환하여 비교를 용이하게 합니다.
    """
    if not name:
        return ""
    # 괄호와 그 안의 내용 제거 (보통 옵션이나 부가설명)
    name = re.sub(r'\(.*?\)|\[.*?\]|\{.*?\}', '', name)
    # 특수문자 제거 및 공백 정규화
    name = re.sub(r'[^\w\s]', '', name)
    # 2개 이상의 연속된 공백을 1개로
    name = re.sub(r'\s+', ' ', name).strip()
    return name.lower()

def get_name_similarity(name1, name2):
    """
    두 제품명 사이의 문자열 유사도를 계산합니다 (0~1).
    """
    if not name1 or not name2:
        return 0.0
        
    c1 = clean_product_name(name1)
    c2 = clean_product_name(name2)
    
    # 길이가 짧은 쪽이 완전히 포함되어 있다면 높은 점수 부여 (옵션 등이 다를 수 있으므로)
    if len(c1) > 0 and len(c2) > 0:
        if c1 in c2 or c2 in c1:
            return 0.9  # 부분 일치 가산점
            
    matcher = difflib.SequenceMatcher(None, c1, c2)
    return matcher.ratio()

def extract_keywords(name):
    """
    검색에 사용할 핵심 키워드를 추출합니다. (단순 버전)
    """
    clean_name = clean_product_name(name)
    words = clean_name.split()
    
    # 너무 짧은 단어 제외 (의미 없는 조사, 단어 등 필터링 목적)
    # 전통공예품의 경우 보통 한글 형태소 분석기가 좋지만, 일단 간단하게 길이 2 이상만 추출
    keywords = [w for w in words if len(w) >= 2]
    
    if not keywords and words:
        return words
        
    return keywords
