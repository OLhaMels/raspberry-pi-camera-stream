#!/usr/bin/env python3
"""Camera to a self-hosted, low-latency video stream. Nothing else.

This module runs the camera and, optionally, encodes and serves the main
stream. It knows nothing about QR codes, overlays or anything else a
consumer might want from the frames: all of that reaches in through the
public `picam2` attribute, which is a plain Picamera2 object with its full
API intact (`capture_array`, `pre_callback`, ...).

There is no media server process. ffmpeg itself owns the listening socket
(`tcp://0.0.0.0:8554?listen=1`) and hands the stream to whoever connects,
so the whole stack is this file plus the ffmpeg that FfmpegOutput starts.

Why MPEG-TS over TCP rather than RTSP: ffmpeg cannot host an RTSP server.
In ffmpeg 7.1 `rtsp_flags` (and its `listen` value) is a *demuxer* option
only - `ffmpeg -h muxer=rtsp` does not list it - so `-f rtsp
rtsp://0.0.0.0:8554/cam` tries to *connect out* to 0.0.0.0:8554 and dies
with "Connection refused" whatever flags you add. libavformat simply has
no RTSP server side (no DESCRIBE/SETUP/PLAY). A TCP listener carrying
MPEG-TS is the closest thing ffmpeg can host by itself, and it is the
lower-latency one at that: no RTP packetisation, no session setup, first
byte straight down the socket.

    python3 streamer.py            # plain video, no QR detection

and from anywhere on the LAN:

    ffplay -fflags nobuffer -flags low_delay -framedrop tcp://<IP_Pi>:8554
    vlc --network-caching=0 tcp://<IP_Pi>:8554
"""

import argparse
import collections
import signal
import socket
import subprocess
import sys
import threading
import time

import prctl
from av.codec.context import ThreadType
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput, Output

DEFAULT_URL = "tcp://0.0.0.0:8554?listen=1"


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


class BufferedFfmpegOutput(FfmpegOutput):
    """FfmpegOutput that never blocks its caller, dropping video instead.

    This matters because of where outputframe() is called from. picamera2's
    Encoder.encode() runs the encode *synchronously*, on the camera's own
    request-handling thread, and hands the packet straight to the output.
    The stock FfmpegOutput writes it to ffmpeg's stdin pipe and blocks if
    that pipe is full - which stalls the camera thread, and with it every
    other consumer of these frames, including a QR detector calling
    capture_array() on the lores stream.

    That pipe *will* fill: with `?listen=1` ffmpeg blocks in accept() until
    a viewer connects, reading nothing, and 64 KB of pipe is about a tenth
    of a second of 4 Mbit/s video. So without this class the camera freezes
    whenever nobody is watching, which is exactly backwards.

    Here the camera thread only appends to a short deque and returns; a
    writer thread does the blocking write. When the deque is full we drop,
    and once we have dropped anything we keep dropping until the next
    keyframe, so the viewer resyncs cleanly on an IDR (one second apart at
    the encoder's default iperiod=30) instead of decoding into a mess of
    missing references.

    start() also rebuilds the ffmpeg command line rather than inheriting
    it, because the *input* side needs options picamera2 does not expose -
    see INPUT_OPTIONS below.
    """

    # What picamera2's own command line is missing. Left to its defaults,
    # ffmpeg probes the raw H.264 arriving on stdin with a 5 MB / 5 s
    # budget, and it does that eagerly, before a viewer has connected. The
    # video it swallows while probing is not thrown away: it is buffered and
    # dumped at the first viewer the moment one appears. Measured here, that
    # was 840 KB - about 1.6 seconds of video - delivered in the first
    # 100 ms, which is to say every viewer started a second and a half
    # behind live and stayed there.
    #
    # There is nothing to probe: we know exactly what is on that pipe,
    # because the encoder upstream put it there. Naming the format and
    # asking for a token probe keeps ffmpeg from reading ahead at all.
    #
    # ffmpeg logs one warning per start as a result - "not enough frames to
    # estimate rate; consider increasing probesize" - which is expected and
    # harmless: nothing downstream needs that estimate, because
    # -use_wallclock_as_timestamps gives every packet a real timestamp.
    # Don't try to silence it by raising probesize; tested up to 200 KB and
    # with -r/-framerate, and the warning comes from -analyzeduration 0
    # regardless. Raising the probe only brings the buffering back.
    INPUT_OPTIONS = ["-f", "h264",
                     "-probesize", "32",
                     "-analyzeduration", "0",
                     "-fflags", "nobuffer",
                     "-use_wallclock_as_timestamps", "1",
                     "-thread_queue_size", "64"]

    def __init__(self, output_filename, depth=5, **kwargs):
        # depth is deliberately shallow - five frames, about a sixth of a
        # second. Whatever is sitting here when a viewer connects is stale
        # video that gets delivered before anything live does, so a deep
        # queue buys jitter tolerance at the cost of exactly the latency
        # this whole pipeline exists to avoid.
        super().__init__(output_filename, **kwargs)
        self._queue = collections.deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closing = threading.Event()
        self._depth = depth
        self._dropping = False
        self._writer = None
        self.dropped = 0
        # FfmpegOutput leaves this at None for video-only output, which makes
        # its stop() wait forever for ffmpeg to exit. ffmpeg parked in
        # accept() with no viewer never will: it is not reading the stdin we
        # just closed. A timeout turns that hang into a terminate().
        self.timeout = 2

    def start(self):
        command = (["ffmpeg", "-loglevel", "warning", "-y"]
                   + self.INPUT_OPTIONS
                   + ["-i", "-", "-c:v", "copy"]
                   + self.output_filename.split())
        # preexec_fn mirrors picamera2's own: it makes sure ffmpeg dies with
        # us even if we are killed without getting to stop().
        self.ffmpeg = subprocess.Popen(
            command, stdin=subprocess.PIPE,
            preexec_fn=lambda: prctl.set_pdeathsig(signal.SIGKILL))
        # Skip FfmpegOutput.start(), which would launch a second ffmpeg with
        # the command line we just replaced; Output.start() only flips the
        # recording flag that outputframe() checks.
        Output.start(self)

        self._closing.clear()
        self._writer = threading.Thread(target=self._drain, daemon=True,
                                        name="ffmpeg-writer")
        self._writer.start()

    def stop(self):
        self._closing.set()
        self._wake.set()
        # Kill ffmpeg before touching its stdin. With no viewer connected
        # ffmpeg is parked in accept() and reads nothing, so the writer
        # thread is very likely blocked in write() on a full pipe - and
        # Python's buffered writer holds a lock for the duration of that
        # write, which the parent's stdin.close() would then wait on for
        # ever. Ending the process first makes the blocked write fail
        # instead, releasing thread and lock. (Verified: without this,
        # SIGINT left the process hung with the writer stuck in
        # FfmpegOutput.outputframe.)
        process = self.ffmpeg
        if process is not None and process.poll() is None:
            process.terminate()
        if self._writer is not None:
            self._writer.join(timeout=2.0)
            self._writer = None
        super().stop()

    # ---- camera thread --------------------------------------------------

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None,
                    audio=False):
        with self._lock:
            if self._dropping and not keyframe:
                self.dropped += 1
                return
            if len(self._queue) >= self._depth:
                if not keyframe:
                    self._dropping = True
                    self.dropped += 1
                    return
                # A keyframe arrived with the queue still full: throw the
                # backlog away and restart the stream from here.
                self.dropped += len(self._queue)
                self._queue.clear()
            self._dropping = False
            self._queue.append((frame, keyframe, timestamp))
        self._wake.set()

    # ---- writer thread --------------------------------------------------

    def _drain(self):
        while True:
            with self._lock:
                item = self._queue.popleft() if self._queue else None
            if item is None:
                if self._closing.is_set():
                    return
                self._wake.wait(0.1)
                self._wake.clear()
                continue
            frame, keyframe, timestamp = item
            super().outputframe(frame, keyframe, timestamp)


class Streamer:
    """Owns the camera: configures it, optionally encodes and serves the main
    stream, and otherwise gets out of the way.

    `lores_size` is optional. Pass it if some consumer wants a second,
    lower-resolution stream straight off the ISP - qr_detector.py does, for
    frame-rate and image-quality reasons that are none of this class's
    business; leave it out for a plain single-stream camera.

    Everything a caller needs in order to hook into frames - `pre_callback`,
    `capture_array()`, `capture_metadata()` and the rest - lives on the
    Picamera2 instance itself, exposed here as `picam2`. This class does not
    wrap or narrow that API; it only owns the object's lifecycle.
    """

    def __init__(self, main_size=(1280, 720), lores_size=None,
                 lores_format="YUV420", url=DEFAULT_URL, bitrate=4_000_000,
                 threads=4, reconnect=True):
        self.main_size = main_size
        self.lores_size = lores_size
        self.url = url
        self.bitrate = bitrate
        self.threads = threads
        self.reconnect = reconnect

        self.picam2 = Picamera2()
        config_kwargs = {"main": {"size": main_size}}
        if lores_size is not None:
            config_kwargs["lores"] = {"size": lores_size, "format": lores_format}
        self.picam2.configure(self.picam2.create_video_configuration(**config_kwargs))

        self._encoder = None
        self._output = None
        self._publishing = False
        self._stop_flag = threading.Event()
        self._supervisor = None

    # ---- lifecycle ------------------------------------------------------

    def start(self, publish=True):
        """Start the camera; if `publish`, also encode and serve the stream.

        `publish=False` starts the camera and nothing else, for callers that
        only want frames (a detector running with no video output at all).
        """
        if not publish:
            self.picam2.start()
            return

        self._encoder, self._output = self._build()
        self.picam2.start_recording(self._encoder, self._output)
        self._publishing = True

        if self.reconnect:
            self._supervisor = threading.Thread(
                target=self._supervise, daemon=True, name="stream-supervisor")
            self._supervisor.start()

    def stop(self):
        """Stop whatever start() began, and release camera and socket."""
        self._stop_flag.set()
        if self._supervisor is not None:
            self._supervisor.join(timeout=3.0)
            self._supervisor = None
        if self._publishing:
            # Tears down the encoder and the ffmpeg process with it, which is
            # what closes the listening socket on port 8554.
            self.picam2.stop_recording()
            self._publishing = False
        else:
            self.picam2.stop()
        self.picam2.close()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    # ---- internals ------------------------------------------------------

    def _build(self):
        """One encoder plus one ffmpeg, configured for minimum delay.

        muxdelay/muxpreload stop ffmpeg holding packets back before it starts
        sending; flush_packets pushes every TS packet down the socket as soon
        as it is written instead of waiting for a block to fill.
        """
        encoder = LowLatencyH264Encoder(bitrate=self.bitrate, threads=self.threads)
        output = BufferedFfmpegOutput(
            "-muxdelay 0 -muxpreload 0 -flush_packets 1 -f mpegts " + self.url)
        return encoder, output

    def _supervise(self):
        """Re-arm the listening socket after a viewer goes away.

        ffmpeg serves exactly one connection and exits when it ends - the
        write to a closed socket kills it. Without this the stream would be
        a one-shot: the first viewer gets video, everyone after them gets
        connection refused. Restarting only the encoder leaves the camera
        (and any detector reading from it) running throughout.
        """
        while not self._stop_flag.wait(0.5):
            process = self._output.ffmpeg
            if process is None or process.poll() is None:
                continue
            print("viewer gone (ffmpeg exit {}); listening again on {}".format(
                process.returncode, self.url), flush=True)
            self.picam2.stop_encoder()
            if self._stop_flag.is_set():
                return
            self._encoder, self._output = self._build()
            self.picam2.start_encoder(self._encoder, self._output)


def local_ip():
    """Best guess at the address a viewer on the LAN should aim at.

    No packet is actually sent: connecting a UDP socket only makes the
    kernel pick the route, and with it the source address.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def viewer_hint(url):
    """The command to paste on the viewing machine, with a real IP in it."""
    port = url.rsplit(":", 1)[-1].split("?")[0]
    return ("ffplay -fflags nobuffer -flags low_delay -framedrop "
            "tcp://{}:{}".format(local_ip(), port))


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
    """Stand-alone smoke test: camera straight to the socket, no processing."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_URL,
                        help="where ffmpeg listens (default: %(default)s)")
    parser.add_argument("--size", default="1280x720", help="video stream size")
    parser.add_argument("--bitrate", type=int, default=4_000_000)
    parser.add_argument("--threads", type=int, default=4,
                        help="encoder threads (slice-threaded, so no added delay)")
    parser.add_argument("--no-reconnect", dest="reconnect", action="store_false",
                        help="serve one viewer, then stop listening")
    args = parser.parse_args()

    main_w, main_h = (int(v) for v in args.size.split("x"))
    video = Streamer(main_size=(main_w, main_h), url=args.url,
                     bitrate=args.bitrate, threads=args.threads,
                     reconnect=args.reconnect)
    signal.signal(signal.SIGTERM, _shutdown_on_sigterm)
    video.start(publish=True)
    print("Serving {}x{} on {}\n  {}\nCtrl+C to stop".format(
        main_w, main_h, args.url, viewer_hint(args.url)), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        video.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
