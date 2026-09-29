"""Write a complete Xcursor theme directory (and optional Windows/macOS exports)."""

import os

from . import imaging
from .imaging import MASTER_SIZE
from .project import (TRANS_OBJECT, ImageCache, Project, frame_delay_ms, frame_list,
                      render_frame, uses_role_icon)
from .roles import ROLE_BY_NAME, ROLES
from .xcursor import write_xcursor

EXPORT_SIZES = [256, 192, 128, 96]
SMALL_SIZES = [64, 48, 32, 24]
MARKER = "X-CursorMaker=true"


class ThemeExistsError(Exception):
    """Target directory holds a theme that was not made by Cursor Maker."""


class Cancelled(Exception):
    """The user cancelled a running export."""


def theme_dir(project: Project) -> str:
    return os.path.join(os.path.expanduser(project.output_dir), project.theme_name)


def export_dir(project: Project) -> str:
    """Folder for the Windows .zip and macOS .cape: <export_dir>/<theme_name>."""
    return os.path.join(os.path.expanduser(project.export_dir), project.theme_name)


def _is_foreign_theme(path: str) -> bool:
    index = os.path.join(path, "index.theme")
    if not os.path.exists(path):
        return False
    if not os.path.exists(index):
        return bool(os.listdir(path))
    with open(index, encoding="utf-8", errors="replace") as f:
        return MARKER not in f.read()


def _remove(path: str):
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)


def _validate(project: Project):
    """Raise a readable error if the project cannot be exported yet."""
    name = project.theme_name.strip()
    if not name or "/" in name:
        raise ValueError("Theme name must be non-empty and contain no '/'")
    missing = project.missing_required()
    if missing:
        raise ValueError("Missing images for: " + ", ".join(missing))

    ai_roles = [n for n, c in project.roles.items()
                if c.is_set and c.trans_mode == TRANS_OBJECT
                and (c.obj_points or any(f.obj_points for f in c.frames))]
    if ai_roles:
        from . import segment
        if not segment.deps_available():
            raise RuntimeError(f"Roles {', '.join(ai_roles)} use AI object selection, which "
                               f"needs: {segment.INSTALL_HINT}")
        if not segment.models_present():
            raise RuntimeError("AI object selection model is not downloaded yet "
                               "(select the AI mode in the app to download it)")


def _sizes(project: Project) -> list[int]:
    return EXPORT_SIZES + (SMALL_SIZES if project.include_small else [])


def _frame_source(project: Project, cache: ImageCache, progress=None, cancel=None):
    """get_frames(role_name) -> ([256 frames], config) or None, with rendering
    shared between the Linux theme and the Windows/macOS exports.

    Rendered once per source role, or once per (source, role) when the source
    shows the role-specific hotspot icon, so a fallback such as ew-resize gets
    the <-> icon rather than e-resize's ->.

    progress(text) is called for every rendered frame; cancel is a
    threading.Event checked between frames (raises Cancelled when set).
    """
    rendered = {}

    def get_frames(role_name):
        src = project.resolve(role_name)
        if src is None:
            return None
        cfg = project.roles[src]
        key = (src, role_name) if uses_role_icon(cfg) else (src, src)
        if key not in rendered:
            n = len(frame_list(cfg))
            frames = []
            for i in range(n):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if progress:
                    progress(f"Rendering {role_name}: frame {i + 1}/{n} "
                             f"({len(rendered)} cursors done)")
                frames.append(render_frame(cfg, cache, i, key[1], project.icon_theme))
            rendered[key] = frames
        return rendered[key], cfg
    return get_frames


def export_platform(project: Project, platform: str, cache: ImageCache | None = None,
                    progress=None, cancel=None, notes=None) -> str:
    """Export only the Windows ("windows") or macOS ("mac") package, without
    touching the Linux theme. Returns the written .zip / .cape path.
    progress/cancel: see _frame_source. notes (a list) receives remarks such
    as Windows cursors that had frames dropped to fit the size limit."""
    from . import platforms
    _validate(project)
    cache = cache or ImageCache()
    get_frames = _frame_source(project, cache, progress, cancel)
    other = export_dir(project)
    os.makedirs(other, exist_ok=True)
    if platform == "windows":
        return platforms.build_windows_package(project, get_frames, other, _sizes(project),
                                               progress, notes)
    if platform == "mac":
        return platforms.build_mac_cape(project, get_frames, other, progress)
    raise ValueError(f"Unknown platform {platform!r}")


def build_theme(project: Project, cache: ImageCache | None = None,
                overwrite_foreign: bool = False, progress=None,
                cancel=None) -> tuple[str, list[str], list[str]]:
    """Generate the theme. Returns (theme directory, notes, extra export files).

    The Linux theme is always written; the Windows zip and macOS cape are
    added when project.export_windows / project.export_mac are set.
    progress/cancel: see _frame_source.
    """
    _validate(project)
    name = project.theme_name.strip()
    cache = cache or ImageCache()
    out = theme_dir(project)
    if _is_foreign_theme(out) and not overwrite_foreign:
        raise ThemeExistsError(out)
    cursors = os.path.join(out, "cursors")
    os.makedirs(cursors, exist_ok=True)

    sizes = _sizes(project)
    notes = []
    get_frames = _frame_source(project, cache, progress, cancel)

    def xcursor_images(frames, cfg):
        """All (size, hot_x, hot_y, image, delay) entries for one cursor file."""
        delay = frame_delay_ms(cfg) if len(frames) > 1 else 0
        images = []
        for size in sizes:
            scale = size / MASTER_SIZE
            hx, hy = round(cfg.hot_x * scale), round(cfg.hot_y * scale)
            images += [(size, hx, hy, imaging.resize(f, size), delay) for f in frames]
        return images

    def needs_own_file(role_name):
        """Own image, or a fallback whose image carries a role-specific icon."""
        src = project.resolve(role_name)
        return src is not None and (src == role_name or uses_role_icon(project.roles[src]))

    # Pass 1: roles with their own image(s) get a real file.
    written = set()
    for role in ROLES:
        if not needs_own_file(role.name):
            continue
        frames, cfg = get_frames(role.name)
        path = os.path.join(cursors, role.name)
        _remove(path)
        write_xcursor(path, xcursor_images(frames, cfg))
        written.add(role.name)
        if len(frames) > 1:
            notes.append(f"{role.name}: animated, {len(frames)} frames")

    # Pass 2: roles without an image become symlinks to the role they fall
    # back to (keeps animated themes small instead of duplicating files).
    for role in ROLES:
        src_name = project.resolve(role.name)
        if src_name is None:
            notes.append(f"{role.name}: no image, inherited from Adwaita")
            continue
        if role.name in written:          # already a real file (own image or icon)
            if src_name != role.name:
                notes.append(f"{role.name}: using image from {src_name}")
            continue
        link = os.path.join(cursors, role.name)
        _remove(link)
        os.symlink(src_name, link)
        written.add(role.name)
        notes.append(f"{role.name}: using image from {src_name}")

    for role in ROLES:
        if role.name not in written:
            continue
        for alias in role.aliases:
            if alias in ROLE_BY_NAME:
                continue
            link = os.path.join(cursors, alias)
            _remove(link)
            os.symlink(role.name, link)

    with open(os.path.join(out, "index.theme"), "w", encoding="utf-8") as f:
        f.write("[Icon Theme]\n"
                f"Name={name}\n"
                "Comment=Made with Cursor Maker\n"
                "Inherits=Adwaita\n"
                f"{MARKER}\n")

    extra = []
    if project.export_windows or project.export_mac:
        from . import platforms
        other = export_dir(project)
        os.makedirs(other, exist_ok=True)
        if project.export_windows:
            extra.append(platforms.build_windows_package(project, get_frames, other, sizes,
                                                         progress, notes))
        if project.export_mac:
            extra.append(platforms.build_mac_cape(project, get_frames, other, progress))
    return out, notes, extra
