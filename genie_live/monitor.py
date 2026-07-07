from __future__ import annotations

"""Live meeting monitor — dual-window transcription + rolling analysis."""

import json
import logging
import math
import queue
import re
import subprocess
import tempfile
import time
import threading
import wave
from collections import deque
from pathlib import Path

from genie_core.audio import transcribe_audio
from genie_core.llm import LMStudioClient

logger = logging.getLogger(__name__)


def _chunk_peak_db(path: str) -> float:
    """Peak level of a 16-bit PCM wav in dBFS (0 = full scale).

    Returns None when the file can't be analyzed (unexpected format) so
    callers treat the chunk as audible rather than silently dropping it.
    """
    try:
        with wave.open(path, "rb") as w:
            if w.getsampwidth() != 2:
                return None
            peak = 0
            while True:
                frames = w.readframes(65536)
                if not frames:
                    break
                mv = memoryview(frames).cast("h")
                for sample in mv:
                    a = -sample if sample < 0 else sample
                    if a > peak:
                        peak = a
            if peak == 0:
                return -96.0
            return 20.0 * math.log10(peak / 32768.0)
    except Exception:
        logger.exception("peak analysis failed for %s", path)
        return None


def _collapse_inline_repeats(text: str) -> str:
    """Collapse whisper's in-segment hallucination loops.

    A single segment can contain the same phrase looped many times
    (observed live: 「我们将在最后的一段时间中,」×7 inside one 27s
    segment). A phrase of 4+ chars repeated 3+ times back-to-back is
    almost never real speech — keep one occurrence.
    """
    if len(text) < 16:
        return text
    return re.sub(r"(.{4,60}?)(?:\1){2,}", r"\1", text)


def _append_collapsed(target: list, segments: list, offset: float, quality: str):
    """Append transcribed segments, collapsing consecutive duplicate text.

    Whisper hallucination on noise repeats the same sentence in a tight
    loop ("測試一下" x10); collapsing keeps one entry and extends its end
    time. Only adjacent (< 4s gap) identical texts are merged, so a phrase
    genuinely said again later still gets its own segment.
    """
    for seg in segments:
        seg["text"] = _collapse_inline_repeats(seg["text"])
        text = seg["text"].strip()
        start = seg["start"] + offset
        end = seg["end"] + offset
        last = target[-1] if target else None
        if (last is not None and last["text"].strip() == text
                and start - last["end"] < 4.0):
            last["end"] = max(last["end"], end)
            continue
        target.append({"start": start, "end": end, "text": seg["text"],
                       "quality": quality})


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

    All whisper work is serialized on the ASR worker thread — the MLX
    whisper backend must never be called concurrently. LLM analysis and
    summary calls run on a separate LLM worker (with request coalescing)
    so slow LLM inference never starves transcription.
    """

    def __init__(
        self,
        lm_studio_url: str = "http://localhost:1234/v1",
        text_model: str = None,
        questions: list = None,
        fast_model: str = "small",
        slow_model: str = "medium",
        slow_window: int = 30,
        vocab_path: str = None,
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

        # Silence gate. Whisper hallucinates fluent text on silence/noise,
        # so chunks whose peak is below the gate threshold skip transcription.
        # "auto" derives the threshold from the observed noise floor (20th
        # percentile of recent chunk peaks + margin) because the floor is
        # highly environment-dependent (measured: WeMeet loopback ~-23 dB
        # peak vs real speech ~-14 dB peak).
        self.gate_mode = "auto"           # "auto" | "manual" | "off"
        self.gate_manual_db = -20.0
        self._peak_history = deque(maxlen=90)
        self._last_peak_db = None
        self._gate_skipped = 0
        self._gate_auto_margin = 6.0
        self._gate_auto_cap = -12.0       # never gate louder chunks than this
        self._gate_abs_floor_db = -55.0   # below this is silence, always

        # ASR vocabulary. Plain terms bias whisper decoding via
        # initial_prompt; "錯詞=正詞" entries additionally force a text
        # replacement after transcription (for stubborn homophones).
        # Persisted to vocab_path so it survives restarts.
        self._vocab_raw = []              # user entries as typed
        self._vocab_terms = []            # user hotword terms
        self._corrections = {}            # wrong -> right
        self._vocab_auto = []             # terms auto-discovered mid-meeting
        self._vocab_blacklist = set()     # user-rejected auto terms (session)
        self._vocab_path = Path(
            vocab_path or Path.home() / ".genie" / "live_vocabulary.json")
        self._load_vocabulary()

        # Whole-session timeline summary: chronological blocks of
        # {"start_sec","end_sec","topic","points","decisions"}. Only the
        # last (open) block may be revised by later updates; earlier
        # blocks are frozen, so cost stays bounded and content can't drift.
        self._session_timeline = []
        self._summary_upto_time = 0.0

        # Incremented by reset(); worker drops tasks from older generations.
        self._generation = 0

        # Two workers: the ASR queue serializes whisper work (the MLX
        # backend must never run concurrently); the LLM queue runs
        # analysis/summary calls, which can take tens of seconds each and
        # must never block transcription (observed live: analysis on the
        # shared worker backlogged fast tasks by 700s).
        self._queue = queue.Queue()
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="monitor-asr-worker")
        self._worker.start()
        self._llm_queue = queue.Queue()
        self._llm_worker = threading.Thread(
            target=self._llm_worker_loop, daemon=True, name="monitor-llm-worker")
        self._llm_worker.start()

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
            self._gate_skipped = 0
            self._session_timeline = []
            self._summary_upto_time = 0.0
            # _peak_history is kept: the acoustic environment doesn't
            # change just because a new session started.

    def add_audio_chunk(self, chunk_path: str, chunk_index: int, chunk_seconds: int):
        """Queue a new audio chunk for dual-window processing.

        Non-blocking: enqueues a fast-pass task immediately and, when
        ``slow_window`` seconds have accumulated, a slow-pass task.
        Chunks below the silence-gate threshold skip the fast pass; a slow
        batch is skipped only when every chunk in it was silent.
        """
        offset = chunk_index * chunk_seconds
        peak_db = _chunk_peak_db(chunk_path)

        with self._lock:
            gen = self._generation
            self._last_peak_db = peak_db
            if peak_db is not None:
                self._peak_history.append(peak_db)
            threshold = self._gate_threshold()
            silent = (peak_db is not None and threshold is not None
                      and peak_db < threshold)
            if silent:
                self._gate_skipped += 1
            self._slow_buffer.append({
                "path": chunk_path,
                "index": chunk_index,
                "offset": offset,
                "chunk_seconds": chunk_seconds,
                "silent": silent,
            })
            accumulated = sum(c["chunk_seconds"] for c in self._slow_buffer)
            batch = None
            if accumulated >= self.slow_window:
                batch = list(self._slow_buffer)
                self._slow_buffer = []

        if silent:
            logger.info("gate: chunk %d silent (peak %.1f dB < %.1f dB), skipping fast pass",
                        chunk_index, peak_db, threshold)
        else:
            self._queue.put(
                ("fast", {"path": chunk_path, "offset": offset}, time.monotonic(), gen))
        if batch:
            if any(not c["silent"] for c in batch):
                self._queue.put(("slow", batch, time.monotonic(), gen))
            else:
                logger.info("gate: slow batch %s all silent, skipped",
                            [c["index"] for c in batch])

    def _gate_threshold(self) -> float:
        """Active gate threshold in dBFS, or None to disable (hold lock).

        Auto mode gates only when the recent peak distribution is clearly
        bimodal (a quiet cluster well below a loud cluster). In a
        continuous-audio environment every chunk is loud-ish; a naive
        percentile floor then drifts up and gates real speech (observed
        live: threshold hit the cap and skipped -16 dB speech chunks).
        """
        if self.gate_mode == "off":
            return None
        if self.gate_mode == "manual":
            return self.gate_manual_db
        # Absolute floor: peaks this low are digital/near silence no matter
        # what the distribution looks like (observed live: an all-silent
        # meeting is unimodal, which disabled the bimodal gate and whisper
        # hallucinated on -91 dB zeros for minutes).
        peaks = sorted(self._peak_history)
        n = len(peaks)
        if n < 12:
            return self._gate_abs_floor_db
        p20 = peaks[int(n * 0.2)]
        p80 = peaks[int(n * 0.8)]
        if p80 - p20 < 8.0:
            return self._gate_abs_floor_db  # unimodal: only hard-floor gating
        return min(max(p20 + self._gate_auto_margin, self._gate_abs_floor_db),
                   p80 - 3.0, self._gate_auto_cap)

    def set_gate(self, mode: str = None, threshold_db: float = None):
        """Update silence-gate settings from the UI."""
        with self._lock:
            if mode is not None:
                if mode not in ("auto", "manual", "off"):
                    raise ValueError("gate mode must be auto/manual/off")
                self.gate_mode = mode
            if threshold_db is not None:
                self.gate_manual_db = max(-96.0, min(0.0, float(threshold_db)))
        self._notify_update()

    @staticmethod
    def _parse_vocab_entries(entries: list):
        """Split raw entries into (kept_raw, prompt_terms, corrections)."""
        terms, corrections, kept = [], {}, []
        for e in entries or []:
            e = str(e).strip()
            if not e:
                continue
            kept.append(e)
            if "=" in e:
                wrong, _, right = e.partition("=")
                wrong, right = wrong.strip(), right.strip()
                if wrong and right:
                    corrections[wrong] = right
                    terms.append(right)
                continue
            terms.append(e)
        return kept, terms, corrections

    def set_vocabulary(self, entries: list):
        """Update the user ASR hotword list from the UI (hot, no restart).

        Plain entries become whisper initial_prompt terms; entries written
        as ``錯詞=正詞`` also force a post-transcription replacement.
        User entries are persisted to ``vocab_path`` and reloaded on
        startup; auto-discovered terms live alongside them (see
        ``_merge_auto_terms``).
        """
        raw, terms, corrections = self._parse_vocab_entries(entries)
        with self._lock:
            self._vocab_raw = raw
            self._vocab_terms = terms
            self._corrections = corrections
            # A term explicitly added by the user leaves the auto pool.
            self._vocab_auto = [t for t in self._vocab_auto if t not in terms]
        self._save_vocabulary()
        self._notify_update()

    def blacklist_auto_term(self, term: str):
        """Remove an auto-discovered term and stop re-learning it."""
        term = str(term).strip()
        with self._lock:
            self._vocab_auto = [t for t in self._vocab_auto if t != term]
            self._vocab_blacklist.add(term)
        self._save_vocabulary()
        self._notify_update()

    def _merge_auto_terms(self, candidates: list):
        """Fold LLM-extracted terms into the auto vocabulary (hold no lock).

        Auto terms extend the hotword prompt for the rest of the meeting:
        a proper noun mentioned early biases transcription of its later
        mentions. Capped FIFO so a long meeting can't grow the prompt
        unboundedly; user entries and blacklisted terms are never touched.
        """
        cleaned = []
        for t in candidates or []:
            t = str(t).strip().strip("、,;。 ")
            if not (2 <= len(t) <= 30) or "\n" in t or t.isdigit():
                continue
            cleaned.append(t)
        if not cleaned:
            return
        changed = False
        with self._lock:
            known = set(self._vocab_terms) | set(self._vocab_auto) | self._vocab_blacklist
            for t in cleaned:
                if t in known:
                    continue
                self._vocab_auto.append(t)
                known.add(t)
                changed = True
            while len(self._vocab_auto) > self.AUTO_VOCAB_MAX:
                dropped = self._vocab_auto.pop(0)
                logger.info("auto vocab full, dropping oldest term %r", dropped)
                changed = True
        if changed:
            self._save_vocabulary()
            self._notify_update()

    def _load_vocabulary(self):
        """Load persisted vocabulary at startup (missing file is fine).

        Accepts both the old format (plain list = user entries) and the
        current ``{"user": [...], "auto": [...]}`` format.
        """
        try:
            if not self._vocab_path.exists():
                return
            data = json.loads(self._vocab_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                user, auto = data, []
            elif isinstance(data, dict):
                user, auto = data.get("user", []), data.get("auto", [])
            else:
                return
            kept, terms, corrections = self._parse_vocab_entries(user)
            self._vocab_raw = kept
            self._vocab_terms = terms
            self._corrections = corrections
            self._vocab_auto = [str(t).strip() for t in auto if str(t).strip()]
            logger.info("loaded %d user + %d auto vocabulary entries from %s",
                        len(kept), len(self._vocab_auto), self._vocab_path)
        except Exception:
            logger.exception("failed to load vocabulary from %s", self._vocab_path)

    def _save_vocabulary(self):
        try:
            with self._lock:
                payload = {"user": list(self._vocab_raw),
                           "auto": list(self._vocab_auto)}
            self._vocab_path.parent.mkdir(parents=True, exist_ok=True)
            self._vocab_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            logger.exception("failed to save vocabulary to %s", self._vocab_path)

    def _initial_prompt(self) -> str:
        """Hotword prompt for whisper, or None (hold self._lock)."""
        terms = self._vocab_terms + self._vocab_auto
        if not terms:
            return None
        return "會議詞彙：" + "、".join(terms)

    def _apply_corrections(self, segments: list) -> list:
        with self._lock:
            corrections = dict(self._corrections)
        if not corrections:
            return segments
        for seg in segments:
            for wrong, right in corrections.items():
                if wrong in seg["text"]:
                    seg["text"] = seg["text"].replace(wrong, right)
        return segments

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
        self._request_analysis(gen)

    def get_full_transcript(self) -> list:
        """Complete merged transcript (refined + uncovered fast tail)."""
        with self._lock:
            return [dict(s) for s in self._merged_segments]

    def get_state(self) -> dict:
        with self._lock:
            recent = self._merged_segments[-15:] if self._merged_segments else []
            display = [{
                "start": s["start"],
                "end": s["end"],
                "text": s["text"],
                "quality": s.get("quality", "fast"),
            } for s in recent]

            threshold = self._gate_threshold()
            return {
                "transcript_count": len(self._merged_segments),
                "fast_count": len(self._fast_segments),
                "refined_count": len(self._slow_segments),
                "frame_count": len(self.screen_frames),
                "recent_transcript": display,
                "latest_analysis": self.analysis_history[-1] if self.analysis_history else None,
                "questions": self._get_question_status(),
                "session_timeline": list(self._session_timeline),
                "vocabulary": list(self._vocab_raw),
                "vocabulary_auto": list(self._vocab_auto),
                "audio_gate": {
                    "mode": self.gate_mode,
                    "threshold_db": threshold,
                    "manual_db": self.gate_manual_db,
                    "last_peak_db": self._last_peak_db,
                    "noise_floor_db": (sorted(self._peak_history)[
                        max(0, int(len(self._peak_history) * 0.2) - 1)]
                        if len(self._peak_history) >= 6 else None),
                    "skipped_chunks": self._gate_skipped,
                },
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
            except Exception:
                logger.exception("worker task %r failed", kind)
            finally:
                self._queue.task_done()

    def _request_analysis(self, gen: int = None):
        if gen is None:
            with self._lock:
                gen = self._generation
        self._llm_queue.put(("analyze", None, time.monotonic(), gen))

    def _llm_worker_loop(self):
        while True:
            kind, payload, enqueued_at, gen = self._llm_queue.get()
            # Coalesce: analysis always looks at the *current* transcript,
            # so a backlog of pending requests collapses into one run.
            drained = 0
            try:
                while True:
                    k2, _, _, g2 = self._llm_queue.get_nowait()
                    self._llm_queue.task_done()
                    drained += 1
                    gen = g2
            except queue.Empty:
                pass
            if drained:
                logger.info("coalesced %d pending analysis requests", drained)
            try:
                with self._lock:
                    if gen != self._generation:
                        continue
                if kind == "analyze":
                    self._analyze_current_state()
            except Exception:
                logger.exception("llm worker task %r failed", kind)
            finally:
                self._llm_queue.task_done()

    def _fast_pass(self, chunk_path: str, offset: float, gen: int):
        """Quick transcription of a single short chunk (small model)."""
        try:
            with self._lock:
                prompt = self._initial_prompt()
            segments = transcribe_audio(
                chunk_path, language="zh",
                model=self.fast_model, backend="mlx",
                initial_prompt=prompt,
            )
            segments = self._apply_corrections(segments)
        except Exception:
            logger.exception("fast pass failed for %s", chunk_path)
            return

        with self._lock:
            if gen != self._generation:
                return
            _append_collapsed(self._fast_segments, segments, offset, "fast")
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
            self._request_analysis(gen)
            return

        if len(chunk_infos) == 1:
            # Single chunk: no concat, no temp file needed.
            self._refine_one(chunk_infos[0]["path"], chunk_infos[0]["offset"], gen)
            self._request_analysis(gen)
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

        self._request_analysis(gen)

    def _refine_one(self, audio_path: str, base_offset: float, gen: int):
        """Transcribe one audio file with the slow model and merge results."""
        try:
            with self._lock:
                prompt = self._initial_prompt()
            segments = transcribe_audio(
                audio_path, language="zh",
                model=self.slow_model, backend="mlx",
                initial_prompt=prompt,
            )
            segments = self._apply_corrections(segments)
        except Exception:
            logger.exception("refined transcription failed for %s", audio_path)
            return

        with self._lock:
            if gen != self._generation:
                return
            _append_collapsed(self._slow_segments, segments, base_offset, "refined")
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

    AUTO_VOCAB_MAX = 30

    ANALYSIS_STYLE = (
        "所有描述用繁體中文；逐字稿中已是英文的技術術語、產品名、API 名"
        "保留英文原文。嚴禁把中文詞彙自行翻譯成英文（實測失誤案例："
        "把「退貨率」寫成 Filter Rate、「漏光」寫成 Thermal Burn——"
        "逐字稿沒有的英文詞一律不得出現）。"
    )

    def _analyze_current_state(self):
        with self._lock:
            if not self._merged_segments:
                return
            recent = list(self._merged_segments[-20:])
            questions = list(self.questions)

        # Mark rough (fast-pass) lines: term extraction must ignore them —
        # learning a garbled term poisons the hotword prompt, which then
        # biases later transcription toward the garble (observed live:
        # noise-hallucinated 電源器 became a hotword, then a topic title).
        transcript_text = "\n".join(
            "[%02d:%02d]%s %s" % (int(s["start"] // 60), int(s["start"] % 60),
                                  "" if s.get("quality") == "refined" else "(粗)",
                                  s["text"])
            for s in recent
        )

        with self._lock:
            known_terms = self._vocab_terms + self._vocab_auto

        prompt = (
            "You are monitoring a live meeting. Recent transcript "
            "((粗) 標記 = 粗轉寫，內容不可靠):\n%s\n\n"
            "Provide JSON: {\"current_topic\": \"...\", \"status\": \"...\", "
            "\"key_points\": [...], \"disputes\": [{\"topic\": \"...\", \"positions\": [...]}], "
            "\"new_terms\": [逐字稿中出現的專有名詞/產品名/英文縮寫/領域術語，"
            "尚未在既有詞彙表 %s 中的，最多5個，沒有給空陣列；"
            "只能取自沒有(粗)標記的行；只收真實出現、拼寫可信、且是通用或"
            "領域正規用語的詞，看起來像轉寫錯誤的怪詞不要收]"
        ) % (transcript_text,
             json.dumps(known_terms, ensure_ascii=False) if known_terms else "[]")

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
                system="Real-time meeting analyst. Output ONLY valid JSON. "
                       "Be concise. " + self.ANALYSIS_STYLE,
                temperature=0.2,
            )
        except Exception:
            logger.exception("LLM analysis request failed")
            return

        analysis = self._parse_json(response)
        analysis["timestamp"] = time.time()
        new_terms = analysis.pop("new_terms", None)

        with self._lock:
            self.analysis_history.append(analysis)

        if new_terms:
            self._merge_auto_terms(new_terms)

        self._notify_update()
        self._update_session_summary()

    def _update_session_summary(self):
        """Fold new transcript into the chronological timeline summary.

        Runs on the worker thread after each analysis round. The LLM sees
        only the last (open) timeline block plus transcript newer than the
        last folded timestamp; it may revise that block and/or append new
        blocks. Earlier blocks are frozen — bounded cost, no drift.
        """
        with self._lock:
            new_segs = [s for s in self._merged_segments
                        if s["start"] >= self._summary_upto_time]
            if not new_segs:
                return
            frozen = self._session_timeline[:-1]
            open_block = (self._session_timeline[-1]
                          if self._session_timeline else None)
            upto = max(s["end"] for s in new_segs)

        new_text = "\n".join(
            "[%02d:%02d] %s" % (int(s["start"] // 60), int(s["start"] % 60), s["text"])
            for s in new_segs
        )
        prompt = (
            "你在維護一場進行中會議的「時間軸整理」：按時間順序，"
            "每個時間段一個議題區塊。\n"
            "目前進行中的區塊（可修改；null 表示還沒有）：\n%s\n\n"
            "新增逐字稿（時間戳為 分:秒）：\n%s\n\n"
            "規則：一個區塊只能包含一個議題。新內容延續同一議題時，"
            "更新進行中的區塊（擴充 points、延長 end_sec）；出現"
            "「第N個議題」「下一個議題」「接下來討論」等切換語，或內容"
            "明顯屬於不同主題時，必須結束該區塊並開新區塊，寧可多切"
            "不可合併；一次可回傳多個區塊。點列要具體，含結論與數據。\n"
            "輸出 JSON：{\"blocks\": [{\"start_sec\": 秒數, \"end_sec\": 秒數, "
            "\"topic\": \"...\", \"points\": [\"...\"], \"decisions\": [\"...\"]}]}"
            "（blocks[0] 是進行中區塊的更新版，其後為新區塊）"
        ) % (json.dumps(open_block, ensure_ascii=False), new_text)

        try:
            response = self.llm.complete(
                prompt=prompt,
                system="Meeting timeline summarizer. Output ONLY valid JSON. "
                       + self.ANALYSIS_STYLE,
                temperature=0.2,
            )
            result = self._parse_json(response)
        except Exception:
            logger.exception("session timeline update failed")
            return

        blocks = result.get("blocks") if isinstance(result, dict) else None
        if not isinstance(blocks, list) or not all(
                isinstance(b, dict) and "topic" in b for b in blocks):
            logger.warning("timeline response malformed, keeping previous")
            return

        with self._lock:
            self._session_timeline = frozen + blocks
            self._summary_upto_time = upto

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
