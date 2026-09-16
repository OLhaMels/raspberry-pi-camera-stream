# Pi camera QR reader — RTSP, no media server

An IMX219 camera on a Raspberry Pi 5. The Pi **reads QR codes itself** and, in
parallel, serves a video preview over RTSP so another machine can watch what the
camera sees.

The split matters: the QR payloads are meant to become commands for a
manipulator, so decoding has to happen on the Pi. The video preview only exists
so a developer can confirm that a read succeeded, and it can be switched off
(`--no-stream`) without affecting reading at all.

Two Python files, nothing to install, and **no MediaMTX** — no media server
process at all. The RTSP server is a class inside `streamer.py`.

## How it works

```
IMX219 -> libcamera / picamera2
            |
            +-- main  1280x720 -> overlay burned in -> H.264 (libx264, slice-threaded)
            |                        |                     -> ffmpeg -f rtp -> 127.0.0.1:5004
            |                        |                                              |
            |                        |                        RtspServer (streamer.py) <-+
            |                        |                              | RTSP on 0.0.0.0:8554
            |                        |                              v
            |                        |                         ffplay / VLC
            |                        |
            +-- lores  640x480 YUV420 -> zbar -> payload -> handle_code()
                                                        -> overlay state
```

Both streams leave the ISP at the same time, so a read never depends on the
video link, on the network, or on H.264 artefacts. Only the preview does.

### Why the RTSP server is written here

The obvious design is to let ffmpeg host the port itself with
`-rtsp_flags listen`. **It cannot.** In ffmpeg 7.1 `rtsp_flags` is a *demuxer*
option — `ffmpeg -h muxer=rtsp` does not list it — so on an output it is
silently ignored and ffmpeg tries to *connect out* instead:

```
$ ffmpeg ... -rtsp_flags listen -f rtsp rtsp://0.0.0.0:8554/cam
[tcp @ ...] Connection to tcp://0.0.0.0:8554?timeout=0 failed: Connection refused
[out#0/rtsp @ ...] Could not write header (incorrect codec parameters ?): Connection refused
```

`-listen 1` and `?listen=1` fail the same way; the port is never bound.
libavformat has no RTSP server side at all — no DESCRIBE, no SETUP, no PLAY.

So `RtspServer` in `streamer.py` does that part, and ffmpeg keeps doing what it
is genuinely good at: H.264 in, RTP packets out. The server copies those packets
from a loopback socket to every playing client. It never parses or repacketises
them, which is why 720p30 costs it nothing measurable.

Feeding it over UDP rather than a pipe is deliberate: a UDP socket never blocks
the writer, so ffmpeg runs at full speed whether anyone is watching or not. A
player that connects gets live video immediately rather than a backlog recorded
while it was away.

## Files

| File | What it is |
| --- | --- |
| `streamer.py` | Camera, low-latency encoder, ffmpeg, and the RTSP server. Knows nothing about QR codes. |
| `qr_detector.py` | libzbar binding, scan thread, overlay, and the `main()` that wires it all to a camera. |

`streamer.py` runs on its own (`python3 streamer.py`) and gives plain video with
no QR handling — useful for testing the video path in isolation.

## Requirements

**On the Pi: nothing to install.** This runs on what Raspberry Pi OS already
ships:

- `python3-picamera2`, `python3-numpy`, `python3-av`, `python3-pil`, `ffmpeg`
- `libzbar0` (loaded by hand through ctypes — `python3-pyzbar` is **not** needed
  and is not packaged for Debian trixie)

**On the viewing machine: a player.** Install commands are under *On the viewing
machine* below.

Verified on a Raspberry Pi 5 with an IMX219: Debian trixie, kernel 6.18,
ffmpeg 7.1.5, libcamera v0.7.1, zbar 0.23.93. Watched back with ffmpeg 9.0.1 on
Windows 11 and with VLC.

## Running

### On the Pi

```bash
cd ~/raspberry-pi-camera-stream/rtsp
python3 qr_detector.py
```

That is the whole thing. It prints the address to point a player at:

```
encoder: slice threading, 4 thread(s)
Serving 1280x720 at rtsp://10.1.1.30:8554/cam
  ffplay -rtsp_transport tcp -fflags nobuffer -flags low_delay rtsp://10.1.1.30:8554/cam
Scanning 640x480, OSD on - Ctrl+C to stop
```

Every new QR code is reported on stdout as it is read:

```
[22:41:07] QR-Code q=1  at 291,118 262x262px
           -> https://example.com/pi-cam-test-42
```

Ctrl+C stops it and releases the camera and both sockets.

### On the viewing machine

Install a player first. On Windows 11:

```powershell
winget install Gyan.FFmpeg
```

Then **open a new terminal.** PowerShell reads `PATH` once, when it starts, so a
window that was already open before the install will still tell you

```
ffplay : The term 'ffplay' is not recognized as the name of a cmdlet ...
```

That is the stale `PATH`, not a failed install. A fresh window fixes it; to
avoid reopening, refresh the current one instead:

```powershell
$env:Path = [Environment]::GetEnvironmentVariable('Path','User') + ';' + [Environment]::GetEnvironmentVariable('Path','Machine')
```

On Debian, Ubuntu or Raspberry Pi OS: `sudo apt install ffmpeg`. On macOS:
`brew install ffmpeg`.

Then point it at the Pi:

```bash
ffplay -rtsp_transport tcp -fflags nobuffer -flags low_delay rtsp://10.1.1.30:8554/cam
```

VLC works too and needs no terminal:

```powershell
winget install VideoLAN.VLC
```

Then **Media → Open Network Stream** and paste the URL. Set **Tools →
Preferences → Input/Codecs → Network caching** to 0 first, or VLC adds a second
of its own buffering on top of everything this project does to avoid exactly
that.

`-rtsp_transport tcp` is the reliable choice and is what you want over Wi-Fi.
Plain UDP works too — drop the flag and the player will negotiate it — and is
marginally lower latency on a quiet wired LAN, at the cost of visible tearing
when packets are lost.

Several viewers can watch at once, and a viewer can leave and come back without
disturbing the Pi.

### Can the two files run in separate terminals?

No. `qr_detector.py` **already runs both** — it starts the camera, the encoder
and the RTSP server through `Streamer`, and scans in a thread alongside them.
One process, one command.

Starting `streamer.py` in one terminal and `qr_detector.py` in another does not
work, because only one process can hold the camera. The second one dies
immediately:

```
ERROR V4L2 'imx219 11-0010': Unable to set controls: Device or resource busy
RuntimeError: Failed to acquire camera: Device or resource busy
RuntimeError: Camera __init__ sequence did not complete.
```

That is also why the two are split the way they are: `streamer.py` owns the
`Picamera2` object and publishes it as `.picam2`, and `qr_detector.py` reaches
into *that* object for lores frames and for the overlay callback. They share one
camera by living in one process, not by each opening their own.

If you want a second terminal, give it the log rather than a second process:

```bash
# terminal 1
python3 qr_detector.py --log ~/qr_hits.log

# terminal 2 - just the reads, as they happen
tail -f ~/qr_hits.log
```

(Reading QR codes out of the *video stream* in a separate process would avoid
the conflict, but it is the design this project deliberately avoids: reads would
then depend on H.264 artefacts and on the network. The lores stream comes
straight off the ISP, so a read never depends on the preview at all.)

### Other ways to run it

```bash
# video only, no QR detection at all - tests the video path in isolation,
# INSTEAD of qr_detector.py, never alongside it
python3 streamer.py

# QR reading only, no video served - the camera still runs
python3 qr_detector.py --no-stream

# check the zbar binding against a still image; needs no camera and no picamera2
python3 qr_detector.py --self-test ../tests/qr_test.png

# log every new read to a file as well as the console
python3 qr_detector.py --log ~/qr_hits.log
```

### Stopping it

`Ctrl+C` in the foreground. Detached — started by systemd, or with
`setsid nohup ... &` — send SIGTERM, which is what both `pkill` and
`systemctl stop` do by default:

```bash
pkill -f qr_detector.py
```

Either way the same shutdown runs: the scan thread is joined, the camera and
the socket are released, and a summary line is printed.

```
Stopped after 8s: 222 scans (27.4/s), 0 code(s) read
```

Do **not** stop it with `pkill -9`: that kills the process outright, and while
the kernel still reclaims the camera and the port, nothing gets the chance to
shut down in order.

### Options

| Flag | Default | What it does |
| --- | --- | --- |
| `--rtsp URL` | `rtsp://0.0.0.0:8554/cam` | Where to serve RTSP. |
| `--size WxH` | `1280x720` | Size of the video stream. |
| `--scan-size WxH` | `640x480` | Size of the lores stream used for decoding. |
| `--bitrate N` | `4000000` | H.264 bitrate. |
| `--threads N` | `4` | Encoder threads. Slice-threaded, so more threads add no delay. |
| `--rtp-port N` | `5004` | Loopback port ffmpeg sends RTP to. Also uses N+1 to N+3. |
| `--scan-interval S` | `0.0` | Seconds between scans. 0 means as fast as lores frames arrive. |
| `--repeat-after S` | `5.0` | How long a code must be out of frame before it is reported again. |
| `--heartbeat S` | `60.0` | Seconds between liveness lines. 0 turns them off. |
| `--osd-hold S` | `5.0` | How long the readout stays on screen after a read. |
| `--no-osd` | off | Do not burn the outline and readout into the video. |
| `--no-stream` | off | Read QR codes only; serve no video. |
| `--log PATH` | — | Append every new read to this file. |
| `--self-test IMAGE` | — | Decode a still image and exit. No camera needed. |

`streamer.py` on its own takes `--rtsp`, `--size`, `--bitrate`, `--threads` and
`--rtp-port`, with the same meanings.

### Starting it at boot

```ini
# /etc/systemd/system/qr-detector.service
[Unit]
Description=Pi camera QR reader (RTSP)
After=network-online.target

[Service]
User=cam_user
WorkingDirectory=/home/cam_user/raspberry-pi-camera-stream/rtsp
ExecStart=/usr/bin/python3 qr_detector.py --log /home/cam_user/qr_hits.log
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now qr-detector
journalctl -u qr-detector -f
```

## Latency

The pipeline is built so that nothing queues:

- **libx264 slice threading** instead of picamera2's frame threading. Frame
  threading makes libx264 hold `thread_count` frames before emitting the first
  packet — 100 ms at 4 threads. `LowLatencyH264Encoder` forces
  `ThreadType.SLICE`, and the first packet comes out after one frame. The line
  `encoder: slice threading, 4 thread(s)` at startup is that being read back,
  not assumed. The Pi 5 has no hardware H.264 encoder, so this stands in for one.
- **A token input probe on ffmpeg.** Left at its defaults ffmpeg probes the raw
  H.264 on stdin with a 5 MB / 5 s budget, and buffers what it reads. Measured
  here before the fix: 840 KB — about 1.6 seconds of video — dumped at the first
  viewer in its first 100 ms. `-f h264 -probesize 32 -analyzeduration 0` removes
  the read-ahead entirely.
- **A single-slot mailbox** (`queue.Queue(maxsize=1)`) between the scan thread
  and the overlay. The drawing side only ever wants the newest detection, never
  a backlog.

Measured on this Pi, with a hand-written RTSP client:

| | |
| --- | --- |
| First RTP packet after PLAY | 26 ms |
| Bytes in the first 100 ms | 47 KB, against 50 KB/100 ms steady — i.e. no backlog |
| QR scan rate | 24–28 scans/s (the target was 15–20 Hz) |
| QR decoding, end to end | confirmed on hardware — real codes read off the lores stream while streaming |
| Clean shutdown, Ctrl+C or SIGTERM | 1–3 s, no orphaned ffmpeg, port released |

Glass-to-glass latency — photons to pixels on the viewer — was **not** measured;
it depends on your player's buffering and your network as much as on this code.
What is measured above is that this end contributes no queue of its own.

## Notes and known behaviour

- **Hold the code fully inside the frame.** zbar will still decode one that is
  clipped by an edge — it extrapolates the polygon, and you can spot this in the
  reported position, where a corner goes negative (`at 229,-22`) — but those
  reads come back at `q=1`, zbar's lowest confidence, and they are the ones that
  fail intermittently. A code wholly in frame reads far more reliably. The
  overlay handles off-frame corners safely either way: coordinates are clamped
  before anything is drawn.
- ffmpeg logs `not enough frames to estimate rate; consider increasing
  probesize` once per start. Expected and harmless: it is a consequence of
  `-analyzeduration 0`, and nothing downstream needs that estimate because
  `-use_wallclock_as_timestamps` gives every packet a real timestamp. Raising
  probesize does not silence it (tested to 200 KB, and with `-r`/`-framerate`) —
  it only brings the buffering back.
- The server serves one stream and ignores the path in the request URL, so
  `rtsp://pi:8554/anything` reaches the same camera.
- A keyframe is emitted every second (picamera2's default `iperiod=30`), which
  is how quickly a newly connected viewer gets its first decodable picture.
- Only one process can hold the camera. If startup fails with a camera-busy
  error, something else is still running — check with
  `pgrep -af 'python3 (streamer|qr_detector)'`.

## Where to put your own code

`handle_code(code)` in `qr_detector.py` is called once per newly-read payload
and does nothing yet. That is the hook for the manipulator: parse `code.text`
into a command and send it. It is kept separate from scanning and from the
console log so that acting on a read stays one obvious place.

## The sibling project

[`../tcp`](../tcp) is the same thing with the transport swapped: ffmpeg
hosts the socket itself and serves MPEG-TS over plain TCP, so it needs no
server code at all, but the client URL is `tcp://pi:8554` rather than RTSP, and
a viewer joins about 0.2 s behind live. Prefer this project unless you
specifically want ffmpeg to own the socket.
