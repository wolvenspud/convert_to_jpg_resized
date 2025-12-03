#!/usr/bin/env python3
"""
• Convert all .webp and .png files in the current directory to .jpg
  (largest dimension = 600 px), making transparent areas white,
  then delete the originals.
• Resize all existing .jpg/.jpeg files so their largest dimension is 600 px
  (overwriting in-place).

Requires: Pillow  (pip install pillow)
"""

from pathlib import Path
from PIL import Image, ImageOps

MAX_DIM = 600
JPEG_OPTS = dict(format="JPEG", quality=90, optimize=True, progressive=True)

def resize(img: Image.Image) -> Image.Image:
    """Resize maintaining aspect ratio so longest side = MAX_DIM."""
    img = ImageOps.exif_transpose(img)
    img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)
    return img

def to_jpeg_rgb(img: Image.Image) -> Image.Image:
    """
    Convert image to RGB for JPEG.
    If it has transparency, composite onto a white background.
    """
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        # Convert to RGBA to ensure alpha is accessible
        img = img.convert("RGBA")
        white_bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(white_bg, img)
        return img.convert("RGB")
    return img.convert("RGB")

# ---- 1) convert .webp / .png → .jpg ----------------------------------------
created_jpgs = set()

for src_path in Path(".").iterdir():
    if src_path.suffix.lower() not in {".webp", ".png"}:
        continue

    with Image.open(src_path) as im:
        resized = resize(im)
        rgb_jpeg_ready = to_jpeg_rgb(resized)

        dst_path = src_path.with_suffix(".jpg")
        rgb_jpeg_ready.save(dst_path, **JPEG_OPTS)

    src_path.unlink()        # remove the source only after success
    created_jpgs.add(dst_path.name)
    print(f"{src_path.name} → {dst_path.name} ({rgb_jpeg_ready.width}×{rgb_jpeg_ready.height}), source removed")

# ---- 2) resize existing JPEGs ----------------------------------------------
for jpg_path in Path(".").glob("*.jp*g"):
    if jpg_path.name in created_jpgs:
        continue

    with Image.open(jpg_path) as im:
        if max(im.size) <= MAX_DIM:
            continue

        resized = resize(im).convert("RGB")
        resized.save(jpg_path, **JPEG_OPTS)

    print(f"{jpg_path.name} resized in-place to {resized.width}×{resized.height}")
