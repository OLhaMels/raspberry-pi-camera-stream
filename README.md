# Raspberry Pi camera QR reader

An IMX219 camera on a Raspberry Pi 5. The Pi **reads QR codes itself** and, in
parallel, publishes a video preview over RTSP so another machine can watch what
the camera sees.

The split matters: the QR payloads are meant to become commands for a
manipulator, so decoding has to happen on the Pi. The video preview only exists
so a developer can confirm that a read succeeded, and it can be switched off
(`--no-stream`) without affecting reading at all.

## How it works

```
IMX219 -> libcamera / picamera2
            |
            +-- main  1280x720 -> H.264 (libx264, slice-threaded)
            |                       -> ffmpeg -> MediaMTX -> RTSP -> viewer
            |
            +-- lores  640x480 YUV420 -> zbar -> payload -> handle_code()
```

Both streams leave the ISP at the same time, so a read never depends on the
video link, on the network, or on H.264 artefacts. Only the preview does.

## Files

| File | What it is |
| --- | --- |
| `qr_stream.py` | The program. Camera setup, streaming, scan loop, reporting. |
| `zbar_lite.py` | ctypes binding to libzbar — scan a grayscale frame, get payloads back. |
| `low_latency_encoder.py` | H.264 encoder subclass that removes picamera2's frame-threading delay. |
| `mediamtx.yml` | MediaMTX config. Unmodified upstream default for v1.21.0. |
| `mediamtx` | The RTSP server binary. **Not in git** (63 MB) — see below. |
| `tests/qr_test.png` | A bare QR code, payload `https://example.com/pi-cam-test-42`. |
| `tests/qr_scene.png` | The same code inside a 720p frame, i.e. roughly what the camera sees. |
| `udp_cam_stream.py` | The original streaming-only script. Superseded by `qr_stream.py`, kept for reference. |

## Requirements

Nothing to install. This runs on what Raspberry Pi OS already ships:

- `python3-picamera2`, `python3-numpy`, `python3-av`, `ffmpeg`
- `libzbar0t64` — the zbar library, already present; called directly via ctypes
- the `mediamtx` binary, only for the preview

If `mediamtx` is missing (it is not tracked in git), fetch the arm64 build:

```sh
wget https://github.com/bluenviron/mediamtx/releases/download/v1.21.0/mediamtx_v1.21.0_linux_arm64.tar.gz
tar -xzf mediamtx_v1.21.0_linux_arm64.tar.gz mediamtx
rm mediamtx_v1.21.0_linux_arm64.tar.gz
```

## Running

Two processes, from this directory:

```sh
./mediamtx mediamtx.yml                    # terminal 1: RTSP server
python3 qr_stream.py --log qr_hits.log     # terminal 2: camera, stream + scan
```

Every read is printed and appended to the log:

```
[19:12:44] QR-Code q=1  at 268,141 96x96px
           -> https://example.com/pi-cam-test-42
```

A code is reported once when it appears, and again only after it has been out of
frame for `--repeat-after` seconds (default 5), so holding one in view does not
spam. `--heartbeat` prints a liveness line with the scan rate every 60 s.

Reading without publishing video:

```sh
python3 qr_stream.py --no-stream
```

**Only one process can hold the camera.** Stop this program before running
`rpicam-*`, `udp_cam_stream.py`, or anything else that opens the sensor.

### Watching the preview from another machine

The flags matter more than the player. FFmpeg's H.264 decoder frame-threads by
default, which buffers one frame per thread — on a 6-core laptop that measured
200 ms of pure latency. `-flags low_delay` or `-thread_type slice` removes it:

```sh
ffplay -rtsp_transport tcp -fflags nobuffer -flags low_delay \
       -thread_type slice -threads 1 -framedrop \
       -probesize 32 -analyzeduration 0 rtsp://<PI_IP>:8554/cam
```

GStreamer usually goes lower still, since it has no display queue of its own:

```sh
gst-launch-1.0 rtspsrc location=rtsp://<PI_IP>:8554/cam protocols=tcp \
  latency=0 drop-on-latency=true ! rtph264depay ! h264parse ! \
  avdec_h264 thread-type=2 max-threads=1 ! videoconvert ! autovideosink sync=false
```

Prefer TCP. Over UDP this stream loses RTP packets — MediaMTX has to remux the
publisher's oversized packets (1460 > 1440 bytes) — and the loss arrives as
corrupted macroblocks.

## Checking the decoder without a camera

```sh
python3 qr_stream.py --self-test tests/qr_scene.png
# OK: QR-Code -> https://example.com/pi-cam-test-42 (quality 1, bbox (536, 256, 209, 209))
```

This exercises the real decode path, so it is the fastest way to tell a decoder
problem apart from a camera or focus problem.

## Where to add manipulator commands

`handle_code(code)` in `qr_stream.py`. The scan loop's job is to notice a code;
this function's job is to act on it. It is called once per newly seen code:

```python
def handle_code(code):
    print(code.text)      # payload as text
    print(code.raw)       # payload as bytes, if commands are ever binary
    print(code.quality)   # zbar's confidence
    print(code.bbox)      # (x, y, w, h) in the 640x480 scan frame
```

Validate before acting: a camera will happily read a QR code from a poster on
the wall behind the workbench.

## Options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--rtsp` | `rtsp://localhost:8554/cam` | Where to publish video. |
| `--no-stream` | off | Read codes only, publish nothing. |
| `--size` | `1280x720` | Video stream size. |
| `--scan-size` | `640x480` | Size of the stream used for decoding. |
| `--bitrate` | `4000000` | H.264 bitrate. |
| `--threads` | `4` | Encoder threads (slice-threaded, so they add no delay). |
| `--interval` | `0.05` | Seconds between scans. `0` scans as fast as frames arrive. |
| `--repeat-after` | `5.0` | Seconds a code must be gone before it is reported again. |
| `--heartbeat` | `60.0` | Seconds between liveness lines. `0` disables. |
| `--log` | none | File to append reads to. |
| `--self-test` | none | Decode a still image and exit. |

Small or distant codes may need `--scan-size 1280x720`. It costs CPU but reads
much smaller codes.

## Design notes

Three decisions here look unusual and are deliberate.

### zbar is called through ctypes, not through a wrapper

`python3-pyzbar` does not exist in Debian trixie, and this Pi cannot reach a
package mirror (see below), so no wrapper could be installed. But `libzbar0t64`
— the zbar library itself — is already on the system, and zbar is the best QR
decoder available. `zbar_lite.py` binds the dozen calls that are actually needed.
Note the package name: searching for `libzbar0` finds nothing and wrongly
suggests zbar is unavailable.

OpenCV was the other candidate. Its libraries are on the Pi too (pulled in by
`rpicam-apps-opencv-postprocess`), but without the Python bindings, and
`python3-opencv` wants several hundred MB of dependencies including VTK and
OpenMPI.

### The encoder is slice-threaded

The Pi 5 has no hardware H.264 encoder, so picamera2 encodes with libx264 —
and pins libav to frame-level threading, which makes x264 hold one frame per
thread before it emits anything. `tune=zerolatency`, which picamera2 does set,
cannot undo it: FFmpeg's libx264 wrapper derives sliced-threads from the codec
context's thread type. Measured at 1280x720 on this Pi:

| Threading | First packet after | Latency added | Encode throughput |
| --- | --- | --- | --- |
| FRAME, auto (6 threads) — picamera2's default | 6 frames | 167 ms | 109 fps |
| FRAME, 4 threads | 4 frames | 100 ms | 105 fps |
| **SLICE, 4 threads** — what we use | **1 frame** | **0 ms** | 93 fps |
| single thread | 1 frame | 0 ms | 70 fps |

`low_latency_encoder.py` flips the thread type after picamera2 has configured
the stream and prints what actually took effect, so a regression is visible in
the log rather than silent.

### Video is published over TCP

`FfmpegOutput` publishes to MediaMTX over loopback. Left on the default UDP,
MediaMTX logged repeated `RTP packets lost` for the publisher and viewers saw
corrupted macroblocks. Over TCP the same session loses nothing. `-muxdelay 0
-muxpreload 0` stops ffmpeg holding packets back on top of that.

## Known gotchas

**The Pi has no working default route out of the box.** Two default routes exist
and the wrong one wins: `eth0 via 10.1.1.1` (metric 100, a direct link to a
laptop with no gateway on it) beats the real gateway on wifi (metric 600). Every
outbound connection black-holes, which is why apt and git push fail while
`curl --interface wlan0` works. Clear it at runtime:

```sh
sudo ip route del default via 10.1.1.1 dev eth0
```

That does not survive a reboot. The route comes from the `netplan-eth0` profile,
so the permanent fix is to stop netplan handing `eth0` a gateway
(`dhcp4-overrides: use-routes: false`, or drop the gateway/routes entry) in
`/etc/netplan/*.yaml`. A side effect worth knowing: with no route out, NTP
cannot sync either, so timestamps in older logs are wrong — this Pi's clock was
three days behind until the route was cleared.

**Pushing to GitHub needs credentials that are not stored anywhere.** No
credential helper, no `~/.git-credentials`, no SSH key. `git push` will prompt
for a username and a personal access token.

**`git status` will not show `mediamtx`.** The 63 MB binary is in `.gitignore`
on purpose, together with `*.tar.gz`, `*.h264`, `*.log` and `__pycache__`.

**MediaMTX writes `auto.crt` and `auto.key` into its working directory.** They
are a self-signed certificate it generates for its MoQ server (`moqServerKey:
auto.key` in the config), not anyone's credentials, and they come back on the
next start. Both are gitignored. Setting `moq: no` in `mediamtx.yml` stops them
being created at all.
