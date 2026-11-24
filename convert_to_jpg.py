#!/usr/bin/env python3
"""
• Convert all .webp and .png files in the current directory to .jpg
  (largest dimension = 600 px), making transparent areas white,
  then delete the originals.
• Resize all existing .jpg/.jpeg files so their largest dimension is 600 px
  (overwriting in-place).
• Keep metadata: the source's colour profile, EXIF, XMP, IPTC, comment and DPI
  are carried over, and whatever the source lacks gets a neutral placeholder
  (sRGB profile, baseline EXIF, minimal XMP, 72 dpi), so no output is a bare
  JPEG. JPEGs that are already small enough are not re-encoded; the blocks
  they lack are spliced in.

Requires: Pillow  (pip install pillow)
"""

import os
import shutil
import struct
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import ExifTags, Image, ImageCms, ImageOps

MAX_DIM = 600
JPEG_OPTS = dict(format="JPEG", quality=90, optimize=True, progressive=True)

# ---- placeholders, used only for metadata the source does not have ---------
SOFTWARE = "convert_to_jpg.py"
DEFAULT_DPI = (72, 72)
NOW = datetime.now().strftime("%Y:%m:%d %H:%M:%S")
SRGB_PROFILE = ImageCms.createProfile("sRGB")
SRGB_ICC = ImageCms.ImageCmsProfile(SRGB_PROFILE).tobytes()
DUMMY_XMP = (
    '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/"'
    ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
    f' xmp:CreatorTool="{SOFTWARE}" dc:format="image/jpeg"/>'
    "</rdf:RDF></x:xmpmeta>"
    '<?xpacket end="w"?>'
).encode()

Tag, IFD = ExifTags.Base, ExifTags.IFD
# PNG text chunks have no JPEG counterpart; the standard ones map onto EXIF
PNG_TEXT_TO_EXIF = {
    "Description": Tag.ImageDescription,
    "Title": Tag.ImageDescription,
    "Author": Tag.Artist,
    "Artist": Tag.Artist,
    "Copyright": Tag.Copyright,
    "Software": Tag.Software,
}
MAX_SEGMENT = 65533  # largest payload one JPEG marker segment can hold
XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"


# ---- pixels ----------------------------------------------------------------
def flatten(img: Image.Image) -> Image.Image:
    """Composite any transparency onto white; return an 8-bit RGB or CMYK image."""
    if img.mode.startswith("I"):  # 16-bit greyscale: scale down instead of clipping
        img = img.convert("I").point(lambda v: v / 257).convert("L")
    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(white, rgba).convert("RGB")
    return img if img.mode == "CMYK" else img.convert("RGB")


def to_rgb(img: Image.Image, icc: bytes | None) -> tuple[Image.Image, bytes]:
    """
    Return an RGB image and the colour profile to embed with it.
    An RGB profile is carried over untouched, CMYK is converted through its
    profile into sRGB, and anything untagged is labelled sRGB.
    """
    if icc:
        try:
            profile = ImageCms.ImageCmsProfile(BytesIO(icc))
            space = profile.profile.xcolor_space.strip()
            if space == img.mode == "RGB":
                return img, icc
            if space == img.mode == "CMYK":
                srgb = ImageCms.profileToProfile(img, profile, SRGB_PROFILE, outputMode="RGB")
                return srgb, SRGB_ICC
        except (ImageCms.PyCMSError, OSError, ValueError):
            pass  # unreadable profile: treat the image as untagged
    return img.convert("RGB"), SRGB_ICC


# ---- metadata --------------------------------------------------------------
def pick_dpi(info: dict, exif: Image.Exif) -> tuple[int, int]:
    """Resolution from the file header, else from EXIF, else the default."""
    dpi = info.get("dpi")
    if not dpi and Tag.XResolution in exif and Tag.YResolution in exif:
        per_inch = 2.54 if exif.get(Tag.ResolutionUnit) == 3 else 1  # 3 = per cm
        dpi = (exif[Tag.XResolution] * per_inch, exif[Tag.YResolution] * per_inch)
    try:
        x, y = (round(float(v)) for v in dpi)
    except (TypeError, ValueError, ZeroDivisionError):
        return DEFAULT_DPI
    return (x, y) if 0 < x < 65536 and 0 < y < 65536 else DEFAULT_DPI


def fill_exif(exif: Image.Exif, size, dpi, title: str, colour_space: int) -> bytes:
    """Serialise EXIF for the output: source tags kept, gaps filled with placeholders."""
    for tag, value in {
        Tag.ImageDescription: title,
        Tag.XResolution: dpi[0],
        Tag.YResolution: dpi[1],
        Tag.ResolutionUnit: 2,  # inches
        Tag.Software: SOFTWARE,
        Tag.DateTime: NOW,
        Tag.YCbCrPositioning: 1,  # centred
    }.items():
        exif.setdefault(tag, value)
    exif[Tag.Orientation] = 1  # any rotation is already applied to the pixels
    for tag, value in ((Tag.ImageWidth, size[0]), (Tag.ImageLength, size[1])):
        if tag in exif:
            exif[tag] = value

    sub = dict(exif.get_ifd(IFD.Exif))
    if IFD.Interop in sub:
        sub[IFD.Interop] = exif.get_ifd(IFD.Interop)
    sub.pop(Tag.MakerNote, None)  # its internal offsets do not survive a rewrite
    for tag, value in {
        Tag.ExifVersion: b"0232",
        Tag.DateTimeOriginal: NOW,
        Tag.DateTimeDigitized: NOW,
        Tag.ComponentsConfiguration: b"\x01\x02\x03\x00",  # Y, Cb, Cr
        Tag.FlashPixVersion: b"0100",
        Tag.ColorSpace: colour_space,
    }.items():
        sub.setdefault(tag, value)
    sub[Tag.ExifImageWidth], sub[Tag.ExifImageHeight] = size
    exif[IFD.Exif] = sub
    return fix_undefined_types(exif.tobytes())


def fix_undefined_types(data: bytes) -> bytes:
    """
    Pillow writes these EXIF fields with data type BYTE where the standard
    requires UNDEFINED. Both are one byte per value, so only the type code in
    each directory entry needs correcting.
    """
    tags = {Tag.ComponentsConfiguration, Tag.UserComment, Tag.FileSource, Tag.SceneType}
    buf, tiff = bytearray(data), 6  # the TIFF structure starts after "Exif\0\0"
    order = "<" if buf[tiff:tiff + 2] == b"II" else ">"

    def entries(ifd_offset):
        (count,) = struct.unpack_from(order + "H", buf, tiff + ifd_offset)
        return [tiff + ifd_offset + 2 + 12 * i for i in range(count)]

    (ifd0,) = struct.unpack_from(order + "L", buf, tiff + 4)
    for entry in entries(ifd0):
        tag, _, _, value = struct.unpack_from(order + "HHLL", buf, entry)
        if tag == IFD.Exif:
            for sub_entry in entries(value):
                sub_tag, kind = struct.unpack_from(order + "HH", buf, sub_entry)
                if sub_tag in tags and kind == 1:
                    struct.pack_into(order + "H", buf, sub_entry + 2, 7)
    return bytes(buf)


def safe_exif(exif: Image.Exif, *args) -> bytes:
    """fill_exif(), falling back to placeholders alone if the source EXIF cannot be rewritten."""
    try:
        data = fill_exif(exif, *args)
        if len(data) <= MAX_SEGMENT:
            return data
    except Exception:  # malformed source EXIF can fail in many ways when re-serialised
        pass
    return fill_exif(Image.Exif(), *args)


def segment(marker: int, payload: bytes) -> bytes:
    """One JPEG marker segment."""
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


# ---- conversion ------------------------------------------------------------
def to_jpeg(src: Image.Image, title: str) -> tuple[Image.Image, dict]:
    """Return the resized RGB image and the metadata keywords to save it with."""
    img = ImageOps.exif_transpose(src)  # a copy, with EXIF rotation applied
    info, exif = img.info, img.getexif()
    dpi = pick_dpi(info, exif)
    for key, tag in PNG_TEXT_TO_EXIF.items():
        if isinstance(info.get(key), str):
            exif.setdefault(tag, info[key])

    rgb, icc = to_rgb(flatten(img), info.get("icc_profile"))
    rgb.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)

    xmp = info.get("xmp") or info.get("XML:com.adobe.xmp")
    if isinstance(xmp, str):
        xmp = xmp.encode()
    if not xmp or len(xmp) > MAX_SEGMENT - len(XMP_HEADER):
        xmp = DUMMY_XMP

    colour_space = 1 if icc is SRGB_ICC else 0xFFFF  # sRGB, else "see profile"
    meta = dict(
        icc_profile=icc,
        exif=safe_exif(exif, rgb.size, dpi, title, colour_space),
        xmp=xmp,
        dpi=dpi,
    )
    if info.get("comment"):
        meta["comment"] = info["comment"]
    iptc = [data for name, data in getattr(src, "applist", []) if name == "APP13"]
    if iptc:
        meta["extra"] = b"".join(segment(0xED, data) for data in iptc)
    return rgb, meta


def replace_file(dst: Path, like: Path, write) -> None:
    """
    Write via a temp file and swap it in, so a failed write never leaves a
    truncated image behind. The new file takes the permissions of `like`.
    """
    tmp = dst.with_name(f".{dst.name}.tmp")
    try:
        write(tmp)
        shutil.copymode(like, tmp)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def add_missing_metadata(path: Path) -> list[str]:
    """
    Splice placeholder EXIF / colour profile / XMP into a JPEG that lacks
    them, without re-encoding its pixels. Returns the names of what was added.
    """
    added = {}
    with Image.open(path) as im:
        if im.format != "JPEG":
            return []
        tag_srgb = im.mode == "RGB" and not im.info.get("icc_profile")
        if not im.info.get("exif"):
            dpi = pick_dpi(im.info, Image.Exif())
            exif = fill_exif(Image.Exif(), im.size, dpi, path.stem, 1 if tag_srgb else 0xFFFF)
            added["EXIF"] = segment(0xE1, exif)
        if tag_srgb:
            added["sRGB profile"] = segment(0xE2, b"ICC_PROFILE\x00\x01\x01" + SRGB_ICC)
        if not im.info.get("xmp"):
            added["XMP"] = segment(0xE1, XMP_HEADER + DUMMY_XMP)
    if not added:
        return []

    data = path.read_bytes()
    pos = 2  # just past the SOI marker; skip the JFIF header and any existing EXIF
    while data[pos:pos + 2] == b"\xff\xe0" or (
        data[pos:pos + 2] == b"\xff\xe1" and data[pos + 4:pos + 10] == b"Exif\x00\x00"
    ):
        pos += 2 + struct.unpack(">H", data[pos + 2:pos + 4])[0]
    spliced = data[:pos] + b"".join(added.values()) + data[pos:]
    replace_file(path, path, lambda tmp: tmp.write_bytes(spliced))
    return list(added)


# ---- 1) convert .webp / .png → .jpg ----------------------------------------
created_jpgs = set()

for src_path in sorted(Path(".").iterdir()):
    if src_path.suffix.lower() not in {".webp", ".png"}:
        continue

    with Image.open(src_path) as im:
        jpeg, meta = to_jpeg(im, src_path.stem)

    dst_path = src_path.with_suffix(".jpg")
    replace_file(dst_path, src_path, lambda tmp: jpeg.save(tmp, **JPEG_OPTS, **meta))

    src_path.unlink()        # remove the source only after success
    created_jpgs.add(dst_path.name)
    print(f"{src_path.name} → {dst_path.name} ({jpeg.width}×{jpeg.height}), source removed")

# ---- 2) resize existing JPEGs ----------------------------------------------
for jpg_path in sorted(Path(".").glob("*.jp*g")):
    if jpg_path.name in created_jpgs:
        continue

    jpeg = None
    with Image.open(jpg_path) as im:
        if max(im.size) > MAX_DIM:
            jpeg, meta = to_jpeg(im, jpg_path.stem)

    if jpeg:
        replace_file(jpg_path, jpg_path, lambda tmp: jpeg.save(tmp, **JPEG_OPTS, **meta))
        print(f"{jpg_path.name} resized in-place to {jpeg.width}×{jpeg.height}")
    elif added := add_missing_metadata(jpg_path):
        print(f"{jpg_path.name} kept as is, added placeholder {', '.join(added)}")
