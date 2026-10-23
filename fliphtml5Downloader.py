"""
FlipHTML5 Downloader - pure Python, no Node required.

Works with old format (fliphtml5_pages = list) and new format
(fliphtml5_pages / bookConfig = encrypted "v01..." string).

New format is decrypted with FlipHTML5's own deString WASM
(static.fliphtml5.com/.../js/deString.js) executed via the
pip package `wasmtime`.

Install deps:
    pip install requests img2pdf Pillow wasmtime

Usage:
    python3 fliphtml5Downloader.py
    python3 fliphtml5Downloader.py ceeok/pkdn
    python3 fliphtml5Downloader.py https://online.fliphtml5.com/ceeok/pkdn/
"""

import base64
import io
import json
import os
import re
import sys
from urllib.parse import urljoin

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DESTRING_URL = (
    "https://static.fliphtml5.com/resourceFiles/html5_templates/js/deString.js"
)
DESTRING_WASM_RE = re.compile(r"data:application/octet-stream;base64,([A-Za-z0-9+/=]+)")
WASM_PAGE_SIZE = 65536

_session = requests.Session()
_session.headers.update({"User-Agent": UA})

# ---------------------------------------------------------------------------
# Pure-Python deString WASM runtime (no Node)
# ---------------------------------------------------------------------------

_runtime_cache = {}


def _cache_js_path():
    base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, ".cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "deString.js")


def _ensure_destring_js():
    js_path = _cache_js_path()
    if os.path.exists(js_path) and os.path.getsize(js_path) > 0:
        return js_path
    print("Downloading deString.js decoder (one time)...")
    r = _session.get(DESTRING_URL, timeout=30)
    r.raise_for_status()
    with open(js_path, "wb") as f:
        f.write(r.content)
    return js_path


def _extract_wasm(js_path):
    with open(js_path, "r", encoding="utf-8", errors="replace") as f:
        code = f.read()
    m = DESTRING_WASM_RE.search(code)
    if not m:
        raise RuntimeError("embedded deString wasm not found in deString.js")
    return base64.b64decode(m.group(1))


class _DeStringRuntime:
    def __init__(self, wasm_bytes):
        from wasmtime import Linker, Module, Store

        store = Store()
        module = Module(store.engine, wasm_bytes)
        linker = Linker(store.engine)
        self._define_imports(linker, module)
        instance = linker.instantiate(store, module)
        exports = instance.exports(store)
        self._store = store
        self._memory = exports["memory"]
        self._malloc = exports["malloc"]
        self._free = exports["free"]
        self._destring = exports["DeString"]
        exports["emscripten_stack_init"](store)
        exports["__wasm_call_ctors"](store)

    def _import_type(self, module, mod, name):
        for imp in module.imports:
            if imp.module == mod and imp.name == name:
                return imp.type
        raise RuntimeError("missing wasm import %s.%s" % (mod, name))

    def _define_imports(self, linker, module):
        linker.define_func(
            "env", "emscripten_run_script",
            self._import_type(module, "env", "emscripten_run_script"),
            self._run_script, access_caller=True,
        )
        linker.define_func(
            "env", "emscripten_memcpy_big",
            self._import_type(module, "env", "emscripten_memcpy_big"),
            self._memcpy_big, access_caller=True,
        )
        linker.define_func(
            "wasi_snapshot_preview1", "fd_write",
            self._import_type(module, "wasi_snapshot_preview1", "fd_write"),
            self._fd_write, access_caller=True,
        )
        linker.define_func(
            "env", "emscripten_resize_heap",
            self._import_type(module, "env", "emscripten_resize_heap"),
            self._resize_heap, access_caller=True,
        )

    @staticmethod
    def _caller_memory(caller):
        mem = caller.get("memory")
        if mem is None:
            raise RuntimeError("wasm memory unavailable")
        return mem

    def _run_script(self, _caller, _ptr):
        return None

    def _memcpy_big(self, caller, dest, src, size):
        if size <= 0:
            return None
        mem = self._caller_memory(caller)
        chunk = bytes(mem.read(caller, src, src + size))
        mem.write(caller, chunk, dest)
        return None

    def _fd_write(self, caller, _fd, iovs, iovs_len, nwritten):
        mem = self._caller_memory(caller)
        total = 0
        for i in range(iovs_len):
            base = iovs + i * 8
            ln = int.from_bytes(mem.read(caller, base + 4, base + 8), "little")
            total += ln
        mem.write(caller, total.to_bytes(4, "little"), nwritten)
        return 0

    def _resize_heap(self, caller, requested):
        from wasmtime import WasmtimeError

        mem = self._caller_memory(caller)
        cur = mem.data_len(caller)
        if requested <= cur:
            return 1
        pages = (requested - cur + WASM_PAGE_SIZE - 1) // WASM_PAGE_SIZE
        try:
            mem.grow(caller, pages)
            return 1
        except WasmtimeError:
            return 0

    def decode(self, value):
        data = value.encode("utf-8") + b"\x00"
        ptr = self._malloc(self._store, len(data))
        try:
            self._memory.write(self._store, data, ptr)
            out = self._destring(self._store, ptr)
            return self._read_c_string(out)
        finally:
            self._free(self._store, ptr)

    def _read_c_string(self, ptr):
        if ptr <= 0:
            return ""
        mem_len = self._memory.data_len(self._store)
        if ptr >= mem_len:
            return ""
        raw = bytes(self._memory.read(self._store, ptr, mem_len))
        nul = raw.find(b"\x00")
        if nul >= 0:
            raw = raw[:nul]
        return raw.decode("utf-8", errors="replace")


def _get_runtime():
    if "rt" in _runtime_cache:
        return _runtime_cache["rt"]
    try:
        import wasmtime  # noqa: F401
    except ImportError:
        raise RuntimeError("pip package 'wasmtime' is required: pip install wasmtime")
    js_path = _ensure_destring_js()
    rt = _DeStringRuntime(_extract_wasm(js_path))
    _runtime_cache["rt"] = rt
    return rt


def destring_decode(value):
    rt = _get_runtime()
    text = rt.decode(value)
    # trim trailing garbage after final ] or }
    j = text.rfind("]")
    b = text.rfind("}")
    end = max(j, b)
    if end != -1:
        text = text[: end + 1]
    return text


def parse_pages_json(text):
    raw = text.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        s = raw.find("[")
        e = raw.rfind("]")
        if s == -1 or e == -1 or e <= s:
            return None
        try:
            data = json.loads(raw[s: e + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, list) else None


def decode_pages(pages_raw):
    """Accept old list format or new encrypted string. Returns list or None."""
    if isinstance(pages_raw, list):
        return pages_raw
    if isinstance(pages_raw, str):
        t = pages_raw.strip()
        if t.startswith("["):
            return parse_pages_json(t)
        print("Encrypted fliphtml5_pages detected, decoding with wasmtime (no Node)...")
        decoded = destring_decode(t)
        pages = parse_pages_json(decoded)
        if pages is None:
            raise RuntimeError("could not parse decoded fliphtml5_pages")
        return pages
    return None


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def normalize_book_name(raw):
    raw = raw.strip().strip("'\"")
    m = re.search(r"online\.fliphtml5\.com/([^#?\s]+)", raw)
    if m:
        raw = m.group(1)
    m = re.search(r"fliphtml5\.com/([^#?\s]+)", raw)
    if m:
        raw = m.group(1)
    raw = raw.strip("/").split("#")[0].split("?")[0]
    # drop trailing /index.html etc, keep first two segments
    parts = [p for p in raw.split("/") if p]
    if len(parts) >= 2 and parts[-1] in ("index.html", "index.htm"):
        parts = parts[:-1]
    if len(parts) > 2:
        parts = parts[:2]
    return "/".join(parts)


def fetch_config(book_name):
    url = "https://online.fliphtml5.com/{0}/javascript/config.js".format(book_name)
    # allow ?timestamp query like the site uses
    r = _session.get(url, timeout=30)
    r.raise_for_status()
    text = r.text.strip()
    if text.startswith("var htmlConfig = "):
        text = text[len("var htmlConfig = "):]
    if text.endswith(";"):
        text = text[:-1]
    return json.loads(text)


def page_filename(page, idx):
    if isinstance(page, str):
        return page
    if isinstance(page, dict):
        n = page.get("n")
        if isinstance(n, list) and n:
            return n[0]
        if isinstance(n, str) and n:
            return n
    return None


def build_page_url(base_url, filename, size="large"):
    if filename.startswith("http://") or filename.startswith("https://"):
        return filename
    p = filename
    if p.startswith("./"):
        p = p[2:]
    if p.startswith("/"):
        p = p[1:]
    if p.startswith("files/"):
        return urljoin(base_url, p)
    return urljoin(base_url, "files/%s/%s" % (size, p))


def download_with_retry(url, timeout=30, attempts=5):
    last = None
    for i in range(attempts):
        try:
            r = _session.get(url, timeout=timeout)
            if r.status_code == 200 and r.content:
                # guard against HTML error pages masquerading as images
                ct = r.headers.get("Content-Type", "")
                if "text/html" in ct and len(r.content) < 5000:
                    last = "html:%d" % r.status_code
                    continue
                return r.content
            last = "http:%d" % r.status_code
        except requests.RequestException as e:
            last = type(e).__name__
    raise RuntimeError("failed %s (%s)" % (url, last))


def to_jpeg_bytes(raw):
    """Convert webp/png/anything to JPEG bytes for img2pdf."""
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as im:
        if im.mode in ("RGBA", "LA", "PA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, "JPEG", quality=95)
        return out.getvalue()


def main():
    if len(sys.argv) > 1:
        raw_input = sys.argv[1]
    else:
        raw_input = input("Enter the book link in the format *****/****:\n")
    book_name = normalize_book_name(raw_input)
    if "/" not in book_name:
        print("Expected format *****/****, e.g. ceeok/pkdn. Got: %r" % raw_input)
        sys.exit(2)

    print("Book: %s" % book_name)
    config = fetch_config(book_name)
    pages = decode_pages(config.get("fliphtml5_pages"))

    # fallback: some books hide pages inside bookConfig
    if not pages and isinstance(config.get("bookConfig"), str):
        print("Trying bookConfig fallback...")
        pages = decode_pages(destring_decode(config["bookConfig"]))
    if not pages:
        print("error: fliphtml5_pages not found or could not be decoded")
        sys.exit(2)

    print("Found %d pages" % len(pages))

    meta = config.get("meta") or {}
    title = meta.get("title") or book_name.replace("/", "_")
    folder_name = re.sub(r'[<>:"/\\|?*]', "_", title).strip() or "fliphtml5_book"
    os.makedirs(folder_name, exist_ok=True)

    base_url = "https://online.fliphtml5.com/%s/" % book_name

    pdf_images = []
    for idx, page in enumerate(pages):
        fname = page_filename(page, idx)
        if not fname:
            print("Page %d has no filename, skipping" % (idx + 1))
            continue
        # primary: files/large/<hash>, fallback: files/thumb/<hash>, files/medium/<hash>
        candidates = [
            build_page_url(base_url, fname, "large"),
        ]
        # webp -> jpg fallback and vice versa
        if fname.endswith(".webp"):
            candidates.append(build_page_url(base_url, fname[:-5] + ".jpg", "large"))
        elif fname.endswith(".jpg"):
            candidates.append(build_page_url(base_url, fname[:-4] + ".webp", "large"))
        candidates.append(build_page_url(base_url, fname, "medium"))
        candidates.append(build_page_url(base_url, fname, "thumb"))

        content = None
        used_url = ""
        for u in candidates:
            try:
                content = download_with_retry(u)
                used_url = u
                break
            except RuntimeError:
                continue
        if content is None:
            print("FAILED page %d/%d, skipped. Tried: %s" % (idx + 1, len(pages), candidates[0]))
            continue

        ext = ".jpg"
        if "webp" in used_url and ".jpg" not in used_url:
            ext = ".webp"
        file_path = os.path.join(folder_name, "%d%s" % (idx + 1, ext))
        with open(file_path, "wb") as f:
            f.write(content)
        try:
            pdf_images.append(to_jpeg_bytes(content))
        except Exception as e:
            print("warn: page %d image convert failed (%s), using raw" % (idx + 1, e))
            pdf_images.append(content)
        print("Downloading Page %d / %d ....." % (idx + 1, len(pages)))

    if not pdf_images:
        print("Nothing downloaded, PDF not created.")
        sys.exit(2)

    print("Downloading Complete. Don't Close, hold-on. We are yet to make PDF.")
    import img2pdf

    pdf_path = "%s.pdf" % folder_name
    with open(pdf_path, "wb") as f:
        f.write(img2pdf.convert(pdf_images))
    print("The pdf named %s has been saved in your working directory." % pdf_path)
    print("Thank you for using this script.")


if __name__ == "__main__":
    main()
