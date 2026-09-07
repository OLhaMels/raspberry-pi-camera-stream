import time

from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

picam2 = Picamera2()

video_config = picam2.create_video_configuration(
    main={"size": (1280, 720)}
)

picam2.configure(video_config)

encoder = H264Encoder(bitrate=4_000_000)

output = FfmpegOutput(
    "-f rtsp rtsp://localhost:8554/cam"
)

print("Stream started. Open on VLC: rtsp://<IP_Pi>:8554/cam")
print("Press Ctrl+C to stop.")

picam2.start_recording(encoder, output)

try:
    while True:
        time.sleep(1)

except KeyboardInterrupt:
    pass

finally:
    picam2.stop_recording()
    print("Stream stopped")
