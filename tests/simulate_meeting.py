"""Simulate a live meeting by feeding an existing recording chunk-by-chunk.

Uses dual-window transcription:
- 5s chunks for fast updates
- Every 30s of accumulated chunks triggers a refined pass

Usage: python simulate_meeting.py <video_path> [--speed 5]
"""
from __future__ import annotations

import argparse
import subprocess
import tempfile
import threading
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "genie-core"))

from genie_live.monitor import MeetingMonitor
from genie_live.server import create_app


def simulate(video_path: str, port: int = 5200, text_model: str = None,
             whisper_model: str = "medium", speed: float = 5.0,
             fast_window: int = 5, slow_window: int = 30):

    dur = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", video_path
    ]).decode().strip()
    duration = float(dur)
    num_chunks = int(duration / fast_window) + 1

    print("Simulating %.0fs meeting: %d x %ds chunks (%.1fx speed)" % (
        duration, num_chunks, fast_window, speed))
    print("Dual window: %ds fast + %ds refined" % (fast_window, slow_window))

    app, socketio = create_app(text_model=text_model)

    monitor = MeetingMonitor(
        text_model=text_model,
        whisper_model=whisper_model,
        fast_window=fast_window,
        slow_window=slow_window,
    )

    def on_update(state):
        socketio.emit("state_update", state)
    monitor.on_update(on_update)

    @app.route("/api/state")
    def override_state():
        from flask import jsonify
        return jsonify(monitor.get_state())

    def feed_chunks():
        time.sleep(2)
        tmpdir = tempfile.mkdtemp()

        for i in range(num_chunks):
            start_time = i * fast_window
            if start_time >= duration:
                break

            chunk_dur = min(fast_window, duration - start_time)
            chunk_path = "%s/chunk_%04d.wav" % (tmpdir, i)

            subprocess.run([
                "ffmpeg", "-ss", str(start_time),
                "-i", video_path,
                "-t", str(chunk_dur),
                "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                chunk_path, "-y"
            ], capture_output=True)

            # Screenshot every 6th chunk (~30s)
            if i % (slow_window // fast_window) == 0:
                frame_path = "%s/frame_%04d.png" % (tmpdir, i)
                subprocess.run([
                    "ffmpeg", "-ss", str(start_time),
                    "-i", video_path,
                    "-vframes", "1", "-q:v", "2",
                    frame_path, "-y"
                ], capture_output=True)
                if Path(frame_path).exists():
                    monitor.add_screen_frame(frame_path, i, time.time())

            t = "%02d:%02d" % (int(start_time // 60), int(start_time % 60))
            state = monitor.get_state()
            print("[%s] chunk %d/%d | fast:%d refined:%d total:%d" % (
                t, i + 1, num_chunks,
                state["fast_count"], state["refined_count"], state["transcript_count"]))

            monitor.add_audio_chunk(chunk_path, i, fast_window)

            time.sleep(fast_window / speed)

        print("\nSimulation complete!")

    feeder = threading.Thread(target=feed_chunks, daemon=True)
    feeder.start()

    print("Monitor: http://localhost:%d\n" % port)
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("--port", type=int, default=5200)
    parser.add_argument("--text-model", default=None)
    parser.add_argument("--whisper-model", default="medium")
    parser.add_argument("--speed", type=float, default=5.0)
    parser.add_argument("--fast-window", type=int, default=5)
    parser.add_argument("--slow-window", type=int, default=30)
    args = parser.parse_args()

    simulate(args.video, args.port, args.text_model, args.whisper_model,
             args.speed, args.fast_window, args.slow_window)
