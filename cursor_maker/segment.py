"""Click-to-select object segmentation with MobileSAM (ONNX).

Optional feature: needs `numpy` and `onnxruntime`, plus the model files which
are downloaded on first use to ~/.cache/cursor-maker/models.
"""

import hashlib
import os
import threading
import urllib.request
from collections import OrderedDict

from PIL import Image

MODEL_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
                         "cursor-maker", "models")
BASE_URL = "https://huggingface.co/Acly/MobileSAM/resolve/main/"
ENCODER = "mobile_sam_image_encoder.onnx"
DECODER = "sam_mask_decoder_multi.onnx"
MODELS = [ENCODER, DECODER]
INSTALL_HINT = "pip install --user numpy onnxruntime"
# finished masks are also kept on disk so exports and later sessions don't have
# to run the model again (it takes ~1 s per frame)
MASK_DIR = os.path.join(os.path.dirname(MODEL_DIR), "masks")

SAM_SIZE = 1024
PIXEL_MEAN = (123.675, 116.28, 103.53)
PIXEL_STD = (58.395, 57.12, 57.375)


def deps_available() -> bool:
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def models_present() -> bool:
    return all(os.path.isfile(os.path.join(MODEL_DIR, m)) for m in MODELS)


def download_models(progress_cb=None):
    """Download missing model files. progress_cb(done_bytes, total_bytes, name)."""
    os.makedirs(MODEL_DIR, exist_ok=True)
    for name in MODELS:
        dest = os.path.join(MODEL_DIR, name)
        if os.path.isfile(dest):
            continue
        part = dest + ".part"
        with urllib.request.urlopen(BASE_URL + name, timeout=60) as resp, \
                open(part, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while chunk := resp.read(1 << 16):
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total, name)
        os.replace(part, dest)


class Segmenter:
    def __init__(self):
        self._lock = threading.Lock()
        self._enc = self._dec = None
        self._embeddings = OrderedDict()   # key -> (embedding, scale)
        self._masks = OrderedDict()        # (key, points) -> L image

    def _load(self):
        if self._enc is not None:
            return
        import onnxruntime as ort
        if not models_present():
            raise RuntimeError("Segmentation model not downloaded")
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        prov = ["CPUExecutionProvider"]
        self._enc = ort.InferenceSession(os.path.join(MODEL_DIR, ENCODER), opts, providers=prov)
        self._dec = ort.InferenceSession(os.path.join(MODEL_DIR, DECODER), opts, providers=prov)

    @staticmethod
    def _cache_put(cache, key, value, limit):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)

    def _embedding(self, crop: Image.Image, key):
        import numpy as np
        hit = self._embeddings.get(key)
        if hit is not None:
            self._embeddings.move_to_end(key)
            return hit
        w, h = crop.size
        scale = SAM_SIZE / max(w, h)
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        rgb = np.asarray(crop.convert("RGB").resize((nw, nh), Image.BILINEAR),
                         dtype=np.float32)
        inp = self._enc.get_inputs()[0]
        if len(inp.shape) == 3:          # preprocessing built into the model
            x = rgb
        else:                             # NCHW, normalize + pad ourselves
            x = (rgb - np.array(PIXEL_MEAN, np.float32)) / np.array(PIXEL_STD, np.float32)
            x = np.pad(x, ((0, SAM_SIZE - nh), (0, SAM_SIZE - nw), (0, 0)))
            x = x.transpose(2, 0, 1)[None]
        emb = self._enc.run(None, {inp.name: x})[0]
        value = (emb, scale)
        self._cache_put(self._embeddings, key, value, 6)
        return value

    @staticmethod
    def _disk_path(key, points) -> str:
        """Mask file for (crop key, points). key[0] is the source image path; its
        modification time and size are included so an edited image is redone."""
        try:
            st = os.stat(key[0])
            stamp = (st.st_mtime_ns, st.st_size)
        except (OSError, TypeError, IndexError):
            stamp = None
        digest = hashlib.sha1(repr((key, tuple(points), stamp)).encode()).hexdigest()
        return os.path.join(MASK_DIR, digest[:2], digest + ".png")

    def cached(self, key, points) -> bool:
        points = tuple(points)
        return (key, points) in self._masks or os.path.isfile(self._disk_path(key, points))

    def mask(self, crop: Image.Image, key, points) -> Image.Image:
        """points: [(x, y, label)] in crop pixel coords; label 1=keep, 0=exclude."""
        import numpy as np
        points = tuple(points)
        with self._lock:
            hit = self._masks.get((key, points))
            if hit is not None:
                return hit
            disk = self._disk_path(key, points)
            if os.path.isfile(disk):
                try:
                    img = Image.open(disk).convert("L")
                    img.load()
                    if img.size == crop.size:
                        self._cache_put(self._masks, (key, points), img, 256)
                        return img
                except OSError:
                    pass            # unreadable cache file: compute it again
            self._load()
            emb, scale = self._embedding(crop, key)
            coords = [[x * scale, y * scale] for x, y, _ in points] + [[0.0, 0.0]]
            labels = [float(lbl) for _, _, lbl in points] + [-1.0]
            w, h = crop.size
            feeds = {
                "image_embeddings": emb,
                "point_coords": np.array([coords], np.float32),
                "point_labels": np.array([labels], np.float32),
                "mask_input": np.zeros((1, 1, 256, 256), np.float32),
                "has_mask_input": np.zeros((1,), np.float32),
                "orig_im_size": np.array([h, w], np.float32),
            }
            names = {i.name for i in self._dec.get_inputs()}
            outs = self._dec.run(None, {k: v for k, v in feeds.items() if k in names})
            out_names = [o.name for o in self._dec.get_outputs()]
            masks = outs[out_names.index("masks")] if "masks" in out_names else outs[0]
            ious = outs[out_names.index("iou_predictions")] if "iou_predictions" in out_names \
                else outs[1]
            if masks.shape[1] == 4:
                # index 0 is the single-mask output, 1..3 the multimask guesses;
                # SAM uses the guesses only for an ambiguous single click.
                best = 1 + int(np.argmax(ious[0, 1:])) if len(points) == 1 else 0
            else:
                best = int(np.argmax(ious[0]))
            m = (masks[0, best] > 0).astype(np.uint8) * 255
            img = Image.fromarray(m, "L")
            if img.size != crop.size:
                img = img.resize(crop.size, Image.BILINEAR)
            self._cache_put(self._masks, (key, points), img, 256)  # room for video frames
            try:
                os.makedirs(os.path.dirname(disk), exist_ok=True)
                tmp = disk + ".part"
                img.save(tmp, "PNG")
                os.replace(tmp, disk)
            except OSError:
                pass                # the disk cache is only an optimisation
            return img


_segmenter = None
_segmenter_lock = threading.Lock()


def get_segmenter() -> Segmenter:
    global _segmenter
    with _segmenter_lock:
        if _segmenter is None:
            _segmenter = Segmenter()
        return _segmenter
