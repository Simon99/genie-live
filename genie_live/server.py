from __future__ import annotations

import json
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO

from .monitor import MeetingMonitor
from .capture import AudioCapture, ScreenCapture


def create_app(
    lm_studio_url: str = "http://localhost:1234/v1",
    text_model: str = None,
    vision_model: str = None,
    audio_device: str = "default",
) -> tuple:
    app = Flask(__name__)
    CORS(app)
    socketio = SocketIO(app, cors_allowed_origins="*")

    monitor = MeetingMonitor(
        lm_studio_url=lm_studio_url,
        text_model=text_model,
        vision_model=vision_model,
    )

    audio_capture = None
    screen_capture = None

    def on_state_update(state):
        socketio.emit("state_update", state)

    monitor.on_update(on_state_update)

    @app.route("/api/start", methods=["POST"])
    def start_monitoring():
        nonlocal audio_capture, screen_capture

        data = request.get_json() or {}
        questions = data.get("questions", [])
        monitor.set_questions(questions)

        device = data.get("audio_device", audio_device)

        audio_capture = AudioCapture(device=device, chunk_seconds=10)
        audio_capture.on_chunk(monitor.add_transcript_chunk)

        screen_capture = ScreenCapture(interval=10.0)
        screen_capture.on_frame(monitor.add_screen_frame)

        audio_capture.start()
        screen_capture.start()

        return jsonify({"status": "started"})

    @app.route("/api/stop", methods=["POST"])
    def stop_monitoring():
        nonlocal audio_capture, screen_capture
        if audio_capture:
            audio_capture.stop()
        if screen_capture:
            screen_capture.stop()
        return jsonify({"status": "stopped"})

    @app.route("/api/state", methods=["GET"])
    def get_state():
        return jsonify(monitor.get_state())

    @app.route("/api/questions", methods=["POST"])
    def update_questions():
        data = request.get_json()
        monitor.set_questions(data.get("questions", []))
        return jsonify({"status": "updated"})

    @app.route("/")
    def index():
        return _fallback_ui()

    return app, socketio


def _fallback_ui():
    return """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Genie Live Monitor</title>
<style>
*{box-sizing:border-box} body{font-family:sans-serif;margin:0;padding:20px;background:#1a1a2e;color:#eee}
h1{color:#e94560} .panel{background:#16213e;padding:20px;border-radius:10px;margin:10px 0}
.status{font-size:1.2em;color:#0f3460} .transcript{max-height:300px;overflow-y:auto;font-size:0.9em}
.time{color:#e94560;font-family:monospace} .fast{opacity:0.6;font-style:italic} .refined{opacity:1;font-weight:500}
.badge{font-size:0.7em;padding:1px 5px;border-radius:3px;margin-left:5px} .badge-fast{background:#e67e22;color:#fff} .badge-ok{background:#27ae60;color:#fff}
.stats{font-size:0.85em;color:#888;margin-bottom:10px} .question{padding:5px;margin:5px 0;border-left:3px solid #3498db}
.found{border-color:#27ae60} .dispute{background:#553;padding:10px;border-radius:5px;margin:5px 0}
button{padding:10px 20px;background:#e94560;color:white;border:none;border-radius:5px;cursor:pointer;margin:5px}
button:hover{background:#c0392b} input,textarea{width:100%;padding:8px;margin:5px 0;border-radius:5px;border:1px solid #333;background:#0f3460;color:#eee}
</style></head><body>
<h1>Genie Live Monitor</h1>
<div class="panel">
<button onclick="startMonitor()">Start</button>
<button onclick="stopMonitor()">Stop</button>
<button onclick="refreshState()">Refresh</button>
</div>
<div class="panel">
<h3>Questions Checklist</h3>
<textarea id="questions" rows="4" placeholder="One question per line..."></textarea>
<button onclick="updateQuestions()">Update Questions</button>
<div id="question-status"></div>
</div>
<div class="panel">
<h3>Current Status</h3>
<div id="analysis"></div>
</div>
<div class="panel">
<h3>Live Transcript</h3>
<div id="transcript" class="transcript"></div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.4/socket.io.min.js"></script>
<script>
const socket=io();
socket.on('state_update',d=>renderState(d));
function renderState(s){
  let stats='Fast: '+(s.fast_count||0)+' | Refined: '+(s.refined_count||0)+' | Total: '+(s.transcript_count||0)+' | Frames: '+(s.frame_count||0);
  let t='<div class="stats">'+stats+'</div>';
  (s.recent_transcript||[]).forEach(seg=>{
    const m=Math.floor(seg.start/60),sc=Math.floor(seg.start%60);
    const cls=seg.quality==='refined'?'refined':'fast';
    const badge=seg.quality==='refined'?'<span class="badge badge-ok">refined</span>':'<span class="badge badge-fast">fast</span>';
    t+='<div class="'+cls+'"><span class="time">['+String(m).padStart(2,'0')+':'+String(sc).padStart(2,'0')+']</span> '+seg.text+badge+'</div>';
  });
  document.getElementById('transcript').innerHTML=t;
  const a=s.latest_analysis;
  if(a){
    let h='<p class="status">Topic: '+(a.current_topic||'...')+'</p>';
    h+='<p>'+( a.status||'')+'</p>';
    (a.key_points||[]).forEach(p=>{h+='<li>'+p+'</li>';});
    (a.disputes||[]).forEach(d=>{h+='<div class="dispute">Dispute: '+d.topic+'<ul>';(d.positions||[]).forEach(p=>{h+='<li>'+p+'</li>';});h+='</ul></div>';});
    document.getElementById('analysis').innerHTML=h;
  }
  let qh='';
  (s.questions||[]).forEach(q=>{
    const cls=q.status==='found'?'question found':'question';
    qh+='<div class="'+cls+'"><strong>'+q.question+'</strong><br>'+( q.finding||'Pending...')+'</div>';
  });
  document.getElementById('question-status').innerHTML=qh;
}
async function startMonitor(){
  const qs=document.getElementById('questions').value.split('\\n').filter(q=>q.trim());
  await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({questions:qs})});
}
async function stopMonitor(){await fetch('/api/stop',{method:'POST'});}
async function refreshState(){const r=await fetch('/api/state');renderState(await r.json());}
async function updateQuestions(){
  const qs=document.getElementById('questions').value.split('\\n').filter(q=>q.trim());
  await fetch('/api/questions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({questions:qs})});
}
</script></body></html>"""
