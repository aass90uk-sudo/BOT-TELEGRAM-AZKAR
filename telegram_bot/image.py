"""أدوات معالجة صور المجلة: استخراج صور الصفحات ونصوصها من ملف PDF.

يُستخدم هذا الملف لتحويل ملف مجلة PDF إلى صور صفحات (JPG) مع استخراج
النص الموجود في كل صفحة، وحفظها في magazine/images و magazine/pages.json
كي يستخدمها bot عبر magazine.py عند النشر اليومي.
"""

import json
import os

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAGAZINE_DIR = os.path.join(_BASE_DIR, "magazine")
MAGAZINE_IMAGES_DIR = os.path.join(MAGAZINE_DIR, "images")
MAGAZINE_PDF = os.path.join(MAGAZINE_DIR, "magazine.pdf")
MAGAZINE_PAGES_JSON = os.path.join(MAGAZINE_DIR, "pages.json")


def get_page_image_path(page_num: int) -> str:
    """يُعيد مسار صورة صفحة معيّنة (رقم الصفحة يبدأ من 1)."""
    return os.path.join(MAGAZINE_IMAGES_DIR, f"page_{page_num:03d}.jpg")


def page_image_exists(page_num: int) -> bool:
    return os.path.exists(get_page_image_path(page_num))


def rebuild_from_pdf(pdf_path: str = None, dpi: int = 150) -> int:
    """يستخرج صورة كل صفحة ونصها من ملف PDF ويحفظها في magazine/images
    و magazine/pages.json. يُستخدم عند استبدال محتوى المجلة بملف جديد.

    يُعيد عدد الصفحات التي تمت معالجتها.
    """
    import fitz  # PyMuPDF

    pdf_path = pdf_path or MAGAZINE_PDF
    os.makedirs(MAGAZINE_IMAGES_DIR, exist_ok=True)

    doc = fitz.open(pdf_path)
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)
    pages_data = []

    try:
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=matrix)
            pix.save(get_page_image_path(i))
            text = page.get_text().strip()
            pages_data.append({"page": i, "text": text})

        with open(MAGAZINE_PAGES_JSON, "w", encoding="utf-8") as f:
            json.dump(pages_data, f, ensure_ascii=False, indent=2)
    finally:
        doc.close()

    return len(pages_data)


if __name__ == "__main__":
    count = rebuild_from_pdf()
    print(f"تم استخراج {count} صفحة من ملف المجلة بنجاح.")
