"""
SimilarityScorer - 0~100 유사도 점수 산출
specs/04-유사도-랭킹.md 기반

점수 체계:
- 제목 텍스트 유사도: 35점
- 브랜드 일치: 15점
- 모델명 일치: 20점
- 규격/옵션 토큰 일치: 15점
- 이미지 유사도: 10점
- 가격 안정성: 5점
"""
import re
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)

# 불용어 목록 (검색/매칭에 영향 없는 단어)
STOPWORDS = {
    '정품', '국내배송', '무료배송', '당일배송', '새상품', '신상품', '특가', '할인', '세일',
    '빠른배송', '해외직구', '병행수입', '리퍼', '중고', '업체발송', '로켓배송', '로켓',
    '당일', '내일', '오늘', '배송', '판매', '최저가', '공식', '인증', '정식', '수입',
}

# 브랜드 키워드 패턴
BRAND_PATTERN = re.compile(
    r'\b(삼성|samsung|LG|엘지|애플|apple|Sony|소니|Panasonic|파나소닉|Philips|필립스|'
    r'Bosch|보쉬|Dyson|다이슨|Nike|나이키|Adidas|아디다스|뉴발란스|New\s*Balance|'
    r'Xiaomi|샤오미|Huawei|화웨이|HP|Dell|Lenovo|레노버|ASUS|아수스|MSI|'
    r'Coway|코웨이|Winia|위니아|Haier|하이얼|Sharp|샤프|Toshiba|도시바)\b',
    re.IGNORECASE
)

# 모델번호 패턴
# 예: SM-S938, WH-1000XM5, A2890, SM938, XM5
MODEL_PATTERN = re.compile(
    r'\b('
    r'[A-Z]{1,4}[-_][A-Z0-9]{2,10}'   # SM-S938, WH-1000XM5
    r'|[A-Z]{1,3}[0-9]{3,6}[A-Z0-9]{0,4}'  # A2890, SM938
    r'|[A-Z][A-Z0-9]{1,3}[-_][0-9]{3,6}[A-Z0-9]{0,4}'  # GT-I9500
    r')\b'
)

# 규격/용량 패턴
SPEC_PATTERN = re.compile(
    r'\b(\d+(?:\.\d+)?)\s*(GB|TB|MB|ml|ML|L|ℓ|mg|kg|g|cm|mm|m|인치|inch|개입|개|팩|세트|매|장|EA)\b',
    re.IGNORECASE
)

# 색상 패턴
COLOR_PATTERN = re.compile(
    r'\b(블랙|화이트|실버|그레이|레드|블루|그린|골드|베이지|브라운|핑크|퍼플|옐로우|'
    r'Black|White|Silver|Gray|Grey|Red|Blue|Green|Gold|Beige|Brown|Pink|Purple|Yellow)\b',
    re.IGNORECASE
)


def normalize_text(text: str) -> str:
    """텍스트 정규화: 소문자, 특수문자 제거, 불용어 제거"""
    if not text:
        return ''
    text = text.lower()
    # HTML 태그 제거
    text = re.sub(r'<[^>]+>', ' ', text)
    # 특수문자를 공백으로
    text = re.sub(r'[^\w\s가-힣a-zA-Z0-9]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    tokens = text.split()
    tokens = [t for t in tokens if t not in STOPWORDS and len(t) >= 2]
    return ' '.join(tokens)


def extract_brand(text: str) -> str:
    """텍스트에서 브랜드 키워드 추출 (소문자 반환)"""
    m = BRAND_PATTERN.search(text)
    return m.group(1).lower() if m else ''


def extract_models(text: str) -> List[str]:
    """텍스트에서 모델번호 추출"""
    return [m.upper() for m in MODEL_PATTERN.findall(text)]


def extract_specs(text: str) -> List[str]:
    """텍스트에서 규격/색상 토큰 추출"""
    specs = [f"{m[0]}{m[1].lower()}" for m in SPEC_PATTERN.findall(text)]
    colors = [c.lower() for c in COLOR_PATTERN.findall(text)]
    return list(set(specs + colors))


def token_similarity(s1: str, s2: str) -> float:
    """두 정규화 텍스트 간 토큰 기반 유사도 (0~1)"""
    if not s1 or not s2:
        return 0.0
    t1 = set(s1.split())
    t2 = set(s2.split())
    if not t1 or not t2:
        return 0.0
    # Jaccard + 포함 가산점
    intersection = t1 & t2
    union = t1 | t2
    jaccard = len(intersection) / len(union)
    # 짧은 쪽이 긴 쪽에 많이 포함될수록 가산
    shorter, longer = (t1, t2) if len(t1) <= len(t2) else (t2, t1)
    coverage = len(shorter & longer) / max(len(shorter), 1)
    return min(1.0, (jaccard + coverage) / 2)


def seq_similarity(s1: str, s2: str) -> float:
    """SequenceMatcher 기반 문자 단위 유사도"""
    from difflib import SequenceMatcher
    if not s1 or not s2:
        return 0.0
    return SequenceMatcher(None, s1, s2).ratio()


class SimilarityScorer:
    """
    제품명 + (선택적) 이미지 기반 0~100 유사도 점수 산출기.
    SimilarityScorer(source_name) 로 초기화 후
    scorer.score(candidate) 또는 scorer.score_all(candidates) 사용.
    """

    def __init__(self, source_name: str, source_price: Optional[str] = None):
        self.source_name = source_name
        self.source_norm = normalize_text(source_name)
        self.source_brand = extract_brand(source_name)
        self.source_models = extract_models(source_name)
        self.source_specs = extract_specs(source_name)
        self.source_price = self._parse_price(source_price or '0')
        # 이미지 분석값 (외부에서 주입)
        self.source_embedding = None

    def _parse_price(self, price_str: str) -> int:
        try:
            return int(re.sub(r'[^0-9]', '', str(price_str)))
        except Exception:
            return 0

    def score(self, candidate, image_analyzer=None) -> float:
        """
        후보 상품 하나에 대한 0~100 유사도 점수 반환.
        candidate: ProductResult 객체 (title, price, local_thumbnail_path 사용)
        """
        total = 0.0

        # ── 1. 제목 텍스트 유사도 (35점) ──
        cand_norm = normalize_text(candidate.title)
        # 토큰 유사도 + 시퀀스 유사도 혼합
        tok_sim = token_similarity(self.source_norm, cand_norm)
        seq_sim = seq_similarity(self.source_norm, cand_norm)
        text_sim = tok_sim * 0.6 + seq_sim * 0.4
        total += text_sim * 35

        # ── 2. 브랜드 일치 (15점) ──
        cand_brand = extract_brand(candidate.title)
        if self.source_brand and cand_brand:
            if self.source_brand == cand_brand:
                total += 15
            elif self.source_brand[:4] in cand_brand or cand_brand[:4] in self.source_brand:
                total += 7
            # 브랜드가 다르면 0점
        elif not self.source_brand:
            # 원본에 브랜드 정보 없으면 중립 (7.5점)
            total += 7.5

        # ── 3. 모델명 일치 (20점) ──
        cand_models = extract_models(candidate.title)
        if self.source_models and cand_models:
            matched = len(set(self.source_models) & set(cand_models))
            total += min(20, matched * 10)
        elif not self.source_models:
            # 원본에 모델번호 없으면 중립 (10점)
            total += 10

        # ── 4. 규격/옵션 토큰 일치 (15점) ──
        cand_specs = extract_specs(candidate.title)
        if self.source_specs and cand_specs:
            matched_specs = len(set(self.source_specs) & set(cand_specs))
            spec_ratio = matched_specs / max(len(self.source_specs), 1)
            total += spec_ratio * 15
            # 규격이 있는데 완전히 다른 경우 패널티
            if matched_specs == 0:
                total -= 5
        elif not self.source_specs:
            # 원본에 규격 없으면 중립 (7.5점)
            total += 7.5

        # ── 5. 이미지 유사도 (10점) ──
        if (image_analyzer is not None
                and self.source_embedding is not None
                and candidate.local_thumbnail_path):
            try:
                cand_emb = image_analyzer.get_embedding(candidate.local_thumbnail_path)
                if cand_emb is not None:
                    img_sim = image_analyzer.get_clip_similarity(
                        self.source_embedding, cand_emb
                    )
                    # CLIP 코사인은 보통 0.5~1.0 → 0~1로 정규화
                    normalized = max(0.0, (img_sim - 0.5) / 0.5)
                    total += normalized * 10
                else:
                    total += 5  # 임베딩 실패 → 중립
            except Exception as e:
                logger.debug(f"Image similarity error ({candidate.id}): {e}")
                total += 5
        else:
            total += 5  # 이미지 정보 없으면 중립

        # ── 6. 가격 안정성 (5점) ──
        cand_price = self._parse_price(candidate.price)
        if self.source_price > 0 and cand_price > 0:
            ratio = min(cand_price, self.source_price) / max(cand_price, self.source_price)
            if ratio >= 0.5:
                total += 5   # 가격 차이 50% 이내 → 만점
            elif ratio >= 0.2:
                total += 2   # 가격 차이 80% 이내 → 부분
            # 그 이하면 이상치로 간주 → 0점
        else:
            total += 2  # 가격 정보 없으면 중립

        return min(100.0, max(0.0, round(total, 1)))

    def score_all(self, candidates: list, image_analyzer=None) -> list:
        """
        모든 후보에 점수를 매기고 similarity_score를 설정 후
        점수 내림차순으로 정렬하여 반환.
        """
        for c in candidates:
            c.similarity_score = self.score(c, image_analyzer)
        return sorted(candidates, key=lambda x: x.similarity_score, reverse=True)

    def top_n(self, candidates: list, n: int = 10, image_analyzer=None) -> list:
        """점수 상위 n개만 반환"""
        scored = self.score_all(candidates, image_analyzer)
        return scored[:n]
