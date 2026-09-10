#!/usr/bin/env python3
"""Read QR codes on the Raspberry Pi itself, and keep streaming video for a viewer.

The camera feeds two streams at once:

  main  1280x720 -> H.264 -> MediaMTX -> RTSP   (the preview on the laptop)
  lores  640x480 -> zbar                        (what actually reads the codes)

Decoding happens here rather than on the laptop because the payloads are meant
to become commands for the manipulator, so the Pi has to see them itself. The
lores stream comes straight out of the ISP, so reads never depend on the video
link or on H.264 artefacts.

    python3 qr_stream.py                 # stream + scan
    python3 qr_stream.py --no-stream     # scan only, no RTSP publishing
    python3 qr_stream.py --self-test qr.png
"""

import argparse
import sys
import time

import numpy as np

import zbar_lite

DEFAULT_RTSP = "rtsp://localhost:8554/cam"


def handle_code(code):
    """Called once per newly seen code. This is where the manipulator hooks in.

    Kept deliberately separate from the scan loop: the loop's job is to notice a
    code, this function's job is to act on it. Right now it only reports.
    """
    # TODO(manipulator): parse code.text into a command and send it to the arm.
    return


class Reporter:
    """Prints each payload once, and again only after it has been out of frame."""

    def __init__(self, repeat_after, log_path=None):
        self.repeat_after = repeat_after
        self.last_seen = {}
        self.total = 0
        self.log = open(log_path, "a", buffering=1) if log_path else None

    def offer(self, code):
        now = time.monotonic()
        previous = self.last_seen.get(code.raw)
        self.last_seen[code.raw] = now
        if previous is not None and now - previous < self.repeat_after:
            return False

        self.total += 1
        stamp = time.strftime("%H:%M:%S")
        where = ""
        if code.bbox:
            where = "  at {},{} {}x{}px".format(*code.bbox)
        print("[{}] {} q={}{}\n           -> {}".format(
            stamp, code.kind, code.quality, where, code.text), flush=True)
        if self.log:
            self.log.write("{} {}\t{}\n".format(
                time.strftime("%Y-%m-%d %H:%M:%S"), code.kind, code.text))
        return True


def gray_plane(frame, width, height):
    """Y plane of a YUV420 capture, cropped to the real width and made contiguous.

    picamera2 hands back rows padded to the stream stride, so the frame has to be
    cropped before the bytes can be handed to zbar as a plain grayscale buffer.
    """
    return np.ascontiguousarray(frame[:height, :width])


def self_test(path):
    """Decode a still image, so the decoder can be checked without the camera."""
    from PIL import Image

    image = Image.open(path).convert("L")
    width, height = image.size
    data = np.asarray(image, dtype=np.uint8)
    with zbar_lite.Scanner() as scanner:
        codes = scanner.scan_gray(data, width, height)
    if not codes:
        sys.exit("FAIL: no QR code found in " + path)
    for code in codes:
        print("OK: {} -> {} (quality {}, bbox {})".format(
            code.kind, code.text, code.quality, code.bbox))
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rtsp", default=DEFAULT_RTSP, help="where to publish video")
    parser.add_argument("--no-stream", action="store_true",
                        help="only read QR codes, do not publish video")
    parser.add_argument("--size", default="1280x720", help="video stream size")
    parser.add_argument("--scan-size", default="640x480",
                        help="size of the stream used for decoding")
    parser.add_argument("--bitrate", type=int, default=4_000_000)
    parser.add_argument("--threads", type=int, default=4,
                        help="encoder threads (slice-threaded, so no added delay)")
    parser.add_argument("--interval", type=float, default=0.05,
                        help="seconds between scans (0 = as fast as frames arrive)")
    parser.add_argument("--repeat-after", type=float, default=5.0,
                        help="seconds a code must be gone before it is reported again")
    parser.add_argument("--heartbeat", type=float, default=60.0,
                        help="seconds between liveness lines (0 = off)")
    parser.add_argument("--log", help="append every read to this file")
    parser.add_argument("--self-test", metavar="IMAGE",
                        help="decode a still image and exit (no camera needed)")
    args = parser.parse_args()

    if args.self_test:
        return self_test(args.self_test)

    main_w, main_h = (int(v) for v in args.size.split("x"))
    scan_w, scan_h = (int(v) for v in args.scan_size.split("x"))

    from picamera2 import Picamera2
    from picamera2.outputs import FfmpegOutput

    from low_latency_encoder import LowLatencyH264Encoder

    picam2 = Picamera2()
    picam2.configure(picam2.create_video_configuration(
        main={"size": (main_w, main_h)},
        lores={"size": (scan_w, scan_h), "format": "YUV420"},
    ))

    output = None
    if args.no_stream:
        picam2.start()
    else:
        # TCP, not the default UDP: over UDP MediaMTX has to remux this
        # publisher's oversized RTP packets and drops some, which shows up as
        # corrupted macroblocks in the viewer. muxdelay/muxpreload stop ffmpeg
        # from holding packets back before it starts sending.
        output = FfmpegOutput(
            "-muxdelay 0 -muxpreload 0 -f rtsp -rtsp_transport tcp " + args.rtsp)
        picam2.start_recording(
            LowLatencyH264Encoder(bitrate=args.bitrate, threads=args.threads), output)
        print("Video: {} ({}x{})".format(args.rtsp, main_w, main_h), flush=True)

    reporter = Reporter(args.repeat_after, args.log)
    scanner = zbar_lite.Scanner()
    print("Scanning {}x{} with zbar {} - Ctrl+C to stop".format(
        scan_w, scan_h, zbar_lite.version()), flush=True)

    scans = 0
    started = time.monotonic()
    last_beat = started
    try:
        while True:
            frame = picam2.capture_array("lores")
            for code in scanner.scan_gray(
                    gray_plane(frame, scan_w, scan_h), scan_w, scan_h):
                if reporter.offer(code):
                    handle_code(code)
            scans += 1

            now = time.monotonic()
            if args.heartbeat and now - last_beat >= args.heartbeat:
                last_beat = now
                print("[{}] alive: {} scans, {:.1f}/s, {} code(s) read".format(
                    time.strftime("%H:%M:%S"), scans, scans / (now - started),
                    reporter.total), flush=True)
            if args.interval:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        scanner.close()
        if args.no_stream:
            picam2.stop()
        else:
            picam2.stop_recording()
        elapsed = time.monotonic() - started
        print("\nStopped after {:.0f}s: {} scans ({:.1f}/s), {} code(s) read".format(
            elapsed, scans, scans / elapsed if elapsed else 0, reporter.total),
            flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
