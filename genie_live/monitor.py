from __future__ import annotations

"""Live meeting monitor — dual-window transcription + rolling analysis."""

import json
import logging
import queue
import subprocess
import tempfile
import time
import threading
from pathlib import Path

from genie_core.audio import transcribe_audio
from genie_core.llm import LMStudioClient

logger = logging.getLogger(__name__)


class MeetingMonitor:
    """Real-time meeting monitoring with dual-window transcription.

    Two transcription passes over the same incoming audio chunks:

    - fast pass: each chunk (whatever ``chunk_seconds`` the capture layer
      uses, typically 10s) is transcribed immediately with a small model
      (``fast_model``, default "small") for low-latency rough display.
    - slow pass: once ``slow_window`` seconds of chunks have accumulated,
      they are concatenated and re-transcribed with a larger model
      (``slow_model``, default "medium"); the refined result replaces the
      fast segments it covers.

    All whisper work is serialized on a single worker thread — the MLX
    whisper backend must never be called concurrently. LLM analysis tasks
    also run on the worker so callers (HTTP handlers) never block.
    """

    def __init__(
        self,
        lm_studio_url: str = "http://localhost:1234/v1",
        text_model: str = None,
        questions: list = None,
        fast_model: str = "small",
        slow_model: str = "medium",
        slow_window: int = 30,
    ):
        self.llm = LMStudioClient(base_url=lm_studio_url, model=text_model)
        self.questions = list(questions or [])
        self.fast_model = fast_model
        self.slow_model = slow_model
        self.slow_window = slow_window

        # Dual transcript storage
        # _fast_segments: rough, immediately available; entries are deleted
        #   once a refined segment covers their time range.
        # _slow_segments: refined, authoritative once present.
        self._fast_segments = []  # {"start","end","text","quality":"fast"}
        self._slow_segments = []  # {"start","end","text","quality":"refined"}
        self._merged_segments = []  # final merged view

        self.screen_frames = []
        self.analysis_history = []
        self._lock = threading.Lock()
        self._update_callbacks = []

        # Slow window accumulation ({"path","index","offset","chunk_seconds"})
        self._slow_buffer = []

        # Incremented by reset(); worker drops tasks from older generations.
        self._generation = 0

        # Single worker serializes all whisper + LLM work.
        self._queue = queue.Queue()
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="monitor-worker")
        self._worker.start()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def on_update(self, callback):
        self._update_callbacks.append(callback)

    def reset(self):
        """Clear all transcript segments, buffers and analysis.

        Pending queued tasks from before the reset are discarded by the
        worker (generation check), so a fresh session starts clean.
        """
        with self._lock:
            self._generation += 1
            self._fast_segments = []
            self._slow_segments = []
            self._merged_segments = []
            self._slow_buffer = []
            self.analysis_history = []
            self.screen_frames = []

    def add_audio_chunk(self, chunk_path: str, chunk_index: int, chunk_seconds: int):
        """Queue a new audio chunk for dual-window processing.

        Non-blocking: enqueues a fast-pass task immediately and, when
        ``slow_window`` seconds have accumulated, a slow-pass task.
        """
        offset = chunk_index * chunk_seconds

        with self._lock:
            gen = self._generation
            self._slow_buffer.append({
                "path": chunk_path,
                "index": chunk_index,
                "offset": offset,
                "chunk_seconds": chunk_seconds,
            })
            accumulated = sum(c["chunk_seconds"] for c in self._slow_buffer)
            batch = None
            if accumulated >= self.slow_window:
                batch = list(self._slow_buffer)
                self._slow_buffer = []

        self._queue.put(
            ("fast", {"path": chunk_path, "offset": offset}, time.monotonic(), gen))
        if batch:
            self._queue.put(("slow", batch, time.monotonic(), gen))

    def add_screen_frame(self, frame_path: str, index: int, timestamp: float):
        with self._lock:
            self.screen_frames.append({
                "path": frame_path,
                "index": index,
                "timestamp": timestamp,
            })
            # Keep in sync with ScreenCapture's on-disk retention.
            if len(self.screen_frames) > 120:
                self.screen_frames = self.screen_frames[-120:]

    def set_questions(self, questions: list):
        """Update the question checklist. Analysis runs asynchronously."""
        with self._lock:
            self.questions = list(questions or [])
            gen = self._generation
        self._queue.put(("analyze", None, time.monotonic(), gen))

    def get_state(self) -> dict:
        with self._lock:
            recent = self._merged_segments[-15:] if self._merged_segments else []
            display = [{
                "start": s["start"],
                "end": s["end"],
                "text": s["text"],
                "quality": s.get("quality", "fast"),
            } for s in recent]

            return {
                "transcript_count": len(self._merged_segments),
                "fast_count": len(self._fast_segments),
                "refined_count": len(self._slow_segments),
                "frame_count": len(self.screen_frames),
                "recent_transcript": display,
                "latest_analysis": self.analysis_history[-1] if self.analysis_history else None,
                "questions": self._get_question_status(),
            }

    # ------------------------------------------------------------------ #
    # Worker
    # ------------------------------------------------------------------ #

    def _worker_loop(self):
        while True:
            kind, payload, enqueued_at, gen = self._queue.get()
            try:
                with self._lock:
                    if gen != self._generation:
                        logger.info("dropping stale %s task (reset happened)", kind)
                        continue
                if kind == "fast":
                    # If the fast pass is backlogged past the slow window,
                    # the refined pass will cover this chunk anyway — drop it.
                    age = time.monotonic() - enqueued_at
                    if age > self.slow_window and not self._queue.empty():
                        logger.warning(
                            "dropping backlogged fast task (age %.1fs > %ds)",
                            age, self.slow_window)
                        continue
                    self._fast_pass(payload["path"], payload["offset"], gen)
                elif kind == "slow":
                    self._slow_pass(payload, gen)
                elif kind == "analyze":
                    self._analyze_current_state()
            except Exception:
                logger.exception("worker task %r failed", kind)
            finally:
                self._queue.task_done()

    def _fast_pass(self, chunk_path: str, offset: float, gen: int):
        """Quick transcription of a single short chunk (small model)."""
        try:
            segments = transcribe_audio(
                chunk_path, language="zh",
                model=self.fast_model, backend="mlx",
            )
        except Exception:
            logger.exception("fast pass failed for %s", chunk_path)
            return

        with self._lock:
            if gen != self._generation:
                return
            for seg in segments:
                self._fast_segments.append({
                    "start": seg["start"] + offset,
                    "end": seg["end"] + offset,
                    "text": seg["text"],
                    "quality": "fast",
                })
            self._merge_segments()

        self._notify_update()

    def _slow_pass(self, chunk_infos: list, gen: int):
        """Refined transcription of accumulated chunks (larger model)."""
        # If chunk indices are not contiguous (a chunk was lost), concat
        # would shift every later timestamp. Fall back to per-chunk refined
        # transcription with each chunk's own offset.
        contiguous = all(
            chunk_infos[i + 1]["index"] == chunk_infos[i]["index"] + 1
            for i in range(len(chunk_infos) - 1)
        )
        if not contiguous:
            logger.warning(
                "slow pass: non-contiguous chunk indices %s — refining per chunk",
                [c["index"] for c in chunk_infos])
            for info in chunk_infos:
                self._refine_one(info["path"], info["offset"], gen)
            self._analyze_current_state()
            return

        if len(chunk_infos) == 1:
            # Single chunk: no concat, no temp file needed.
            self._refine_one(chunk_infos[0]["path"], chunk_infos[0]["offset"], gen)
            self._analyze_current_state()
            return

        combined_path = None
        try:
            fd_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            fd_file.close()
            combined_path = fd_file.name

            concat_inputs = []
            for info in chunk_infos:
                concat_inputs.extend(["-i", info["path"]])
            filter_str = "%sconcat=n=%d:v=0:a=1[out]" % (
                "".join("[%d:a]" % i for i in range(len(chunk_infos))),
                len(chunk_infos))
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"] + concat_inputs + [
                "-filter_complex", filter_str,
                "-map", "[out]",
                combined_path, "-y",
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=120)
            if result.returncode != 0:
                logger.error(
                    "ffmpeg concat failed (rc=%d): %s",
                    result.returncode,
                    result.stderr.decode("utf-8", errors="replace")[-2000:])
                return

            self._refine_one(combined_path, chunk_infos[0]["offset"], gen)
        except Exception:
            logger.exception("slow pass failed")
        finally:
            if combined_path:
                Path(combined_path).unlink(missing_ok=True)

        self._analyze_current_state()

    def _refine_one(self, audio_path: str, base_offset: float, gen: int):
        """Transcribe one audio file with the slow model and merge results."""
        try:
            segments = transcribe_audio(
                audio_path, language="zh",
                model=self.slow_model, backend="mlx",
            )
        except Exception:
            logger.exception("refined transcription failed for %s", audio_path)
            return

        with self._lock:
            if gen != self._generation:
                return
            for seg in segments:
                self._slow_segments.append({
                    "start": seg["start"] + base_offset,
                    "end": seg["end"] + base_offset,
                    "text": seg["text"],
                    "quality": "refined",
                })
            self._merge_segments()

        self._notify_update()

    # ------------------------------------------------------------------ #
    # Merging / analysis
    # ------------------------------------------------------------------ #

    def _merge_segments(self):
        """Merge fast and refined segments (must hold self._lock).

        Fast segments whose time range is covered by a refined segment are
        deleted permanently — only the uncovered tail keeps fast entries, so
        the merge cost stays proportional to the live tail, not the whole
        meeting.
        """
        covered = [(s["start"], s["end"]) for s in self._slow_segments]

        kept_fast = []
        for fast in self._fast_segments:
            overlaps = any(
                fast["start"] < ce and fast["end"] > cs for (cs, ce) in covered)
            if not overlaps:
                kept_fast.append(fast)
        self._fast_segments = kept_fast

        merged = list(self._slow_segments) + kept_fast
        merged.sort(key=lambda s: s["start"])
        self._merged_segments = merged

    def _analyze_current_state(self):
        with self._lock:
            if not self._merged_segments:
                return
            recent = list(self._merged_segments[-20:])
            questions = list(self.questions)

        transcript_text = "\n".join(
            "[%02d:%02d] %s" % (int(s["start"] // 60), int(s["start"] % 60), s["text"])
            for s in recent
        )

        prompt = (
            "You are monitoring a live meeting. Recent transcript:\n%s\n\n"
            "Provide JSON: {\"current_topic\": \"...\", \"status\": \"...\", "
            "\"key_points\": [...], \"disputes\": [{\"topic\": \"...\", \"positions\": [...]}]"
        ) % transcript_text

        if questions:
            prompt += ', "question_findings": {'
            prompt += ", ".join(
                '%s: "info or null"' % json.dumps(q, ensure_ascii=False)
                for q in questions
            )
            prompt += "}"

        prompt += "}"

        try:
            response = self.llm.complete(
                prompt=prompt,
                system="Real-time meeting analyst. Output ONLY valid JSON. Be concise.",
                temperature=0.2,
            )
        except Exception:
            logger.exception("LLM analysis request failed")
            return

        analysis = self._parse_json(response)
        analysis["timestamp"] = time.time()

        with self._lock:
            self.analysis_history.append(analysis)

        self._notify_update()

    def _get_question_status(self) -> list:
        """Must hold self._lock (called from get_state)."""
        result = []
        latest = self.analysis_history[-1] if self.analysis_history else {}
        findings = latest.get("question_findings", {}) or {}
        for q in self.questions:
            result.append({
                "question": q,
                "status": "found" if findings.get(q) else "pending",
                "finding": findings.get(q) or "",
            })
        return result

    def _notify_update(self):
        state = self.get_state()
        for cb in self._update_callbacks:
            try:
                cb(state)
            except Exception:
                logger.exception("update callback failed")

    def _parse_json(self, text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        start = text.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            break
        logger.warning("analysis response was not valid JSON: %.200s", text)
        return {"raw": text[:300]}
