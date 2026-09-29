"""Standard cursor icons taken from an installed cursor theme.

Used for the "hotspot icon": each cursor can show the normal icon for its
role (arrow for default, hand for pointer, I-beam for text, ...) with the
icon's own hotspot placed on ours. Icons come from any installed Xcursor theme;
Adwaita (always present on GNOME) is the default and the final fallback.
"""

import os
from functools import lru_cache

from .roles import ROLE_BY_NAME
from .xcursor import read_xcursor_images

ICON_DIRS = [os.path.expanduser("~/.icons"), os.path.expanduser("~/.local/share/icons"),
             "/usr/share/icons"]
DEFAULT_THEME = "Adwaita"
MARKER = "X-CursorMaker=true"    # same marker theme.py writes into our own themes


def _theme_paths(name: str) -> list[str]:
    """All folders called `name` in the icon directories (user dirs first)."""
    return [os.path.join(d, name) for d in ICON_DIRS if os.path.isdir(os.path.join(d, name))]


def _read_index(path: str) -> str:
    try:
        with open(os.path.join(path, "index.theme"), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def list_cursor_themes() -> list[str]:
    """Names of installed cursor themes, excluding ones made by Cursor Maker
    (so a theme never stamps its own images onto itself)."""
    names = set()
    for d in ICON_DIRS:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            path = os.path.join(d, name)
            if os.path.isdir(os.path.join(path, "cursors")) and MARKER not in _read_index(path):
                names.add(name)
    return sorted(names, key=str.lower)


def _inherits(name: str) -> list[str]:
    """Themes listed in `Inherits=` of a theme's index.theme."""
    for path in _theme_paths(name):
        for line in _read_index(path).splitlines():
            if line.strip().lower().startswith("inherits="):
                value = line.split("=", 1)[1].replace('"', "")
                return [t.strip() for t in value.split(",") if t.strip()]
    return []


def find_icon_file(theme: str, role: str) -> str | None:
    """Cursor file for a role: role name, then its legacy aliases, searched in
    the theme, its Inherits chain, then Adwaita; finally the theme's default."""
    role_def = ROLE_BY_NAME.get(role)
    names = [role] + list(role_def.aliases if role_def else ())
    order, queue = [], [theme]
    while queue:                         # breadth-first over Inherits, no repeats
        t = queue.pop(0)
        if t not in order:
            order.append(t)
            queue += _inherits(t)
    if DEFAULT_THEME not in order:
        order.append(DEFAULT_THEME)
    for candidates in (names, ["default", "left_ptr"]):
        for t in order:
            for path in _theme_paths(t):
                for n in candidates:
                    f = os.path.join(path, "cursors", n)
                    if os.path.isfile(f):
                        return f
    return None


@lru_cache(maxsize=128)
def load_icon(theme: str, role: str):
    """(RGBA image at the theme's largest size, hot_x, hot_y) for a role, or None.
    Animated theme cursors (e.g. Adwaita's wait) use their first frame."""
    path = find_icon_file(theme, role)
    if path is None:
        return None
    images = read_xcursor_images(path)
    if not images:
        return None
    biggest = max(size for size, *_ in images)
    size, xh, yh, _, img = next(i for i in images if i[0] == biggest)   # first frame
    return img, xh, yh
