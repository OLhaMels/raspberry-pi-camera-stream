#!/usr/bin/env python3
"""QR detection, on-screen overlay, and the app that ties them to a camera.

Everything that knows what a QR code is lives in this file: the ctypes
binding to libzbar (Debian trixie has no python3-pyzbar, so the handful of
calls actually needed are bound here by hand), the scan thread, the overlay
drawing, and the pre_callback that puts the two together. streamer.py is
QR-agnostic - it only knows how to run a camera and serve video - so this
module reaches into the camera it hands back rather than the other way
round.

The camera feeds two streams at once:

    main   1280x720 -> H.264 -> ffmpeg -> TCP/MPEG-TS   (what a viewer sees)
    lores   640x480 -> QRDetector                       (what reads codes)

The lores stream comes straight out of the ISP, so reads never depend on
the video link or on H.264 artefacts, and decoding runs at whatever rate
the ISP delivers lores frames - 30 fps by default, comfortably above the
15-20 Hz target.

Overlay drawing happens once per frame in the camera's own pre_callback,
wired up in main() below: it first drains anything new out of the
detector's single-slot mailbox, then draws whatever is still current. So
Osd itself needs no locking; the only cross-thread handoff in the whole
pipeline is that mailbox.

    python3 qr_detector.py                      # stream + scan
    python3 qr_detector.py --no-stream          # scan only, no video out
    python3 qr_detector.py --self-test qr.png   # decode a still, no camera
"""

import argparse
import ctypes
import ctypes.util
import queue
import signal
import sys
import threading
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Deliberately not imported here: picamera2 (MappedArray) and streamer
# (Streamer). Both pull in libcamera, which --self-test has no use for and
# which is not installed on a plain development machine. main() imports
# them only once it knows a camera is actually wanted.

# Kept in step with streamer.DEFAULT_URL by hand rather than imported, for
# the same reason: importing streamer would drag picamera2 in at module
# import time and break --self-test off-Pi.
DEFAULT_URL = "tcp://0.0.0.0:8554?listen=1"

ZBAR_NONE = 0
ZBAR_QRCODE = 64
ZBAR_CFG_ENABLE = 0


def _fourcc(code):
    return ord(code[0]) | ord(code[1]) << 8 | ord(code[2]) << 16 | ord(code[3]) << 24


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


def _bbox_of(corners):
    if not corners:
        return None
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


class Code:
    """One decoded symbol: payload, quality, and where it sat in the frame.

    `corners` is the polygon zbar traced around the symbol (usually the four
    corners of the QR finder pattern, in perimeter order) in the pixel space
    of the frame that was scanned. `bbox` is the axis-aligned box around
    those corners, kept for callers that just want a quick rectangle.
    """

    __slots__ = ("kind", "raw", "quality", "corners", "bbox")

    def __init__(self, kind, raw, quality, corners):
        self.kind = kind
        self.raw = raw
        self.quality = quality
        self.corners = corners
        self.bbox = _bbox_of(corners)

    @property
    def text(self):
        """Payload as text; undecodable bytes are kept visible, not dropped."""
        return self.raw.decode("utf-8", "replace")

    def __repr__(self):
        return "Code({} {!r})".format(self.kind, self.text)


class Scanner:
    """Thin wrapper around one zbar_image_scanner_t.

    Not thread-safe - QRDetector keeps exactly one, used only from its own
    scan thread.
    """

    def __init__(self, qr_only=True):
        self._scanner = _lib.zbar_image_scanner_create()
        if not self._scanner:
            raise RuntimeError("zbar_image_scanner_create failed")
        if qr_only:
            # Everything off, then QR back on: skips the 1D barcode passes,
            # which are pure overhead here and an occasional source of false
            # reads.
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

            if _lib.zbar_scan_image(self._scanner, image) <= 0:
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
                                  _corners(symbol)))
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


def _corners(symbol):
    points = _lib.zbar_symbol_get_loc_size(symbol)
    return [(_lib.zbar_symbol_get_loc_x(symbol, i),
             _lib.zbar_symbol_get_loc_y(symbol, i))
            for i in range(points)]


def gray_plane(frame, width, height):
    """Y plane of a YUV420 capture, cropped to the real width, made contiguous.

    picamera2 hands back rows padded to the stream stride, so the frame has
    to be cropped before the bytes can go to zbar as a plain grayscale
    buffer.
    """
    return np.ascontiguousarray(frame[:height, :width])


class QRDetector:
    """Owns the scan loop: pulls lores frames straight from the camera,
    decodes them, and republishes the latest result set for a consumer to
    pick up whenever it is ready.

    `latest()` never blocks and never raises on an empty mailbox - it just
    returns the last thing this thread finished, or None if nothing has been
    scanned yet. That single-slot, always-overwrite mailbox is the
    non-blocking letterbox the drawing side reads from: it only ever cares
    about the most current set of codes, never a backlog of stale ones.
    """

    def __init__(self, picam2, size, stream="lores", interval=0.0,
                 repeat_after=5.0, qr_only=True, log_path=None,
                 on_new_code=None):
        self.picam2 = picam2
        self.stream = stream
        self.width, self.height = size
        self.interval = interval
        self.repeat_after = repeat_after
        self.qr_only = qr_only

        self._mailbox = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="qr-detector")
        self._last_seen = {}
        self._on_new_code = on_new_code
        self._log = open(log_path, "a", buffering=1) if log_path else None

        self.scans = 0
        self.total_reads = 0

    def start(self):
        self._thread.start()
        return self

    def stop(self, timeout=2.0):
        self._stop.set()
        self._thread.join(timeout=timeout)
        if self._log:
            self._log.close()

    def latest(self):
        """Most recent list of Code objects seen in frame, or None if the
        detector has not completed a scan yet. Never blocks."""
        try:
            return self._mailbox.get_nowait()
        except queue.Empty:
            return None

    # ---- runs on the detector thread ------------------------------------

    def _run(self):
        with Scanner(qr_only=self.qr_only) as scanner:
            while not self._stop.is_set():
                frame = self.picam2.capture_array(self.stream)
                gray = gray_plane(frame, self.width, self.height)
                codes = scanner.scan_gray(gray, self.width, self.height)
                self.scans += 1

                for code in codes:
                    if self._is_new(code):
                        self._report(code)
                        if self._on_new_code:
                            self._on_new_code(code)

                self._publish(codes)
                if self.interval:
                    time.sleep(self.interval)

    def _is_new(self, code):
        """True once per payload, then again only after it has been out of
        frame for `repeat_after` seconds."""
        now = time.monotonic()
        previous = self._last_seen.get(code.raw)
        self._last_seen[code.raw] = now
        return previous is None or now - previous >= self.repeat_after

    def _report(self, code):
        self.total_reads += 1
        stamp = time.strftime("%H:%M:%S")
        where = "  at {},{} {}x{}px".format(*code.bbox) if code.bbox else ""
        print("[{}] {} q={}{}\n           -> {}".format(
            stamp, code.kind, code.quality, where, code.text), flush=True)
        if self._log:
            self._log.write("{} {}\t{}\n".format(
                time.strftime("%Y-%m-%d %H:%M:%S"), code.kind, code.text))

    def _publish(self, codes):
        # Drop whatever is waiting, then replace it - a single slot is all a
        # "latest value" needs, and it means put and get are both O(1) and
        # neither side ever blocks.
        try:
            self._mailbox.get_nowait()
        except queue.Empty:
            pass
        try:
            self._mailbox.put_nowait(codes)
        except queue.Full:
            pass


def _self_test(path):
    """Decode a still image, so the zbar binding can be checked without a camera."""
    image = Image.open(path).convert("L")
    width, height = image.size
    data = np.asarray(image, dtype=np.uint8)
    with Scanner() as scanner:
        codes = scanner.scan_gray(data, width, height)
    if not codes:
        return "FAIL: no QR code found in " + path
    for code in codes:
        print("OK: {} -> {} (quality {}, corners {})".format(
            code.kind, code.text, code.quality, code.corners))
    return None


FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
BOX_COLOUR = (0, 255, 0)
BOX_THICKNESS = 3
BOX_HOLD = 0.35   # an outline outlives its scan just long enough not to flicker
MAX_CHARS = 52


class Osd:
    """Overlay state, owned entirely by the camera thread's pre_callback:
    each frame it is first told about any new detection, then asked to draw.
    Because both calls happen back to back on the same thread, the state
    here needs no locking of its own - the only cross-thread handoff in this
    pipeline is QRDetector's mailbox, upstream of this class.

    Colours are deliberately channel-order agnostic - green, white, black -
    because the main stream is XBGR8888 and only the middle channel is
    unambiguous.
    """

    def __init__(self, frame_size, scan_size, hold=5.0, font_size=22,
                 font_path=FONT_PATH):
        self.frame_w, self.frame_h = frame_size
        self.scale_x = self.frame_w / scan_size[0]
        self.scale_y = self.frame_h / scan_size[1]
        self.hold = hold
        self.font = ImageFont.truetype(font_path, font_size)
        self._text_cache = (None, None)   # (key, mask)
        self._boxes = None                # (polygons, until)
        self._readout = None              # (mask, until)

    # ---- called once per frame, before draw() ---------------------------

    def show(self, codes):
        """Register the codes seen in the most recent scan."""
        if not codes:
            return
        now = time.monotonic()

        polygons = []
        for code in codes:
            if code.corners:
                polygons.append(tuple(
                    (int(x * self.scale_x), int(y * self.scale_y))
                    for x, y in code.corners))
        if polygons:
            self._boxes = (tuple(polygons), now + BOX_HOLD)

        primary = codes[0]
        text = primary.text.replace("\n", " ")
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS - 1] + "…"
        detail = "{}  q={}".format(time.strftime("%H:%M:%S"), primary.quality)
        if len(codes) > 1:
            detail += "  (+{} more)".format(len(codes) - 1)
        self._readout = (self._mask(text, detail), now + self.hold)

    # ---- called once per frame, right after show() ----------------------

    def draw(self, frame):
        """Draw the current overlay onto one main-stream frame, in place."""
        now = time.monotonic()
        height, width = frame.shape[0], min(frame.shape[1], self.frame_w)

        boxes = self._boxes
        if boxes and boxes[1] > now:
            for polygon in boxes[0]:
                self._polygon(frame, polygon, height, width)

        readout = self._readout
        if readout and readout[1] > now:
            self._band(frame, readout[0], height, width)

    # ---- internals ------------------------------------------------------

    def _mask(self, text, detail):
        """Render the two readout lines once, as a boolean pixel mask."""
        key = (text, detail)
        if self._text_cache[0] == key:
            return self._text_cache[1]

        pad = 10
        line_gap = 4
        top = self.font.getbbox(text)
        bottom = self.font.getbbox(detail)
        text_w = max(top[2], bottom[2])
        top_h = top[3]
        image = Image.new("L", (text_w + 2 * pad,
                                top_h + bottom[3] + line_gap + 2 * pad), 0)
        draw = ImageDraw.Draw(image)
        draw.text((pad, pad - top[1]), text, fill=255, font=self.font)
        draw.text((pad, pad + top_h + line_gap - bottom[1]), detail,
                  fill=255, font=self.font)
        mask = np.asarray(image) > 96
        self._text_cache = (key, mask)
        return mask

    def _polygon(self, frame, points, height, width):
        """Trace a closed outline through `points` (already main-frame pixels).

        This is the polygon zbar found, not its bounding box, so a code held
        at an angle gets an outline that actually follows it. QR codes only
        occupy a small part of the frame and this only runs while one is in
        view, so a plain per-pixel Bresenham stamp is cheap enough - no need
        to pull in a drawing library for four short lines.
        """
        n = len(points)
        if n < 2:
            return
        for i in range(n):
            self._line(frame, points[i], points[(i + 1) % n], height, width)

    def _line(self, frame, p0, p1, height, width):
        x0, y0 = p0
        x1, y1 = p1
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        t = BOX_THICKNESS // 2 + 1
        while True:
            xa, ya = max(0, x0 - t), max(0, y0 - t)
            xb, yb = min(width, x0 + t), min(height, y0 + t)
            if xb > xa and yb > ya:
                frame[ya:yb, xa:xb, :3] = BOX_COLOUR
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def _band(self, frame, mask, height, width):
        mask_h, mask_w = mask.shape
        margin = 16
        x0 = margin
        y0 = height - mask_h - margin
        if y0 < 0 or x0 + mask_w > width:
            # Frame too small for the readout as rendered; skip it rather
            # than crop it into something unreadable.
            return
        band = frame[y0:y0 + mask_h, x0:x0 + mask_w]
        band[..., :3] //= 4          # darken, so white text reads over anything
        band[..., :3][mask] = 255


def handle_code(code):
    """Where a downstream consumer - the manipulator, say - would react to a
    newly-read payload. Wired into the detector as `on_new_code` below, and
    kept separate from scanning and from the console log: this function's
    only job is to act, once, the moment a new code is decided.
    """
    # TODO(manipulator): parse code.text into a command and send it to the arm.
    return


def _shutdown_on_sigterm(signum, frame):
    """Make SIGTERM take the same path out as Ctrl+C.

    systemd's `systemctl stop` and a bare `pkill` both send SIGTERM, whose
    default action is to kill the process on the spot - no finally block, so
    the camera and the sockets get released by the kernel on exit rather than
    by stop(). Raising the exception Ctrl+C already raises gives every way of
    asking one orderly shutdown path.
    """
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-test", metavar="IMAGE",
                        help="decode a still image and exit, no camera needed")
    parser.add_argument("--url", default=DEFAULT_URL,
                        help="where ffmpeg listens (default: %(default)s)")
    parser.add_argument("--no-stream", action="store_true",
                        help="only read QR codes, do not serve video")
    parser.add_argument("--size", default="1280x720", help="video stream size")
    parser.add_argument("--scan-size", default="640x480",
                        help="size of the stream used for decoding")
    parser.add_argument("--bitrate", type=int, default=4_000_000)
    parser.add_argument("--threads", type=int, default=4,
                        help="encoder threads (slice-threaded, so no added delay)")
    parser.add_argument("--scan-interval", type=float, default=0.0,
                        help="seconds between detector scans "
                             "(0 = as fast as lores frames arrive)")
    parser.add_argument("--repeat-after", type=float, default=5.0,
                        help="seconds a code must be gone before it is reported again")
    parser.add_argument("--heartbeat", type=float, default=60.0,
                        help="seconds between liveness lines (0 = off)")
    parser.add_argument("--no-osd", dest="osd", action="store_false",
                        help="do not burn the outline and readout into the video")
    parser.add_argument("--osd-hold", type=float, default=5.0,
                        help="seconds the readout stays up after a read")
    parser.add_argument("--no-reconnect", dest="reconnect", action="store_false",
                        help="serve one viewer, then stop listening")
    parser.add_argument("--log", help="append every new read to this file")
    args = parser.parse_args()

    if args.self_test:
        print("zbar", version())
        return _self_test(args.self_test)

    signal.signal(signal.SIGTERM, _shutdown_on_sigterm)

    # Imported here, not at the top of the file, so that --self-test above
    # still runs on a machine with no picamera2/libcamera installed.
    from picamera2 import MappedArray

    from streamer import Streamer, viewer_hint

    main_w, main_h = (int(v) for v in args.size.split("x"))
    scan_w, scan_h = (int(v) for v in args.scan_size.split("x"))
    scan_size = (scan_w, scan_h)

    # streamer.py knows nothing about QR codes or overlays - it just hands
    # back a configured camera. Everything QR-specific below reaches into
    # that object directly: capture_array() for the detector, pre_callback
    # for the overlay.
    video = Streamer(main_size=(main_w, main_h), lores_size=scan_size,
                     url=args.url, bitrate=args.bitrate, threads=args.threads,
                     reconnect=args.reconnect)

    detector = QRDetector(video.picam2, scan_size, interval=args.scan_interval,
                          repeat_after=args.repeat_after, log_path=args.log,
                          on_new_code=handle_code)

    osd = None
    if args.osd and not args.no_stream:
        osd = Osd((main_w, main_h), scan_size, hold=args.osd_hold)

        def draw_overlay(request):
            # Runs on the camera thread, before the frame reaches the encoder.
            codes = detector.latest()
            if codes is not None:
                osd.show(codes)
            with MappedArray(request, "main") as mapped:
                osd.draw(mapped.array)

        video.picam2.pre_callback = draw_overlay

    detector.start()
    video.start(publish=not args.no_stream)

    if args.no_stream:
        print("Scanning only (no video output).", flush=True)
    else:
        print("Serving {}x{} on {}\n  {}".format(
            main_w, main_h, args.url, viewer_hint(args.url)), flush=True)
    print("Scanning {}x{}{} - Ctrl+C to stop".format(
        scan_w, scan_h, ", OSD on" if osd is not None else ""), flush=True)

    started = time.monotonic()
    last_beat = started
    try:
        while True:
            time.sleep(1)
            if args.heartbeat:
                now = time.monotonic()
                if now - last_beat >= args.heartbeat:
                    last_beat = now
                    elapsed = now - started
                    print("[{}] alive: {} scans, {:.1f}/s, {} code(s) read".format(
                        time.strftime("%H:%M:%S"), detector.scans,
                        detector.scans / elapsed if elapsed else 0,
                        detector.total_reads), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        detector.stop()
        video.stop()
        elapsed = time.monotonic() - started
        print("\nStopped after {:.0f}s: {} scans ({:.1f}/s), {} code(s) read".format(
            elapsed, detector.scans, detector.scans / elapsed if elapsed else 0,
            detector.total_reads), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
