"""Import an existing cursor set so it can be edited.

Supported inputs (see `import_set`):
  * Linux Xcursor theme: a theme folder (with cursors/), its cursors/ folder,
    or a .tar(.gz/.xz/.bz2) / .zip archive containing one.
  * Windows: a folder or .zip with .cur/.ani files. Slots come from install.inf
    (the Schemes line or the per-cursor registry lines); without an .inf,
    file names are matched by keywords (normal, busy, link, ...).
  * macOS: a Mousecape .cape file.

Every cursor is decoded to RGBA frames at its largest size plus hotspot and
frame delay (`ImportedSet`). `apply_import` then writes the frames as PNG files
to ~/.cache/cursor-maker/imports/ and turns them into normal, editable roles.
"""

import copy
import hashlib
import io
import os
import plistlib
import re
import shutil
import struct
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field

from PIL import Image, IcoImagePlugin

from .imaging import MASTER_SIZE
from .project import MAX_FRAMES, Frame, Project, RoleConfig
from .roles import ROLES
from .xcursor import read_xcursor_images

IMPORT_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
                          "cursor-maker", "imports")
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2")


@dataclass
class ImportedCursor:
    frames: list            # RGBA images, all the same size
    hot: tuple[int, int]    # hotspot in the frames' pixels
    delay_ms: int = 0       # per-frame delay (0 for a still cursor)


@dataclass
class ImportedSet:
    name: str
    kind: str                                   # "linux" | "windows" | "mac"
    cursors: dict[str, ImportedCursor] = field(default_factory=dict)   # role -> cursor
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ detection
def import_set(path: str) -> ImportedSet:
    """Read a cursor set from a folder or file (kind detected automatically)."""
    path = os.path.abspath(path)
    low = path.lower()
    if os.path.isdir(path):
        return _import_folder(path)
    if low.endswith(".cape"):
        return import_cape(path)
    if low.endswith(ARCHIVE_SUFFIXES):
        with tempfile.TemporaryDirectory(prefix="cursor-maker-import-") as tmp:
            _extract(path, tmp)
            result = _import_folder(tmp)
            if result.name in ("", os.path.basename(tmp)):
                result.name = _archive_stem(path)
            return result
    if low.endswith((".inf", ".cur", ".ani")):
        return import_windows(os.path.dirname(path))
    raise ValueError("Not a cursor set: choose a theme folder, a Windows cursor folder, "
                     "a .zip/.tar archive or a .cape file")


def _archive_stem(path):
    name = os.path.basename(path)
    for suffix in sorted(ARCHIVE_SUFFIXES, key=len, reverse=True):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return name


def _extract(archive, dest):
    """Unpack a zip/tar safely (no absolute paths or '..' escapes)."""
    if archive.lower().endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)                  # zipfile strips unsafe path parts
    else:
        with tarfile.open(archive) as t:
            t.extractall(dest, filter="data")   # rejects unsafe members


def _is_xcursor(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"Xcur"
    except OSError:
        return False


def _import_folder(folder) -> ImportedSet:
    """Decide what a folder holds: Linux theme(s) or Windows cursor files."""
    if os.path.isdir(os.path.join(folder, "cursors")):
        return import_linux(folder)
    if os.path.basename(folder) == "cursors":
        return import_linux(os.path.dirname(folder))
    themes, windows = [], None
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        if "cursors" in dirs and os.path.isdir(os.path.join(root, "cursors")):
            themes.append(root)
        if windows is None and any(f.lower().endswith((".cur", ".ani", ".inf")) for f in files):
            windows = root
    if themes:
        result = import_linux(themes[0])
        if len(themes) > 1:
            result.notes.append(f"The archive holds {len(themes)} themes; imported "
                                f"{os.path.basename(themes[0])}.")
        return result
    if windows:
        return import_windows(windows)
    raise ValueError("No cursors found (no Xcursor theme, .cur/.ani files or .cape)")


# --------------------------------------------------------------------- Linux
def import_linux(theme_dir) -> ImportedSet:
    """Xcursor theme: every role (or one of its legacy aliases) at its largest size."""
    name = os.path.basename(os.path.normpath(theme_dir))
    try:
        with open(os.path.join(theme_dir, "index.theme"), encoding="utf-8",
                  errors="replace") as f:
            m = re.search(r"^\s*Name\s*=\s*(.+)$", f.read(), re.M)
            if m:
                name = m.group(1).strip()
    except OSError:
        pass
    result = ImportedSet(name, "linux")
    cursors = os.path.join(theme_dir, "cursors")
    for role in ROLES:
        for candidate in (role.name, *role.aliases):
            f = os.path.join(cursors, candidate)
            if os.path.isfile(f) and _is_xcursor(f):
                try:
                    result.cursors[role.name] = _from_xcursor(f)
                except (ValueError, struct.error) as e:
                    result.notes.append(f"{role.name}: skipped ({e})")
                break
    if not result.cursors:
        raise ValueError(f"{theme_dir} has no readable Xcursor files")
    return result


def _from_xcursor(path) -> ImportedCursor:
    images = read_xcursor_images(path)
    biggest = max(size for size, *_ in images)
    frames = [i for i in images if i[0] == biggest]      # file order = frame order
    delays = [d for _, _, _, d, _ in frames if d]
    return ImportedCursor([img for *_, img in frames][:MAX_FRAMES],
                          (frames[0][1], frames[0][2]),
                          round(sum(delays) / len(delays)) if len(frames) > 1 and delays else 0)


# ------------------------------------------------------------------- Windows
# Windows pointer slots in scheme order -> our role (None = no matching role).
WINDOWS_SLOTS = ["Arrow", "Help", "AppStarting", "Wait", "Crosshair", "IBeam", "NWPen", "No",
                 "SizeNS", "SizeWE", "SizeNWSE", "SizeNESW", "SizeAll", "UpArrow", "Hand",
                 "Pin", "Person"]
WINDOWS_SLOT_ROLE = {"Arrow": "default", "Help": "help", "AppStarting": "progress",
                     "Wait": "wait", "Crosshair": "crosshair", "IBeam": "text",
                     "No": "not-allowed", "SizeNS": "ns-resize", "SizeWE": "ew-resize",
                     "SizeNWSE": "nwse-resize", "SizeNESW": "nesw-resize", "SizeAll": "move",
                     "Hand": "pointer"}
# file-name keywords used when there is no install.inf (checked in this order;
# short keywords must match a whole word, long ones may be part of a word)
WINDOWS_KEYWORDS = [
    ("NWPen", ("handwriting", "pen", "nwpen")),
    ("AppStarting", ("working", "appstarting", "background", "work")),
    ("Wait", ("busy", "wait")),
    ("Help", ("help", "question")),
    ("Crosshair", ("precision", "crosshair", "cross")),
    ("IBeam", ("text", "ibeam", "beam")),
    ("No", ("unavailable", "forbidden", "notallowed", "no")),
    ("SizeNWSE", ("diagonal1", "dgn1", "diag1", "sizenwse", "nwse")),
    ("SizeNESW", ("diagonal2", "dgn2", "diag2", "sizenesw", "nesw")),
    ("SizeNS", ("vertical", "vert", "sizens", "ns")),
    ("SizeWE", ("horizontal", "horz", "horiz", "sizewe", "we", "ew")),
    ("SizeAll", ("move", "sizeall")),
    ("UpArrow", ("alternate", "uparrow", "alt")),
    ("Hand", ("link", "hand")),
    ("Pin", ("location", "pin")),
    ("Person", ("person",)),
    ("Arrow", ("normal", "arrow", "pointer", "default", "select")),
]


def _read_text(path):
    raw = open(path, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8", errors="replace").lstrip("﻿")


def _inf_slots(inf_path) -> dict[str, str]:
    """{slot: file name} from an install.inf (Schemes line, else per-slot lines)."""
    text = _read_text(inf_path)
    strings = {}
    in_strings = False
    for line in text.splitlines():
        s = line.split(";", 1)[0].strip()
        if s.startswith("["):
            in_strings = s.lower() == "[strings]"
        elif in_strings and "=" in s:
            k, v = s.split("=", 1)
            strings[k.strip().lower()] = v.strip().strip('"')

    def expand(value):
        for _ in range(3):                       # nested %vars%
            value = re.sub(r"%([^%]+)%", lambda m: strings.get(m.group(1).lower(), ""), value)
        return re.split(r"[\\/]", value.strip())[-1]

    slots = {}
    for line in text.splitlines():
        s = line.split(";", 1)[0]
        if "control panel\\cursors\\schemes" in s.lower():
            value = re.findall(r'"([^"]*)"', s)
            if value:
                parts = value[-1].split(",")
                for slot, part in zip(WINDOWS_SLOTS, parts):
                    if part.strip():
                        slots[slot] = expand(part)
                return slots
    for line in text.splitlines():                # HKCU,"Control Panel\Cursors","Arrow",...
        m = re.match(r'\s*HKCU\s*,\s*"Control Panel\\Cursors"\s*,\s*"?(\w+)"?\s*,[^,]*,\s*"([^"]+)"',
                     line.split(";", 1)[0], re.I)
        if m and m.group(1) in WINDOWS_SLOTS:
            slots[m.group(1)] = expand(m.group(2))
    return slots


def _keyword_slot(filename):
    stem = os.path.splitext(filename)[0].lower()
    compact = re.sub(r"[^a-z0-9]", "", stem)
    words = set(re.findall(r"[a-z]+|\d+", stem))
    for slot, keys in WINDOWS_KEYWORDS:
        for k in keys:
            if (len(k) >= 4 and k in compact) or k in words:
                return slot
    return None


def import_windows(folder) -> ImportedSet:
    """Folder of .cur/.ani files, mapped to slots via install.inf or file names."""
    files = {f.lower(): f for f in os.listdir(folder)
             if f.lower().endswith((".cur", ".ani"))}
    if not files:
        raise ValueError(f"No .cur or .ani files in {folder}")
    name = os.path.basename(os.path.normpath(folder))
    slots = {}
    infs = [f for f in os.listdir(folder) if f.lower().endswith(".inf")]
    if infs:
        slots = {s: f for s, f in _inf_slots(os.path.join(folder, infs[0])).items()
                 if f.lower() in files}
        m = re.search(r'SCHEME_NAME\s*=\s*"([^"]+)"', _read_text(os.path.join(folder, infs[0])),
                      re.I)
        if m:
            name = m.group(1)
    result = ImportedSet(name, "windows")
    if not slots:
        for low, real in sorted(files.items()):
            slot = _keyword_slot(real)
            if slot and slot not in slots:
                slots[slot] = real
        result.notes.append("No usable install.inf: cursors were matched by file name.")
    for slot, fname in slots.items():
        role = WINDOWS_SLOT_ROLE.get(slot)
        if role is None:
            result.notes.append(f"{slot} ({fname}) has no matching role; not imported.")
            continue
        path = os.path.join(folder, files[fname.lower()])
        try:
            result.cursors[role] = (_from_ani(path) if path.lower().endswith(".ani")
                                    else _from_cur(open(path, "rb").read()))
        except Exception as e:                   # damaged or unusual file
            result.notes.append(f"{fname}: could not be read ({e})")
    if not result.cursors:
        raise ValueError("None of the Windows cursor files could be read")
    return result


def _cur_frame(data: bytes):
    """Largest image of a .cur/.ico file in memory -> (RGBA image, (hot_x, hot_y))."""
    _, typ, count = struct.unpack_from("<HHH", data, 0)
    if typ not in (1, 2) or count == 0:
        raise ValueError("not a cursor/icon file")
    entries = []
    for i in range(count):
        w, h, _, _, f1, f2, size, off = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        entries.append((w or 256, h or 256, f1, f2, size, off))
    best = max(range(count), key=lambda i: entries[i][0])
    w, h, hx, hy, size, off = entries[best]
    blob = data[off:off + size]
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        img = Image.open(io.BytesIO(blob)).convert("RGBA")
    else:
        # Let Pillow's ICO reader decode any DIB depth (1/4/8/24/32 bit + AND mask):
        # build a one-entry ICO whose planes/bit-count fields are the real ones.
        bpp = struct.unpack_from("<H", blob, 14)[0]
        ico = (struct.pack("<HHH", 0, 1, 1)
               + struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, 1, bpp, len(blob), 22) + blob)
        img = IcoImagePlugin.IcoFile(io.BytesIO(ico)).frame(0).convert("RGBA")
    if typ == 1:                                 # plain icon: no hotspot, use top-left
        hx = hy = 0
    return img, (hx, hy)


def _from_cur(data: bytes) -> ImportedCursor:
    img, hot = _cur_frame(data)
    return ImportedCursor([img], hot, 0)


def _from_ani(path) -> ImportedCursor:
    """RIFF 'ACON': frames from LIST 'fram', order from 'seq ', timing from 'rate'/anih."""
    data = open(path, "rb").read()
    if data[:4] != b"RIFF" or data[8:12] != b"ACON":
        raise ValueError("not an animated cursor")
    anih, rates, seq, icons = None, None, None, []
    pos = 12
    while pos + 8 <= len(data):
        tag, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        body = data[pos + 8:pos + 8 + size]
        if tag == b"anih":
            anih = struct.unpack_from("<9I", body)
        elif tag == b"rate":
            rates = list(struct.unpack_from(f"<{size // 4}I", body))
        elif tag == b"seq ":
            seq = list(struct.unpack_from(f"<{size // 4}I", body))
        elif tag == b"LIST" and body[:4] == b"fram":
            p = 4
            while p + 8 <= len(body):
                t, sz = body[p:p + 4], struct.unpack_from("<I", body, p + 4)[0]
                if t == b"icon":
                    icons.append(body[p + 8:p + 8 + sz])
                p += 8 + sz + (sz % 2)
        pos += 8 + size + (size % 2)
    if anih is None or not icons:
        raise ValueError("animated cursor has no frames")
    if not anih[8] & 1:
        raise ValueError("raw-bitmap animated cursors are not supported")
    decoded = [_cur_frame(i) for i in icons]
    order = [i for i in (seq or range(len(decoded))) if i < len(decoded)][:MAX_FRAMES]
    jiffies = rates if rates else [anih[7]] * len(order)
    delay = round(sum(jiffies[:len(order)]) / max(1, len(order)) * 1000 / 60)
    size = max(decoded[i][0].size for i in order)
    frames = [_fit(decoded[i][0], size) for i in order]
    return ImportedCursor(frames, decoded[order[0]][1], delay if len(frames) > 1 else 0)


def _fit(img, size):
    """Frames of one cursor must share a size; pad smaller ones (top-left kept)."""
    if img.size == size:
        return img
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    canvas.paste(img, (0, 0))
    return canvas


# --------------------------------------------------------------------- macOS
def import_cape(path) -> ImportedSet:
    """Mousecape cape: largest representation, split into frames (stacked top to bottom)."""
    from .platforms import MAC_ROLES
    with open(path, "rb") as f:
        cape = plistlib.load(f)
    result = ImportedSet(str(cape.get("CapeName") or os.path.splitext(os.path.basename(path))[0]),
                         "mac")
    cursors = cape.get("Cursors") or {}
    for ident, role in MAC_ROLES.items():        # first listed identifier wins per role
        if role in result.cursors or ident not in cursors:
            continue
        c = cursors[ident]
        try:
            reps = [Image.open(io.BytesIO(bytes(r))).convert("RGBA")
                    for r in c.get("Representations", [])]
            if not reps:
                continue
            rep = max(reps, key=lambda r: r.width)
            n = max(1, int(c.get("FrameCount", 1)))
            fh = rep.height // n
            frames = [rep.crop((0, i * fh, rep.width, (i + 1) * fh)) for i in range(n)]
            scale = rep.width / float(c.get("PointsWide") or rep.width)
            hot = (round(float(c.get("HotSpotX", 0)) * scale),
                   round(float(c.get("HotSpotY", 0)) * scale))
            delay = round(float(c.get("FrameDuration", 0)) * 1000) if n > 1 else 0
            result.cursors[role] = ImportedCursor(frames[:MAX_FRAMES], hot, delay)
        except Exception as e:
            result.notes.append(f"{ident}: could not be read ({e})")
    if not result.cursors:
        raise ValueError("The cape has no cursors this app can use")
    return result


# ---------------------------------------------------------------- into project
# Required roles a set may lack (Windows has no single-edge resize or grab
# cursors): fill them from the closest imported cursor, else the arrow.
FILL_FROM = {
    "n-resize": "ns-resize", "s-resize": "ns-resize",
    "e-resize": "ew-resize", "w-resize": "ew-resize",
    "ne-resize": "nesw-resize", "sw-resize": "nesw-resize",
    "nw-resize": "nwse-resize", "se-resize": "nwse-resize",
    "grab": "pointer", "grabbing": "pointer", "progress": "wait", "wait": "progress",
    "move": "all-scroll",
}


def apply_import(project: Project, imp: ImportedSet, source_path: str) -> list[str]:
    """Replace the project's roles with the imported cursors. Frames are saved as
    PNGs under IMPORT_DIR so the project can reference them. Returns notes."""
    key = hashlib.sha1(f"{source_path}|{os.path.getmtime(source_path)}".encode()).hexdigest()[:10]
    folder = os.path.join(IMPORT_DIR, re.sub(r"[^A-Za-z0-9_-]+", "_", imp.name)[:40] + "-" + key)
    if os.path.isdir(folder):
        shutil.rmtree(folder)
    notes = list(imp.notes)
    roles = {r.name: RoleConfig() for r in ROLES}
    for role, cur in imp.cursors.items():
        os.makedirs(os.path.join(folder, role), exist_ok=True)
        paths = []
        for i, img in enumerate(cur.frames):
            p = os.path.join(folder, role, f"{i:03d}.png")
            img.save(p)
            paths.append(p)
        w, h = cur.frames[0].size
        side = max(w, h)                                  # square, padded if needed
        cfg = roles[role]
        cfg.source = paths[0]
        cfg.frames = [Frame(p) for p in paths] if len(paths) > 1 else []
        # the editor's FPS control goes up to 60
        cfg.fps = min(60.0, round(1000 / cur.delay_ms, 2)) if cur.delay_ms else 10.0
        cfg.crop_side = side
        cfg.crop_x, cfg.crop_y = (w - side) // 2, (h - side) // 2
        scale = MASTER_SIZE / side
        cfg.hot_x = max(0, min(MASTER_SIZE - 1, round((cur.hot[0] - cfg.crop_x) * scale)))
        cfg.hot_y = max(0, min(MASTER_SIZE - 1, round((cur.hot[1] - cfg.crop_y) * scale)))
    for role in ROLES:                                    # fill missing required roles
        if role.required and not roles[role.name].is_set:
            src = FILL_FROM.get(role.name)
            if not (src and roles[src].is_set):
                src = "default" if roles["default"].is_set else next(
                    (n for n, c in roles.items() if c.is_set), None)
            if src:
                roles[role.name] = copy.deepcopy(roles[src])
                notes.append(f"{role.name}: not in the set, copied from {src}")
    project.roles = roles
    project.theme_name = f"{imp.name}-Edit"
    small = [r for r, c in imp.cursors.items() if c.frames[0].width < 96]
    if small:
        notes.append(f"{len(small)} cursors are smaller than 96 px in the original and "
                     "will look soft at large sizes.")
    return notes
