"""Per-role settings and project save/load."""

import json
import os
from collections import OrderedDict
from dataclasses import asdict, dataclass, field

from PIL import Image, ImageChops

from . import imaging
from .imaging import MASTER_SIZE
from .roles import ROLE_BY_NAME, ROLES

TRANS_NONE = "none"
TRANS_COLOR = "color"
TRANS_FLOOD = "flood"
TRANS_OBJECT = "object"


MAX_FRAMES = 120          # hard limit per cursor
WARN_FRAMES = 60          # above this the exported files get large


@dataclass
class Frame:
    """One animation frame: an image file (and frame number inside it)."""
    source: str
    index: int = 0        # frame number inside a multi-frame file (GIF/WebP/APNG)
    # own selection (x, y, side); only used when the role's same_crop is False
    crop: tuple[int, int, int] | None = None
    # own AI object clicks [(x, y, label)]; only used when the role's
    # same_points is False (None = use the role's points)
    obj_points: list[tuple[int, int, int]] | None = None


@dataclass
class RoleConfig:
    source: str | None = None
    crop_x: int = 0
    crop_y: int = 0
    crop_side: int = MASTER_SIZE
    trans_mode: str = TRANS_NONE
    trans_color: tuple[int, int, int] = (255, 255, 255)
    tolerance: int = 32
    hot_x: int = 0      # hotspot in 256x256 master coordinates
    hot_y: int = 0
    # object selection clicks: (x, y, label) in master coords, 1=keep 0=exclude
    obj_points: list[tuple[int, int, int]] = field(default_factory=list)
    # hotspot icon: "icon" = the standard cursor icon for the role being written
    # (from Project.icon_theme), placed with its own hotspot on ours;
    # "flat" = a flat arrow whose tip is on the hotspot (direction/colors below)
    arrow: bool = False
    arrow_style: str = "icon"
    arrow_dir: str = "up-left"
    arrow_size: int = 64          # icon size in 256x256 master pixels
    arrow_fill: tuple[int, int, int] = (255, 255, 255)
    arrow_outline: tuple[int, int, int] = (0, 0, 0)
    # text label; position is the text center in master coords
    text_on: bool = False
    text: str = ""
    text_x: int = 128
    text_y: int = 224
    text_size: int = 40
    text_font: str = "Sans"
    text_bold: bool = True
    text_italic: bool = False
    text_fill: tuple[int, int, int] = (255, 255, 255)
    text_outline: tuple[int, int, int] = (0, 0, 0)
    text_outline_width: int = 3
    # animation: empty frames = still cursor made from `source`. When frames
    # exist, `source` mirrors frames[0].source so is_set keeps working.
    frames: list[Frame] = field(default_factory=list)
    fps: float = 10.0
    same_crop: bool = True    # one selection (crop_x/y/side) for every frame
    same_points: bool = True  # one set of AI object clicks (obj_points) for every frame

    @property
    def is_set(self) -> bool:
        return self.source is not None

    @property
    def is_animated(self) -> bool:
        return len(self.frames) > 1


@dataclass
class Project:
    theme_name: str = "MyCursors"
    output_dir: str = os.path.expanduser("~/.icons")
    include_small: bool = False
    # extra packages written next to the Linux theme, into <export_dir>/<theme_name>/
    export_windows: bool = False     # .cur files + install.inf, zipped
    export_mac: bool = False         # Mousecape .cape file
    # test option: keep every frame in Windows .ani files (no ~2 MB size limit);
    # packaged under the scheme name "<theme>-FullSize"
    windows_full_size: bool = False
    export_dir: str = os.path.expanduser("~/Documents/Cursor Maker")
    icon_theme: str = "Adwaita"      # installed cursor theme the hotspot icons come from
    roles: dict[str, RoleConfig] = field(
        default_factory=lambda: {r.name: RoleConfig() for r in ROLES})

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Project":
        data = json.loads(text)
        roles = {r.name: RoleConfig() for r in ROLES}
        for name, cfg in data.get("roles", {}).items():
            if name in roles:
                cfg["trans_color"] = tuple(cfg.get("trans_color", (255, 255, 255)))
                cfg["obj_points"] = [tuple(pt) for pt in cfg.get("obj_points", [])]
                for key in ("arrow_fill", "arrow_outline", "text_fill", "text_outline"):
                    if key in cfg:
                        cfg[key] = tuple(cfg[key])
                if cfg.get("arrow") and "arrow_style" not in cfg:
                    cfg["arrow_style"] = "flat"      # projects from before icons existed
                cfg["frames"] = [
                    Frame(f["source"], f.get("index", 0),
                          tuple(f["crop"]) if f.get("crop") else None,
                          [tuple(pt) for pt in f["obj_points"]]
                          if f.get("obj_points") is not None else None)
                    for f in cfg.get("frames", [])]
                roles[name] = RoleConfig(**cfg)
        return cls(theme_name=data.get("theme_name", "MyCursors"),
                   output_dir=data.get("output_dir", os.path.expanduser("~/.icons")),
                   include_small=data.get("include_small", False),
                   export_windows=data.get("export_windows", False),
                   export_mac=data.get("export_mac", False),
                   windows_full_size=data.get("windows_full_size", False),
                   export_dir=data.get("export_dir",
                                       os.path.expanduser("~/Documents/Cursor Maker")),
                   icon_theme=data.get("icon_theme", "Adwaita"),
                   roles=roles)

    def missing_required(self) -> list[str]:
        return [r.name for r in ROLES if r.required and not self.roles[r.name].is_set]

    def resolve(self, name: str) -> str | None:
        """Name of the role whose image is used for `name` (following fallbacks)."""
        seen = set()
        while name and name not in seen:
            seen.add(name)
            if self.roles[name].is_set:
                return name
            name = ROLE_BY_NAME[name].fallback
        return None


class ImageCache:
    """Keeps decoded source frames so re-rendering is cheap.

    LRU-limited because a video import can have ~100 large frames; evicted
    frames are simply decoded again from disk when needed.
    """

    LIMIT = 48

    def __init__(self):
        self._cache: OrderedDict[tuple[str, int], Image.Image] = OrderedDict()

    def get(self, path: str) -> Image.Image:
        return self.get_frame(path, 0)

    def get_frame(self, path: str, index: int) -> Image.Image:
        key = (path, index)
        img = self._cache.get(key)
        if img is None:
            img = imaging.load_image(path, index)
            self._cache[key] = img
            while len(self._cache) > self.LIMIT:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(key)
        return img


def frame_list(cfg: RoleConfig) -> list[Frame]:
    """Frames of a role; a still cursor is one frame made from cfg.source."""
    return cfg.frames if cfg.frames else [Frame(cfg.source)]


def frame_crop(cfg: RoleConfig, i: int = 0) -> tuple[int, int, int]:
    """(x, y, side) selection used for frame i."""
    frames = frame_list(cfg)
    f = frames[min(i, len(frames) - 1)]
    if not cfg.same_crop and f.crop is not None:
        return f.crop
    return cfg.crop_x, cfg.crop_y, cfg.crop_side


def frame_delay_ms(cfg: RoleConfig) -> int:
    return max(10, round(1000 / max(0.1, cfg.fps)))


def render_crop(cfg: RoleConfig, cache: ImageCache, i: int = 0) -> Image.Image:
    """The selected square of frame i at source resolution, before transparency."""
    frames = frame_list(cfg)
    f = frames[min(i, len(frames) - 1)]
    x, y, side = frame_crop(cfg, i)
    return imaging.crop_square(cache.get_frame(f.source, f.index), x, y, side)


def crop_key(cfg: RoleConfig, i: int = 0):
    """Identifies frame i's selected pixels (AI masks are cached by this)."""
    frames = frame_list(cfg)
    f = frames[min(i, len(frames) - 1)]
    return (f.source, f.index, *frame_crop(cfg, i))


def frame_points(cfg: RoleConfig, i: int = 0) -> list[tuple[int, int, int]]:
    """AI object clicks used for frame i (the frame's own ones when the role's
    frames don't share points and the frame has some; else the role's)."""
    if not cfg.same_points and cfg.frames:
        f = cfg.frames[min(i, len(cfg.frames) - 1)]
        if f.obj_points is not None:
            return f.obj_points
    return cfg.obj_points


def crop_points(cfg: RoleConfig, side: int, i: int = 0):
    """Frame i's object points converted from master coords to crop pixel coords."""
    f = side / MASTER_SIZE
    return [(min(side - 1, int((x + 0.5) * f)), min(side - 1, int((y + 0.5) * f)), lbl)
            for x, y, lbl in frame_points(cfg, i)]


def mask_ready(cfg: RoleConfig, i: int = 0) -> bool:
    """True if rendering frame i will not need to run the segmentation model."""
    if cfg.trans_mode != TRANS_OBJECT or not frame_points(cfg, i):
        return True
    from .segment import get_segmenter
    return get_segmenter().cached(crop_key(cfg, i), crop_points(cfg, frame_crop(cfg, i)[2], i))


def apply_transparency(img: Image.Image, cfg: RoleConfig, i: int = 0) -> Image.Image:
    """Apply the role's transparency mode to frame i's crop."""
    if cfg.trans_mode == TRANS_COLOR:
        return imaging.make_color_transparent(img, cfg.trans_color, cfg.tolerance)
    if cfg.trans_mode == TRANS_FLOOD:
        return imaging.remove_background_floodfill(img, cfg.tolerance)
    if cfg.trans_mode == TRANS_OBJECT and frame_points(cfg, i):
        from .segment import get_segmenter
        mask = get_segmenter().mask(img, crop_key(cfg, i), crop_points(cfg, img.width, i))
        out = img.copy()
        out.putalpha(ImageChops.darker(out.getchannel("A"), mask))
        return out
    return img


def add_arrow(master: Image.Image, cfg: RoleConfig, role: str | None = None,
              icon_theme: str = "Adwaita") -> Image.Image:
    """Hotspot marker: the role's standard cursor icon, or the flat arrow.

    `role` is the cursor being written (it can differ from the role the
    settings come from, e.g. for fallbacks), so each gets its own icon.
    """
    if not cfg.arrow:
        return master
    if cfg.arrow_style == "icon" and role:
        from .cursor_icons import load_icon
        found = load_icon(icon_theme, role)
        if found is not None:
            icon, ix, iy = found
            return imaging.paste_icon(master, icon, (ix, iy), cfg.hot_x, cfg.hot_y,
                                      cfg.arrow_size)
    return imaging.draw_hotspot_arrow(master, cfg.hot_x, cfg.hot_y, cfg.arrow_dir,
                                      cfg.arrow_size, cfg.arrow_fill, cfg.arrow_outline)


def add_text(master: Image.Image, cfg: RoleConfig) -> Image.Image:
    if not cfg.text_on or not cfg.text.strip():
        return master
    return imaging.draw_text(master, cfg.text, cfg.text_x, cfg.text_y, cfg.text_font,
                             cfg.text_size, cfg.text_bold, cfg.text_italic, cfg.text_fill,
                             cfg.text_outline, cfg.text_outline_width)


def add_overlays(master: Image.Image, cfg: RoleConfig, role: str | None = None,
                 icon_theme: str = "Adwaita") -> Image.Image:
    """Hotspot icon/arrow and text drawn on top of the 256x256 master."""
    return add_text(add_arrow(master, cfg, role, icon_theme), cfg)


def render_frame(cfg: RoleConfig, cache: ImageCache, i: int = 0, role: str | None = None,
                 icon_theme: str = "Adwaita") -> Image.Image:
    """Final 256x256 RGBA image of frame i: crop -> transparency -> resize -> overlays.
    `role` / `icon_theme` pick the hotspot icon (see add_arrow)."""
    crop = apply_transparency(render_crop(cfg, cache, i), cfg, i)
    return add_overlays(imaging.resize(crop, MASTER_SIZE), cfg, role, icon_theme)


def render_master(cfg: RoleConfig, cache: ImageCache, role: str | None = None,
                  icon_theme: str = "Adwaita") -> Image.Image:
    """Final 256x256 RGBA image for a role (its first frame)."""
    return render_frame(cfg, cache, 0, role, icon_theme)


def render_frames(cfg: RoleConfig, cache: ImageCache, role: str | None = None,
                  icon_theme: str = "Adwaita") -> list[Image.Image]:
    """All 256x256 frames of a role (one item for a still cursor)."""
    return [render_frame(cfg, cache, i, role, icon_theme) for i in range(len(frame_list(cfg)))]


def uses_role_icon(cfg: RoleConfig) -> bool:
    """True if the rendered image depends on which role it is written for."""
    return cfg.arrow and cfg.arrow_style == "icon"
