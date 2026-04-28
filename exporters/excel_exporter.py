import os
import openpyxl
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from typing import List, Dict
import logging
from scrapers.base_scraper import ProductResult

logger = logging.getLogger(__name__)

class ExcelExporter:
    def export(self, products: List[ProductResult], detail_data: Dict[str, Dict], output_path: str):
        """
        엑셀 저장:
        - 제품 정보 (매칭단계, 플랫폼, 제품명, 가격, URL)
        - 썸네일
        - 상세페이지 풀 스크린샷
        - MHTML 원본 파일 경로 (= "다른 이름으로 저장" 급 품질)
        """
        logger.info(f"Exporting {len(products)} products to Excel: {output_path}")

        try:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "경쟁사_제품_분석"

            # ── 헤더 ──
            headers = ["매칭단계", "플랫폼", "제품명", "가격", "URL", "썸네일", "상세 스크린샷", "원본 MHTML 경로"]
            header_fill = PatternFill(start_color="2D3748", end_color="2D3748", fill_type="solid")
            header_font = Font(name="맑은 고딕", bold=True, color="FFFFFF", size=11)

            for col_idx, header in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col_idx)
                cell.value = header
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal='center', vertical='center')

            # 열 너비
            widths = {'A': 10, 'B': 14, 'C': 45, 'D': 14, 'E': 55, 'F': 22, 'G': 90, 'H': 50}
            for col, w in widths.items():
                ws.column_dimensions[col].width = w

            # 테두리
            thin_border = Border(
                left=Side(style='thin'), right=Side(style='thin'),
                top=Side(style='thin'), bottom=Side(style='thin')
            )

            # ── 데이터 행 ──
            tier_colors = {1: "E53E3E", 2: "DD6B20", 3: "3182CE", 0: "718096"}
            row_idx = 2

            for prod in products:
                # 텍스트 데이터
                tier_label = {1: "1단계(동일)", 2: "2단계(유사)", 3: "3단계(색상다름)", 0: "미분류"}.get(prod.match_tier, "?")
                ws.cell(row=row_idx, column=1, value=tier_label)
                ws.cell(row=row_idx, column=2, value=prod.platform)
                ws.cell(row=row_idx, column=3, value=prod.title)
                ws.cell(row=row_idx, column=4, value=prod.price)

                # URL을 하이퍼링크로
                url_cell = ws.cell(row=row_idx, column=5, value=prod.product_url)
                url_cell.hyperlink = prod.product_url
                url_cell.font = Font(color="4299E1", underline="single")

                # 행 높이
                ws.row_dimensions[row_idx].height = 100

                # 티어 색상 표시
                tier_color = tier_colors.get(prod.match_tier, "718096")
                ws.cell(row=row_idx, column=1).font = Font(bold=True, color=tier_color)

                # 테두리 적용
                for c in range(1, 9):
                    ws.cell(row=row_idx, column=c).border = thin_border

                # ── 썸네일 이미지 삽입 ──
                if prod.local_thumbnail_path and os.path.exists(prod.local_thumbnail_path):
                    try:
                        img = ExcelImage(prod.local_thumbnail_path)
                        img.width = 100
                        img.height = 100
                        ws.add_image(img, f"F{row_idx}")
                    except Exception as e:
                        logger.error(f"Cannot add thumbnail: {e}")

                # ── 상세 데이터 (새 형식: dict) ──
                product_data = detail_data.get(prod.id, {})

                # screenshots
                screenshots = product_data.get("screenshots", [])
                if screenshots:
                    col_offset = 0
                    for part_path in screenshots:
                        if not os.path.exists(part_path):
                            continue
                        try:
                            from openpyxl.utils import get_column_letter
                            img_col = 7 + col_offset
                            col_letter = get_column_letter(img_col)
                            ws.column_dimensions[col_letter].width = 120

                            img_detail = ExcelImage(part_path)
                            orig_w = img_detail.width or 800
                            orig_h = img_detail.height or 600
                            target_w = 800
                            img_detail.width = target_w
                            img_detail.height = int(target_w * (orig_h / max(orig_w, 1)))

                            ws.add_image(img_detail, f"{col_letter}{row_idx}")
                            col_offset += 1
                        except Exception as e:
                            logger.error(f"Cannot add detail image: {e}")

                # MHTML 경로
                mhtml_path = product_data.get("mhtml_path", "")
                if mhtml_path:
                    mhtml_cell = ws.cell(row=row_idx, column=8, value=mhtml_path)
                    mhtml_cell.font = Font(size=9, color="999999")
                    mhtml_cell.alignment = Alignment(wrap_text=True, vertical='top')

                row_idx += 1

            wb.save(output_path)
            logger.info("Excel file generated successfully.")
            return True

        except Exception as e:
            logger.error(f"Failed to export excel: {e}")
            return False
