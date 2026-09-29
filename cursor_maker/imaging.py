"""Pillow helpers: loading, cropping, transparency and resizing."""

from PIL import Image, ImageChops, ImageDraw, ImageOps

MASTER_SIZE = 256


def load_image(path, index: int = 0) -> Image.Image:
    """Decode frame `index` of an image file (0 for still images) as RGBA."""
    img = Image.open(path)
    if index:
        img.seek(index)
    img = ImageOps.exif_transpose(img)
    return img.convert("RGBA")


def frame_info(path) -> tuple[int, float | None]:
    """(frame count, average frame duration in ms or None) of an image file.

    Animated GIF, WebP and APNG report several frames; still images report 1.
    """
    with Image.open(path) as img:
        n = getattr(img, "n_frames", 1)
        durations = []
        for i in range(n):
            img.seek(i)
            if img.info.get("duration"):
                durations.append(img.info["duration"])
    return n, (sum(durations) / len(durations) if durations else None)


def crop_square(img: Image.Image, x: int, y: int, side: int) -> Image.Image:
    return img.crop((x, y, x + side, y + side))


def _color_distance_mask(img: Image.Image, rgb, tolerance: int) -> Image.Image:
    """L mask: 255 where the pixel's max channel difference to rgb <= tolerance."""
    solid = Image.new("RGB", img.size, tuple(rgb))
    diff = ImageChops.difference(img.convert("RGB"), solid)
    r, g, b = diff.split()
    maxdiff = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return maxdiff.point(lambda v: 255 if v <= tolerance else 0)


def make_color_transparent(img: Image.Image, rgb, tolerance: int) -> Image.Image:
    """Make every pixel close to rgb fully transparent."""
    mask = _color_distance_mask(img, rgb, tolerance)
    out = img.copy()
    alpha = ImageChops.subtract(out.getchannel("A"), mask)
    out.putalpha(alpha)
    return out


def remove_background_floodfill(img: Image.Image, tolerance: int) -> Image.Image:
    """Make the background connected to the four corners transparent."""
    w, h = img.size
    base = img.convert("RGB")
    corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    region = Image.new("L", img.size, 0)
    # Fill with two different marker colors; a pixel is in the region if it
    # changed in at least one of the passes (handles pixels that already
    # equalled one marker color).
    for marker in ((255, 0, 255), (0, 255, 0)):
        work = base.copy()
        for c in corners:
            ImageDraw.floodfill(work, c, marker, thresh=tolerance)
        changed = ImageChops.difference(work, base).convert("L").point(lambda v: 255 if v else 0)
        region = ImageChops.lighter(region, changed)
    out = img.copy()
    out.putalpha(ImageChops.subtract(out.getchannel("A"), region))
    return out


def resize(img: Image.Image, size: int) -> Image.Image:
    # Pillow premultiplies RGBA internally for non-nearest filters.
    if img.size == (size, size):
        return img.copy()
    return img.resize((size, size), Image.LANCZOS)


# screen angles (degrees, y axis down) of the direction the arrow tip points
ARROW_DIRECTIONS = {
    "right": 0, "down-right": 45, "down": 90, "down-left": 135,
    "left": 180, "up-left": 225, "up": 270, "up-right": 315,
}
_SUPERSAMPLE = 4


def _arrow_shape(size: float):
    """Flat dart pointing left with its tip at (0, 0) and a concave back."""
    w1, w2, ctrl = (0.77 * size, -0.64 * size), (0.77 * size, 0.64 * size), (0.45 * size, 0.0)
    pts = [(0.0, 0.0), w1]
    steps = 12
    for i in range(1, steps):
        t = i / steps
        # quadratic Bezier from w1 to w2 bending toward the tip
        x = (1 - t) ** 2 * w1[0] + 2 * (1 - t) * t * ctrl[0] + t ** 2 * w2[0]
        y = (1 - t) ** 2 * w1[1] + 2 * (1 - t) * t * ctrl[1] + t ** 2 * w2[1]
        pts.append((x, y))
    pts.append(w2)
    return pts


def draw_hotspot_arrow(img: Image.Image, hx: int, hy: int, direction: str, size: int,
                       fill=(255, 255, 255), outline=(0, 0, 0)) -> Image.Image:
    """Composite a flat arrow whose tip sits on pixel (hx, hy)."""
    import math
    k = _SUPERSAMPLE
    rot = math.radians(ARROW_DIRECTIONS[direction] - 180)
    c, s = math.cos(rot), math.sin(rot)
    tx, ty = (hx + 0.5) * k, (hy + 0.5) * k
    pts = [(tx + (x * c - y * s) * k, ty + (x * s + y * c) * k) for x, y in _arrow_shape(size)]
    layer = Image.new("RGBA", (img.width * k, img.height * k), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.polygon(pts, fill=tuple(fill) + (255,))
    # outline as one closed line with round joins: no gaps between the curve
    # segments, and it covers the hotspot pixel at the tip
    draw.line(pts + pts[:2], fill=tuple(outline) + (255,),
              width=max(k, round(size * k / 18)), joint="curve")
    out = img.convert("RGBA").copy()
    out.alpha_composite(layer.resize(img.size, Image.LANCZOS))
    return out


_font_paths: dict[tuple, str | None] = {}


def resolve_font(family: str, bold: bool, italic: bool) -> str | None:
    """Font file for a family via fontconfig (cached). None if not found."""
    key = (family, bold, italic)
    if key not in _font_paths:
        import subprocess
        pattern = family + (":bold" if bold else "") + (":italic" if italic else "")
        try:
            path = subprocess.run(["fc-match", "-f", "%{file}", pattern], capture_output=True,
                                  text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            path = ""
        _font_paths[key] = path or None
    return _font_paths[key]


def load_font(family: str, size: int, bold: bool, italic: bool):
    from PIL import ImageFont
    path = resolve_font(family, bold, italic)
    try:
        font = ImageFont.truetype(path, size) if path else ImageFont.load_default(size)
    except OSError:
        return ImageFont.load_default(size)
    if bold:
        # variable fonts (e.g. Noto on Fedora) need the weight axis set explicitly
        try:
            axes = font.get_variation_axes()
            font.set_variation_by_axes([
                max(a["minimum"], min(700, a["maximum"])) if a.get("name") in (b"Weight", "Weight")
                else a["default"] for a in axes])
        except (OSError, AttributeError, KeyError):
            pass
    return font


def draw_text(img: Image.Image, text: str, x: int, y: int, family: str, size: int,
              bold=False, italic=False, fill=(255, 255, 255), outline=(0, 0, 0),
              outline_width=3) -> Image.Image:
    """Draw text centered on (x, y) with an optional outline."""
    out = img.convert("RGBA").copy()
    font = load_font(family, size, bold, italic)
    ImageDraw.Draw(out).multiline_text(
        (x, y), text, font=font, anchor="mm", align="center", fill=tuple(fill) + (255,),
        stroke_width=outline_width, stroke_fill=tuple(outline) + (255,))
    return out


def paste_icon(img: Image.Image, icon: Image.Image, icon_hot: tuple[int, int],
               hx: int, hy: int, size: int) -> Image.Image:
    """Composite `icon` scaled to size x size so its own hotspot lands on (hx, hy).

    icon_hot is the hotspot in the icon's original pixels. Parts falling
    outside the image are clipped.
    """
    scale = size / icon.width
    scaled = icon.resize((size, max(1, round(icon.height * scale))), Image.LANCZOS)
    ox = hx - round(icon_hot[0] * scale)
    oy = hy - round(icon_hot[1] * scale)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    layer.paste(scaled, (ox, oy))
    out = img.convert("RGBA").copy()
    out.alpha_composite(layer)
    return out
