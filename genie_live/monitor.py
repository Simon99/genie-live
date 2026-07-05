from __future__ import annotations

"""Live meeting monitor — orchestrates capture, transcription, and analysis."""

import json
import time
import threading
from pathlib import Path

from genie_core.audio import transcribe_audio
from genie_core.llm import LMStudioClient


class MeetingMonitor:
    """Real-time meeting monitoring with rolling analysis."""

    def __init__(
        self,
        lm_studio_url: str = "http://localhost:1234/v1",
        text_model: str = None,
        vision_model: str = None,
        questions: list = None,
    ):
        self.llm = LMStudioClient(base_url=lm_studio_url, model=text_model)
        self.vision_model = vision_model
        self.questions = questions or []

        self.transcript_segments = []
        self.screen_frames = []
        self.analysis_history = []
        self._lock = threading.Lock()
        self._update_callbacks = []

    def on_update(self, callback):
        """Register callback(state_dict) for real-time UI updates."""
        self._update_callbacks.append(callback)

    def add_transcript_chunk(self, audio_path: str, chunk_index: int):
        """Process a new audio chunk and add to running transcript."""
        try:
            segments = transcribe_audio(audio_path, language="zh", model="tiny")
            offset = chunk_index * 10.0

            with self._lock:
                for seg in segments:
                    adjusted = {
                        "start": seg["start"] + offset,
                        "end": seg["end"] + offset,
                        "text": seg["text"],
                    }
                    self.transcript_segments.append(adjusted)

            self._analyze_current_state()
        except Exception:
            pass

    def add_screen_frame(self, frame_path: str, index: int, timestamp: float):
        """Process a new screenshot."""
        with self._lock:
            self.screen_frames.append({
                "path": frame_path,
                "index": index,
                "timestamp": timestamp,
            })

    def set_questions(self, questions: list):
        """Update the question checklist."""
        self.questions = questions
        self._analyze_current_state()

    def get_state(self) -> dict:
        """Get current monitoring state."""
        with self._lock:
            return {
                "transcript_count": len(self.transcript_segments),
                "frame_count": len(self.screen_frames),
                "recent_transcript": self.transcript_segments[-10:] if self.transcript_segments else [],
                "latest_analysis": self.analysis_history[-1] if self.analysis_history else None,
                "questions": self._get_question_status(),
            }

    def _analyze_current_state(self):
        """Run LLM analysis on current accumulated data."""
        with self._lock:
            if not self.transcript_segments:
                return
            recent = self.transcript_segments[-20:]

        transcript_text = "\n".join(
            "[%02d:%02d] %s" % (int(s["start"] // 60), int(s["start"] % 60), s["text"])
            for s in recent
        )

        prompt_parts = [
            "You are monitoring a live meeting. Here is the recent transcript:\n",
            transcript_text,
            "\n\nProvide a brief JSON status update:",
            '{"current_topic": "...", "status": "...", "key_points": [...], '
        ]

        if self.questions:
            prompt_parts.append('"question_findings": {')
            for q in self.questions:
                prompt_parts.append('"%s": "relevant info found or null",' % q)
            prompt_parts.append('},')

        prompt_parts.append('"disputes": [{"topic": "...", "positions": [...]}]}')

        try:
            response = self.llm.complete(
                prompt="".join(prompt_parts),
                system="You are a real-time meeting analyst. Output ONLY valid JSON. Be concise.",
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
        """Get status of each question from latest analysis."""
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
