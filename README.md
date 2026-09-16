# Raspberry Pi camera QR reader

An IMX219 camera on a Raspberry Pi 5. The Pi **reads QR codes itself** and, in
parallel, serves a low-latency video preview so another machine can watch what
the camera sees.

The split matters: the QR payloads are meant to become commands for a
manipulator, so decoding has to happen on the Pi. The video preview only exists
so a developer can confirm that a read succeeded, and it can be switched off
(`--no-stream`) without affecting reading at all.

```
IMX219 -> libcamera / picamera2
            |
            +-- main  1280x720 -> overlay burned in -> H.264 -> the network
            |
            +-- lores  640x480 YUV420 -> zbar -> payload -> handle_code()
                                                        -> overlay state
```

Both streams leave the ISP at the same time, so a read never depends on the
video link, on the network, or on H.264 artefacts. Only the preview does.

## Two variants — pick one

The camera, the encoder and the QR pipeline are identical in both. They differ
only in how the video leaves the Pi, and each is a **self-contained pair of
Python files** you can copy to a Pi and run.

| | [`rtsp/`](rtsp) | [`tcp/`](tcp) |
| --- | --- | --- |
| Client URL | `rtsp://pi:8554/cam` | `tcp://pi:8554` |
| Who hosts the port | a small RTSP server inside `streamer.py` | ffmpeg itself |
| Viewers at once | several | one (reconnects freely) |
| Backlog when a viewer joins | none — first RTP packet 26 ms after PLAY | ~0.2 s |
| Extra code | ~200 lines of RTSP | none |

**Start with [`rtsp/`](rtsp).** It keeps the ordinary `rtsp://` address, takes
several viewers, and joins with no backlog. Choose [`tcp/`](tcp) only if you
specifically want no network-server code in the project and ffmpeg to own the
socket.

Each directory has its own README with the full detail: install, options,
systemd unit, measured numbers, and the behaviour worth knowing about.

## Quick start

```bash
git clone https://github.com/OLhaMels/raspberry-pi-camera-stream.git
cd raspberry-pi-camera-stream/rtsp
python3 qr_detector.py
```

It prints the address to point a player at, and every QR code it reads:

```
encoder: slice threading, 4 thread(s)
Serving 1280x720 at rtsp://10.1.1.30:8554/cam
  ffplay -rtsp_transport tcp -fflags nobuffer -flags low_delay rtsp://10.1.1.30:8554/cam
Scanning 640x480, OSD on - Ctrl+C to stop

[23:48:27] QR-Code q=1  at 213,49 305x406px
           -> http://takesoftwarekobo.blog.fc2.com/
```

On the viewing machine you need a player — `winget install Gyan.FFmpeg` on
Windows, `sudo apt install ffmpeg` on Debian, `brew install ffmpeg` on macOS.
VLC works too. See either README for the details.

## Layout

| Path | What it is |
| --- | --- |
| [`rtsp/`](rtsp) | The RTSP variant: `streamer.py`, `qr_detector.py`, and its README. |
| [`tcp/`](tcp) | The MPEG-TS-over-TCP variant, same two files. |
| `tests/` | `qr_test.png` (a bare QR code) and `qr_scene.png` (the same code in a 720p frame), for `--self-test`. |

### Why the two directories duplicate `qr_detector.py`

They are near-identical — the difference is the transport argument and the
default URL. Factoring the common part into a shared module was considered and
rejected: each variant is meant to be two self-contained files that can be
copied to a Pi and run, with no import path to get right and nothing else to
bring along. The duplication is the price of that, and it is deliberate.

## Requirements

**On the Pi: nothing to install.** This runs on what Raspberry Pi OS already
ships — `python3-picamera2`, `python3-numpy`, `python3-av`, `python3-pil`,
`ffmpeg`, and `libzbar0` (bound by hand through ctypes, because Debian trixie
has no `python3-pyzbar`).

Verified on a Raspberry Pi 5 with an IMX219: Debian trixie, kernel 6.18,
ffmpeg 7.1.5, libcamera v0.7.1, zbar 0.23.93. Watched back with ffmpeg 9.0.1 on
Windows 11 and with VLC.

## History

Earlier versions published the stream to **MediaMTX**, a separate server
process that had to be installed and started first. That is gone: neither
variant here needs any process beyond the two Python files and the ffmpeg they
start.

The last MediaMTX-based version is tagged **`v1-mediamtx`** if you need it:

```bash
git checkout v1-mediamtx
```

Note that ffmpeg *cannot* host an RTSP server itself — `-rtsp_flags listen` is
a demuxer-only option and is silently ignored on an output, so `-f rtsp
rtsp://0.0.0.0:8554/cam` tries to connect out and fails. Each variant's README
explains what it does instead.

## Where to put your own code

`handle_code(code)` in `qr_detector.py` is called once per newly-read payload
and does nothing yet. That is the hook for the manipulator: parse `code.text`
into a command and send it.
