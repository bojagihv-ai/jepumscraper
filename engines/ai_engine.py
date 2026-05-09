"""
AI Engine — 로컬 Ollama(Gemma4 등) / OpenAI GPT-4o / Google Gemini Vision 지원

용도:
  1. 이미지 → 최적 검색 키워드 생성  (검색 전, 1회 실행)
  2. 후보 제품 재랭킹                 (검색 후, 선택적 — 느림)

엔진 선택: settings['ai_engine_type'] = 'ollama' | 'openai' | 'gemini'
"""
import base64
import json
import logging
import re
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# 공통 유틸
# ──────────────────────────────────────────────────────────

def _encode_image(image_path: str) -> Optional[str]:
    """이미지 파일 → base64 문자열"""
    try:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        logger.warning(f"[AIEngine] 이미지 인코딩 실패 ({image_path}): {e}")
        return None


def _parse_response(text: str) -> Any:
    """LLM 응답에서 JSON 배열 또는 숫자 추출"""
    if not text:
        return None
    # 마크다운 코드블록 제거
    text = re.sub(r"```(?:json)?\s*", "", text).strip("`").strip()
    # JSON 전체 파싱 시도
    try:
        return json.loads(text)
    except Exception:
        pass
    # JSON 배열만 추출
    m = re.search(r"\[.*?\]", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    # 숫자 추출 (점수 응답용)
    m = re.search(r"\b(\d{1,3})\b", text)
    if m:
        return int(m.group(1))
    return None


_KEYWORD_PROMPT = (
    "이 제품 이미지를 보고 한국 온라인 쇼핑몰(쿠팡, 네이버, G마켓 등)에서 "
    "검색할 최적의 키워드를 3~5개 제안해줘. "
    "현재 입력된 키워드는 '{product_name}'야. "
    "가장 효과적인 키워드를 JSON 배열 형태로만 답해줘. "
    '예시: ["복주머니", "전통 한복 파우치", "드로스트링 주머니"]'
)

_SCORE_PROMPT = (
    "원본 제품: 이미지1, 제품명='{source_name}'\n"
    "후보 제품: 이미지2, 제품명='{cand_title}'\n\n"
    "두 제품이 같은 제품인지 이미지와 제품명을 모두 참고해서 판단하고, "
    "0~100 사이의 유사도 점수만 숫자로 답해줘.\n"
    "100=완전히 동일, 70~99=같은 제품(색상/옵션 차이), "
    "40~69=비슷한 종류, 0~39=다른 제품"
)


# ──────────────────────────────────────────────────────────
# Ollama 엔진 (Gemma4, LLaVA 등 로컬 모델)
# ──────────────────────────────────────────────────────────

class OllamaEngine:
    def __init__(self, model: str = "gemma4:latest", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def check_available(self) -> dict:
        import urllib.request
        try:
            req = urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=4)
            data = json.loads(req.read())
            models = [m["name"] for m in data.get("models", [])]
            target = self.model.split(":")[0]
            has_model = any(target in m for m in models)
            return {
                "ok": True,
                "models": models,
                "has_target_model": has_model,
                "message": (
                    f"Ollama 연결됨. 모델 '{self.model}' "
                    + ("준비됨" if has_model else "없음 → 터미널: ollama pull " + self.model)
                ),
            }
        except Exception as e:
            return {"ok": False, "message": f"Ollama 연결 실패 (localhost:11434): {e}"}

    def _chat(self, prompt: str, images: List[str] = None, timeout: int = 120) -> str:
        import urllib.request
        import urllib.error
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        if images:
            payload["messages"][0]["images"] = images
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                return data.get("message", {}).get("content", "")
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            raise RuntimeError(
                f"Ollama HTTP {e.code} — model={self.model} — {err_body}"
            ) from e

    def analyze_image_for_keywords(self, image_path: str, product_name: str) -> List[str]:
        b64 = _encode_image(image_path)
        if not b64:
            return [product_name]
        prompt = _KEYWORD_PROMPT.format(product_name=product_name)
        try:
            response = self._chat(prompt, images=[b64], timeout=60)
            result = _parse_response(response)
            if isinstance(result, list) and result:
                return [str(k) for k in result if k]
        except Exception as e:
            logger.warning(f"[Ollama] 키워드 분석 실패: {e}")
        return [product_name]

    def score_candidate(self, source_b64: str, cand_b64: str, cand_title: str, source_name: str = "") -> int:
        """원본(이미지+이름) vs 후보(이미지+이름) → 0~100 유사도"""
        prompt = _SCORE_PROMPT.format(source_name=source_name or "알 수 없음", cand_title=cand_title)
        try:
            response = self._chat(prompt, images=[source_b64, cand_b64], timeout=60)
            result = _parse_response(response)
            if isinstance(result, (int, float)):
                return max(0, min(100, int(result)))
        except Exception as e:
            logger.warning(f"[Ollama] 후보 점수 실패: {e}")
        return -1


# ──────────────────────────────────────────────────────────
# OpenAI 엔진 (GPT-4o Vision)
# ──────────────────────────────────────────────────────────

class OpenAIEngine:
    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.api_key = api_key
        self.model = model

    def check_available(self) -> dict:
        if not self.api_key:
            return {"ok": False, "message": "OpenAI API 키가 설정되지 않았습니다."}
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self.api_key)
            client.models.list()
            return {"ok": True, "message": f"OpenAI 연결됨 (모델: {self.model})"}
        except ImportError:
            return {"ok": False, "message": "openai 패키지 미설치 → pip install openai"}
        except Exception as e:
            return {"ok": False, "message": f"OpenAI 연결 실패: {e}"}

    def _chat(self, prompt: str, images_b64: List[str] = None) -> str:
        from openai import OpenAI
        client = OpenAI(api_key=self.api_key)
        content: list = [{"type": "text", "text": prompt}]
        if images_b64:
            for b64 in images_b64:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                })
        response = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": content}],
            max_tokens=500,
        )
        return response.choices[0].message.content or ""

    def analyze_image_for_keywords(self, image_path: str, product_name: str) -> List[str]:
        b64 = _encode_image(image_path)
        if not b64:
            return [product_name]
        prompt = _KEYWORD_PROMPT.format(product_name=product_name)
        try:
            response = self._chat(prompt, images_b64=[b64])
            result = _parse_response(response)
            if isinstance(result, list) and result:
                return [str(k) for k in result if k]
        except Exception as e:
            logger.warning(f"[OpenAI] 키워드 분석 실패: {e}")
        return [product_name]

    def score_candidate(self, source_b64: str, cand_b64: str, cand_title: str, source_name: str = "") -> int:
        """원본(이미지+이름) vs 후보(이미지+이름) → 0~100 유사도"""
        prompt = _SCORE_PROMPT.format(source_name=source_name or "알 수 없음", cand_title=cand_title)
        try:
            response = self._chat(prompt, images_b64=[source_b64, cand_b64])
            result = _parse_response(response)
            if isinstance(result, (int, float)):
                return max(0, min(100, int(result)))
        except Exception as e:
            logger.warning(f"[OpenAI] 후보 점수 실패: {e}")
        return -1


# ──────────────────────────────────────────────────────────
# Google Gemini 엔진
# ──────────────────────────────────────────────────────────

class GeminiEngine:
    def __init__(self, api_key: str, model: str = "gemini-2.0-flash"):
        self.api_key = api_key
        self.model = model

    def check_available(self) -> dict:
        if not self.api_key:
            return {"ok": False, "message": "Gemini API 키가 설정되지 않았습니다."}
        try:
            import google.generativeai as genai
            genai.configure(api_key=self.api_key)
            m = genai.GenerativeModel(self.model)
            m.generate_content("ping", generation_config={"max_output_tokens": 5})
            return {"ok": True, "message": f"Gemini 연결됨 (모델: {self.model})"}
        except ImportError:
            return {"ok": False, "message": "google-generativeai 패키지 미설치 → pip install google-generativeai"}
        except Exception as e:
            return {"ok": False, "message": f"Gemini 연결 실패: {e}"}

    def _generate(self, prompt: str, images_b64: List[str] = None) -> str:
        import google.generativeai as genai
        genai.configure(api_key=self.api_key)
        m = genai.GenerativeModel(self.model)
        parts: list = [prompt]
        if images_b64:
            for b64 in images_b64:
                parts.append({"mime_type": "image/jpeg", "data": b64})
        response = m.generate_content(parts)
        return response.text or ""

    def analyze_image_for_keywords(self, image_path: str, product_name: str) -> List[str]:
        b64 = _encode_image(image_path)
        if not b64:
            return [product_name]
        prompt = _KEYWORD_PROMPT.format(product_name=product_name)
        try:
            response = self._generate(prompt, images_b64=[b64])
            result = _parse_response(response)
            if isinstance(result, list) and result:
                return [str(k) for k in result if k]
        except Exception as e:
            logger.warning(f"[Gemini] 키워드 분석 실패: {e}")
        return [product_name]

    def score_candidate(self, source_b64: str, cand_b64: str, cand_title: str, source_name: str = "") -> int:
        """원본(이미지+이름) vs 후보(이미지+이름) → 0~100 유사도"""
        prompt = _SCORE_PROMPT.format(source_name=source_name or "알 수 없음", cand_title=cand_title)
        try:
            response = self._generate(prompt, images_b64=[source_b64, cand_b64])
            result = _parse_response(response)
            if isinstance(result, (int, float)):
                return max(0, min(100, int(result)))
        except Exception as e:
            logger.warning(f"[Gemini] 후보 점수 실패: {e}")
        return -1


# ──────────────────────────────────────────────────────────
# 팩토리 & 고수준 공통 함수
# ──────────────────────────────────────────────────────────

def get_engine(settings: dict):
    """설정에 따라 AI 엔진 반환. 비활성화/오류 시 None."""
    if not settings.get("ai_engine_enabled", False):
        return None
    engine_type = settings.get("ai_engine_type", "ollama")
    if engine_type == "ollama":
        return OllamaEngine(
            model=settings.get("ai_model", "gemma4:latest"),
            base_url=settings.get("ai_ollama_url", "http://localhost:11434"),
        )
    if engine_type == "openai":
        return OpenAIEngine(
            api_key=settings.get("ai_openai_key", ""),
            model=settings.get("ai_model", "gpt-4o"),
        )
    if engine_type == "gemini":
        return GeminiEngine(
            api_key=settings.get("ai_gemini_key", ""),
            model=settings.get("ai_model", "gemini-2.0-flash"),
        )
    logger.warning(f"[AIEngine] 알 수 없는 엔진 타입: {engine_type}")
    return None


def optimize_keyword(engine, image_path: str, product_name: str) -> List[str]:
    """
    이미지 + 기존 키워드 → AI가 제안하는 최적화 키워드 목록.
    실패 시 원래 키워드 단일 목록 반환.
    """
    if engine is None:
        return [product_name]
    try:
        keywords = engine.analyze_image_for_keywords(image_path, product_name)
        logger.info(f"[AIEngine] 키워드 최적화: '{product_name}' → {keywords}")
        return keywords if keywords else [product_name]
    except Exception as e:
        logger.warning(f"[AIEngine] optimize_keyword 실패: {e}")
        return [product_name]


def rerank_candidates(
    engine,
    source_image_path: str,
    candidates: list,
    top_n: int = 20,
    source_name: str = "",
) -> list:
    """
    상위 top_n개 후보를 AI Vision으로 재평가.
    원본 이미지+이름, 후보 이미지+이름을 모두 LLM에 전달.
    기존 similarity_score(60%)와 AI 점수(40%)를 혼합 후 재정렬.
    나머지 후보는 그대로 뒤에 붙임.
    """
    if engine is None or not candidates:
        return candidates
    try:
        source_b64 = _encode_image(source_image_path)
        if not source_b64:
            return candidates

        to_score = candidates[:top_n]
        rest = candidates[top_n:]

        for candidate in to_score:
            if not getattr(candidate, "local_thumbnail_path", ""):
                continue
            cand_b64 = _encode_image(candidate.local_thumbnail_path)
            if not cand_b64:
                continue
            ai_score = engine.score_candidate(
                source_b64, cand_b64, candidate.title, source_name=source_name
            )
            if ai_score >= 0:
                orig = float(getattr(candidate, "similarity_score", 0) or 0)
                candidate.similarity_score = round(orig * 0.6 + ai_score * 0.4, 1)
                logger.debug(
                    f"[AIEngine] {candidate.title[:20]} "
                    f"orig={orig:.1f} ai={ai_score} → {candidate.similarity_score}"
                )

        reranked = sorted(
            to_score,
            key=lambda x: float(getattr(x, "similarity_score", 0) or 0),
            reverse=True,
        )
        logger.info(f"[AIEngine] 재랭킹 완료: {len(to_score)}개 처리")
        return reranked + rest

    except Exception as e:
        logger.warning(f"[AIEngine] rerank_candidates 실패: {e}")
        return candidates
