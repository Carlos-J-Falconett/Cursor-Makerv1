"""Export the cursor set for Windows 11 (.cur + install.inf, zipped) and macOS
(a Mousecape .cape file).

Both exporters take a `get_frames(role_name)` callable returning
`([256x256 RGBA frames], role_config)` or None. It is supplied by
`theme.build_theme` so each cursor is rendered once for all three platforms.
A still cursor has one frame. The role names here are our own CSS names;
fallbacks (e.g. ns-resize -> n-resize) are already resolved by `get_frames`.

Animated cursors become .ani files on Windows and vertical frame strips in the
macOS cape (Mousecape stacks frames top to bottom; PointsWide/PointsHigh are
the size of one frame).
"""

import io
import os
import plistlib
import re
import shutil
import zipfile

from PIL import Image

from . import imaging
from .imaging import MASTER_SIZE
from .fileutil import atomic_open, replace_dir
from .project import frame_delay_ms
from .wincur import ani_size, cur_bytes, write_ani_blobs, write_cur

# --------------------------------------------------------------------- Windows
# The 17 pointer slots of a Windows scheme, in the exact order Windows stores
# them in HKCU\Control Panel\Cursors\Schemes. Each entry is
# (registry value name, file name we write, our role used for it).
# Animated roles are written with the .ani extension instead of .cur.
WINDOWS_ROLES = [
    ("Arrow", "Normal.cur", "default"),
    ("Help", "Help.cur", "help"),
    ("AppStarting", "Working.cur", "progress"),
    ("Wait", "Busy.cur", "wait"),
    ("Crosshair", "Precision.cur", "crosshair"),
    ("IBeam", "Text.cur", "text"),
    ("NWPen", "Handwriting.cur", "default"),   # no pen role; normal arrow instead
    ("No", "Unavailable.cur", "not-allowed"),
    ("SizeNS", "Vertical.cur", "ns-resize"),
    ("SizeWE", "Horizontal.cur", "ew-resize"),
    ("SizeNWSE", "Diagonal1.cur", "nwse-resize"),
    ("SizeNESW", "Diagonal2.cur", "nesw-resize"),
    ("SizeAll", "Move.cur", "move"),
    ("UpArrow", "Alternate.cur", "default"),    # "alternate select"; no own role
    ("Hand", "Link.cur", "pointer"),
    ("Pin", "Location.cur", "pointer"),
    ("Person", "Person.cur", "pointer"),
]


def safe_name(name: str) -> str:
    """File/INF-safe version of the theme name (ASCII letters, digits, space, _ -)."""
    return re.sub(r"[^A-Za-z0-9 _-]", "_", name).strip() or "Cursors"


def _install_inf(scheme: str, files: dict[str, str]) -> str:
    """install.inf that copies the cursors to C:\\Windows\\Cursors\\<scheme>,
    registers the scheme and makes it the active one.
    files maps each registry slot name to the file written for it."""
    cur = r"%10%\%CUR_DIR%"
    scheme_paths = ",".join(f"{cur}\\%{reg}%" for reg, _, _ in WINDOWS_ROLES)
    lines = [
        "; Cursor scheme made with Cursor Maker.",
        '; Install: right-click this file > "Show more options" > "Install".',
        "; Then: Settings > Bluetooth & devices > Mouse > Additional mouse settings >",
        ";       Pointers tab > choose the scheme > Apply.",
        "",
        "[Version]",
        'signature="$CHICAGO$"',
        "",
        "[DefaultInstall]",
        "CopyFiles = Scheme.Cur",
        "AddReg    = Scheme.Reg",
        "",
        "[DestinationDirs]",
        'Scheme.Cur = 10,"%CUR_DIR%"',     # 10 = the Windows directory
        "",
        "[Scheme.Reg]",
        # register the scheme: 17 comma-separated paths in slot order
        f'HKCU,"Control Panel\\Cursors\\Schemes","%SCHEME_NAME%",,"{scheme_paths}"',
        # and make it the current scheme (0x00020000 = REG_EXPAND_SZ)
        'HKCU,"Control Panel\\Cursors",,0x00020000,"%SCHEME_NAME%"',
    ]
    lines += [f'HKCU,"Control Panel\\Cursors","{reg}",0x00020000,"{cur}\\%{reg}%"'
              for reg, _, _ in WINDOWS_ROLES]
    lines += [
        'HKCU,"Control Panel\\Cursors","Scheme Source",0x00010001,1',   # 1 = user scheme
        "",
        "[Scheme.Cur]",
        *[files[reg] for reg, _, _ in WINDOWS_ROLES],
        "",
        "[Strings]",
        f'CUR_DIR     = "Cursors\\{scheme}"',
        f'SCHEME_NAME = "{scheme}"',
        *[f'{reg} = "{files[reg]}"' for reg, _, _ in WINDOWS_ROLES],
        "",
    ]
    return "\r\n".join(lines)   # INF files use CRLF line endings


# Reloads the cursors listed in HKCU\Control Panel\Cursors (which install.inf
# sets) without the Pointers dialog or signing out:
# SystemParametersInfo(SPI_SETCURSORS = 0x57, ..., SPIF_UPDATEINIFILE | SPIF_SENDCHANGE = 3)
APPLY_BAT = r"""@echo off
rem Made with Cursor Maker: reload the installed cursors right now.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -Name N -Namespace W -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool SystemParametersInfo(uint a, uint b, System.IntPtr c, uint d);'; if ([W.N]::SystemParametersInfo(0x57, 0, [IntPtr]::Zero, 3)) { 'Cursors reloaded.' } else { 'Windows refused to reload the cursors.' }"
pause
""".replace("\n", "\r\n")

WINDOWS_README = """{name} - Windows 11 cursor scheme (made with Cursor Maker)

1. Extract this zip.
2. Right-click install.inf > "Show more options" > "Install"
   (allow the administrator prompt; files go to C:\\Windows\\Cursors\\{name}).
3. Settings > Bluetooth & devices > Mouse > Additional mouse settings >
   Pointers tab > Scheme: "{name}" > Apply.
   Pick exactly "{name}": schemes from earlier installs under other names
   point to folders that no longer exist and show "missing or corrupt".
   Remove those old schemes with the Delete button.

Apply-Cursors.bat reloads the installed cursors immediately (after step 2),
without opening the Pointers tab or signing out.
{full_note}
Each .cur file (and every frame of an animated .ani file) contains these
sizes: {sizes} px. Windows picks the closest one for the pointer size set in
Settings > Accessibility > Mouse pointer and touch.
"""


# Windows documents no limit, but its Pointers dialog rejects big animated
# cursors as "missing or corrupt". In real testing a 1.6 MB .ani loaded while
# every file from 2.4 MB up failed, so animated cursors are kept under this
# budget by evenly dropping frames (the animation keeps its total length).
WINDOWS_MAX_ANI_BYTES = 1_600_000
WINDOWS_WARN_BYTES = 2_000_000


def large_windows_files(zip_path, limit=WINDOWS_WARN_BYTES) -> list[tuple[str, int]]:
    """[(file name, bytes)] of cursor files in an exported zip that are over limit."""
    with zipfile.ZipFile(zip_path) as z:
        return [(os.path.basename(i.filename), i.file_size) for i in z.infolist()
                if i.filename.endswith((".ani", ".cur")) and i.file_size > limit]


def _fit_ani(blobs, delay_ms, budget=WINDOWS_MAX_ANI_BYTES):
    """Evenly thin encoded frames until the .ani fits the budget.
    Returns (kept blobs, new delay_ms)."""
    n = len(blobs)
    keep = n
    while keep > 2 and ani_size([blobs[i * n // keep] for i in range(keep)]) > budget:
        keep = max(2, min(keep - 1, int(keep * budget / ani_size(
            [blobs[i * n // keep] for i in range(keep)]))))
    return [blobs[i * n // keep] for i in range(keep)], round(delay_ms * n / keep)


def build_windows_package(project, get_frames, out_dir, sizes, progress=None,
                          notes=None) -> str:
    # (project.windows_full_size: keep all frames; see WINDOWS_MAX_ANI_BYTES)
    """Write <out_dir>/<Name>-Windows/ and zip it. Returns the .zip path.

    The folder is built as "<Name>-Windows.part" and only renamed into place
    (replacing an older export) once every file is written, so a cancelled or
    interrupted export never leaves an incomplete package behind.
    """
    full = getattr(project, "windows_full_size", False)
    scheme = safe_name(project.theme_name) + ("-FullSize" if full else "")
    folder = os.path.join(out_dir, f"{scheme}-Windows")
    work = folder + ".part"
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work)
    files = {}
    try:
        for n, (reg, fname, role) in enumerate(WINDOWS_ROLES, start=1):
            found = get_frames(role)
            if found is None:   # cannot happen while all required roles are set
                raise ValueError(f"No image for {role}, needed by Windows {fname}")
            frames, cfg = found

            def cur_images(master):
                """One .cur worth of images (every size) for a single frame."""
                out = []
                for size in sizes:
                    scale = size / MASTER_SIZE
                    out.append((size, round(cfg.hot_x * scale), round(cfg.hot_y * scale),
                                imaging.resize(master, size)))
                return out

            if progress:
                progress(f"Writing Windows cursor {n}/{len(WINDOWS_ROLES)} ({fname})")
            if len(frames) > 1:
                fname = fname[:-4] + ".ani"
                blobs = [cur_bytes(cur_images(f)) for f in frames]
                if full:                     # test mode: no size limit
                    kept, delay = blobs, frame_delay_ms(cfg)
                else:
                    kept, delay = _fit_ani(blobs, frame_delay_ms(cfg))
                if len(kept) < len(blobs) and notes is not None:
                    notes.append(f"{fname}: {len(blobs)} → {len(kept)} frames "
                                 "(Windows size limit)")
                write_ani_blobs(os.path.join(work, fname), kept, delay)
            else:
                write_cur(os.path.join(work, fname), cur_images(frames[0]))
            files[reg] = fname
        with open(os.path.join(work, "install.inf"), "w", encoding="ascii", newline="") as f:
            f.write(_install_inf(scheme, files))
        with open(os.path.join(work, "INSTALL.txt"), "w", encoding="utf-8") as f:
            f.write(WINDOWS_README.format(
                name=scheme, sizes=", ".join(map(str, sorted(sizes))),
                full_note=("\nFULL-SIZE TEST BUILD: every frame is kept, so files may be over\n"
                           "the ~2 MB Windows seems to accept. The Pointers tab may call them\n"
                           "corrupt; test with Apply-Cursors.bat or by signing out and in.\n"
                           if full else "")))
        with open(os.path.join(work, "Apply-Cursors.bat"), "w", encoding="ascii",
                  newline="") as f:
            f.write(APPLY_BAT)
        if progress:
            progress("Compressing the Windows zip…")
        zip_path = os.path.join(out_dir, f"{scheme}-Windows.zip")
        with atomic_open(zip_path) as raw, zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as z:
            for fname in sorted(os.listdir(work)):
                z.write(os.path.join(work, fname), f"{scheme}-Windows/{fname}")
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)    # cancelled/failed: leave nothing behind
        raise
    replace_dir(work, folder)
    return zip_path


# ----------------------------------------------------------------------- macOS
# Mousecape cursor identifiers (names from Mousecape's MCDefs.m) -> our role.
# Camera, "poof", counting and empty cursors are not exported; macOS keeps
# its own versions of those.
_CG = "com.apple.coregraphics."
_C = "com.apple.cursor."
MAC_ROLES = {
    _CG + "Arrow": "default",
    _CG + "IBeam": "text",
    _CG + "IBeamXOR": "text",
    _CG + "Alias": "alias",
    _CG + "Copy": "copy",
    _CG + "Move": "move",
    _CG + "ArrowCtx": "context-menu",
    _CG + "Wait": "wait",
    # macOS 26 (Tahoe) looks the arrow and I-beam up under new "S" names; without
    # these the arrow and text cursor stay the system default on Tahoe
    _CG + "ArrowS": "default",
    _CG + "IBeamS": "text",
    _C + "2": "alias",            # Link
    _C + "3": "not-allowed",      # Forbidden
    _C + "4": "progress",         # Busy
    _C + "5": "copy",             # Copy Drag
    _C + "7": "crosshair",
    _C + "8": "crosshair",        # Crosshair 2
    _C + "11": "grabbing",        # Closed hand
    _C + "12": "grab",            # Open hand
    _C + "13": "pointer",         # Pointing hand
    _C + "17": "w-resize",
    _C + "18": "e-resize",
    _C + "19": "ew-resize",
    _C + "20": "crosshair",       # "Cell XOR" in Mousecape; macOS 26 uses it for the crosshair
    _C + "21": "n-resize",
    _C + "22": "s-resize",
    _C + "23": "ns-resize",
    _C + "24": "context-menu",    # Ctx Menu
    _C + "26": "vertical-text",   # IBeam H.
    _C + "27": "e-resize",        # Window E
    _C + "28": "ew-resize",       # Window E-W
    _C + "29": "ne-resize",
    _C + "30": "nesw-resize",
    _C + "31": "n-resize",
    _C + "32": "ns-resize",
    _C + "33": "nw-resize",
    _C + "34": "nwse-resize",
    _C + "35": "se-resize",
    _C + "36": "s-resize",
    _C + "37": "sw-resize",
    _C + "38": "w-resize",
    _C + "39": "move",            # Resize Square
    _C + "40": "help",
    _C + "41": "cell",
    _C + "42": "zoom-in",
    _C + "43": "zoom-out",
}
MAC_POINTS = 32                    # logical cursor size in points
MAC_SCALES = [1, 2, 5, 10]         # Mousecape's standard 1x/2x/5x/10x: 32/64/160/320 px
MAC_MAX_FRAMES = 24                # Mousecape's limit for animated cursors

MAC_README = """{name} - macOS cursor set (made with Cursor Maker)

macOS has no built-in way to change cursor themes, so this uses the free
Mousecape app (https://github.com/alexzielenski/Mousecape/releases).

1. Install and open Mousecape.
2. Drag {file} into the Mousecape window (or File > Import).
3. Select it and choose Cape > Apply (or double-click it).

The cursor is {points} pt with images up to {max_px} px. Make it bigger with
System Settings > Accessibility > Display > Pointer size.

macOS 26 (Tahoe): the cape includes the new ArrowS / IBeamS names Tahoe uses
for the arrow and text cursor. Install Mousecape's helper tool
(Mousecape > Install Helper Tool) so the cape stays applied.
"""


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _thin_frames(frames, delay_ms):
    """Evenly drop frames above Mousecape's limit; stretch the delay so the
    animation keeps its total length. Returns (frames, seconds per frame)."""
    n = len(frames)
    if n <= MAC_MAX_FRAMES:
        return frames, delay_ms / 1000
    keep = [frames[i * n // MAC_MAX_FRAMES] for i in range(MAC_MAX_FRAMES)]
    return keep, delay_ms * n / MAC_MAX_FRAMES / 1000


def _strip(frames, px):
    """Frames resized to px and stacked top to bottom in one image."""
    strip = Image.new("RGBA", (px, px * len(frames)), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        strip.paste(imaging.resize(f, px), (0, i * px))
    return strip


def build_mac_cape(project, get_frames, out_dir, progress=None) -> str:
    """Write <out_dir>/<Name>.cape (+ install notes). Returns the .cape path.
    The .cape is written atomically (see fileutil)."""
    name = safe_name(project.theme_name)
    cursors = {}
    for n, (ident, role) in enumerate(MAC_ROLES.items(), start=1):
        if progress:
            progress(f"Writing macOS cursor {n}/{len(MAC_ROLES)} ({role})")
        found = get_frames(role)
        if found is None:
            continue
        frames, cfg = found
        frames, duration = _thin_frames(frames, frame_delay_ms(cfg))
        pt_scale = MAC_POINTS / MASTER_SIZE
        cursors[ident] = {
            "FrameCount": len(frames),
            "FrameDuration": float(duration) if len(frames) > 1 else 1.0,
            "HotSpotX": float(round(cfg.hot_x * pt_scale, 2)),   # in points
            "HotSpotY": float(round(cfg.hot_y * pt_scale, 2)),
            "PointsWide": float(MAC_POINTS),
            "PointsHigh": float(MAC_POINTS),
            "Representations": [_png(_strip(frames, MAC_POINTS * s)) for s in MAC_SCALES],
        }
    cape = {
        "Author": "Cursor Maker",
        "CapeName": project.theme_name,
        "CapeVersion": 1.0,
        "Cloud": False,
        "Cursors": cursors,
        "HiDPI": True,
        "Identifier": "local.cursormaker." + re.sub(r"[^A-Za-z0-9]", "", name).lower(),
        "MinimumVersion": 2.0,
        "Version": 2.0,
    }
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}.cape")
    with atomic_open(path) as f:
        plistlib.dump(cape, f, fmt=plistlib.FMT_XML)
    with open(os.path.join(out_dir, "INSTALL-macOS.txt"), "w", encoding="utf-8") as f:
        f.write(MAC_README.format(name=name, file=os.path.basename(path), points=MAC_POINTS,
                                  max_px=MAC_POINTS * MAC_SCALES[-1]))
    return path
