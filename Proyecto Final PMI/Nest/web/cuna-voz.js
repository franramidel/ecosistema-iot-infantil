"use strict";
// Cuna simulada para SCRUM-31 B. Con una cuna real, el firmware hace lo mismo con la clave del dispositivo
// (POST /api/voice/poll y /api/voice/signal con device_id). Aquí usa la sesión de la familia.
const $ = id => document.getElementById(id);
let run = null;
const say = (text, error = false) => { $("status").textContent = text; $("status").classList.toggle("error", error); };
async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const data = await res.json();
  if (!res.ok) { const e = Error(data.error || "La operación falló."); e.status = res.status; throw e; }
  return data;
}
async function init() {
  try {
    const st = await api("/api/state");
    $("child").innerHTML = st.children.map(c => '<option value="' + c.id + '"></option>').join("");
    [...$("child").options].forEach((o, i) => o.textContent = st.children[i].name);
    if (!st.children.length) say("Crea un perfil de bebé en el panel primero.", true);
  } catch (err) { say(err.status === 401 ? "Inicia sesión en el panel y vuelve a abrir esta página." : err.message, true); $("listen").disabled = true; }
}
const send = (child, type, data) => api("/api/voice/signal", {child_id: child, role: "cuna", type, data});
$("listen").onclick = async () => {
  if (!navigator.mediaDevices?.getUserMedia || !window.RTCPeerConnection) return say("Este navegador no permite audio en vivo aquí. Usa localhost o HTTPS.", true);
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true}}); }
  catch { return say("No se pudo usar el micrófono. Revisa el permiso del navegador.", true); }
  const child = $("child").value;
  run = {child, stream, last: 0, closed: false, pc: null};
  $("listen").hidden = true; $("stop").hidden = false; $("child").disabled = true;
  say("A la escucha. Llama desde el panel (Control → Llamada de voz).");
  loop(run);
};
$("stop").onclick = () => stop(true);
function closePeer(mine) { mine.pc?.close(); mine.pc = null; $("speaker").srcObject = null; }
function stop(notify) {
  const mine = run;
  if (!mine) return;
  run = null; mine.closed = true; closePeer(mine);
  mine.stream.getTracks().forEach(t => t.stop());
  if (notify) send(mine.child, "hangup").catch(() => {});
  $("listen").hidden = false; $("stop").hidden = true; $("child").disabled = false;
  say("Cuna detenida.");
}
async function answer(mine, offer) {
  closePeer(mine);
  const cfg = await api("/api/voice/config");
  const pc = mine.pc = new RTCPeerConnection({iceServers: cfg.ice_servers});
  mine.stream.getTracks().forEach(t => pc.addTrack(t, mine.stream));
  pc.ontrack = ev => { $("speaker").srcObject = ev.streams[0]; };
  pc.onicecandidate = ev => { if (ev.candidate) send(mine.child, "candidate", ev.candidate.toJSON()).catch(() => {}); };
  pc.onconnectionstatechange = () => {
    if (mine.pc !== pc) return;
    if (pc.connectionState === "connected") say("En llamada con la app. Escucha y habla.");
    else if (["failed", "disconnected"].includes(pc.connectionState)) say("Se perdió la conexión. A la escucha otra vez.", true);
  };
  await pc.setRemoteDescription(offer);
  await pc.setLocalDescription(await pc.createAnswer());
  await send(mine.child, "answer", {type: "answer", sdp: pc.localDescription.sdp});
}
async function loop(mine) {
  while (run === mine && !mine.closed) {
    try {
      const data = await api("/api/voice/poll", {child_id: mine.child, role: "cuna", after: mine.last, wait: 15});
      if (run !== mine) return;
      mine.last = Math.max(mine.last, data.last);
      for (const m of data.messages) {
        if (m.type === "offer") { say("Llamada entrante…"); await answer(mine, m.data); }
        else if (m.type === "candidate" && mine.pc) await mine.pc.addIceCandidate(m.data).catch(() => {});
        else if (m.type === "hangup") { closePeer(mine); say("La app colgó. A la escucha."); }
      }
    } catch (err) {
      if (err.status === 401 || err.status === 403) { say(err.message, true); stop(false); return; }
      say(err.message + " Reintentando…", true);
      await new Promise(r => setTimeout(r, 2000));
    }
  }
}
window.addEventListener("pagehide", () => stop(true));
init();
