"""Simulate a live meeting by feeding an existing recording chunk-by-chunk.

Uses dual-window transcription:
- short chunks for fast updates (small model)
- every --slow-window seconds of accumulated chunks triggers a refined pass
  (larger model)

Usage: python simulate_meeting.py <video_path> [--speed 5]
"""
from __future__ import annotations

import argparse
import logging
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
             fast_model: str = "small", slow_model: str = "medium",
             speed: float = 5.0, chunk_seconds: int = 5, slow_window: int = 30):

    dur = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", video_path
    ]).decode().strip()
    duration = float(dur)
    num_chunks = int(duration / chunk_seconds) + 1

    print("Simulating %.0fs meeting: %d x %ds chunks (%.1fx speed)" % (
        duration, num_chunks, chunk_seconds, speed))
    print("Dual window: %ds chunks (%s) + %ds refined (%s)" % (
        chunk_seconds, fast_model, slow_window, slow_model))

    monitor = MeetingMonitor(
        text_model=text_model,
        fast_model=fast_model,
        slow_model=slow_model,
        slow_window=slow_window,
    )

    # Inject the simulated monitor so /api/state and SocketIO pushes all
    # use it (re-registering the same route would be ignored by Flask).
    app, socketio = create_app(monitor=monitor, text_model=text_model)

    def feed_chunks():
        time.sleep(2)
        tmpdir = tempfile.mkdtemp()

        for i in range(num_chunks):
            start_time = i * chunk_seconds
            if start_time >= duration:
                break

            chunk_dur = min(chunk_seconds, duration - start_time)
            chunk_path = "%s/chunk_%04d.wav" % (tmpdir, i)

            subprocess.run([
                "ffmpeg", "-ss", str(start_time),
                "-i", video_path,
                "-t", str(chunk_dur),
                "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                chunk_path, "-y"
            ], capture_output=True)

            # Screenshot every slow_window seconds
            if i % max(1, slow_window // chunk_seconds) == 0:
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

            monitor.add_audio_chunk(chunk_path, i, chunk_seconds)

            time.sleep(chunk_seconds / speed)

        print("\nSimulation complete!")

    feeder = threading.Thread(target=feed_chunks, daemon=True)
    feeder.start()

    print("Monitor: http://127.0.0.1:%d\n" % port)
    socketio.run(app, host="127.0.0.1", port=port, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("--port", type=int, default=5200)
    parser.add_argument("--text-model", default=None)
    parser.add_argument("--fast-model", default="small")
    parser.add_argument("--slow-model", default="medium")
    parser.add_argument("--speed", type=float, default=5.0)
    parser.add_argument("--chunk-seconds", type=int, default=5)
    parser.add_argument("--slow-window", type=int, default=30)
    args = parser.parse_args()

    simulate(args.video, args.port, args.text_model, args.fast_model,
             args.slow_model, args.speed, args.chunk_seconds, args.slow_window)
