/* Genie Live Monitor — self-contained SPA (Preact + htm, no build step).
 *
 * All dynamic content is rendered through htm/Preact text interpolation,
 * which creates text nodes — never innerHTML — so transcript / LLM output /
 * question text cannot inject markup (XSS-safe by construction).
 */
(function () {
  "use strict";

  var h = preact.h;
  var render = preact.render;
  var Component = preact.Component;
  var html = htm.bind(h);

  function fmtTime(sec) {
    var s = Math.max(0, Math.floor(sec));
    var m = Math.floor(s / 60);
    return String(m).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }

  async function postJSON(url, body) {
    var resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!resp.ok) {
      throw new Error(url + " -> HTTP " + resp.status);
    }
    return resp.json();
  }

  /* ---------------- transcript panel ---------------- */

  var TranscriptPanel = /** @this */ (function () {
    function TranscriptPanel() {
      Component.apply(this, arguments);
      this.state = { autoScroll: true };
      this.boxRef = null;
      this.onScroll = this.onScroll.bind(this);
    }
    TranscriptPanel.prototype = Object.create(Component.prototype);

    TranscriptPanel.prototype.componentDidUpdate = function () {
      if (this.state.autoScroll && this.boxRef) {
        this.boxRef.scrollTop = this.boxRef.scrollHeight;
      }
    };

    TranscriptPanel.prototype.onScroll = function () {
      var el = this.boxRef;
      if (!el) return;
      var nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      if (nearBottom !== this.state.autoScroll) {
        this.setState({ autoScroll: nearBottom });
      }
    };

    TranscriptPanel.prototype.render = function (props) {
      var self = this;
      var s = props.serverState || {};
      var segs = s.recent_transcript || [];
      var stats =
        "fast " + (s.fast_count || 0) +
        " | refined " + (s.refined_count || 0) +
        " | 總段數 " + (s.transcript_count || 0) +
        " | 畫面 " + (s.frame_count || 0);

      return html`
        <div class="panel">
          <div class="panel-head">
            <h3>即時逐字稿</h3>
            <button class="mini" onClick=${function () {
              self.setState({ autoScroll: !self.state.autoScroll });
            }}>
              ${this.state.autoScroll ? "自動捲動：開" : "自動捲動：暫停"}
            </button>
          </div>
          <div class="stats">${stats}</div>
          <div class="transcript"
               ref=${function (el) { self.boxRef = el; }}
               onScroll=${this.onScroll}>
            ${segs.length === 0
              ? html`<div class="empty">尚無逐字稿。開始錄製後，語音會即時出現在這裡。</div>`
              : segs.map(function (seg, i) {
                  var refined = seg.quality === "refined";
                  return html`
                    <div class="seg ${refined ? "seg-refined" : "seg-fast"}" key=${i}>
                      <span class="time">[${fmtTime(seg.start)}]</span>
                      ${seg.text}
                      <span class="qbadge ${refined ? "qbadge-refined" : "qbadge-fast"}">
                        ${refined ? "refined" : "fast"}
                      </span>
                    </div>`;
                })}
          </div>
        </div>`;
    };
    return TranscriptPanel;
  })();

  /* ---------------- analysis / disputes ---------------- */

  function AnalysisPanel(props) {
    var a = (props.serverState || {}).latest_analysis;
    return html`
      <div class="panel">
        <h3>當前主題與重點</h3>
        ${!a
          ? html`<div class="muted">尚無分析結果（每輪 refined 轉寫後更新）。</div>`
          : html`
            <div>
              <p class="topic">${a.current_topic || "（主題辨識中）"}</p>
              ${a.status ? html`<p class="summary-status">${a.status}</p>` : null}
              ${(a.key_points && a.key_points.length)
                ? html`<ul class="keypoints">
                    ${a.key_points.map(function (p, i) {
                      return html`<li key=${i}>${String(p)}</li>`;
                    })}
                  </ul>`
                : html`<div class="muted">尚無重點摘要。</div>`}
            </div>`}
      </div>`;
  }

  function DisputesPanel(props) {
    var a = (props.serverState || {}).latest_analysis;
    var disputes = (a && a.disputes) || [];
    if (!disputes.length) return null;
    return html`
      <div class="panel">
        <h3>爭議偵測</h3>
        ${disputes.map(function (d, i) {
          return html`
            <div class="dispute" key=${i}>
              <div class="dtopic">${d.topic || "（未命名爭議）"}</div>
              <ul>
                ${(d.positions || []).map(function (p, j) {
                  return html`<li key=${j}>${String(p)}</li>`;
                })}
              </ul>
            </div>`;
        })}
      </div>`;
  }

  /* ---------------- questions ---------------- */

  var QuestionsPanel = (function () {
    function QuestionsPanel() {
      Component.apply(this, arguments);
      this.state = { draft: "" };
    }
    QuestionsPanel.prototype = Object.create(Component.prototype);

    QuestionsPanel.prototype.render = function (props) {
      var self = this;
      var questions = (props.serverState || {}).questions || [];
      var texts = questions.map(function (q) { return q.question; });

      function add() {
        var t = self.state.draft.trim();
        if (!t || texts.indexOf(t) !== -1) return;
        self.setState({ draft: "" });
        props.onSetQuestions(texts.concat([t]));
      }
      function remove(q) {
        props.onSetQuestions(texts.filter(function (t) { return t !== q; }));
      }

      return html`
        <div class="panel">
          <h3>問題清單</h3>
          <div class="qadd">
            <input type="text" placeholder="新增想在會議中確認的問題…"
              value=${this.state.draft}
              onInput=${function (e) { self.setState({ draft: e.target.value }); }}
              onKeyDown=${function (e) { if (e.key === "Enter") add(); }} />
            <button class="secondary" onClick=${add}>新增</button>
          </div>
          ${questions.length === 0
            ? html`<div class="muted">尚無問題。新增後系統會在會議中主動搜集答案。</div>`
            : questions.map(function (q, i) {
                var found = q.status === "found";
                return html`
                  <div class="question ${found ? "found" : ""}" key=${i}>
                    <div class="qrow">
                      <span class="qtext">${q.question}</span>
                      <span class="qstatus ${found ? "found" : "pending"}">
                        ${found ? "answered" : "pending"}
                      </span>
                      <button class="mini" onClick=${function () { remove(q.question); }}>刪除</button>
                    </div>
                    ${q.finding
                      ? html`<div class="finding">${q.finding}</div>`
                      : html`<div class="finding muted">（尚未收集到內容）</div>`}
                  </div>`;
              })}
        </div>`;
    };
    return QuestionsPanel;
  })();

  /* ---------------- root app ---------------- */

  var App = (function () {
    function App() {
      Component.apply(this, arguments);
      this.state = {
        serverState: null,
        socketConnected: false,
        busy: false,
        uiError: null,
      };
      this.socket = null;
      this.pollTimer = null;
    }
    App.prototype = Object.create(Component.prototype);

    App.prototype.componentDidMount = function () {
      var self = this;
      try {
        this.socket = io();
        this.socket.on("connect", function () {
          self.setState({ socketConnected: true });
        });
        this.socket.on("disconnect", function () {
          self.setState({ socketConnected: false });
        });
        this.socket.on("state_update", function (s) {
          self.setState({ serverState: s });
        });
      } catch (e) {
        self.setState({ socketConnected: false });
      }
      this.fetchState();
      // HTTP fallback polling: fast when socket is down, slow heartbeat
      // even when connected (covers missed pushes / capture errors).
      var ticks = 0;
      this.pollTimer = setInterval(function () {
        ticks += 1;
        if (!self.state.socketConnected || ticks % 5 === 0) {
          self.fetchState();
        }
      }, 3000);
    };

    App.prototype.componentWillUnmount = function () {
      if (this.pollTimer) clearInterval(this.pollTimer);
      if (this.socket) this.socket.close();
    };

    App.prototype.fetchState = function () {
      var self = this;
      fetch("/api/state")
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .then(function (s) {
          self.setState({ serverState: s, uiError: null });
        })
        .catch(function (e) {
          self.setState({ uiError: "無法取得伺服器狀態：" + e.message });
        });
    };

    App.prototype.action = function (fn) {
      var self = this;
      this.setState({ busy: true, uiError: null });
      Promise.resolve()
        .then(fn)
        .catch(function (e) {
          self.setState({ uiError: String(e && e.message ? e.message : e) });
        })
        .then(function () {
          self.setState({ busy: false });
          self.fetchState();
        });
    };

    App.prototype.render = function () {
      var self = this;
      var s = this.state.serverState || {};
      var capture = s.capture || {};
      var recording = !!capture.recording;
      var captureError = capture.error || null;

      var connBadge = this.state.socketConnected
        ? html`<span class="badge badge-ok">即時連線</span>`
        : (this.state.serverState
            ? html`<span class="badge badge-warn">HTTP 輪詢中</span>`
            : html`<span class="badge badge-off">離線</span>`);

      var recBadge = captureError
        ? html`<span class="badge badge-danger">錄製錯誤</span>`
        : (recording
            ? html`<span class="badge badge-ok">錄製中</span>`
            : html`<span class="badge badge-off">未錄製</span>`);

      function currentQuestionTexts() {
        return ((self.state.serverState || {}).questions || [])
          .map(function (q) { return q.question; });
      }

      return html`
        <div>
          <div class="topbar">
            <h1>Genie Live Monitor</h1>
            ${connBadge}
            ${recBadge}
            <span class="spacer"></span>
            <button disabled=${this.state.busy || recording}
              onClick=${function () {
                self.action(function () {
                  return postJSON("/api/start", { questions: currentQuestionTexts() });
                });
              }}>開始錄製</button>
            <button class="secondary" disabled=${this.state.busy || !recording}
              onClick=${function () {
                self.action(function () { return postJSON("/api/stop"); });
              }}>停止錄製</button>
          </div>

          ${captureError
            ? html`<div class="error-banner">錄製失敗：${captureError}</div>`
            : null}
          ${this.state.uiError
            ? html`<div class="error-banner">${this.state.uiError}</div>`
            : null}

          <div class="layout">
            <div>
              <${TranscriptPanel} serverState=${this.state.serverState} />
            </div>
            <div>
              <${AnalysisPanel} serverState=${this.state.serverState} />
              <${DisputesPanel} serverState=${this.state.serverState} />
              <${QuestionsPanel}
                serverState=${this.state.serverState}
                onSetQuestions=${function (list) {
                  self.action(function () {
                    return postJSON("/api/questions", { questions: list });
                  });
                }} />
            </div>
          </div>
        </div>`;
    };
    return App;
  })();

  render(h(App, null), document.getElementById("root"));
})();
