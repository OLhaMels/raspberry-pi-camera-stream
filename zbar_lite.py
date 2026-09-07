"""Minimal ctypes binding for libzbar, enough to scan a grayscale frame for QR codes.

The Pi already ships libzbar (package libzbar0t64), but Debian trixie has no
python3-pyzbar and the Pi cannot reach a package mirror, so the few calls that
are actually needed are bound here by hand instead of installing a wrapper.
"""

import ctypes
import ctypes.util

ZBAR_NONE = 0
ZBAR_QRCODE = 64
ZBAR_CFG_ENABLE = 0


def _fourcc(code):
    return (ord(code[0]) | ord(code[1]) << 8 | ord(code[2]) << 16 | ord(code[3]) << 24)


GRAY_FORMAT = _fourcc("Y800")


def _load():
    path = ctypes.util.find_library("zbar") or "libzbar.so.0"
    lib = ctypes.CDLL(path)

    lib.zbar_version.argtypes = [ctypes.POINTER(ctypes.c_uint)] * 3
    lib.zbar_image_scanner_create.restype = ctypes.c_void_p
    lib.zbar_image_scanner_destroy.argtypes = [ctypes.c_void_p]
    lib.zbar_image_scanner_set_config.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lib.zbar_image_scanner_set_config.restype = ctypes.c_int

    lib.zbar_image_create.restype = ctypes.c_void_p
    lib.zbar_image_destroy.argtypes = [ctypes.c_void_p]
    lib.zbar_image_set_format.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib.zbar_image_set_size.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
    lib.zbar_image_set_data.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]

    lib.zbar_scan_image.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.zbar_scan_image.restype = ctypes.c_int

    lib.zbar_image_first_symbol.argtypes = [ctypes.c_void_p]
    lib.zbar_image_first_symbol.restype = ctypes.c_void_p
    lib.zbar_symbol_next.argtypes = [ctypes.c_void_p]
    lib.zbar_symbol_next.restype = ctypes.c_void_p
    lib.zbar_symbol_get_type.argtypes = [ctypes.c_void_p]
    lib.zbar_symbol_get_type.restype = ctypes.c_int
    lib.zbar_symbol_get_data.argtypes = [ctypes.c_void_p]
    lib.zbar_symbol_get_data.restype = ctypes.c_void_p
    lib.zbar_symbol_get_data_length.argtypes = [ctypes.c_void_p]
    lib.zbar_symbol_get_data_length.restype = ctypes.c_uint
    lib.zbar_symbol_get_quality.argtypes = [ctypes.c_void_p]
    lib.zbar_symbol_get_quality.restype = ctypes.c_int
    lib.zbar_symbol_get_loc_size.argtypes = [ctypes.c_void_p]
    lib.zbar_symbol_get_loc_size.restype = ctypes.c_uint
    lib.zbar_symbol_get_loc_x.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    lib.zbar_symbol_get_loc_x.restype = ctypes.c_int
    lib.zbar_symbol_get_loc_y.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    lib.zbar_symbol_get_loc_y.restype = ctypes.c_int
    lib.zbar_get_symbol_name.argtypes = [ctypes.c_int]
    lib.zbar_get_symbol_name.restype = ctypes.c_char_p
    return lib


_lib = _load()


def version():
    major, minor, patch = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint()
    _lib.zbar_version(ctypes.byref(major), ctypes.byref(minor), ctypes.byref(patch))
    return "{}.{}.{}".format(major.value, minor.value, patch.value)


class Code:
    """One decoded symbol."""

    __slots__ = ("kind", "raw", "quality", "bbox")

    def __init__(self, kind, raw, quality, bbox):
        self.kind = kind
        self.raw = raw
        self.quality = quality
        self.bbox = bbox  # (x, y, w, h) in the scanned frame, or None

    @property
    def text(self):
        """Payload as text; undecodable bytes are kept visible rather than dropped."""
        return self.raw.decode("utf-8", "replace")

    def __repr__(self):
        return "Code({} {!r})".format(self.kind, self.text)


class Scanner:
    """Scans grayscale frames. Not thread-safe; use one per thread."""

    def __init__(self, qr_only=True):
        self._scanner = _lib.zbar_image_scanner_create()
        if not self._scanner:
            raise RuntimeError("zbar_image_scanner_create failed")
        if qr_only:
            # Everything off, then QR back on: skips the 1D barcode passes, which
            # are pure overhead here and an occasional source of false reads.
            _lib.zbar_image_scanner_set_config(
                self._scanner, ZBAR_NONE, ZBAR_CFG_ENABLE, 0)
            _lib.zbar_image_scanner_set_config(
                self._scanner, ZBAR_QRCODE, ZBAR_CFG_ENABLE, 1)

    def scan_gray(self, data, width, height):
        """Scan one 8-bit grayscale frame. `data` must be width*height bytes."""
        if self._scanner is None:
            raise RuntimeError("scanner is closed")
        expected = width * height
        view = memoryview(data)
        if not view.contiguous:
            raise ValueError("frame must be contiguous")
        # A 2D view slices by rows, so flatten to bytes before taking the plane.
        if view.ndim != 1 or view.format != "B":
            view = view.cast("B")
        if len(view) < expected:
            raise ValueError("expected {} bytes, got {}".format(expected, len(view)))
        buf = bytes(view[:expected])

        image = _lib.zbar_image_create()
        if not image:
            raise RuntimeError("zbar_image_create failed")
        try:
            _lib.zbar_image_set_format(image, GRAY_FORMAT)
            _lib.zbar_image_set_size(image, width, height)
            _lib.zbar_image_set_data(image, buf, len(buf), None)

            found = _lib.zbar_scan_image(self._scanner, image)
            if found <= 0:
                return []

            codes = []
            symbol = _lib.zbar_image_first_symbol(image)
            while symbol:
                length = _lib.zbar_symbol_get_data_length(symbol)
                pointer = _lib.zbar_symbol_get_data(symbol)
                raw = ctypes.string_at(pointer, length) if pointer else b""
                kind = _lib.zbar_get_symbol_name(
                    _lib.zbar_symbol_get_type(symbol)).decode()
                codes.append(Code(kind, raw,
                                  _lib.zbar_symbol_get_quality(symbol),
                                  _bbox(symbol)))
                symbol = _lib.zbar_symbol_next(symbol)
            return codes
        finally:
            _lib.zbar_image_destroy(image)

    def close(self):
        if self._scanner is not None:
            _lib.zbar_image_scanner_destroy(self._scanner)
            self._scanner = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _bbox(symbol):
    points = _lib.zbar_symbol_get_loc_size(symbol)
    if not points:
        return None
    xs = [_lib.zbar_symbol_get_loc_x(symbol, i) for i in range(points)]
    ys = [_lib.zbar_symbol_get_loc_y(symbol, i) for i in range(points)]
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
