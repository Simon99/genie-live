from __future__ import annotations

import argparse


def main():
    parser = argparse.ArgumentParser(description="Genie Live Meeting Monitor")
    parser.add_argument("--port", type=int, default=5200)
    parser.add_argument("--url", default="http://localhost:1234/v1", help="LM Studio API URL")
    parser.add_argument("--text-model", default=None, help="Text model for analysis")
    parser.add_argument("--vision-model", default=None, help="Vision model for screen analysis")
    parser.add_argument("--audio-device", default="default", help="Audio input device")

    args = parser.parse_args()

    from .server import create_app
    app, socketio = create_app(
        lm_studio_url=args.url,
        text_model=args.text_model,
        vision_model=args.vision_model,
        audio_device=args.audio_device,
    )
    print("Genie Live Monitor starting on http://localhost:%d" % args.port)
    socketio.run(app, host="0.0.0.0", port=args.port, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
