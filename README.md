# Raspberry Pi Camera Streaming

Streaming video from an IMX219 camera connected to Raspberry Pi 5.

## Technologies

- Raspberry Pi 5
- IMX219 camera
- libcamera
- Picamera2
- H.264
- FFmpeg
- MediaMTX
- RTSP

## Architecture

IMX219 → libcamera/Picamera2 → H.264 → FFmpeg → MediaMTX → RTSP → VLC

## Run

Start MediaMTX:

./mediamtx mediamtx.yml
In another terminal:

python3 udp_cam_stream.py

Open the stream in VLC:

rtsp://<RASPBERRY_PI_IP>:8554/cam
