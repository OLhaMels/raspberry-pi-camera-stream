#!/usr/bin/env python3
"""Camera to RTSP, with the RTSP server inside this file. Nothing else.

This module runs the camera and, optionally, encodes and publishes the main
stream. It knows nothing about QR codes, overlays or anything else a
consumer might want from the frames: all of that reaches in through the
public `picam2` attribute, which is a plain Picamera2 object with its full
API intact (`capture_array`, `pre_callback`, ...).

There is no media server process - no MediaMTX, nothing to install or
start first. The intent was for ffmpeg itself to host the RTSP port with
`-rtsp_flags listen`, but it cannot: in ffmpeg 7.1 `rtsp_flags` is a
*demuxer* option (`ffmpeg -h muxer=rtsp` does not list it), so `-f rtsp
rtsp://0.0.0.0:8554/cam` tries to *connect out* to 0.0.0.0:8554 and dies
with "Connection refused" whatever flags are added. libavformat has no
RTSP server side at all - no DESCRIBE, no SETUP, no PLAY.

So the server side lives here instead, as RtspServer. ffmpeg still does
every part of the job it is actually good at - H.264 in, RTP packets out -
and simply aims those packets at a loopback port:

    camera -> LowLatencyH264Encoder -> ffmpeg -f rtp -> 127.0.0.1:5004
                                                            |
                                    RtspServer (this file) <-+
                                        | RTSP on 0.0.0.0:8554
                                        v
                                     ffplay / VLC

The client-facing address is the ordinary one, rtsp://<IP_Pi>:8554/cam,
over TCP-interleaved or UDP, whichever the player asks for.

    python3 streamer.py            # plain video, no QR detection

and from anywhere on the LAN:

    ffplay -rtsp_transport tcp -fflags nobuffer -flags low_delay rtsp://<IP_Pi>:8554/cam
    vlc --network-caching=0 rtsp://<IP_Pi>:8554/cam
"""

import argparse
import os
import random
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

import prctl
from av.codec.context import ThreadType
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput, Output

DEFAULT_RTSP = "rtsp://0.0.0.0:8554/cam"
DEFAULT_RTP_PORT = 5004


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


class LowLatencyFfmpegOutput(FfmpegOutput):
    """FfmpegOutput with the input-side options picamera2 does not expose.

    Left to its defaults, ffmpeg probes the raw H.264 arriving on stdin
    with a 5 MB / 5 s budget before it writes a single packet - which here
    means several seconds of video swallowed at startup and then released
    in one burst, and an SDP file that does not exist until the probe ends.
    There is nothing to probe: the encoder upstream put those bytes on the
    pipe, so the format is known. Naming it and asking for a token probe
    stops the read-ahead.

    ffmpeg logs one warning per start as a result - "not enough frames to
    estimate rate; consider increasing probesize" - which is expected and
    harmless: -use_wallclock_as_timestamps gives every packet a real
    timestamp, so nothing downstream needs that estimate. Raising probesize
    does not silence it (tested to 200 KB, and with -r/-framerate); it only
    brings the buffering back.
    """

    INPUT_OPTIONS = ["-f", "h264",
                     "-probesize", "32",
                     "-analyzeduration", "0",
                     "-fflags", "nobuffer",
                     "-use_wallclock_as_timestamps", "1",
                     "-thread_queue_size", "64"]

    def __init__(self, output_filename, **kwargs):
        super().__init__(output_filename, **kwargs)
        # FfmpegOutput leaves this at None for video-only output, which makes
        # its stop() wait for ffmpeg indefinitely. A timeout turns a wedged
        # process into a terminate() instead of a hang.
        self.timeout = 2

    def start(self):
        command = (["ffmpeg", "-loglevel", "warning", "-y"]
                   + self.INPUT_OPTIONS
                   + ["-i", "-", "-c:v", "copy"]
                   + self.output_filename.split())
        # preexec_fn mirrors picamera2's own: ffmpeg dies with us even if we
        # are killed without getting to stop().
        self.ffmpeg = subprocess.Popen(
            command, stdin=subprocess.PIPE,
            preexec_fn=lambda: prctl.set_pdeathsig(signal.SIGKILL))
        # Skip FfmpegOutput.start(), which would launch a second ffmpeg with
        # the command line just replaced; Output.start() only flips the
        # recording flag that outputframe() checks.
        Output.start(self)


class _Session:
    """One RTSP client: how it wants its packets, and whether it is playing."""

    __slots__ = ("sock", "addr", "sid", "lock", "interleaved", "client_ports",
                 "playing", "dead")

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.sid = "{:08d}".format(random.randrange(10_000_000, 99_999_999))
        self.lock = threading.Lock()
        self.interleaved = None     # (rtp_channel, rtcp_channel) for RTP/AVP/TCP
        self.client_ports = None    # (rtp_port, rtcp_port) for plain RTP/AVP
        self.playing = False
        self.dead = False


class RtspServer:
    """An RTSP server for exactly one live stream, fed by RTP on loopback.

    It speaks the four methods a player actually needs - OPTIONS, DESCRIBE,
    SETUP, PLAY, plus TEARDOWN and GET_PARAMETER for keepalives - and hands
    out whichever transport the player asks for: RTP/AVP/TCP (interleaved
    down the RTSP connection, the reliable choice and what ffplay picks
    with -rtsp_transport tcp) or plain RTP/AVP over UDP.

    It does not parse, repacketise or even look inside the RTP it forwards.
    ffmpeg's RTP muxer has already done that work, including the
    sprop-parameter-sets in the SDP it writes out; this class copies bytes
    from a loopback socket to every playing client, which is why it can keep
    up with 720p30 without costing anything measurable.

    Feeding it over UDP rather than a pipe is deliberate: a UDP socket never
    blocks the writer. ffmpeg keeps running at full speed whether anyone is
    watching or not, so a player that connects gets live video immediately
    instead of a backlog recorded while it was away, and the camera thread
    can never be stalled by a slow or absent viewer.
    """

    def __init__(self, host="0.0.0.0", port=8554, path="/cam",
                 rtp_port=DEFAULT_RTP_PORT, source_ports=None, sdp_path=None):
        self.host = host
        self.port = port
        self.path = path if path.startswith("/") else "/" + path
        self.rtp_port = rtp_port
        # The exact loopback ports ffmpeg was told to send from. Everything
        # else arriving on these sockets belongs to somebody else - a local
        # player's own RTCP reports, most obviously - and must not be
        # forwarded back out as if it were camera data.
        self.source_ports = source_ports or (rtp_port + 2, rtp_port + 3)
        self.sdp_path = sdp_path

        self._stop = threading.Event()
        self._sessions = []
        self._sessions_lock = threading.Lock()
        self._sdp = None
        self._threads = []

        # Bound before ffmpeg is told to send here, so nothing is missed and
        # the kernel never answers ffmpeg with ICMP port-unreachable.
        self._rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rtp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self._rtp_sock.bind(("127.0.0.1", rtp_port))
        self._rtcp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rtcp_sock.bind(("127.0.0.1", rtp_port + 1))

        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((host, port))
        self._listener.listen(8)

    # ---- lifecycle ------------------------------------------------------

    def start(self):
        for target, name in ((self._accept_loop, "rtsp-accept"),
                             (self._pump_loop, "rtsp-pump")):
            thread = threading.Thread(target=target, daemon=True, name=name)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self):
        self._stop.set()
        # Closing the listener is what wakes the accept loop out of accept().
        for sock in (self._listener, self._rtp_sock, self._rtcp_sock):
            try:
                sock.close()
            except OSError:
                pass
        with self._sessions_lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            try:
                session.sock.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []

    # ---- RTP fan-out ----------------------------------------------------

    def _pump_loop(self):
        """Copy every RTP/RTCP packet ffmpeg sends us to every playing client."""
        socks = [self._rtp_sock, self._rtcp_sock]
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select(socks, [], [], 0.5)
            except (OSError, ValueError):
                return          # sockets closed by stop()
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(65535)
                except OSError:
                    return
                channel = 0 if sock is self._rtp_sock else 1
                if addr != ("127.0.0.1", self.source_ports[channel]):
                    continue
                self._broadcast(channel, data)

    def _broadcast(self, channel, data):
        with self._sessions_lock:
            sessions = list(self._sessions)
        for session in sessions:
            if not session.playing or session.dead:
                continue
            try:
                if session.interleaved is not None:
                    # RFC 2326 section 10.12: '$', channel, 2-byte length,
                    # then the packet, down the RTSP connection itself.
                    frame = (b"$" + bytes([session.interleaved[channel]])
                             + struct.pack("!H", len(data)) + data)
                    with session.lock:
                        session.sock.sendall(frame)
                elif session.client_ports is not None:
                    sock = self._rtp_sock if channel == 0 else self._rtcp_sock
                    sock.sendto(data, (session.addr[0],
                                       session.client_ports[channel]))
            except OSError:
                session.dead = True

    # ---- RTSP ------------------------------------------------------------

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._listener.accept()
            except OSError:
                return          # listener closed by stop()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            thread = threading.Thread(target=self._serve, args=(conn, addr),
                                      daemon=True, name="rtsp-client")
            thread.start()

    def _serve(self, conn, addr):
        session = _Session(conn, addr)
        with self._sessions_lock:
            self._sessions.append(session)
        print("viewer connected from {}".format(addr[0]), flush=True)
        buffer = b""
        try:
            while not self._stop.is_set() and not session.dead:
                try:
                    data = conn.recv(4096)
                except OSError:
                    break
                if not data:
                    break
                buffer += data
                buffer = self._consume(session, buffer)
                if buffer is None:
                    break
        finally:
            with self._sessions_lock:
                if session in self._sessions:
                    self._sessions.remove(session)
            try:
                conn.close()
            except OSError:
                pass
            print("viewer from {} disconnected".format(addr[0]), flush=True)

    def _consume(self, session, buffer):
        """Pull whole messages out of `buffer`; return what is left over."""
        while True:
            if buffer.startswith(b"$"):
                # Interleaved data coming *from* the client. Nothing here
                # wants it, but it has to be stepped over to find the next
                # request.
                if len(buffer) < 4:
                    return buffer
                length = struct.unpack("!H", buffer[2:4])[0]
                if len(buffer) < 4 + length:
                    return buffer
                buffer = buffer[4 + length:]
                continue

            end = buffer.find(b"\r\n\r\n")
            if end < 0:
                return buffer if len(buffer) < 8192 else None
            head = buffer[:end].decode("utf-8", "replace")
            rest = buffer[end + 4:]

            length = 0
            for line in head.split("\r\n")[1:]:
                name, _, value = line.partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value.strip() or 0)
            if len(rest) < length:
                return buffer
            buffer = rest[length:]

            if not self._handle(session, head):
                return None

    def _handle(self, session, head):
        """Answer one request. Returns False to close the connection."""
        lines = head.split("\r\n")
        parts = lines[0].split()
        if len(parts) < 2:
            return False
        method, url = parts[0].upper(), parts[1]
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        cseq = headers.get("cseq", "0")

        if method == "OPTIONS":
            self._reply(session, cseq, extra=[
                "Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER"])
        elif method == "DESCRIBE":
            sdp = self._describe()
            if sdp is None:
                self._reply(session, cseq, code="503 Service Unavailable")
            else:
                base = url if url.endswith("/") else url + "/"
                self._reply(session, cseq, extra=[
                    "Content-Base: " + base,
                    "Content-Type: application/sdp"], body=sdp)
        elif method == "SETUP":
            self._setup(session, cseq, headers.get("transport", ""))
        elif method == "PLAY":
            session.playing = True
            self._reply(session, cseq, extra=[
                "Session: " + session.sid,
                "Range: npt=now-"])
        elif method in ("TEARDOWN",):
            session.playing = False
            self._reply(session, cseq, extra=["Session: " + session.sid])
            return False
        elif method in ("GET_PARAMETER", "SET_PARAMETER"):
            self._reply(session, cseq, extra=["Session: " + session.sid])
        elif method == "PAUSE":
            session.playing = False
            self._reply(session, cseq, extra=["Session: " + session.sid])
        else:
            self._reply(session, cseq, code="501 Not Implemented")
        return True

    def _setup(self, session, cseq, transport):
        """Agree a transport. Interleaved TCP or plain UDP, client's choice."""
        fields = [f.strip() for f in transport.split(";")]
        interleaved = None
        client_ports = None
        for field in fields:
            if field.startswith("interleaved="):
                lo, _, hi = field[len("interleaved="):].partition("-")
                interleaved = (int(lo), int(hi or int(lo) + 1))
            elif field.startswith("client_port="):
                lo, _, hi = field[len("client_port="):].partition("-")
                client_ports = (int(lo), int(hi or int(lo) + 1))

        if "RTP/AVP/TCP" in transport.upper():
            if interleaved is None:
                interleaved = (0, 1)
            session.interleaved = interleaved
            session.client_ports = None
            reply = "RTP/AVP/TCP;unicast;interleaved={}-{}".format(*interleaved)
        elif client_ports is not None:
            session.client_ports = client_ports
            session.interleaved = None
            reply = ("RTP/AVP;unicast;client_port={}-{};server_port={}-{}"
                     .format(client_ports[0], client_ports[1],
                             self.rtp_port, self.rtp_port + 1))
        else:
            # Neither a TCP request nor usable UDP ports: say so plainly and
            # let the player fall back (ffplay and VLC both retry over TCP).
            self._reply(session, cseq, code="461 Unsupported Transport")
            return

        self._reply(session, cseq, extra=[
            "Session: {};timeout=60".format(session.sid),
            "Transport: " + reply])

    def _reply(self, session, cseq, code="200 OK", extra=None, body=""):
        lines = ["RTSP/1.0 " + code,
                 "CSeq: " + str(cseq),
                 "Server: streamer.py"]
        if extra:
            lines += extra
        payload = body.encode("utf-8")
        if payload:
            lines.append("Content-Length: " + str(len(payload)))
        message = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + payload
        try:
            with session.lock:
                session.sock.sendall(message)
        except OSError:
            session.dead = True

    def _describe(self, timeout=5.0):
        """The SDP ffmpeg wrote, rewritten for RTSP. None if it never appeared.

        ffmpeg's SDP describes the loopback RTP flow it is sending, so it
        names a concrete address and port. In RTSP the transport is settled
        later, per client, by SETUP - so the address becomes 0.0.0.0, the
        port becomes 0, and control attributes are added for the client to
        aim its SETUP at. The parts that matter, the rtpmap and the fmtp
        carrying sprop-parameter-sets, are passed through untouched.
        """
        if self._sdp is not None:
            return self._sdp
        if not self.sdp_path:
            return None
        deadline = time.monotonic() + timeout
        raw = ""
        while time.monotonic() < deadline:
            try:
                with open(self.sdp_path) as handle:
                    raw = handle.read()
            except OSError:
                raw = ""
            if "m=" in raw:
                break
            time.sleep(0.1)
        if "m=" not in raw:
            return None

        lines = []
        for line in raw.replace("\r\n", "\n").split("\n"):
            if not line:
                continue
            if line.startswith("c="):
                line = "c=IN IP4 0.0.0.0"
            elif line.startswith("m="):
                fields = line.split()
                fields[1] = "0"
                lines.append("a=control:*")
                line = " ".join(fields)
            lines.append(line)
        # Appended, not inserted after m=, to keep SDP's b= before a= ordering.
        lines.append("a=control:streamid=0")
        self._sdp = "\r\n".join(lines) + "\r\n"
        return self._sdp


class Streamer:
    """Owns the camera: configures it, optionally encodes and publishes the
    main stream over RTSP, and otherwise gets out of the way.

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
                 lores_format="YUV420", rtsp=DEFAULT_RTSP, bitrate=4_000_000,
                 threads=4, rtp_port=DEFAULT_RTP_PORT):
        self.main_size = main_size
        self.lores_size = lores_size
        self.rtsp = rtsp
        self.bitrate = bitrate
        self.threads = threads
        self.rtp_port = rtp_port

        self.picam2 = Picamera2()
        config_kwargs = {"main": {"size": main_size}}
        if lores_size is not None:
            config_kwargs["lores"] = {"size": lores_size, "format": lores_format}
        self.picam2.configure(self.picam2.create_video_configuration(**config_kwargs))

        self._server = None
        self._sdp_path = None
        self._publishing = False

    # ---- lifecycle ------------------------------------------------------

    def start(self, publish=True):
        """Start the camera; if `publish`, also encode and serve RTSP.

        `publish=False` starts the camera and nothing else, for callers that
        only want frames (a detector running with no video output at all).
        """
        if not publish:
            self.picam2.start()
            return

        parts = urlsplit(self.rtsp)
        handle, self._sdp_path = tempfile.mkstemp(prefix="streamer-", suffix=".sdp")
        os.close(handle)

        # Bind every socket before ffmpeg starts, so no RTP is lost and the
        # kernel never bounces a packet back at it.
        self._server = RtspServer(host=parts.hostname or "0.0.0.0",
                                  port=parts.port or 554,
                                  path=parts.path or "/cam",
                                  rtp_port=self.rtp_port,
                                  source_ports=(self.rtp_port + 2,
                                                self.rtp_port + 3),
                                  sdp_path=self._sdp_path).start()

        encoder = LowLatencyH264Encoder(bitrate=self.bitrate, threads=self.threads)
        # muxdelay/muxpreload stop ffmpeg holding packets back before it
        # starts sending. localrtpport keeps ffmpeg's own socket off the two
        # ports RtspServer just bound.
        output = LowLatencyFfmpegOutput(
            "-muxdelay 0 -muxpreload 0 -f rtp -sdp_file {sdp} "
            "rtp://127.0.0.1:{port}?localrtpport={local}&localrtcpport={local2}"
            .format(sdp=self._sdp_path, port=self.rtp_port,
                    local=self.rtp_port + 2, local2=self.rtp_port + 3))
        self.picam2.start_recording(encoder, output)
        self._publishing = True

    def stop(self):
        """Stop whatever start() began, and release camera and sockets."""
        if self._publishing:
            self.picam2.stop_recording()
            self._publishing = False
        else:
            self.picam2.stop()
        if self._server is not None:
            self._server.stop()
            self._server = None
        if self._sdp_path:
            try:
                os.unlink(self._sdp_path)
            except OSError:
                pass
            self._sdp_path = None
        self.picam2.close()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()


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


def public_url(rtsp):
    """The same URL with 0.0.0.0 replaced by an address a viewer can reach."""
    parts = urlsplit(rtsp)
    host = parts.hostname or "0.0.0.0"
    if host in ("0.0.0.0", "::", ""):
        host = local_ip()
    return "rtsp://{}:{}{}".format(host, parts.port or 554, parts.path or "/cam")


def viewer_hint(rtsp):
    """The command to paste on the viewing machine, with a real IP in it."""
    return ("ffplay -rtsp_transport tcp -fflags nobuffer -flags low_delay "
            + public_url(rtsp))


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
    """Stand-alone smoke test: camera straight to RTSP, no processing."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rtsp", default=DEFAULT_RTSP,
                        help="where to serve RTSP (default: %(default)s)")
    parser.add_argument("--size", default="1280x720", help="video stream size")
    parser.add_argument("--bitrate", type=int, default=4_000_000)
    parser.add_argument("--threads", type=int, default=4,
                        help="encoder threads (slice-threaded, so no added delay)")
    parser.add_argument("--rtp-port", type=int, default=DEFAULT_RTP_PORT,
                        help="loopback port ffmpeg sends RTP to")
    args = parser.parse_args()

    main_w, main_h = (int(v) for v in args.size.split("x"))
    video = Streamer(main_size=(main_w, main_h), rtsp=args.rtsp,
                     bitrate=args.bitrate, threads=args.threads,
                     rtp_port=args.rtp_port)
    signal.signal(signal.SIGTERM, _shutdown_on_sigterm)
    video.start(publish=True)
    print("Serving {}x{} at {}\n  {}\nCtrl+C to stop".format(
        main_w, main_h, public_url(args.rtsp), viewer_hint(args.rtsp)), flush=True)
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
