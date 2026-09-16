# Pi camera QR reader — MPEG-TS over TCP, ffmpeg hosts the socket

An IMX219 camera on a Raspberry Pi 5. The Pi **reads QR codes itself** and, in
parallel, serves a video preview so another machine can watch what the camera
sees.

The split matters: the QR payloads are meant to become commands for a
manipulator, so decoding has to happen on the Pi. The video preview only exists
so a developer can confirm that a read succeeded, and it can be switched off
(`--no-stream`) without affecting reading at all.

Two Python files, nothing to install, and **no MediaMTX** — no media server
process at all. ffmpeg itself owns the listening socket, so there is no network
server code in this project whatsoever.

## How it works

```
IMX219 -> libcamera / picamera2
            |
            +-- main  1280x720 -> overlay burned in -> H.264 (libx264, slice-threaded)
            |                        |                     -> ffmpeg -f mpegts
            |                        |                        listening on 0.0.0.0:8554
            |                        |                              |
            |                        |                              v
            |                        |                         ffplay / VLC
            |                        |
            +-- lores  640x480 YUV420 -> zbar -> payload -> handle_code()
                                                        -> overlay state
```

Both streams leave the ISP at the same time, so a read never depends on the
video link, on the network, or on H.264 artefacts. Only the preview does.

### Why not RTSP

The obvious design is to let ffmpeg host an RTSP port with `-rtsp_flags listen`.
**It cannot.** In ffmpeg 7.1 `rtsp_flags` is a *demuxer* option — `ffmpeg -h
muxer=rtsp` does not list it — so on an output it is silently ignored and ffmpeg
tries to *connect out* instead:

```
$ ffmpeg ... -rtsp_flags listen -f rtsp rtsp://0.0.0.0:8554/cam
[tcp @ ...] Connection to tcp://0.0.0.0:8554?timeout=0 failed: Connection refused
[out#0/rtsp @ ...] Could not write header (incorrect codec parameters ?): Connection refused
```

`-listen 1` and `?listen=1` fail the same way; the port is never bound.
libavformat has no RTSP server side at all — no DESCRIBE, no SETUP, no PLAY.

A TCP listener carrying MPEG-TS is the closest thing ffmpeg *can* host on its
own, and it is the leaner of the two: no RTP packetisation, no session setup,
first byte straight down the socket. The cost is that the client URL is no
longer `rtsp://`. If you need real RTSP, use the sibling project (see the end of
this file).

## Files

| File | What it is |
| --- | --- |
| `streamer.py` | Camera, low-latency encoder, and the ffmpeg that serves the socket. Knows nothing about QR codes. |
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
cd ~/raspberry-pi-camera-stream/tcp
python3 qr_detector.py
```

That is the whole thing. It prints the command to paste on the viewing machine:

```
encoder: slice threading, 4 thread(s)
Serving 1280x720 on tcp://0.0.0.0:8554?listen=1
  ffplay -fflags nobuffer -flags low_delay -framedrop tcp://10.1.1.30:8554
Scanning 640x480, OSD on - Ctrl+C to stop
```

Every new QR code is reported on stdout as it is read:

```
[22:41:07] QR-Code q=1  at 291,118 262x262px
           -> https://example.com/pi-cam-test-42
```

Ctrl+C stops it and releases the camera and the socket.

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

Then point it at the Pi. Note the address is `tcp://`, not `rtsp://`, and there
is no `-rtsp_transport` flag — that is the one practical difference from the
sibling project:

```bash
ffplay -fflags nobuffer -flags low_delay -framedrop tcp://10.1.1.30:8554
```

VLC works too and needs no terminal:

```powershell
winget install VideoLAN.VLC
```

Then **Media → Open Network Stream** and paste the URL. Set **Tools →
Preferences → Input/Codecs → Network caching** to 0 first, or VLC adds a second
of its own buffering on top of everything this project does to avoid exactly
that.

**One viewer at a time.** ffmpeg serves a single connection and exits when it
ends; `streamer.py` notices and re-arms the listening socket for the next
viewer, so you can reconnect freely, but two players cannot watch at once — the
second one gets

```
[tcp @ ...] Connection to tcp://10.1.1.30:8554 failed: Connection refused
```

while the first is still attached. If you need several viewers, use the sibling
RTSP project. Pass `--no-reconnect` if you want the stream to stop for good
after the first viewer leaves.

### Can the two files run in separate terminals?

No. `qr_detector.py` **already runs both** — it starts the camera, the encoder
and the ffmpeg that serves the socket through `Streamer`, and scans in a thread
alongside them. One process, one command.

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
| `--url URL` | `tcp://0.0.0.0:8554?listen=1` | Where ffmpeg listens. |
| `--size WxH` | `1280x720` | Size of the video stream. |
| `--scan-size WxH` | `640x480` | Size of the lores stream used for decoding. |
| `--bitrate N` | `4000000` | H.264 bitrate. |
| `--threads N` | `4` | Encoder threads. Slice-threaded, so more threads add no delay. |
| `--no-reconnect` | off | Serve one viewer, then stop listening. |
| `--scan-interval S` | `0.0` | Seconds between scans. 0 means as fast as lores frames arrive. |
| `--repeat-after S` | `5.0` | How long a code must be out of frame before it is reported again. |
| `--heartbeat S` | `60.0` | Seconds between liveness lines. 0 turns them off. |
| `--osd-hold S` | `5.0` | How long the readout stays on screen after a read. |
| `--no-osd` | off | Do not burn the outline and readout into the video. |
| `--no-stream` | off | Read QR codes only; serve no video. |
| `--log PATH` | — | Append every new read to this file. |
| `--self-test IMAGE` | — | Decode a still image and exit. No camera needed. |

`streamer.py` on its own takes `--url`, `--size`, `--bitrate`, `--threads` and
`--no-reconnect`, with the same meanings.

### Starting it at boot

```ini
# /etc/systemd/system/qr-detector.service
[Unit]
Description=Pi camera QR reader (TCP/MPEG-TS)
After=network-online.target

[Service]
User=cam_user
WorkingDirectory=/home/cam_user/raspberry-pi-camera-stream/tcp
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
- **A shallow output queue that drops rather than blocks.** See below — this one
  is load-bearing, not an optimisation.
- **A single-slot mailbox** (`queue.Queue(maxsize=1)`) between the scan thread
  and the overlay. The drawing side only ever wants the newest detection, never
  a backlog.

Measured on this Pi:

| | |
| --- | --- |
| Bytes in the first 100 ms after connecting | 105 KB, against 51 KB/100 ms steady — roughly 0.2 s of backlog |
| QR scan rate | 24–28 scans/s (the target was 15–20 Hz) |
| QR decoding, end to end | confirmed on hardware — real codes read off the lores stream while streaming |
| Clean shutdown, Ctrl+C or SIGTERM | ~1 s, no orphaned ffmpeg, port released |

Glass-to-glass latency — photons to pixels on the viewer — was **not** measured;
it depends on your player's buffering and your network as much as on this code.

## Notes and known behaviour

- **Hold the code fully inside the frame.** zbar will still decode one that is
  clipped by an edge — it extrapolates the polygon, and you can spot this in the
  reported position, where a corner goes negative (`at 229,-22`) — but those
  reads come back at `q=1`, zbar's lowest confidence, and they are the ones that
  fail intermittently. A code wholly in frame reads far more reliably. The
  overlay handles off-frame corners safely either way: coordinates are clamped
  before anything is drawn.
- **Why `BufferedFfmpegOutput` exists.** picamera2 runs the encode
  *synchronously*, on the camera's own request-handling thread, and hands the
  packet straight to the output. The stock `FfmpegOutput` writes it to ffmpeg's
  stdin and blocks when that pipe is full — and with `?listen=1` ffmpeg is
  parked in `accept()` reading nothing until a viewer connects, so the pipe
  fills within about a tenth of a second. Unmodified, that stalls the camera
  thread and with it the QR detector: the Pi would stop reading codes whenever
  nobody was watching. Here the camera thread only appends to a five-frame
  deque and returns; a writer thread does the blocking write and drops frames,
  resuming at the next keyframe so the viewer resyncs cleanly.
- ffmpeg logs `not enough frames to estimate rate; consider increasing
  probesize` once per start. Expected and harmless: it is a consequence of
  `-analyzeduration 0`, and nothing downstream needs that estimate because
  `-use_wallclock_as_timestamps` gives every packet a real timestamp. Raising
  probesize does not silence it (tested to 200 KB, and with `-r`/`-framerate`) —
  it only brings the buffering back.
- `Broken pipe` / `Error writing trailer` lines in the log when a viewer quits
  are normal: that is ffmpeg noticing the socket closed. The next line,
  `viewer gone ...; listening again`, is the socket being re-armed.
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

[`../rtsp`](../rtsp) is the same thing with a real RTSP server written into
`streamer.py`. It keeps the `rtsp://pi:8554/cam` URL, takes several viewers at
once, and joins with no backlog at all (first RTP packet 26 ms after PLAY),
at the cost of about 200 lines of protocol code. Prefer it unless you
specifically want ffmpeg to own the socket and no server code in the project.
