"""Minimal Xcursor file writer/reader (no xcursorgen needed).

Format (all little-endian uint32):
  header : "Xcur", header_size=16, version=0x10000, ntoc
  toc    : ntoc * (type, subtype, position)
  image  : chunk_header=36, type=0xfffd0002, subtype=nominal size, version=1,
           width, height, xhot, yhot, delay, then width*height ARGB pixels
           (premultiplied alpha).

Animation: several images with the same nominal size, stored one after the
other in frame order, each with its delay in milliseconds.
"""

import struct

from PIL import Image

from .fileutil import atomic_open

MAGIC = b"Xcur"
FILE_HEADER_SIZE = 16
FILE_VERSION = 0x10000
IMAGE_TYPE = 0xFFFD0002
IMAGE_HEADER_SIZE = 36
IMAGE_VERSION = 1


def argb_premultiplied_bytes(img: Image.Image) -> bytes:
    """RGBA PIL image -> premultiplied ARGB32 little-endian bytes (B,G,R,A)."""
    r, g, b, a = img.convert("RGBA").convert("RGBa").split()
    return Image.merge("RGBA", (b, g, r, a)).tobytes()


def write_xcursor(path, images):
    """images: iterable of (nominal_size, xhot, yhot, RGBA PIL image[, delay_ms]).

    For an animation pass every frame of every size in frame order; the stable
    sort below groups them by size while keeping that order.
    """
    images = sorted(images, key=lambda t: t[0])
    ntoc = len(images)
    offset = FILE_HEADER_SIZE + 12 * ntoc
    toc = []
    chunks = []
    for item in images:
        size, xhot, yhot, img = item[:4]
        delay = int(item[4]) if len(item) > 4 else 0
        w, h = img.size
        xhot = max(0, min(w - 1, int(xhot)))
        yhot = max(0, min(h - 1, int(yhot)))
        chunk = struct.pack("<9I", IMAGE_HEADER_SIZE, IMAGE_TYPE, size, IMAGE_VERSION,
                            w, h, xhot, yhot, delay) + argb_premultiplied_bytes(img)
        toc.append(struct.pack("<3I", IMAGE_TYPE, size, offset))
        chunks.append(chunk)
        offset += len(chunk)
    with atomic_open(path) as f:
        f.write(MAGIC + struct.pack("<3I", FILE_HEADER_SIZE, FILE_VERSION, ntoc))
        f.writelines(toc)
        f.writelines(chunks)


def read_xcursor_info(path):
    """Return [(nominal_size, width, height, xhot, yhot, delay)] - used for verification."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != MAGIC:
        raise ValueError(f"{path}: not an Xcursor file")
    _, _, ntoc = struct.unpack_from("<3I", data, 4)
    out = []
    for i in range(ntoc):
        typ, subtype, pos = struct.unpack_from("<3I", data, FILE_HEADER_SIZE + 12 * i)
        if typ != IMAGE_TYPE:
            continue
        _, _, size, _, w, h, xh, yh, delay = struct.unpack_from("<9I", data, pos)
        if pos + IMAGE_HEADER_SIZE + 4 * w * h > len(data):
            raise ValueError(f"{path}: truncated image chunk")
        out.append((size, w, h, xh, yh, delay))
    return out


def read_xcursor_images(path):
    """Decode every image of an Xcursor file.

    Returns [(nominal_size, xhot, yhot, delay, RGBA PIL image)] in file order.
    Pixels are stored premultiplied as B,G,R,A bytes; Pillow's "BGRa" raw mode
    un-premultiplies them.
    """
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != MAGIC:
        raise ValueError(f"{path}: not an Xcursor file")
    _, _, ntoc = struct.unpack_from("<3I", data, 4)
    out = []
    for i in range(ntoc):
        typ, _, pos = struct.unpack_from("<3I", data, FILE_HEADER_SIZE + 12 * i)
        if typ != IMAGE_TYPE:
            continue
        _, _, size, _, w, h, xh, yh, delay = struct.unpack_from("<9I", data, pos)
        start = pos + IMAGE_HEADER_SIZE
        pixels = data[start:start + 4 * w * h]
        if len(pixels) != 4 * w * h:
            raise ValueError(f"{path}: truncated image chunk")
        out.append((size, xh, yh, delay, Image.frombytes("RGBA", (w, h), pixels, "raw", "BGRa")))
    return out
