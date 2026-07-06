from __future__ import annotations

import argparse
import logging


def main():
    parser = argparse.ArgumentParser(description="Genie Live Meeting Monitor")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default 127.0.0.1; this server can "
                             "start mic/screen recording — do not expose it)")
    parser.add_argument("--port", type=int, default=5200)
    parser.add_argument("--url", default="http://localhost:1234/v1", help="LM Studio API URL")
    parser.add_argument("--text-model", default=None, help="Text model for analysis")
    parser.add_argument("--audio-device", default="0",
                        help="avfoundation audio device index/name (default 0)")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from .server import create_app
    app, socketio = create_app(
        lm_studio_url=args.url,
        text_model=args.text_model,
        audio_device=args.audio_device,
    )
    print("Genie Live Monitor starting on http://%s:%d" % (args.host, args.port))
    socketio.run(app, host=args.host, port=args.port, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
