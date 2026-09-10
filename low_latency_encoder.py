"""H.264 encoder for picamera2 with the frame-threading delay taken out.

picamera2's LibavH264Encoder pins libav to frame-level threading, so libx264
holds thread_count frames before it emits the first packet. `tune=zerolatency`,
which picamera2 does set, cannot undo it: FFmpeg's libx264 wrapper decides
sliced-threads from the codec context thread_type, which overrides what the tune
asked for. Measured on this Pi 5 at 1280x720:

    FRAME threading, threads=0 (auto -> 6)   first packet after 6 frames   167 ms
    FRAME threading, threads=4               first packet after 4 frames   100 ms
    SLICE threading, threads=4               first packet after 1 frame      0 ms
    single thread                            first packet after 1 frame      0 ms

Slice threading is the one to have: no queue, and still ~93 fps of encode
throughput at 720p, which is three times what the 30 fps stream needs.
"""

from av.codec.context import ThreadType
from picamera2.encoders import H264Encoder


class LowLatencyH264Encoder(H264Encoder):
    """H264Encoder that slice-threads instead of frame-threads."""

    def __init__(self, *args, threads=4, **kwargs):
        super().__init__(*args, **kwargs)
        self.threads = threads

    def _start(self):
        super()._start()
        # Safe to change here: _start only configures the stream, and libx264
        # reads these when it opens on the first frame, which cannot arrive
        # until start_recording has returned.
        self._stream.codec_context.thread_type = ThreadType.SLICE
        self._stream.codec_context.thread_count = self.threads
        # Read back rather than assume: this is the whole point of the subclass.
        context = self._stream.codec_context
        print("encoder: {} threading, {} thread(s)".format(
            context.thread_type.name.lower(), context.thread_count), flush=True)
