"use strict";
// Jarvis HUD: WebGL orb + live status, fed by Bridge.poll() (see jarvis/hud/__init__.py).

const STATES = {
  starting:  { label: "Starting",     sub: "Loading voice models…",                         c1: [0.12, 0.15, 0.24], c2: [0.45, 0.52, 0.68], swirl: 0.2, energy: 0.2 },
  idle:      { label: "Detecting",    sub: "Say “Hey Jarvis” or press Ctrl+Alt+J",          c1: [0.04, 0.22, 0.85], c2: [0.30, 0.82, 1.00], swirl: 0.35, energy: 0.4 },
  listening: { label: "Listening",    sub: "Go ahead, I'm listening…",                     c1: [0.00, 0.50, 0.70], c2: [0.45, 1.00, 0.94], swirl: 0.8, energy: 0.9 },
  thinking:  { label: "Thinking",     sub: "Working out what to do…",                      c1: [0.30, 0.08, 0.80], c2: [0.85, 0.45, 1.00], swirl: 1.7, energy: 1.0 },
  working:   { label: "Working",      sub: "",                                             c1: [0.72, 0.32, 0.02], c2: [1.00, 0.80, 0.30], swirl: 1.3, energy: 0.9 },
  speaking:  { label: "Speaking",     sub: "",                                             c1: [0.03, 0.50, 0.26], c2: [0.45, 1.00, 0.62], swirl: 0.7, energy: 0.8 },
  question:  { label: "Your answer?", sub: "",                                             c1: [0.80, 0.36, 0.04], c2: [1.00, 0.74, 0.36], swirl: 0.5, energy: 0.7 },
  muted:     { label: "Mic off",      sub: "Type a command, or turn the mic back on",       c1: [0.12, 0.14, 0.20], c2: [0.42, 0.47, 0.58], swirl: 0.12, energy: 0.15 },
  error:     { label: "Error",        sub: "Something went wrong",                          c1: [0.70, 0.04, 0.08], c2: [1.00, 0.40, 0.42], swirl: 0.9, energy: 0.8 },
};

const $ = (id) => document.getElementById(id);
const ui = { state: "starting", level: 0, targetLevel: 0, mode: "compact", busy: false, question: null };

// ---------------------------------------------------------------- WebGL orb
const VERT = "attribute vec2 p; void main(){ gl_Position = vec4(p, 0.0, 1.0); }";
const FRAG = `
precision highp float;
uniform vec2 uRes; uniform float uTime, uLevel, uSwirl, uEnergy; uniform vec3 uC1, uC2, uBg;
float hash(vec3 p){ p = fract(p * 0.3183099 + 0.1); p *= 17.0; return fract(p.x * p.y * p.z * (p.x + p.y + p.z)); }
float noise(vec3 x){
  vec3 i = floor(x), f = fract(x); f = f * f * (3.0 - 2.0 * f);
  return mix(mix(mix(hash(i), hash(i + vec3(1,0,0)), f.x), mix(hash(i + vec3(0,1,0)), hash(i + vec3(1,1,0)), f.x), f.y),
             mix(mix(hash(i + vec3(0,0,1)), hash(i + vec3(1,0,1)), f.x), mix(hash(i + vec3(0,1,1)), hash(i + vec3(1,1,1)), f.x), f.y), f.z);
}
float fbm(vec3 p){ float a = 0.5, s = 0.0; for (int i = 0; i < 4; i++){ s += a * noise(p); p *= 2.03; a *= 0.5; } return s; }
mat2 rot(float a){ float c = cos(a), s = sin(a); return mat2(c, -s, s, c); }
float field(vec3 p){
  vec3 q = p; q.xz *= rot(uTime * uSwirl * 0.55); q.xy *= rot(uTime * uSwirl * 0.27);
  return fbm(q * 2.3 + vec3(0.0, uTime * 0.12 * uSwirl, 0.0));
}
float map(vec3 p){
  float r = 0.60 + 0.09 * uEnergy * (field(p) - 0.5) + 0.07 * uLevel * sin(11.0 * p.y + uTime * 9.0);
  return length(p) - r;
}
vec3 normalAt(vec3 p){
  vec2 e = vec2(0.002, 0.0);
  return normalize(vec3(map(p + e.xyy) - map(p - e.xyy), map(p + e.yxy) - map(p - e.yxy), map(p + e.yyx) - map(p - e.yyx)));
}
void main(){
  vec2 uv = (gl_FragCoord.xy * 2.0 - uRes) / min(uRes.x, uRes.y);
  vec3 ro = vec3(0.0, 0.0, 2.2), rd = normalize(vec3(uv * 0.95, -1.75));
  float t = 0.0, md = 10.0; bool hit = false;
  for (int i = 0; i < 56; i++){
    vec3 p = ro + rd * t; float d = map(p); md = min(md, d);
    if (d < 0.0015){ hit = true; break; }
    t += d * 0.75; if (t > 4.0) break;
  }
  vec3 col = uBg + mix(uC1, uC2, 0.55) * exp(-md * 7.5) * (0.55 + 0.7 * uLevel);
  if (hit){
    vec3 p = ro + rd * t, n = normalAt(p), l = normalize(vec3(-0.5, 0.7, 0.8));
    float dif = max(dot(n, l), 0.0), fre = pow(1.0 - max(dot(n, -rd), 0.0), 3.0);
    float f = field(p * 1.35);
    vec3 inner = mix(uC1, uC2, smoothstep(0.25, 0.8, f));
    col = inner * (0.22 + 0.78 * dif) + uC2 * fre * 1.5 + pow(max(dot(reflect(-l, n), -rd), 0.0), 26.0) * 0.55;
    col += uC2 * smoothstep(0.6, 0.7, f) * (0.5 + uEnergy);
  }
  col *= 1.0 - 0.06 * dot(uv, uv);
  gl_FragColor = vec4(pow(col, vec3(0.92)), 1.0);
}`;

const orb = (() => {
  const canvas = $("orb");
  const gl = canvas.getContext("webgl", { antialias: true, premultipliedAlpha: false });
  if (!gl) return { frame() {} };
  const shader = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) console.error(gl.getShaderInfoLog(s)); return s; };
  const prog = gl.createProgram();
  gl.attachShader(prog, shader(gl.VERTEX_SHADER, VERT));
  gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, FRAG));
  gl.linkProgram(prog); gl.useProgram(prog);
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, "p");
  gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  const u = (n) => gl.getUniformLocation(prog, n);
  const U = { res: u("uRes"), time: u("uTime"), level: u("uLevel"), swirl: u("uSwirl"), energy: u("uEnergy"), c1: u("uC1"), c2: u("uC2"), bg: u("uBg") };
  const cur = { c1: [...STATES.starting.c1], c2: [...STATES.starting.c2], swirl: 0.2, energy: 0.2, phase: 0 };
  const mix = (a, b, k) => a + (b - a) * k;
  let last = performance.now();

  return {
    frame(now) {
      const dt = Math.min(0.05, (now - last) / 1000); last = now;
      const target = STATES[ui.state] || STATES.idle, k = 1 - Math.exp(-dt * 4);
      for (let i = 0; i < 3; i++) { cur.c1[i] = mix(cur.c1[i], target.c1[i], k); cur.c2[i] = mix(cur.c2[i], target.c2[i], k); }
      cur.swirl = mix(cur.swirl, target.swirl, k); cur.energy = mix(cur.energy, target.energy, k);
      cur.phase += dt * (0.6 + cur.swirl);  // integrate speed so swirl changes never jump
      const speak = ui.state === "speaking" ? 0.45 + 0.3 * Math.sin(now / 95) * Math.sin(now / 230) : 0;
      ui.level = mix(ui.level, Math.max(ui.targetLevel, speak), 1 - Math.exp(-dt * 12));
      const dpr = window.devicePixelRatio || 1, w = Math.round(canvas.clientWidth * dpr), h = Math.round(canvas.clientHeight * dpr);
      if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; gl.viewport(0, 0, w, h); }
      gl.uniform2f(U.res, w, h); gl.uniform1f(U.time, cur.phase); gl.uniform1f(U.level, ui.level);
      gl.uniform1f(U.swirl, 1.0); gl.uniform1f(U.energy, cur.energy);
      gl.uniform3fv(U.c1, cur.c1); gl.uniform3fv(U.c2, cur.c2); gl.uniform3fv(U.bg, [0.024, 0.04, 0.086]);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    },
  };
})();

function loop(now) { orb.frame(now); requestAnimationFrame(loop); }
requestAnimationFrame(loop);

// ---------------------------------------------------------------- state & feed
function setState(state, detail) {
  ui.state = STATES[state] ? state : "idle";
  document.body.className = document.body.className.replace(/state-\S+/, "state-" + ui.state);
  $("state").textContent = STATES[ui.state].label;
  $("detail").textContent = detail || STATES[ui.state].sub;
  $("detail").title = detail || "";
  if (["idle", "muted"].includes(ui.state)) setActivity(null);
}

function setActivity(text) {
  ui.busy = !!text;
  $("activity").classList.toggle("busy", ui.busy);
  if (text) { $("activity-text").textContent = text; $("activity").title = text; }
}

function addFeed(cls, text, who) {
  const feed = $("feed");
  const el = document.createElement("div");
  el.className = cls;
  if (who) { const w = document.createElement("span"); w.className = "who"; w.textContent = who; el.appendChild(w); }
  el.appendChild(document.createTextNode(text));
  feed.appendChild(el);
  while (feed.children.length > 80) feed.removeChild(feed.firstChild);
  feed.scrollTop = feed.scrollHeight;
}

function showQuestion(text, kind) {
  ui.question = text ? kind : null;
  $("question").hidden = !text;
  $("qtext").textContent = text || "";
  $("qbtns").style.display = kind === "confirm" ? "flex" : "none";
  $("input").placeholder = text ? (kind === "confirm" ? "Or type yes / no…" : "Type your answer…") : "Type a command…";
  if (text && ui.mode !== "full") setMode("full");
}

function handle(e) {
  switch (e.kind) {
    case "state": setState(e.state, e.detail); if (e.state === "working") setActivity(e.detail); break;
    case "heard": addFeed("msg you", e.text, e.typed ? "You (typed)" : "You"); setActivity(null); $("activity-text").textContent = "“" + e.text + "”"; break;
    case "say": addFeed("msg jarvis", e.text, "Jarvis"); setActivity(null); $("activity-text").textContent = e.text; break;
    case "tool": if (e.phase === "start") { setActivity(e.label); addFeed("log run", e.label); }
                 else { addFeed("log " + (e.ok ? "ok" : "fail"), e.result || e.label); setActivity(null); } break;
    case "step": setActivity(e.text); addFeed("log run", "step " + e.step + " · " + e.text); break;
    case "question": showQuestion(e.text, e.qtype); break;
    case "question_done": showQuestion(null); break;
    case "error": addFeed("log err", e.text); setState("error", e.text); break;
  }
}

// ---------------------------------------------------------------- bridge (pywebview, or demo when opened in a browser)
let api = null, lastSeq = 0;

async function poll() {
  try {
    const r = await api.poll(lastSeq);
    for (const e of r.events) { lastSeq = Math.max(lastSeq, e.seq); handle(e); }
    ui.targetLevel = r.level;
    $("btn-mic").classList.toggle("off", r.mic_muted);
    $("btn-voice").classList.toggle("off", r.voice_muted);
  } catch (err) { console.error(err); }
  setTimeout(poll, 90);
}

async function setMode(mode) {
  ui.mode = mode;
  document.body.className = document.body.className.replace(/mode-\S+/, "mode-" + mode);
  $("btn-size").title = mode === "full" ? "Shrink" : "Expand";
  $("btn-size").firstElementChild.innerHTML = mode === "full" ? '<path d="M7 10l5 5 5-5"/>' : '<path d="M7 14l5-5 5 5"/>';
  if (api) await api.set_mode(mode);
  $("feed").scrollTop = $("feed").scrollHeight;
}

function demoApi() {  // lets the HUD be previewed in a normal browser with a scripted conversation
  const script = [["state", { state: "idle" }], ["state", { state: "listening" }], ["heard", { text: "Open Chrome and play lofi music" }],
    ["state", { state: "thinking" }], ["tool", { phase: "start", label: "browser task play lofi music on YouTube" }],
    ["state", { state: "working", detail: "browser task play lofi music on YouTube" }], ["step", { step: 1, text: "navigate: opened youtube.com" }],
    ["step", { step: 2, text: "type: typed 'lofi music' into Search" }], ["tool", { phase: "end", ok: true, result: "Started playing lofi beats" }],
    ["say", { text: "Lofi music is playing on YouTube now." }], ["state", { state: "speaking" }],
    ["question", { text: "Should I close Notepad too?", qtype: "confirm" }], ["state", { state: "question" }],
    ["question_done", {}], ["state", { state: "idle" }]];
  let seq = 0, i = 0, t0 = performance.now();
  return {
    poll: async () => {
      const due = Math.floor((performance.now() - t0) / 1600);
      const events = [];
      while (i <= due && i < script.length) { const [kind, data] = script[i++]; events.push({ seq: ++seq, kind, ...data }); }
      if (i >= script.length && due > script.length + 2) { i = 0; t0 = performance.now(); }
      return { events, level: ui.state === "listening" ? 0.5 + 0.4 * Math.random() : 0.05, mic_muted: false, voice_muted: false };
    },
    set_mode: async () => {}, send: async () => {}, talk: async () => {}, answer: async () => {}, stop: async () => {},
    mute_mic: async () => {}, mute_voice: async () => {}, move_by: async () => {}, save_position: async () => {},
    close: async () => {}, get_mode: async () => new URLSearchParams(location.search).get("mode") || "compact",
  };
}

async function connect(realApi) {
  api = realApi;
  await setMode(await api.get_mode());
  poll();
}

window.addEventListener("pywebviewready", () => { if (!api) connect(window.pywebview.api); });
if (new URLSearchParams(location.search).has("demo")) connect(demoApi());  // preview in a normal browser: index.html?demo

// ---------------------------------------------------------------- interactions
$("composer").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const text = $("input").value.trim();
  if (!text || !api) return;
  if (ui.question === "confirm" && /^(y|yes|haan|ha|ok)$/i.test(text)) api.answer(true);
  else if (ui.question === "confirm" && /^(n|no|nahi|na)$/i.test(text)) api.answer(false);
  else api.send(text);
  $("input").value = "";
});
$("btn-talk").onclick = () => api && api.talk();
$("btn-yes").onclick = () => api && api.answer(true);
$("btn-no").onclick = () => api && api.answer(false);
$("btn-stop").onclick = () => api && api.stop();
$("btn-close").onclick = () => api && api.close();
$("btn-orb").onclick = () => setMode("orb");
$("btn-size").onclick = () => setMode(ui.mode === "full" ? "compact" : "full");
$("btn-mic").onclick = () => api && api.mute_mic(!$("btn-mic").classList.contains("off"));
$("btn-voice").onclick = () => api && api.mute_voice(!$("btn-voice").classList.contains("off"));
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && api) api.stop(); });

// Drag the window by the orb or the header. Clicks still work: a drag only starts after 4 px of movement.
let drag = null, clickTimer = null;
document.querySelectorAll(".drag").forEach((el) => el.addEventListener("mousedown", (ev) => {
  if (ev.button !== 0 || ev.target.closest("button, input")) return;
  drag = { x: ev.screenX, y: ev.screenY, moved: false, pending: [0, 0], raf: 0 };
}));
window.addEventListener("mousemove", (ev) => {
  if (!drag) return;
  const dx = ev.screenX - drag.x, dy = ev.screenY - drag.y;
  if (!drag.moved && Math.hypot(dx, dy) < 4) return;
  drag.moved = true; drag.x = ev.screenX; drag.y = ev.screenY;
  drag.pending[0] += dx; drag.pending[1] += dy;
  if (!drag.raf) drag.raf = requestAnimationFrame(() => {
    if (!drag) return;
    const [mx, my] = drag.pending; drag.pending = [0, 0]; drag.raf = 0;
    api && api.move_by(mx, my);
  });
});
window.addEventListener("mouseup", () => {
  if (drag && drag.moved && api) api.save_position();
  setTimeout(() => (drag = null), 0);
});
$("orbwrap").addEventListener("click", () => {
  if (drag && drag.moved) return;
  clearTimeout(clickTimer);
  clickTimer = setTimeout(() => api && api.talk(), 230);  // single click: talk
});
$("orbwrap").addEventListener("dblclick", () => {
  clearTimeout(clickTimer);  // double click: resize
  setMode(ui.mode === "orb" ? "compact" : ui.mode === "compact" ? "full" : "compact");
});
