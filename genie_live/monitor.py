from __future__ import annotations

"""Live meeting monitor — dual-window transcription + rolling analysis."""

import json
import time
import threading
from pathlib import Path

from genie_core.audio import transcribe_audio
from genie_core.llm import LMStudioClient


class MeetingMonitor:
    """Real-time meeting monitoring with dual-window transcription.

    Two parallel transcription windows:
    - 5s fast window: quick rough transcript for immediate display
    - 30s slow window: higher quality transcript that overwrites the fast results
    """

    def __init__(
        self,
        lm_studio_url: str = "http://localhost:1234/v1",
        text_model: str = None,
        vision_model: str = None,
        questions: list = None,
        whisper_model: str = "medium",
        fast_window: int = 5,
        slow_window: int = 30,
    ):
        self.llm = LMStudioClient(base_url=lm_studio_url, model=text_model)
        self.vision_model = vision_model
        self.questions = questions or []
        self.whisper_model = whisper_model
        self.fast_window = fast_window
        self.slow_window = slow_window

        # Dual transcript storage
        # fast_segments: rough, immediately available
        # slow_segments: refined, overwrites fast when ready
        self._fast_segments = []  # list of {"start", "end", "text", "quality": "fast"}
        self._slow_segments = []  # list of {"start", "end", "text", "quality": "refined"}
        self._merged_segments = []  # final merged view

        self.screen_frames = []
        self.analysis_history = []
        self._lock = threading.Lock()
        self._update_callbacks = []

        # Track slow window accumulation
        self._slow_buffer_paths = []
        self._slow_buffer_start_offset = 0

    def on_update(self, callback):
        self._update_callbacks.append(callback)

    def add_audio_chunk(self, chunk_path: str, chunk_index: int, chunk_seconds: int):
        """Process a new audio chunk with dual-window strategy.

        Called for each chunk (should be fast_window sized, e.g. 5s).
        - Immediately transcribes with fast window
        - Accumulates for slow window; when enough chunks, runs refined pass
        """
        offset = chunk_index * chunk_seconds

        # Fast pass: transcribe this chunk immediately
        fast_thread = threading.Thread(
            target=self._fast_pass,
            args=(chunk_path, offset),
            daemon=True,
        )
        fast_thread.start()

        # Accumulate for slow pass
        with self._lock:
            self._slow_buffer_paths.append({
                "path": chunk_path,
                "offset": offset,
                "chunk_seconds": chunk_seconds,
            })

            accumulated = len(self._slow_buffer_paths) * chunk_seconds
            if accumulated >= self.slow_window:
                paths = list(self._slow_buffer_paths)
                self._slow_buffer_paths = []

                slow_thread = threading.Thread(
                    target=self._slow_pass,
                    args=(paths,),
                    daemon=True,
                )
                slow_thread.start()

    def _fast_pass(self, chunk_path: str, offset: float):
        """Quick transcription of a single short chunk."""
        try:
            segments = transcribe_audio(
                chunk_path, language="zh",
                model=self.whisper_model, backend="mlx",
            )

            with self._lock:
                for seg in segments:
                    self._fast_segments.append({
                        "start": seg["start"] + offset,
                        "end": seg["end"] + offset,
                        "text": seg["text"],
                        "quality": "fast",
                    })
                self._merge_segments()

            self._notify_update()
        except Exception as e:
            pass

    def _slow_pass(self, chunk_infos: list):
        """Refined transcription of accumulated chunks (longer context = better quality)."""
        try:
            import subprocess
            import tempfile

            # Concatenate all chunk audio files into one
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as combined:
                combined_path = combined.name

            concat_inputs = []
            for info in chunk_infos:
                concat_inputs.extend(["-i", info["path"]])

            if len(chunk_infos) == 1:
                combined_path = chunk_infos[0]["path"]
            else:
                filter_parts = []
                for i in range(len(chunk_infos)):
                    filter_parts.append("[%d:a]" % i)
                filter_str = "%sconcat=n=%d:v=0:a=1[out]" % (
                    "".join(filter_parts), len(chunk_infos))

                cmd = ["ffmpeg"] + concat_inputs + [
                    "-filter_complex", filter_str,
                    "-map", "[out]",
                    combined_path, "-y"
                ]
                subprocess.run(cmd, capture_output=True)

            base_offset = chunk_infos[0]["offset"]

            segments = transcribe_audio(
                combined_path, language="zh",
                model=self.whisper_model, backend="mlx",
            )

            with self._lock:
                for seg in segments:
                    self._slow_segments.append({
                        "start": seg["start"] + base_offset,
                        "end": seg["end"] + base_offset,
                        "text": seg["text"],
                        "quality": "refined",
                    })
                self._merge_segments()

            if combined_path != chunk_infos[0]["path"]:
                Path(combined_path).unlink(missing_ok=True)

            self._notify_update()
            self._analyze_current_state()
        except Exception:
            pass

    def _merge_segments(self):
        """Merge fast and slow segments. Slow segments overwrite fast ones in overlapping time ranges."""
        # Start with all slow (refined) segments
        merged = list(self._slow_segments)
        covered_ranges = [(s["start"], s["end"]) for s in self._slow_segments]

        # Add fast segments that don't overlap with any slow segment
        for fast in self._fast_segments:
            overlaps = False
            for (cs, ce) in covered_ranges:
                if fast["start"] < ce and fast["end"] > cs:
                    overlaps = True
                    break
            if not overlaps:
                merged.append(fast)

        merged.sort(key=lambda s: s["start"])
        self._merged_segments = merged

    def add_screen_frame(self, frame_path: str, index: int, timestamp: float):
        with self._lock:
            self.screen_frames.append({
                "path": frame_path,
                "index": index,
                "timestamp": timestamp,
            })

    def set_questions(self, questions: list):
        self.questions = questions
        self._analyze_current_state()

    def get_state(self) -> dict:
        with self._lock:
            recent = self._merged_segments[-15:] if self._merged_segments else []
            # Add quality indicator
            display = []
            for s in recent:
                display.append({
                    "start": s["start"],
                    "end": s["end"],
                    "text": s["text"],
                    "quality": s.get("quality", "fast"),
                })

            return {
                "transcript_count": len(self._merged_segments),
                "fast_count": len(self._fast_segments),
                "refined_count": len(self._slow_segments),
                "frame_count": len(self.screen_frames),
                "recent_transcript": display,
                "latest_analysis": self.analysis_history[-1] if self.analysis_history else None,
                "questions": self._get_question_status(),
            }

    def _analyze_current_state(self):
        with self._lock:
            if not self._merged_segments:
                return
            recent = self._merged_segments[-20:]

        transcript_text = "\n".join(
            "[%02d:%02d] %s" % (int(s["start"] // 60), int(s["start"] % 60), s["text"])
            for s in recent
        )

        prompt = (
            "You are monitoring a live meeting. Recent transcript:\n%s\n\n"
            "Provide JSON: {\"current_topic\": \"...\", \"status\": \"...\", "
            "\"key_points\": [...], \"disputes\": [{\"topic\": \"...\", \"positions\": [...]}]"
        ) % transcript_text

        if self.questions:
            prompt += ', "question_findings": {'
            for q in self.questions:
                prompt += '"%s": "info or null",' % q
            prompt += "}"

        prompt += "}"

        try:
            response = self.llm.complete(
                prompt=prompt,
                system="Real-time meeting analyst. Output ONLY valid JSON. Be concise.",
                temperature=0.2,
            )

            analysis = self._parse_json(response)
            analysis["timestamp"] = time.time()

            with self._lock:
                self.analysis_history.append(analysis)

            self._notify_update()
        except Exception:
            pass

    def _get_question_status(self) -> list:
        result = []
        latest = self.analysis_history[-1] if self.analysis_history else {}
        findings = latest.get("question_findings", {})
        for q in self.questions:
            result.append({
                "question": q,
                "status": "found" if findings.get(q) else "pending",
                "finding": findings.get(q, ""),
            })
        return result

    def _notify_update(self):
        state = self.get_state()
        for cb in self._update_callbacks:
            try:
                cb(state)
            except Exception:
                pass

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
        return {"raw": text[:300]}
