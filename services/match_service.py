import logging
from typing import List, Dict, Any
from engines.image_analyzer import get_analyzer
from engines.text_matcher import get_name_similarity
import config
import progress_store

logger = logging.getLogger(__name__)

class MatchService:
    def __init__(self):
        self.analyzer = get_analyzer()
        
    def classify_matches(self, source_image_path: str, source_name: str, candidates: List[Any]) -> Dict[int, List[Any]]:
        """
        후보 제품들을 3단계로 분류합니다.
        candidates는 ProductResult 객체의 리스트입니다.
        """
        logger.info(f"Classifying {len(candidates)} candidates against source image and name: {source_name}")
        
        # 1. 소스 이미지 분석 (미리 계산)
        progress_store.set_status("검색하신 원본 이미지를 분석 중입니다...")
        source_phash = self.analyzer.get_phash(source_image_path)
        source_embedding = self.analyzer.get_embedding(source_image_path)
        source_color_hist = self.analyzer.get_color_histogram(source_image_path)
        
        # 결과를 담을 딕셔너리
        categorized = {
            1: [], # 1단계: 완전히 동일한 이미지 (100% 확정)
            2: [], # 2단계: 형태 유사 + 이름 유사 (60~70% 예측)
            3: [], # 3단계: 형태는 유사하나 스펙이다름(색상 등) (일단 스크랩대상)
            0: [], # 탈락
        }
        
        if not source_phash or source_embedding is None:
            logger.error("Failed to analyze source image.")
            return categorized
            
        total = len(candidates)
        for idx, candidate in enumerate(candidates, 1):
            short_title = candidate.title[:20] + ("..." if len(candidate.title)>20 else "")
            progress_store.set_status(f"[{candidate.platform}] {short_title} 분석 중... ({idx}/{total})")
            
            # 썸네일이 로컬에 없으면 제외
            if not candidate.local_thumbnail_path:
                logger.warning(f"Skipping candidate {candidate.id} - no local thumbnail")
                categorized[0].append(candidate)
                continue
                
            try:
                # 후보 이미지 분석
                cand_phash = self.analyzer.get_phash(candidate.local_thumbnail_path)
                cand_embedding = self.analyzer.get_embedding(candidate.local_thumbnail_path)
                cand_color_hist = self.analyzer.get_color_histogram(candidate.local_thumbnail_path)
                
                # --- 1단계 매칭 (동일 이미지) ---
                phash_diff = self.analyzer.compare_phash(source_phash, cand_phash)
                if phash_diff <= config.PHASH_THRESHOLD:
                    logger.info(f"Tier 1 Match: {candidate.id} (pHash diff: {phash_diff})")
                    candidate.match_tier = 1
                    categorized[1].append(candidate)
                    continue
                    
                # --- CLIP 기반 형태 유사도 계산 ---
                clip_sim = self.analyzer.get_clip_similarity(source_embedding, cand_embedding)
                
                # 이름 유사도 계산
                name_sim = get_name_similarity(source_name, candidate.title)
                
                # 색상 유사도 계산
                color_sim = self.analyzer.compare_colors(source_color_hist, cand_color_hist)
                
                logger.debug(f"Candidate {candidate.title[:15]} - CLIP: {clip_sim:.3f}, Name: {name_sim:.3f}, Color: {color_sim:.3f}")
                
                # --- 2단계 매칭 (형태 유사 + 이름 유사) ---
                if clip_sim >= config.CLIP_SIMILARITY_TIER2 and name_sim >= config.NAME_SIMILARITY_TIER2:
                    logger.info(f"Tier 2 Match: {candidate.id} (CLIP: {clip_sim:.2f}, Name: {name_sim:.2f})")
                    candidate.match_tier = 2
                    categorized[2].append(candidate)
                    continue
                    
                # --- 3단계 매칭 (형태 유사 + 색상/디테일 다름) ---
                # 이름 유사도는 낮더라도, 모양이 꽤 유사하고, 색상이 다른 경우
                if clip_sim >= config.CLIP_SIMILARITY_TIER3 and color_sim < config.COLOR_SIMILARITY_MAX:
                    logger.info(f"Tier 3 Match: {candidate.id} (CLIP: {clip_sim:.2f}, Color: {color_sim:.2f})")
                    candidate.match_tier = 3
                    categorized[3].append(candidate)
                    continue
                    
                # 이도저도 아니면 탈락
                candidate.match_tier = 0
                categorized[0].append(candidate)
                
            except Exception as e:
                logger.error(f"Error classifying {candidate.id}: {e}")
                categorized[0].append(candidate)
                
        # 각 티어 내에서 유사도 순 정렬 등 추가 기능 가능
        return categorized
