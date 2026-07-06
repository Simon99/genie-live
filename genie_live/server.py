from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO

from .monitor import MeetingMonitor
from .capture import AudioCapture, ScreenCapture

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

CHUNK_SECONDS = 10


def create_app(
    monitor: MeetingMonitor = None,
    lm_studio_url: str = "http://localhost:1234/v1",
    text_model: str = None,
    audio_device: str = "0",
) -> tuple:
    """Create the Flask app + SocketIO server.

    ``monitor`` may be injected (tests / simulation); if None a real
    MeetingMonitor is created. The frontend is served from the same
    origin, so no CORS relaxation is configured.
    """
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    socketio = SocketIO(app)  # same-origin only (SocketIO default)

    if monitor is None:
        monitor = MeetingMonitor(lm_studio_url=lm_studio_url, text_model=text_model)

    captures = {"audio": None, "screen": None}

    def _augment_state(state: dict) -> dict:
        audio = captures["audio"]
        state["capture"] = {
            "recording": bool(audio and audio.is_running()),
            "error": audio.error if audio else None,
        }
        return state

    def on_state_update(state):
        socketio.emit("state_update", _augment_state(state))

    monitor.on_update(on_state_update)

    def _stop_captures():
        for key in ("audio", "screen"):
            cap = captures[key]
            if cap is not None:
                try:
                    cap.stop()
                except Exception:
                    logger.exception("failed to stop %s capture", key)
                captures[key] = None

    @app.route("/api/start", methods=["POST"])
    def start_monitoring():
        data = request.get_json(silent=True) or {}

        # A repeated /api/start must not leak the previous session's
        # capture threads or pollute the timeline: stop + reset first.
        _stop_captures()
        monitor.reset()
        monitor.set_questions(data.get("questions", []))

        device = str(data.get("audio_device", audio_device))

        audio_capture = AudioCapture(device=device, chunk_seconds=CHUNK_SECONDS)
        audio_capture.on_chunk(
            lambda path, index: monitor.add_audio_chunk(path, index, CHUNK_SECONDS))

        screen_capture = ScreenCapture(interval=10.0)
        screen_capture.on_frame(monitor.add_screen_frame)

        captures["audio"] = audio_capture
        captures["screen"] = screen_capture

        audio_capture.start()
        screen_capture.start()

        return jsonify({"status": "started"})

    @app.route("/api/stop", methods=["POST"])
    def stop_monitoring():
        _stop_captures()
        return jsonify({"status": "stopped"})

    @app.route("/api/state", methods=["GET"])
    def get_state():
        return jsonify(_augment_state(monitor.get_state()))

    @app.route("/api/questions", methods=["POST"])
    def update_questions():
        data = request.get_json(silent=True) or {}
        # set_questions only enqueues the LLM analysis on the monitor's
        # worker thread — this returns immediately.
        monitor.set_questions(data.get("questions", []))
        return jsonify({"status": "updated"})

    @app.route("/")
    def index():
        return send_from_directory(str(STATIC_DIR), "index.html")

    return app, socketio
