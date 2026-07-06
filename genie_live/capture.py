from __future__ import annotations

"""Audio and screen capture for live meeting monitoring.

Audio capture: a single long-running ffmpeg process records from an
avfoundation device and segments the stream into fixed-length wav chunks
(``-f segment``), so there is no gap between chunks. A watcher loop emits
each segment once it is complete (a higher-numbered segment file exists,
or ffmpeg has exited).

Screen capture: periodic screenshots via macOS ``screencapture``, with a
bounded number of frames kept on disk.
"""

import logging
import subprocess
import tempfile
import time
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioCapture:
    """Capture system/mic audio into gapless rolling chunks.

    ``device`` is the avfoundation audio device index or name. Note that
    avfoundation has no ``:default`` — the default here is ``"0"`` (first
    audio device), and the input spec becomes ``":0"``. List devices with
    ``ffmpeg -f avfoundation -list_devices true -i ""``.
    """

    def __init__(
        self,
        device: str = "0",
        chunk_seconds: int = 10,
        output_dir: str = None,
        max_retries: int = 3,
    ):
        self.device = device
        self.chunk_seconds = chunk_seconds
        self.output_dir = Path(output_dir or tempfile.mkdtemp(prefix="genie_audio_"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_retries = max_retries

        # Set when capture has permanently failed (visible via server state).
        self.error = None

        self._proc = None
        self._thread = None
        self._stop_event = threading.Event()
        self._callbacks = []
        self._next_emit = 0          # first segment index not yet emitted
        self._next_start_number = 0  # segment numbering across ffmpeg restarts

    def on_chunk(self, callback):
        """Register callback(chunk_path, chunk_index) for each completed chunk."""
        self._callbacks.append(callback)

    def is_running(self) -> bool:
        return bool(
            self._thread and self._thread.is_alive() and not self._stop_event.is_set()
        )

    def start(self):
        if self.is_running():
            logger.warning("AudioCapture.start() called while already running")
            return
        self.error = None
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="audio-capture")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                logger.exception("failed to terminate ffmpeg")
        if self._thread:
            self._thread.join(timeout=self.chunk_seconds + 10)
            if self._thread.is_alive():
                logger.error("audio capture thread did not stop in time")

    # ------------------------------------------------------------------ #

    def _input_spec(self) -> str:
        # avfoundation "video:audio"; audio-only is ":<device>".
        return self.device if ":" in self.device else ":" + self.device

    def _run(self):
        retries = 0
        while not self._stop_event.is_set():
            started = time.monotonic()
            returncode, stderr_text = self._run_ffmpeg_once()
            # Emit any final (now complete) segment.
            self._scan_segments(final=True)

            if self._stop_event.is_set():
                logger.info("audio capture stopped")
                return

            elapsed = time.monotonic() - started
            logger.error(
                "ffmpeg exited unexpectedly after %.1fs (rc=%s): %s",
                elapsed, returncode, stderr_text[-2000:])

            if elapsed > self.chunk_seconds * 2:
                # It was recording fine for a while; treat as a fresh failure.
                retries = 0
            retries += 1
            if retries > self.max_retries:
                self.error = (
                    "audio capture failed after %d retries (device %r): %s"
                    % (self.max_retries, self.device, stderr_text[-500:])
                )
                logger.error(self.error)
                self._stop_event.set()
                return

            logger.warning(
                "retrying audio capture in %ds (attempt %d/%d)",
                self.chunk_seconds, retries, self.max_retries)
            self._stop_event.wait(self.chunk_seconds)

    def _run_ffmpeg_once(self):
        """Run one long-lived ffmpeg segmenter; returns (returncode, stderr)."""
        pattern = str(self.output_dir / "chunk_%05d.wav")
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation",
            "-i", self._input_spec(),
            "-acodec", "pcm_s16le",
            "-ar", "16000", "-ac", "1",
            "-f", "segment",
            "-segment_time", str(self.chunk_seconds),
            "-reset_timestamps", "1",
            "-segment_start_number", str(self._next_start_number),
            pattern, "-y",
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except OSError as e:
            logger.exception("failed to launch ffmpeg")
            return None, str(e)
        self._proc = proc

        try:
            while proc.poll() is None and not self._stop_event.is_set():
                self._scan_segments(final=False)
                self._stop_event.wait(0.5)

            if proc.poll() is None:
                # stop() requested; terminate and let the last segment flush.
                try:
                    proc.terminate()
                except OSError:
                    logger.exception("failed to terminate ffmpeg")
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.error("ffmpeg did not exit after terminate; killing")
                proc.kill()
                proc.wait(timeout=5)
        finally:
            self._proc = None

        stderr_text = ""
        try:
            if proc.stderr:
                stderr_text = proc.stderr.read().decode("utf-8", errors="replace")
        except Exception:
            logger.exception("failed to read ffmpeg stderr")
        return proc.returncode, stderr_text

    def _scan_segments(self, final: bool):
        """Emit completed segments in index order.

        A segment is complete when a higher-numbered segment file exists,
        or when ffmpeg has exited (``final=True``).
        """
        files = {}
        for p in self.output_dir.glob("chunk_*.wav"):
            try:
                idx = int(p.stem.split("_")[1])
            except (IndexError, ValueError):
                continue
            files[idx] = p
        if not files:
            return

        max_idx = max(files)
        self._next_start_number = max(self._next_start_number, max_idx + 1)

        for idx in sorted(files):
            if idx < self._next_emit:
                continue
            if idx >= max_idx and not final:
                break  # still being written
            path = files[idx]
            try:
                size = path.stat().st_size
            except OSError:
                logger.exception("cannot stat segment %s", path)
                size = 0
            if size > 1000:
                for cb in self._callbacks:
                    try:
                        cb(str(path), idx)
                    except Exception:
                        logger.exception("chunk callback failed for %s", path)
            else:
                logger.info("skipping near-empty segment %s (%d bytes)", path, size)
            self._next_emit = idx + 1


class ScreenCapture:
    """Periodic screenshot capture with bounded on-disk retention."""

    def __init__(self, interval: float = 10.0, output_dir: str = None,
                 max_frames: int = 60):
        self.interval = interval
        self.output_dir = Path(output_dir or tempfile.mkdtemp(prefix="genie_screen_"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_frames = max_frames
        self._running = False
        self._index = 0
        self._thread = None
        self._callbacks = []
        self._frame_paths = []  # oldest first

    def on_frame(self, callback):
        """Register callback(frame_path, index, timestamp) for each screenshot."""
        self._callbacks.append(callback)

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="screen-capture")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=self.interval + 5)

    def _capture_loop(self):
        while self._running:
            frame_path = str(self.output_dir / ("screen_%05d.png" % self._index))

            try:
                result = subprocess.run(
                    ["screencapture", "-x", "-C", frame_path],
                    capture_output=True, timeout=10,
                )
                if result.returncode != 0:
                    logger.error(
                        "screencapture failed (rc=%d): %s",
                        result.returncode,
                        result.stderr.decode("utf-8", errors="replace")[-500:])
                elif Path(frame_path).exists():
                    self._frame_paths.append(frame_path)
                    self._prune_frames()
                    for cb in self._callbacks:
                        try:
                            cb(frame_path, self._index, time.time())
                        except Exception:
                            logger.exception("frame callback failed for %s", frame_path)
            except subprocess.TimeoutExpired:
                logger.exception("screencapture timed out")
            except Exception:
                logger.exception("screen capture iteration failed")

            self._index += 1
            time.sleep(self.interval)

    def _prune_frames(self):
        """Keep at most max_frames screenshots on disk; delete the oldest."""
        while len(self._frame_paths) > self.max_frames:
            old = self._frame_paths.pop(0)
            try:
                Path(old).unlink(missing_ok=True)
            except OSError:
                logger.exception("failed to delete old frame %s", old)
