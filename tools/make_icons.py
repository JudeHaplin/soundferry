# -*- coding: utf-8 -*-
"""从 AI 生成的原图处理出：多尺寸 .ico + 四种托盘状态图标。"""
from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SRC = ASSETS / "icon.png"  # AI 生成的原始图标（1024x1024）

# 1) 裁掉边缘水印并压成正方形
img = Image.open(SRC).convert("RGBA")
w, h = img.size
img = img.crop((int(w * 0.02), int(h * 0.02), int(w * 0.98), int(h * 0.98)))
side = min(img.size)
img = img.crop(
    (
        (img.width - side) // 2,
        (img.height - side) // 2,
        (img.width + side) // 2,
        (img.height + side) // 2,
    )
).resize((512, 512), Image.LANCZOS)

# 2) 圆角处理（超椭圆观感）
radius = int(512 * 0.22)
mask = Image.new("L", (512, 512), 0)
ImageDraw.Draw(mask).rounded_rectangle((0, 0, 511, 511), radius=radius, fill=255)
icon = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
icon.paste(img, (0, 0), mask)

# 3) 主 .ico（含多尺寸）
ico_path = ASSETS / "soundferry.ico"
icon.save(ico_path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("ico:", ico_path)

# 4) 托盘状态图标：在右下角叠加状态圆点
STATES = {
    "idle": ((150, 158, 170, 255), "空闲"),
    "busy": ((245, 166, 35, 255), "连接中"),
    "on": ((52, 199, 89, 255), "推流中"),
    "err": ((235, 87, 87, 255), "出错"),
}
base64 = icon.resize((64, 64), Image.LANCZOS)
for key, (color, label) in STATES.items():
    tray = base64.copy()
    d = ImageDraw.Draw(tray)
    d.ellipse((40, 40, 60, 60), fill=color, outline=(255, 255, 255, 255), width=3)
    out = ASSETS / f"tray_{key}.png"
    tray.save(out)
    print("tray:", out.name, label)
print("done")
