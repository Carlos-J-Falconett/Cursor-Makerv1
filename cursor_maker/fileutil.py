"""Crash-safe file writing.

Exports can be large and slow; if the app is closed or an export is cancelled
halfway, a file must never be left half-written under its final name (it
would look finished but be corrupt). Everything is written to "<name>.part"
first and renamed into place only when complete.
"""

import os
import shutil
from contextlib import contextmanager


@contextmanager
def atomic_open(path, mode="wb", **kwargs):
    """open() replacement: writes to path + '.part', renames on success."""
    tmp = path + ".part"
    try:
        with open(tmp, mode, **kwargs) as f:
            yield f
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def replace_dir(tmp_dir, final_dir):
    """Swap a fully written folder into place (removing the old one)."""
    if os.path.isdir(final_dir):
        shutil.rmtree(final_dir)
    os.rename(tmp_dir, final_dir)
