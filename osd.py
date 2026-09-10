"""Burn a highlight box and a readout into the video frames, on the Pi.

Drawn here rather than in the viewer for one reason: the box belongs to one
specific frame. The Pi knows where the code was in the frame it just scanned; a
viewer drawing the same box would land it on whatever frame is on screen a
couple of hundred milliseconds later, so it would lead the code and jitter
against it. Burning it in also means any player shows the overlay, with no
client-side code at all.

Colours here are deliberately channel-order agnostic - green, white, black -
because the main stream is XBGR8888 and only the middle channel is unambiguous.
"""

import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
BOX_COLOUR = (0, 255, 0)
BOX_THICKNESS = 3
BOX_HOLD = 0.35   # a box outlives its scan just long enough not to flicker
MAX_CHARS = 52


class Osd:
    """Overlay state, written by the scan loop and read by the camera thread.

    Each slot holds one immutable tuple that is swapped in a single assignment,
    which is all the synchronisation two threads need here: the drawing side
    either sees the whole previous overlay or the whole new one, never a mix.
    """

    def __init__(self, frame_size, scan_size, hold=5.0, font_size=22,
                 font_path=FONT_PATH):
        self.frame_w, self.frame_h = frame_size
        self.scale_x = self.frame_w / scan_size[0]
        self.scale_y = self.frame_h / scan_size[1]
        self.hold = hold
        self.font = ImageFont.truetype(font_path, font_size)
        self._text_cache = (None, None)   # (key, mask)
        self._box = None                  # (x, y, w, h, until)
        self._readout = None              # (mask, until)

    # ---- called from the scan loop -------------------------------------

    def show(self, code):
        """Register a code that is in frame right now."""
        now = time.monotonic()
        if code.bbox:
            x, y, w, h = code.bbox
            self._box = (int(x * self.scale_x), int(y * self.scale_y),
                         int(w * self.scale_x), int(h * self.scale_y),
                         now + BOX_HOLD)
        text = code.text.replace("\n", " ")
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS - 1] + "…"
        detail = "{}  q={}".format(time.strftime("%H:%M:%S"), code.quality)
        if code.bbox:
            detail += "  {}x{}px".format(code.bbox[2], code.bbox[3])
        self._readout = (self._mask(text, detail), now + self.hold)

    # ---- called from the camera thread, once per frame -----------------

    def draw(self, frame):
        """Draw the current overlay onto one main-stream frame in place."""
        now = time.monotonic()
        height, width = frame.shape[0], min(frame.shape[1], self.frame_w)

        box = self._box
        if box and box[4] > now:
            self._outline(frame, box[:4], height, width)

        readout = self._readout
        if readout and readout[1] > now:
            self._band(frame, readout[0], height, width)

    # ---- internals -----------------------------------------------------

    def _mask(self, text, detail):
        """Render the two readout lines once, as a boolean pixel mask."""
        key = (text, detail)
        if self._text_cache[0] == key:
            return self._text_cache[1]

        pad = 10
        line_gap = 4
        top = self.font.getbbox(text)
        bottom = self.font.getbbox(detail)
        text_w = max(top[2], bottom[2])
        top_h = top[3]
        image = Image.new("L", (text_w + 2 * pad,
                                top_h + bottom[3] + line_gap + 2 * pad), 0)
        draw = ImageDraw.Draw(image)
        draw.text((pad, pad - top[1]), text, fill=255, font=self.font)
        draw.text((pad, pad + top_h + line_gap - bottom[1]), detail,
                  fill=255, font=self.font)
        mask = np.asarray(image) > 96
        self._text_cache = (key, mask)
        return mask

    def _outline(self, frame, box, height, width):
        x, y, w, h = box
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(width, x + w), min(height, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        t = BOX_THICKNESS
        frame[y0:min(y0 + t, y1), x0:x1, :3] = BOX_COLOUR
        frame[max(y1 - t, y0):y1, x0:x1, :3] = BOX_COLOUR
        frame[y0:y1, x0:min(x0 + t, x1), :3] = BOX_COLOUR
        frame[y0:y1, max(x1 - t, x0):x1, :3] = BOX_COLOUR

    def _band(self, frame, mask, height, width):
        mask_h, mask_w = mask.shape
        margin = 16
        x0 = margin
        y0 = height - mask_h - margin
        if y0 < 0 or x0 + mask_w > width:
            # Frame too small for the readout as rendered; skip rather than crop
            # it into something unreadable.
            return
        band = frame[y0:y0 + mask_h, x0:x0 + mask_w]
        band[..., :3] //= 4          # darken, so white text reads over anything
        band[..., :3][mask] = 255
