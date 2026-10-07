# ruff: noqa: INP001
from pathlib import Path

from PIL import Image, ImageDraw

qa = Path(__file__).resolve().parent
for viewport in ("desktop", "small"):
    for theme in ("light", "dark"):
        shots = sorted((qa / "screenshots").glob(f"{viewport}-{theme}-*.png"))
        for offset in range(0, len(shots), 4):
            sheet = Image.new("RGB", (1440, 1540), "#dde3ed")
            draw = ImageDraw.Draw(sheet)
            for i, shot in enumerate(shots[offset : offset + 4]):
                image = Image.open(shot).convert("RGB")
                image.thumbnail((704, 732))
                x = (i % 2) * 720 + (720 - image.width) // 2
                y = (i // 2) * 770 + 30
                sheet.paste(image, (x, y))
                draw.text(((i % 2) * 720 + 12, (i // 2) * 770 + 8), shot.name, fill="black")
            sheet.save(qa / "screenshots" / f"contact-{viewport}-{theme}-{offset // 4 + 1}.jpg", quality=92)
print("Contact sheets saved for current screenshots")  # noqa: T201 - CLI progress output
