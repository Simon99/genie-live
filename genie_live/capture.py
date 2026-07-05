from __future__ import annotations

"""Audio and screen capture for live meeting monitoring.

Audio capture: Uses system audio loopback (BlackHole/OBS-style virtual device)
or microphone as fallback. Records in chunks for streaming transcription.

Screen capture: Periodic screenshots of the meeting window.
"""

import subprocess
import tempfile
import time
import threading
from pathlib import Path


class AudioCapture:
    """Capture system audio in rolling chunks for live transcription."""

    def __init__(self, device: str = "default", chunk_seconds: int = 10, output_dir: str = None):
        self.device = device
        self.chunk_seconds = chunk_seconds
        self.output_dir = Path(output_dir or tempfile.mkdtemp())
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._running = False
        self._chunk_index = 0
        self._thread = None
        self._callbacks = []

    def on_chunk(self, callback):
        """Register callback(chunk_path, chunk_index) for each recorded chunk."""
        self._callbacks.append(callback)

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=self.chunk_seconds + 5)

    def _capture_loop(self):
        while self._running:
            chunk_path = str(self.output_dir / ("chunk_%05d.wav" % self._chunk_index))

            # Use ffmpeg to record from audio device
            cmd = [
                "ffmpeg",
                "-f", "avfoundation",
                "-i", ":%s" % self.device,
                "-t", str(self.chunk_seconds),
                "-acodec", "pcm_s16le",
                "-ar", "16000", "-ac", "1",
                chunk_path, "-y"
            ]

            try:
                subprocess.run(cmd, capture_output=True, timeout=self.chunk_seconds + 10)
                if Path(chunk_path).exists() and Path(chunk_path).stat().st_size > 1000:
                    for cb in self._callbacks:
                        try:
                            cb(chunk_path, self._chunk_index)
                        except Exception:
                            pass
            except (subprocess.TimeoutExpired, Exception):
                pass

            self._chunk_index += 1


class ScreenCapture:
    """Periodic screenshot capture of the screen."""

    def __init__(self, interval: float = 10.0, output_dir: str = None):
        self.interval = interval
        self.output_dir = Path(output_dir or tempfile.mkdtemp())
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._running = False
        self._index = 0
        self._thread = None
        self._callbacks = []

    def on_frame(self, callback):
        """Register callback(frame_path, index, timestamp) for each screenshot."""
        self._callbacks.append(callback)

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=self.interval + 5)

    def _capture_loop(self):
        while self._running:
            frame_path = str(self.output_dir / ("screen_%05d.png" % self._index))

            # macOS screencapture
            try:
                subprocess.run(
                    ["screencapture", "-x", "-C", frame_path],
                    capture_output=True, timeout=10
                )
                if Path(frame_path).exists():
                    for cb in self._callbacks:
                        try:
                            cb(frame_path, self._index, time.time())
                        except Exception:
                            pass
            except Exception:
                pass

            self._index += 1
            time.sleep(self.interval)
