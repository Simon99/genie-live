"""Simulate a live meeting by feeding an existing recording chunk-by-chunk.

Usage: python simulate_meeting.py <video_path> [--port 5200] [--chunk-seconds 10]

This starts the web server AND feeds audio/screenshots at real-time pace,
so you can open the browser and watch the monitor update live.
"""
from __future__ import annotations

import argparse
import json
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


def simulate(video_path: str, port: int = 5200, chunk_seconds: int = 10,
             text_model: str = None, speed: float = 1.0):
    """Run meeting simulation."""

    # Get video duration
    dur = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", video_path
    ]).decode().strip()
    duration = float(dur)
    num_chunks = int(duration / chunk_seconds) + 1

    print("Simulating %.0f-second meeting in %d chunks (%.1fx speed)" % (
        duration, num_chunks, speed))

    # Create app
    app, socketio = create_app(text_model=text_model)

    # Access the monitor from the app
    # We'll feed data directly instead of using capture devices
    monitor = MeetingMonitor(text_model=text_model)

    def on_update(state):
        socketio.emit("state_update", state)
    monitor.on_update(on_update)

    # Override the /api/state endpoint to use our monitor
    @app.route("/api/sim-state", methods=["GET"])
    def sim_state():
        from flask import jsonify
        return jsonify(monitor.get_state())

    # Feed chunks in background
    def feed_chunks():
        time.sleep(2)  # Wait for server to start
        tmpdir = tempfile.mkdtemp()

        for i in range(num_chunks):
            if i * chunk_seconds >= duration:
                break

            start_time = i * chunk_seconds
            chunk_dur = min(chunk_seconds, duration - start_time)
            chunk_path = "%s/chunk_%04d.wav" % (tmpdir, i)

            # Extract audio chunk
            subprocess.run([
                "ffmpeg", "-ss", str(start_time),
                "-i", video_path,
                "-t", str(chunk_dur),
                "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                chunk_path, "-y"
            ], capture_output=True)

            # Extract screenshot
            frame_path = "%s/frame_%04d.png" % (tmpdir, i)
            subprocess.run([
                "ffmpeg", "-ss", str(start_time),
                "-i", video_path,
                "-vframes", "1", "-q:v", "2",
                frame_path, "-y"
            ], capture_output=True)

            print("[%02d:%02d] Feeding chunk %d/%d..." % (
                int(start_time // 60), int(start_time % 60), i + 1, num_chunks))

            # Feed to monitor
            monitor.add_transcript_chunk(chunk_path, i)
            if Path(frame_path).exists():
                monitor.add_screen_frame(frame_path, i, time.time())

            # Wait (simulated real-time, adjusted by speed)
            time.sleep(chunk_seconds / speed)

        print("\nSimulation complete!")

    feeder = threading.Thread(target=feed_chunks, daemon=True)
    feeder.start()

    print("Monitor running at http://localhost:%d" % port)
    print("Open browser to watch live updates.\n")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulate live meeting from recording")
    parser.add_argument("video", help="Path to video file")
    parser.add_argument("--port", type=int, default=5200)
    parser.add_argument("--chunk-seconds", type=int, default=10)
    parser.add_argument("--text-model", default=None)
    parser.add_argument("--speed", type=float, default=5.0,
                        help="Simulation speed multiplier (default: 5x)")
    args = parser.parse_args()

    simulate(args.video, args.port, args.chunk_seconds, args.text_model, args.speed)
