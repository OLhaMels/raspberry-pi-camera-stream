#!/usr/bin/env python3
"""Camera, H.264 encoder, and RTSP publishing - nothing else.

This is the video-output half of what used to be one streamer.py: it owns
the Picamera2 instance, the two video streams (`main`, encoded and
published over RTSP; `lores`, exposed for whoever wants raw frames off the
ISP), and the low-latency encoder subclass. It has no idea a QR code
exists, runs no event loop, and does no drawing - a caller that wants to
touch frames does so through the Streamer's own `picam2`, before calling
start().

qr_detector.py is the current example of such a caller: it builds a
Streamer, sets `streamer.picam2.pre_callback` to burn its own overlay into
the main stream, hands `streamer.picam2` to its QRDetector to pull lores
frames from, and drives start()/stop() around its own scan loop. None of
that is this module's business - swap qr_detector.py for anything else
that wants a live camera and an RTSP feed, and this file doesn't change.

    python3 streamer.py                 # camera -> RTSP, no processing at all
"""

import argparse
import sys
import time

from av.codec.context import ThreadType
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

DEFAULT_RTSP = "rtsp://localhost:8554/cam"


class LowLatencyH264Encoder(H264Encoder):
    """H264Encoder that slice-threads instead of frame-threads.

    picamera2's H264Encoder pins libav to frame-level threading, so libx264
    holds thread_count frames before it emits the first packet.
    `tune=zerolatency`, which picamera2 does set, cannot undo it: FFmpeg's
    libx264 wrapper decides sliced-threads from the codec context
    thread_type, which overrides what the tune asked for. Measured on a Pi 5
    at 1280x720:

        FRAME threading, threads=0 (auto -> 6)   first packet after 6 frames   167 ms
        FRAME threading, threads=4               first packet after 4 frames   100 ms
        SLICE threading, threads=4               first packet after 1 frame      0 ms
        single thread                            first packet after 1 frame      0 ms

    Slice threading is the one to have: no queue, and still ~93 fps of
    encode throughput at 720p on that Pi 5, which is three times what a
    30 fps stream needs - the Pi 5 has no hardware H.264 encoder, so this is
    what stands in for one. Re-measure on your own board before trusting
    the numbers above.
    """

    def __init__(self, *args, threads=4, **kwargs):
        super().__init__(*args, **kwargs)
        self.threads = threads

    def _start(self):
        super()._start()
        # Safe to change here: _start only configures the stream, and
        # libx264 reads these when it opens on the first frame, which
        # cannot arrive until start_recording has returned.
        self._stream.codec_context.thread_type = ThreadType.SLICE
        self._stream.codec_context.thread_count = self.threads
        # Read back rather than assume: this is the whole point of the subclass.
        context = self._stream.codec_context
        print("encoder: {} threading, {} thread(s)".format(
            context.thread_type.name.lower(), context.thread_count), flush=True)


class Streamer:
    """Owns the camera. Configures it, optionally encodes and publishes the
    main stream over RTSP, and gets out of the way.

    `lores_size` is optional: pass it if some consumer wants a second,
    lower-resolution stream straight off the ISP (QRDetector does, for
    frame-rate and quality reasons that have nothing to do with this
    class); leave it out for a plain single-stream camera.

    `publish=False` configures and starts the camera but skips the encoder
    and RTSP output entirely, for callers that only want frames (e.g. a
    detector running with no video output at all).

    Everything a caller needs to hook into frames - `pre_callback`,
    `capture_array()`, `capture_metadata()`, and so on - lives on the
    Picamera2 instance itself, exposed here as `picam2`. This class does
    not wrap or narrow that API; it only owns the object's lifecycle.
    """

    def __init__(self, size=(1280, 720), lores_size=None, lores_format="YUV420",
                 rtsp=DEFAULT_RTSP, bitrate=4_000_000, threads=4, publish=True):
        self.size = size
        self.lores_size = lores_size
        self.rtsp = rtsp
        self.bitrate = bitrate
        self.threads = threads
        self.publish = publish

        self.picam2 = Picamera2()
        config_kwargs = {"main": {"size": size}}
        if lores_size is not None:
            config_kwargs["lores"] = {"size": lores_size, "format": lores_format}
        self.picam2.configure(self.picam2.create_video_configuration(**config_kwargs))

        self._recording = False

    def start(self):
        """Start the camera. If `publish`, also start encoding to RTSP."""
        if not self.publish:
            self.picam2.start()
            return
        # TCP, not the default UDP: over UDP MediaMTX has to remux this
        # publisher's oversized RTP packets and drops some, which shows up
        # as corrupted macroblocks in the viewer. muxdelay/muxpreload stop
        # ffmpeg from holding packets back before it starts sending.
        output = FfmpegOutput(
            "-muxdelay 0 -muxpreload 0 -f rtsp -rtsp_transport tcp " + self.rtsp)
        self.picam2.start_recording(
            LowLatencyH264Encoder(bitrate=self.bitrate, threads=self.threads), output)
        self._recording = True

    def stop(self):
        """Stop whatever start() began, and release the camera."""
        if self._recording:
            self.picam2.stop_recording()
            self._recording = False
        else:
            self.picam2.stop()
        self.picam2.close()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()


def main():
    """Stand-alone smoke test: camera straight to RTSP, no processing."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rtsp", default=DEFAULT_RTSP, help="where to publish video")
    parser.add_argument("--size", default="1280x720", help="video stream size")
    parser.add_argument("--bitrate", type=int, default=4_000_000)
    parser.add_argument("--threads", type=int, default=4,
                        help="encoder threads (slice-threaded, so no added delay)")
    args = parser.parse_args()

    main_w, main_h = (int(v) for v in args.size.split("x"))
    streamer = Streamer(size=(main_w, main_h), rtsp=args.rtsp,
                         bitrate=args.bitrate, threads=args.threads)
    streamer.start()
    print("Video: {} ({}x{}) - Ctrl+C to stop".format(args.rtsp, main_w, main_h),
          flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        streamer.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())