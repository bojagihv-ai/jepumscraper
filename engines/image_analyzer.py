import os
import io
import math
from PIL import Image
import imagehash
import torch
from transformers import CLIPProcessor, CLIPModel
from numpy import dot
from numpy.linalg import norm
import config
import logging

# Set up logging
logger = logging.getLogger(__name__)

class ImageAnalyzer:
    def __init__(self):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f"Loading CLIP model '{config.CLIP_MODEL_NAME}' on {self.device}...")
        self.model = CLIPModel.from_pretrained(config.CLIP_MODEL_NAME).to(self.device)
        self.processor = CLIPProcessor.from_pretrained(config.CLIP_MODEL_NAME)
        logger.info("CLIP model loaded successfully.")

    def load_image(self, image_path_or_bytes):
        """이미지를 PIL Image 객체로 로드합니다."""
        try:
            if isinstance(image_path_or_bytes, str):
                return Image.open(image_path_or_bytes).convert("RGB")
            else:
                return Image.open(io.BytesIO(image_path_or_bytes)).convert("RGB")
        except Exception as e:
            logger.error(f"Error loading image: {e}")
            return None

    def get_phash(self, image_path_or_bytes):
        """이미지의 perceptual hash를 생성합니다."""
        img = self.load_image(image_path_or_bytes)
        if img is None:
            return None
        return str(imagehash.phash(img))

    def compare_phash(self, hash1, hash2):
        """두 pHash 값 사이의 해밍 거리를 계산합니다. 낮을수록 비슷합니다."""
        if not hash1 or not hash2:
            return 100 # 임의의 큰 값 (다름)
        
        try:
            h1 = imagehash.hex_to_hash(hash1)
            h2 = imagehash.hex_to_hash(hash2)
            return h1 - h2
        except Exception as e:
            logger.error(f"Error comparing phash: {e}")
            return 100

    def get_embedding(self, image_path_or_bytes):
        """이미지를 CLIP을 통해 임베딩 벡터로 변환합니다."""
        img = self.load_image(image_path_or_bytes)
        if img is None:
            return None
        
        inputs = self.processor(images=img, return_tensors="pt").to(self.device)
        with torch.no_grad():
            image_features = self.model.get_image_features(**inputs)
            if hasattr(image_features, 'pooler_output'):
                image_features = image_features.pooler_output
            elif hasattr(image_features, 'image_embeds'):
                image_features = image_features.image_embeds
            
        # 정규화하여 반환 (numpy 배열)
        image_features = image_features / image_features.norm(p=2, dim=-1, keepdim=True)
        return image_features.cpu().numpy()[0]

    def get_clip_similarity(self, emb1, emb2):
        """두 임베딩 벡터 간의 코사인 유사도를 계산합니다."""
        if emb1 is None or emb2 is None:
            return 0.0
        
        # 정규화된 벡터이므로 내적이 곧 코사인 유사도
        similarity = dot(emb1, emb2)
        return float(similarity)

    def get_color_histogram(self, image_path_or_bytes):
        """이미지의 색상 히스토그램을 추출합니다 (HSV 공간)."""
        img = self.load_image(image_path_or_bytes)
        if img is None:
            return None
        
        # HSV 모드로 변환
        hsv_img = img.convert('HSV')
        # 히스토그램 추출 (H, S, V 각각 256, 총 768)
        hist = hsv_img.histogram()
        
        # 정규화
        total_pixels = sum(hist)
        if total_pixels == 0:
            return hist
        
        normalized_hist = [count / total_pixels for count in hist]
        return normalized_hist

    def compare_colors(self, hist1, hist2):
        """두 히스토그램 간의 유사도를 계산합니다 (Bhattacharyya Distance). 낮을수록 비슷, 여기선 1-거리로 유사도(0~1) 반환."""
        if not hist1 or not hist2 or len(hist1) != len(hist2):
            return 0.0
        
        # 1 - Bhattacharyya distance 사용
        try:
            bhattacharyya_coeff = sum(math.sqrt(h1 * h2) for h1, h2 in zip(hist1, hist2))
            # 완벽한 일치는 1, 다르면 0에 가까워짐
            return float(bhattacharyya_coeff)
        except Exception:
            return 0.0

# Singleton 인스턴스 (필요할 때 로드)
_analyzer_instance = None

def get_analyzer():
    global _analyzer_instance
    if _analyzer_instance is None:
        _analyzer_instance = ImageAnalyzer()
    return _analyzer_instance
