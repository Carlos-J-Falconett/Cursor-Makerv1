"""Minimal Windows .cur writer (static cursors, several sizes per file).

A .cur file is an ICO file with type 2 and the hotspot stored where ICO keeps
planes / bit count. Layout (all little-endian):

  ICONDIR     : reserved=0 (u16), type=2 (u16), count (u16)
  ICONDIRENTRY: width (u8, 0 means 256), height (u8, 0 means 256),
                color count=0 (u8), reserved=0 (u8), hotspot x (u16),
                hotspot y (u16), bytes in image (u32), image offset (u32)
  image data  : BITMAPINFOHEADER (40 bytes, height = 2 * real height because it
                counts the XOR image plus the AND mask), then 32-bit BGRA
                pixels bottom-up, then a 1-bit AND mask bottom-up with each row
                padded to 4 bytes (1 = transparent). Windows takes transparency
                from the alpha channel for 32-bit images; the mask is kept
                consistent for older APIs and other readers.

Sizes below 64 px are stored as uncompressed DIBs; 64 px and larger are
stored as PNG (a complete PNG file in place of the DIB, detected by its
signature), which Windows supports in .ico/.cur/.ani since Vista. Without PNG a
256x256 frame alone is 256 KB, and big animated cursors reach tens of MB, which
Windows' Pointers dialog then rejects as "missing or corrupt".

Animated cursors use the .ani format, a RIFF file:

  "RIFF" size "ACON"
    "anih" 36  : cbSize=36, nFrames, nSteps, cx=0, cy=0, bitCount=0, planes=0,
                 displayRate (in jiffies, 1/60 s), flags=1 (AF_ICON: frames
                 are complete .ico/.cur files)
    "LIST" size "fram"
      "icon" size <complete .cur file>   (one per frame)

Every RIFF chunk is padded to an even length.
"""

import struct

from PIL import Image

from .fileutil import atomic_open

PNG_MIN_SIZE = 64           # sizes >= this are PNG-compressed
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ICONDIR_SIZE = 6
ENTRY_SIZE = 16
BITMAPINFOHEADER_SIZE = 40


def _dib_bytes(img: Image.Image) -> bytes:
    """RGBA image -> BITMAPINFOHEADER + BGRA rows (bottom-up) + AND mask."""
    w, h = img.size
    img = img.convert("RGBA")
    a = img.getchannel("A")
    clear = a.point(lambda v: 255 if v == 0 else 0)        # fully transparent pixels
    # zero the color of transparent pixels so legacy (mask-based) drawing shows nothing
    img = Image.composite(Image.new("RGBA", img.size, (0, 0, 0, 0)), img, clear)
    r, g, b, a = img.split()
    # straight (not premultiplied) BGRA; flip because DIB rows go bottom-up
    xor = Image.merge("RGBA", (b, g, r, a)).transpose(Image.FLIP_TOP_BOTTOM).tobytes()
    # AND mask: bit 1 = transparent. Windows uses alpha for 32-bit cursors, but
    # older APIs and other readers still rely on this mask.
    mask_row = ((w + 31) // 32) * 4           # 1 bit per pixel, rows padded to 4 bytes
    bits = clear.convert("1").transpose(Image.FLIP_TOP_BOTTOM).tobytes()
    packed = (w + 7) // 8                     # Pillow packs "1" rows to whole bytes
    and_mask = b"".join(bits[y * packed:(y + 1) * packed].ljust(mask_row, b"\0")
                        for y in range(h))
    header = struct.pack("<IiiHHIIiiII", BITMAPINFOHEADER_SIZE, w, h * 2, 1, 32, 0,
                         len(xor) + len(and_mask), 0, 0, 0, 0)
    return header + xor + and_mask


def _png_bytes(img: Image.Image) -> bytes:
    """RGBA image as a PNG file (used for large cursor sizes)."""
    import io
    buf = io.BytesIO()
    img.convert("RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def cur_bytes(images) -> bytes:
    """A complete .cur file in memory. images: (size, hot_x, hot_y, RGBA image)."""
    images = sorted(images, key=lambda t: t[0])
    blobs = [_png_bytes(img) if img.width >= PNG_MIN_SIZE else _dib_bytes(img)
             for _, _, _, img in images]
    offset = ICONDIR_SIZE + ENTRY_SIZE * len(images)
    entries = []
    for (size, hx, hy, img), blob in zip(images, blobs):
        w, h = img.size
        hx = max(0, min(w - 1, int(hx)))
        hy = max(0, min(h - 1, int(hy)))
        entries.append(struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, hx, hy,
                                   len(blob), offset))
        offset += len(blob)
    return struct.pack("<HHH", 0, 2, len(images)) + b"".join(entries) + b"".join(blobs)


def write_cur(path, images):
    """images: iterable of (size, hot_x, hot_y, RGBA PIL image), any order."""
    with atomic_open(path) as f:
        f.write(cur_bytes(images))


def _chunk(tag: bytes, payload: bytes) -> bytes:
    """RIFF chunk: 4-char tag, u32 size, payload, pad byte if the size is odd."""
    return tag + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) % 2 else b"")


ANI_FLAG_ICON = 1   # AF_ICON: frames are stored as .ico/.cur data


def write_ani(path, frames, delay_ms: int):
    """Animated cursor. frames: list (one per frame) of .cur image lists,
    i.e. [[(size, hot_x, hot_y, RGBA image), ...], ...]; all frames get delay_ms."""
    write_ani_blobs(path, [cur_bytes(f) for f in frames], delay_ms)


def ani_size(cur_blobs) -> int:
    """File size write_ani_blobs would produce for these encoded frames."""
    return 12 + (8 + 36) + 8 + 4 + sum(8 + len(b) + len(b) % 2 for b in cur_blobs)


def write_ani_blobs(path, cur_blobs, delay_ms: int):
    """Animated cursor from already encoded frames (complete .cur files)."""
    jiffies = max(1, round(delay_ms * 60 / 1000))
    n = len(cur_blobs)
    anih = struct.pack("<9I", 36, n, n, 0, 0, 0, 0, jiffies, ANI_FLAG_ICON)
    fram = b"fram" + b"".join(_chunk(b"icon", b) for b in cur_blobs)
    body = b"ACON" + _chunk(b"anih", anih) + _chunk(b"LIST", fram)
    with atomic_open(path) as f:
        f.write(b"RIFF" + struct.pack("<I", len(body)) + body)


def read_ani(path):
    """(anih fields dict, [embedded .cur bytes]) - used for testing."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"RIFF" or data[8:12] != b"ACON":
        raise ValueError(f"{path}: not an .ani file")
    if struct.unpack_from("<I", data, 4)[0] != len(data) - 8:
        raise ValueError(f"{path}: RIFF size mismatch")
    info, icons, pos = {}, [], 12
    while pos < len(data):
        tag, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        payload = data[pos + 8:pos + 8 + size]
        if tag == b"anih":
            vals = struct.unpack("<9I", payload)
            info = dict(zip(("cbSize", "nFrames", "nSteps", "cx", "cy", "bitCount", "planes",
                             "rate", "flags"), vals))
        elif tag == b"LIST" and payload[:4] == b"fram":
            p = 4
            while p < len(payload):
                t, sz = payload[p:p + 4], struct.unpack_from("<I", payload, p + 4)[0]
                if t == b"icon":
                    icons.append(payload[p + 8:p + 8 + sz])
                p += 8 + sz + (sz % 2)
        pos += 8 + size + (size % 2)
    return info, icons


def read_cur_images(path_or_bytes):
    """Decode every image of a .cur: [(width, height, hot_x, hot_y, format, RGBA)]
    where format is "png" or "dib" - used for testing."""
    import io
    data = path_or_bytes if isinstance(path_or_bytes, bytes) else open(path_or_bytes, "rb").read()
    _, _, count = struct.unpack_from("<HHH", data, 0)
    out = []
    for i in range(count):
        w, h, _, _, hx, hy, size, off = struct.unpack_from(
            "<BBBBHHII", data, ICONDIR_SIZE + ENTRY_SIZE * i)
        w, h, blob = w or 256, h or 256, data[off:off + size]
        if blob.startswith(PNG_SIGNATURE):
            img, fmt = Image.open(io.BytesIO(blob)).convert("RGBA"), "png"
        else:
            px = blob[BITMAPINFOHEADER_SIZE:BITMAPINFOHEADER_SIZE + w * h * 4]
            img = Image.frombytes("RGBA", (w, h), px, "raw", "BGRA").transpose(
                Image.FLIP_TOP_BOTTOM)
            fmt = "dib"
        out.append((w, h, hx, hy, fmt, img))
    return out


def read_cur_entries(path_or_bytes):
    """[(width, height, hot_x, hot_y)] from a .cur directory - used for testing."""
    if isinstance(path_or_bytes, bytes):
        data, path = path_or_bytes, "<bytes>"
    else:
        path = path_or_bytes
        with open(path, "rb") as f:
            data = f.read()
    reserved, typ, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or typ != 2:
        raise ValueError(f"{path}: not a .cur file")
    out = []
    for i in range(count):
        w, h, _, _, hx, hy, size, off = struct.unpack_from(
            "<BBBBHHII", data, ICONDIR_SIZE + ENTRY_SIZE * i)
        if off + size > len(data):
            raise ValueError(f"{path}: truncated image {i}")
        out.append((w or 256, h or 256, hx, hy))
    return out
