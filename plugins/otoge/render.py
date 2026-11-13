"""用 Pillow 生成查分图，供 QQ 指令直接发送。"""

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from .prober import BestScores

FONT_CANDIDATES = (
    "NotoSansCJK-Regular.ttc",
    "NotoSansCJKsc-Regular.otf",
    "msyh.ttc",
    "PingFang.ttc",
)


def render_best_scores(result: BestScores, font_path: str = "") -> bytes | None:
    if not result.groups:
        return None
    regular = None
    for candidate in (font_path, *FONT_CANDIDATES):
        if candidate:
            try:
                regular = ImageFont.truetype(candidate, 19)
                break
            except (OSError, ValueError):
                continue
    if regular is None:
        return None
    title_font = regular.font_variant(size=29)
    width = 1000
    height = 112 + sum(40 + 64 * ((len(rows) + 1) // 2) for _, rows in result.groups)
    image = Image.new("RGB", (width, height), "#10192b")
    draw = ImageDraw.Draw(image)

    def fitted(text, max_width, font):
        if draw.textlength(text, font=font) <= max_width:
            return text
        while text and draw.textlength(text + "…", font=font) > max_width:
            text = text[:-1]
        return text + "…"

    draw.text((24, 20), fitted(result.title, width - 48, title_font), fill="#f5f7ff", font=title_font)
    draw.text((26, 62), f"Rating {result.rating}", fill="#89d8f1", font=regular)
    top = 92
    for label, scores in result.groups:
        draw.text((26, top), f"{label}  ·  {len(scores)} 张", fill="#89d8f1", font=regular)
        top += 39
        for index, row in enumerate(scores):
            x = 24 + index % 2 * 482
            y = top + index // 2 * 64
            draw.rounded_rectangle((x, y, x + 470, y + 58), radius=8, fill="#1b2a43")
            title = fitted(f"{index + 1:02d}  {row.title}", 446, regular)
            detail = fitted("  ·  ".join(filter(None, (row.chart, row.result, f"RA {row.rating}" if row.rating else "", row.badge))), 446, regular)
            draw.text((x + 12, y + 4), title, fill="#ffffff", font=regular)
            draw.text((x + 12, y + 29), detail, fill="#afc3da", font=regular)
        top += 64 * ((len(scores) + 1) // 2)
    output = BytesIO()
    image.save(output, format="JPEG", quality=89)
    return output.getvalue()
