"use strict";
// Persona 5, Sprint 2. Se carga después de app.js y usa sus utilidades globales ($, api, esc, fmt, date, state, chosen, tab,
// notice, options, switchChild, admin, canOperate). app.js llama a p5Render() al final de render() y a p5Reset() al cerrar sesión.
// Cubre SCRUM-50 (biberón), SCRUM-52 (recordatorios), SCRUM-43 B (galería de fotos) y SCRUM-31 B (voz en la app).
let feedEpoch = 0, photoEpoch = 0, photos = [], photoNext = null, photoFor = null, bottleFor = null, bottleDirty = false, call = null;

const setMsg = (id, text, error = false) => { $(id).textContent = text; $(id).classList.toggle("error", error); };
const minutesText = m => m % 60 === 0 ? (m / 60) + " h" : m < 60 ? m + " min" : Math.floor(m / 60) + " h " + (m % 60) + " min";

function p5Reset() {
  feedEpoch++; photoEpoch++; photos = []; photoNext = null; photoFor = null; bottleFor = null; bottleDirty = false;
  $("photo-grid").replaceChildren(); $("photo-more").hidden = true;
  voiceHang(false);
}
function p5Render() {
  const child = state.children.find(c => c.id === state.selected);
  for (const id of ["feeding-selector", "photo-selector"]) $(id).innerHTML = options(state.children, chosen);
  $("feeding-box").hidden = $("photo-box").hidden = !child;
  $("feeding-empty").hidden = $("photo-empty").hidden = !!child;
  $("feeding-panel").hidden = $("photo-panel").hidden = !child;
  if (child && tab === "feeding") loadFeeding();
  if (child && tab === "photos") loadPhotos(false);
  voiceRefresh();
}
$("feeding-selector").onchange = e => switchChild(e.target.value);
$("photo-selector").onchange = e => switchChild(e.target.value);

// --- SCRUM-50: temperatura del biberón ------------------------------------------------------------------------------
const BOTTLE = {normal: ["En rango seguro", "ok"], caliente: ["Muy caliente", "hot"], fria: ["Muy frío", "cold"], sin_lectura: ["Sin lecturas", "idle"]};
async function loadFeeding() {
  const id = chosen, marker = ++feedEpoch, query = "?child=" + encodeURIComponent(id);
  try {
    const [bottle, reminders] = await Promise.all([api("/api/bottle" + query), api("/api/reminders" + query)]);
    if (marker !== feedEpoch || id !== chosen) return;
    renderBottle(bottle); renderReminders(reminders);
  } catch (err) { if (marker === feedEpoch) notice(err.message, true); }
}
function renderBottle(b) {
  const stale = b.reading && !b.fresh;
  const [label, cls] = stale ? ["Sin lectura reciente", "idle"] : BOTTLE[b.status];
  $("bottle-status").textContent = label; $("bottle-status").className = "status " + cls;
  $("bottle-value").textContent = b.reading ? fmt(b.reading.value) + " °C" : "—";
  $("bottle-detail").textContent = (b.reading ? "Última lectura: " + date(b.reading.captured_at) + " · " : "Aún no llegan lecturas de TemperaturaBiberon. · ") +
    "Rango seguro " + fmt(b.temp_min) + " a " + fmt(b.temp_max) + " °C · objetivo " + fmt(b.target) + " °C" + (b.default ? " (valores por defecto)" : "");
  // Alerta visual (RF-ALI-01 CA2): solo con una lectura reciente fuera de rango. El texto siempre acompaña al color.
  const alerta = !stale && b.status === "caliente" ? "⚠ El biberón está muy caliente: puede quemar al bebé. Déjalo enfriar."
    : !stale && b.status === "fria" ? "⚠ El biberón está muy frío para dárselo al bebé. Tíbialo un poco." : "";
  $("bottle-alert").textContent = alerta; $("bottle-alert").hidden = !alerta;
  if (bottleFor !== chosen || !bottleDirty) { $("bottle-min").value = b.temp_min; $("bottle-max").value = b.temp_max; bottleFor = chosen; bottleDirty = false; }
  for (const id of ["bottle-min", "bottle-max"]) $(id).disabled = !admin();
  $("bottle-save").hidden = !admin();
  if (!admin()) setMsg("bottle-msg", "Solo un administrador puede cambiar los límites.");
}
$("bottle-form").addEventListener("input", () => { bottleDirty = true; });
$("bottle-form").onsubmit = async ev => {
  ev.preventDefault();
  const min = $("bottle-min").value.trim(), max = $("bottle-max").value.trim();
  if (!min || !max || !Number.isFinite(Number(min)) || !Number.isFinite(Number(max))) return setMsg("bottle-msg", "Escribe el mínimo y el máximo como números.", true);
  $("bottle-save").disabled = true;
  try {
    const saved = await api("/api/bottle/thresholds", {child_id: chosen, temp_min: Number(min), temp_max: Number(max)});
    bottleDirty = false; setMsg("bottle-msg", "Límites guardados: " + fmt(saved.temp_min) + " a " + fmt(saved.temp_max) + " °C."); loadFeeding();
  } catch (err) { setMsg("bottle-msg", err.message, true); }
  finally { $("bottle-save").disabled = false; }
};

// --- SCRUM-52: recordatorios de toma --------------------------------------------------------------------------------
function renderReminders(data) {
  const ops = canOperate();
  $("reminders-list").innerHTML = data.reminders.map(r => '<div class="row"><div><h3>' +
    esc(r.kind === "interval" ? "Cada " + minutesText(r.interval_min) : "A las " + r.times.join(", ")) +
    '</h3><small>' + (r.enabled ? "Próximo aviso: " + esc(date(r.next_due_at)) : "Desactivado") + '</small></div><div class="rem-actions">' +
    '<button type="button" class="secondary" data-rem="toggle" data-id="' + esc(r.id) + '"' + (ops ? "" : " disabled") + '>' + (r.enabled ? "Desactivar" : "Activar") + '</button>' +
    '<button type="button" class="quiet" data-rem="delete" data-id="' + esc(r.id) + '"' + (ops ? "" : " disabled") + '>Eliminar</button></div></div>').join("") ||
    '<p class="empty">Aún no hay recordatorios para este bebé.</p>';
  for (const id of ["rem-kind", "rem-interval", "rem-times", "rem-add"]) $(id).disabled = !ops;
  if (!ops) setMsg("rem-msg", "Necesitas permiso para operar dispositivos para crear o cambiar recordatorios.");
  remKindChanged(); remList = data.reminders;
}
let remList = [];
function remKindChanged() { const fixed = $("rem-kind").value === "fixed"; $("rem-interval-box").hidden = fixed; $("rem-times-box").hidden = !fixed; }
$("rem-kind").onchange = remKindChanged;
async function remSend(body) {
  try { renderReminders(await api("/api/reminders", {child_id: chosen, ...body})); return true; }
  catch (err) { setMsg("rem-msg", err.message, true); return false; }
}
$("rem-form").onsubmit = async ev => {
  ev.preventDefault();
  let body;
  if ($("rem-kind").value === "interval") {
    const m = Number($("rem-interval").value);
    if (!Number.isInteger(m) || m < 15 || m > 1440) return setMsg("rem-msg", "El intervalo va de 15 minutos a 24 horas (1440 min).", true);
    body = {action: "save", kind: "interval", interval_min: m};
  } else {
    const times = $("rem-times").value.split(/[,\s]+/).filter(Boolean);
    if (!times.length || !times.every(t => /^([01]\d|2[0-3]):[0-5]\d$/.test(t))) return setMsg("rem-msg", "Escribe horas con formato HH:MM de 24 horas, por ejemplo 08:00, 11:00.", true);
    body = {action: "save", kind: "fixed", times};
  }
  $("rem-add").disabled = true;
  if (await remSend(body)) { setMsg("rem-msg", "Recordatorio guardado."); $("rem-times").value = ""; }
  $("rem-add").disabled = !canOperate();
};
$("reminders-list").onclick = async ev => {
  const btn = ev.target.closest("button[data-rem]");
  if (!btn) return;
  const r = remList.find(x => x.id === btn.dataset.id);
  if (!r) return;
  if (btn.dataset.rem === "delete") { if (await remSend({action: "delete", id: r.id})) setMsg("rem-msg", "Recordatorio eliminado."); }
  else if (await remSend({action: "save", id: r.id, kind: r.kind, interval_min: r.interval_min, times: r.times, enabled: !r.enabled})) setMsg("rem-msg", "Recordatorio actualizado.");
};

// --- SCRUM-43 B: galería de fotos -----------------------------------------------------------------------------------
async function loadPhotos(more, force = false) {
  const id = chosen, marker = ++photoEpoch;
  let query = "?limit=24&child=" + encodeURIComponent(id);
  if (more && photoNext) query += "&before=" + encodeURIComponent(photoNext);
  try {
    const data = await api("/api/photos" + query);
    if (marker !== photoEpoch || id !== chosen) return;
    if (more) photos = photos.concat(data.items);
    else {
      const unchanged = !force && photoFor === id && photos[0]?.id === data.items[0]?.id && photos.length >= data.items.length;
      if (unchanged) { photoNext = photoNext ?? data.next; return; }  // misma galería: no se redibuja (evita parpadeos en cada sincronización)
      photos = data.items; photoFor = id;
    }
    photoNext = more ? data.next : (data.next ?? null);
    drawPhotos();
  } catch (err) { if (marker === photoEpoch) setMsg("photo-msg", err.message, true); }
}
function drawPhotos() {
  const grid = $("photo-grid");
  grid.replaceChildren(...photos.map(p => {
    const button = document.createElement("button");
    button.type = "button"; button.className = "photo-item";
    const img = document.createElement("img");
    img.src = p.url; img.loading = "lazy"; img.alt = "Foto capturada el " + date(p.captured_at);
    img.onerror = () => setMsg("photo-msg", "No se pudo cargar una imagen (el enlace pudo vencer). Pulsa Actualizar.", true);
    const label = document.createElement("span");
    label.textContent = date(p.captured_at) + " · " + p.device_id;
    button.append(img, label);
    button.onclick = () => openPhoto(p);
    return button;
  }));
  $("photo-more").hidden = !photoNext;
  setMsg("photo-msg", photos.length ? "" : "Aún no hay fotos. Aparecerán cuando el kit de seguridad capture una.");
}
function openPhoto(p) {
  $("photo-title").textContent = "Foto del " + date(p.captured_at);
  $("photo-big").src = p.url;
  $("photo-cap").textContent = "Dispositivo " + p.device_id + " · " + fmt(Math.round(p.size_bytes / 1024)) + " KB";
  $("photo-dialog").showModal();
}
$("photo-close").onclick = () => $("photo-dialog").close();
$("photo-more").onclick = () => loadPhotos(true);
$("photo-refresh").onclick = () => loadPhotos(false, true);

// --- SCRUM-31 B: llamada de voz bidireccional (lado app) ---------------------------------------------------------------
// WebRTC directo con la cuna. El servidor solo reenvía oferta, respuesta y candidatos (/api/voice/signal y /poll).
const voiceMsg = (text, error = false) => setMsg("voice-msg", text, error);
function voiceRefresh() {
  const btn = $("voice-btn");
  btn.textContent = call ? "Colgar" : "Llamada de voz";
  btn.disabled = call ? false : !canOperate();
  if (!call && !canOperate()) voiceMsg("Necesitas permiso para operar dispositivos para llamar a la cuna.");
}
const signal = (type, data) => api("/api/voice/signal", {child_id: call.child, role: "app", type, data});
$("voice-btn").onclick = () => call ? voiceHang(true) : voiceStart();
async function voiceStart() {
  if (!navigator.mediaDevices?.getUserMedia || !window.RTCPeerConnection) return voiceMsg("Este navegador no permite audio en vivo aquí. Usa localhost o HTTPS.", true);
  const child = chosen;
  $("voice-btn").disabled = true; voiceMsg("Pidiendo acceso al micrófono…");
  let stream, pc;
  try { stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true}}); }
  catch { voiceRefresh(); return voiceMsg("No se pudo usar el micrófono. Revisa el permiso del navegador.", true); }
  try {
    const cfg = await api("/api/voice/config");
    pc = new RTCPeerConnection({iceServers: cfg.ice_servers});
    call = {child, pc, stream, last: 0, closed: false, queue: [], offered: false, connected: false};
    stream.getTracks().forEach(t => pc.addTrack(t, stream));
    pc.ontrack = ev => { $("voice-audio").srcObject = ev.streams[0]; };
    // Los candidatos esperan a que la oferta esté registrada; el servidor rechaza mensajes sin llamada en curso.
    pc.onicecandidate = ev => { if (!ev.candidate) return; if (call?.offered) signal("candidate", ev.candidate.toJSON()).catch(() => {}); else call?.queue.push(ev.candidate.toJSON()); };
    pc.onconnectionstatechange = () => {
      if (!call || call.pc !== pc) return;
      if (pc.connectionState === "connected") { call.connected = true; clearTimeout(call.timeout); voiceMsg("En llamada con la cuna. Puedes hablar."); call.stats = setInterval(voiceStats, 2000); voiceStats(); }
      else if (["failed", "disconnected", "closed"].includes(pc.connectionState)) { voiceMsg("Se perdió la conexión de voz.", true); voiceHang(true); }
    };
    await pc.setLocalDescription(await pc.createOffer());
    await signal("offer", {type: "offer", sdp: pc.localDescription.sdp});
    call.offered = true;
    for (const c of call.queue.splice(0)) signal("candidate", c).catch(() => {});
    voiceMsg("Llamando a la cuna…"); voiceRefresh();
    call.timeout = setTimeout(() => { if (call && !call.connected) { voiceMsg("La cuna no respondió. Revisa que esté a la escucha.", true); voiceHang(true); } }, 20000);
    voicePoll(call);
  } catch (err) {
    stream.getTracks().forEach(t => t.stop()); pc?.close(); call = null;
    voiceRefresh(); voiceMsg(err.message, true);
  }
}
async function voicePoll(mine) {
  while (call === mine && !mine.closed) {
    try {
      const data = await api("/api/voice/poll", {child_id: mine.child, role: "app", after: mine.last, wait: 15});
      if (call !== mine) return;
      mine.last = Math.max(mine.last, data.last);
      for (const m of data.messages) {
        if (m.type === "answer") await mine.pc.setRemoteDescription(m.data);
        else if (m.type === "candidate") await mine.pc.addIceCandidate(m.data).catch(() => {});
        else if (m.type === "hangup") { voiceMsg("La cuna terminó la llamada."); voiceHang(false); return; }
      }
    } catch (err) {
      if (err.status === 401 || call !== mine) return;
      await new Promise(r => setTimeout(r, 1500));
    }
  }
}
async function voiceStats() {
  if (!call) return;
  const stats = await call.pc.getStats();
  for (const s of stats.values()) {
    if (s.type === "candidate-pair" && s.nominated && s.currentRoundTripTime != null) {
      const ms = Math.round(s.currentRoundTripTime * 500);  // RTT/2 aproxima el retardo de ida
      $("voice-stats").textContent = "Retardo de red estimado: ~" + ms + " ms (meta ≤ 500 ms: " + (ms <= 500 ? "cumple" : "NO cumple") + "). No incluye captura ni reproducción.";
      return;
    }
  }
}
function voiceHang(notify = true) {
  const mine = call;
  if (!mine) return;
  call = null; mine.closed = true;
  clearTimeout(mine.timeout); clearInterval(mine.stats);
  if (notify) api("/api/voice/signal", {child_id: mine.child, role: "app", type: "hangup"}).catch(() => {});
  mine.stream.getTracks().forEach(t => t.stop()); mine.pc.close();
  $("voice-audio").srcObject = null; $("voice-stats").textContent = "";
  if (!$("voice-msg").classList.contains("error") && !$("voice-msg").textContent.startsWith("La cuna")) voiceMsg("Llamada finalizada.");
  voiceRefresh();
}
window.addEventListener("pagehide", () => voiceHang(true));
