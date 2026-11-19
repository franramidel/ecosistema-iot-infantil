"use strict";
const $ = id => document.getElementById(id);
let state, chosen = null, tab = "profile", syncResource, syncPromise, syncEpoch = 0, generation = 0, onSave;
let noticeTimer;
// Sprint 2: estado de la gráfica histórica, la consola de auditoría y la pestaña de control.
let rangeMode = false, histData = null, histEpoch = 0, histTimer, histLast = 0, chartThr = null;
let auditRows = [], auditNext = null, auditEpoch = 0, controlEpoch = 0, settingsFor = null;
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const roleName = role => role === "admin" ? "Padre / administrador" : "Cuidador";
const fmt = number => new Intl.NumberFormat("es-CR", {maximumFractionDigits: 3}).format(number);
const date = value => new Date(value).toLocaleString("es-CR");
const admin = () => state?.me.role === "admin";

function notice(msg, error=false) {
  clearTimeout(noticeTimer);
  $("notice").textContent = msg;
  $("notice").className = error ? "error" : "";
  $("notice").hidden = false;
  noticeTimer = setTimeout(() => $("notice").hidden = true, 6000);
}
async function api(path, body) {
  let res;
  try {
    res = await fetch(path, body === undefined ? {} : {
      method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)
    });
  } catch { throw Error("No se pudo conectar con el servidor."); }
  const data = await res.json();
  if (!res.ok) {
    const error = Error(data.error || "La operación falló.");
    error.status = res.status;
    if (res.status === 401 && path !== "/api/login") showLogin();
    throw error;
  }
  return data;
}
function showLogin() {
  generation++;
  stopSync();
  state = null; chosen = null; tab = "profile"; thrFor = null;
  rangeMode = false; histData = null; chartThr = null; histEpoch++; controlEpoch++; auditEpoch++;
  auditRows = []; auditNext = null; auditLoaded = false; auditSig = ""; settingsFor = null; appliedRange = {}; histError = null;
  $("range-live").hidden = true; feedReset();
  if ($("modal").open) $("modal").close();
  $("account").hidden = $("dashboard").hidden = true;
  $("login").hidden = false;
}
async function refresh() {
  const marker = ++generation;
  const data = await api("/api/state" + (chosen ? "?child="+encodeURIComponent(chosen) : ""));
  if (marker !== generation) return;
  state = data; chosen = data.selected;
  $("login").hidden = true; $("dashboard").hidden = $("account").hidden = false;
  render();
  ensureSync();
}
function reportSyncError(error) {
  if (error.status !== 401) notice(error.message,true);
}
function stopSync() {
  syncEpoch++;
  syncResource?.close();
  syncResource = syncPromise = null;
}
function ensureSync() {
  if (syncResource || syncPromise) return;
  const epoch = syncEpoch;
  syncPromise = api("/api/sync").then(config => {
    if (epoch !== syncEpoch || !state) return;
    if (config.mode === "sse") {
      const source = new EventSource("/api/stream");
      const update = () => refresh().catch(reportSyncError);
      source.onmessage = update;
      source.addEventListener("revisar",update);
      syncResource = {close:() => source.close()};
      return;
    }
    if (config.mode !== "realtime" || !config.url || !config.family_id || !config.access_token) {
      throw Error("La configuración de sincronización de Supabase no es válida.");
    }
    syncResource = createRealtimeSync(epoch);
    syncResource.connect(config);
  }).catch(error => {
    if (epoch === syncEpoch && state) reportSyncError(error);
  }).finally(() => {
    if (epoch === syncEpoch) syncPromise = null;
  });
}
function createRealtimeSync(epoch) {
  let socket, stopped = false, retryTimer, heartbeatTimer, tokenTimer, joinTimer, refreshTimer;
  let retries = 0, nextRef = 0, joinRef, reportedFailure = false;
  const isCurrent = () => !stopped && epoch === syncEpoch && !!state;
  const clearConnectionTimers = () => {
    clearInterval(heartbeatTimer);
    clearTimeout(tokenTimer);
    clearTimeout(joinTimer);
    clearTimeout(refreshTimer);
  };
  const scheduleStateRefresh = () => {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => refresh().catch(reportSyncError),100);
  };
  const scheduleReconnect = (error, currentSocket) => {
    if (!isCurrent() || retryTimer || (currentSocket && socket !== currentSocket)) return;
    clearConnectionTimers();
    const previous = socket;
    socket = null;
    if (previous && previous.readyState < WebSocket.CLOSING) previous.close();
    if (error && !reportedFailure) {
      notice(error.message || "Se perdió la conexión de sincronización.",true);
      reportedFailure = true;
    }
    retries++;
    const delay = Math.min(30000,1000 * (2 ** Math.min(retries - 1,5))) + Math.random() * 500;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      connect();
    },delay);
  };
  const scheduleTokenRefresh = (config, currentSocket) => {
    clearTimeout(tokenTimer);
    const delay = Math.max(1000,config.expires_at * 1000 - Date.now() - 60000);
    tokenTimer = setTimeout(async () => {
      try {
        const latest = await api("/api/sync");
        if (!isCurrent() || socket !== currentSocket || currentSocket.readyState !== WebSocket.OPEN) return;
        if (latest.mode !== "realtime" || latest.family_id !== config.family_id) {
          throw Error("La familia de la sesión cambió. Vuelve a iniciar sesión.");
        }
        currentSocket.send(JSON.stringify({
          topic:"realtime:families", event:"access_token",
          payload:{access_token:latest.access_token}, ref:String(++nextRef), join_ref:joinRef
        }));
        scheduleTokenRefresh(latest,currentSocket);
      } catch (error) {
        scheduleReconnect(error,currentSocket);
      }
    },delay);
  };
  const connect = async initialConfig => {
    if (!isCurrent()) return;
    try {
      const config = initialConfig || await api("/api/sync");
      if (!isCurrent()) return;
      if (config.mode !== "realtime" || !config.url || !config.family_id || !config.access_token) {
        throw Error("La configuración de sincronización de Supabase no es válida.");
      }
      const currentSocket = new WebSocket(config.url);
      socket = currentSocket;
      currentSocket.onopen = () => {
        if (socket !== currentSocket) return;
        joinRef = String(++nextRef);
        currentSocket.send(JSON.stringify({
          topic:"realtime:families", event:"phx_join", ref:joinRef, join_ref:joinRef,
          payload:{
            config:{
              broadcast:{ack:false,self:false}, presence:{key:""},
              postgres_changes:[{
                event:"UPDATE",schema:"public",table:"families",
                filter:"id=eq."+config.family_id
              }]
            },
            access_token:config.access_token
          }
        }));
        heartbeatTimer = setInterval(() => {
          if (currentSocket.readyState === WebSocket.OPEN) {
            currentSocket.send(JSON.stringify({
              topic:"phoenix",event:"heartbeat",payload:{},ref:String(++nextRef)
            }));
          }
        },25000);
        joinTimer = setTimeout(() => scheduleReconnect(
          Error("Supabase Realtime no confirmó la suscripción."),currentSocket),10000);
      };
      currentSocket.onmessage = event => {
        if (socket !== currentSocket) return;
        let message;
        try { message = JSON.parse(event.data); }
        catch { scheduleReconnect(Error("Respuesta inválida de Supabase Realtime."),currentSocket); return; }
        if (message.event === "phx_reply" && String(message.ref) === joinRef) {
          if (message.payload?.status !== "ok") {
            scheduleReconnect(Error("Supabase rechazó la suscripción de la familia."),currentSocket);
            return;
          }
          clearTimeout(joinTimer);
          retries = 0; reportedFailure = false;
          scheduleTokenRefresh(config,currentSocket);
          scheduleStateRefresh();
        } else if (message.event === "postgres_changes") {
          scheduleStateRefresh();
        } else if (message.event === "phx_error" || message.event === "phx_close") {
          scheduleReconnect(Error("Supabase cerró la suscripción de la familia."),currentSocket);
        }
      };
      currentSocket.onerror = () => scheduleReconnect(
        Error("No se pudo conectar con Supabase Realtime."),currentSocket);
      currentSocket.onclose = () => scheduleReconnect(
        Error("La conexión con Supabase Realtime se cerró."),currentSocket);
    } catch (error) {
      scheduleReconnect(error);
    }
  };
  return {
    connect,
    close() {
      stopped = true;
      clearConnectionTimers();
      clearTimeout(retryTimer);
      const current = socket;
      socket = null;
      if (current && current.readyState < WebSocket.CLOSING) current.close();
    }
  };
}
document.addEventListener("visibilitychange",() => {
  if (!document.hidden && state) refresh().catch(reportSyncError);
});
$("signin").onsubmit = async e => {
  e.preventDefault();
  const btn = e.target.querySelector("button");
  btn.disabled = true;
  try {
    await api("/api/login",Object.fromEntries(new FormData(e.target)));
    e.target.reset(); await refresh();
  } catch(err) { notice(err.message,true); }
  finally { btn.disabled = false; }
};
$("logout").onclick = async () => {
  try { await api("/api/logout",{}); showLogin(); }
  catch(err) { notice(err.message,true); }
};
async function switchChild(id) {
  chosen = id;
  // Quitar de inmediato los datos del perfil anterior durante el cambio.
  $("child-panel").hidden = true;
  histData = null; histEpoch++; chartThr = null; settingsFor = null; thrFor = null; controlEpoch++;
  try { await refresh(); } catch(err) { notice(err.message,true); }
}
$("selector").onchange = e => switchChild(e.target.value);
$("control-selector").onchange = e => switchChild(e.target.value);
document.querySelectorAll("[data-tab]").forEach(b => b.onclick = () => {
  tab = b.dataset.tab;
  render();
});
function setTabs() {
  if (!admin() && (tab === "people" || tab === "audit")) tab = "profile";
  for (const id of ["profile","hardware","control","people","audit"]) $(id).hidden = tab !== id;
  document.querySelectorAll("[data-tab]").forEach(b => {
    b.classList.toggle("active",b.dataset.tab === tab);
    if (b.dataset.tab === tab) b.setAttribute("aria-current","page"); else b.removeAttribute("aria-current");
  });
}
function options(items, selected) {
  return items.map(item => '<option value="'+esc(item.id)+'"'+(item.id === selected ? " selected" : "")+'>'+esc(item.name)+'</option>').join("");
}
function button(label, action, id) {
  return '<button class="secondary" data-do="'+action+'" data-id="'+esc(id)+'">'+esc(label)+'</button>';
}
function deviceRow(d, inventory) {
  const child = state.children.find(c => c.id === d.child_id);
  const controls = [];
  if (child && state.me.can_operate) controls.push(button(d.enabled ? "Desactivar" : "Activar","operate",d.id));
  if (inventory && admin()) controls.push(button(child ? "Reasignar" : "Asignar","assign",d.id));
  return '<div class="row"><div><h3>'+esc(d.name)+'</h3><small>Device_ID · '+esc(d.id)+'</small>'+
    (inventory ? '<small>'+esc(child?.name || "Sin asignar")+'</small>' : "")+
    '</div><div class="row-actions">'+(child ? '<span class="status">'+(d.enabled ? "Activo" : "Inactivo")+'</span>' : "")+controls.join("")+'</div></div>';
}
function empty(msg) { return '<p class="empty">'+esc(msg)+'</p>'; }
function render() {
  setTabs();
  $("identity").textContent = state.me.email + " · " + roleName(state.me.role);
  for (const id of ["add-child","rename","people-tab","audit-tab","add-device","report"]) $(id).hidden = !admin();
  const child = state.children.find(c => c.id === state.selected);
  $("selector-box").hidden = !child;
  $("selector").innerHTML = options(state.children,chosen);
  $("child-title").textContent = child?.name || "Ficha del bebé";
  $("child-ref").textContent = "Child_ID · " + (chosen || "");
  $("empty-profile").hidden = !!child; $("child-panel").hidden = !child;
  $("empty-help").textContent = admin() ? "Crea el primer perfil para asignarle dispositivos y registrar lecturas." : "El administrador puede crear el primer perfil de bebé.";
  $("report").href = "/api/report?child=" + encodeURIComponent(chosen || "");
  $("inventory").innerHTML = state.devices.map(d=>deviceRow(d,true)).join("") || empty("Todavía no hay dispositivos registrados.");
  const assigned = state.devices.filter(d=>d.child_id === chosen && chosen);
  $("child-devices").innerHTML = assigned.map(d=>deviceRow(d,false)).join("") || empty("Este bebé aún no tiene dispositivos asignados.");
  $("members").innerHTML = state.members.map(m=>'<div class="row"><div><h3>'+esc(m.email)+'</h3><small>'+roleName(m.role)+'</small></div><div class="row-actions"><span class="status">'+
    (m.can_operate ? "Puede operar" : "Solo lectura")+'</span>'+(m.role === "caregiver" ? button("Editar permiso","permission",m.id) : "")+'</div></div>').join("");
  const rows = state.events;
  $("history").innerHTML = rows.length ? '<div class="scroll"><table><thead><tr><th>Fecha de captura</th><th>Device_ID</th><th>Medida</th><th>Lectura</th></tr></thead><tbody>'+
    rows.map(e=>'<tr><td>'+esc(date(e.captured_at))+'</td><td>'+esc(e.device_id)+'</td><td>'+esc(e.metric)+'</td><td>'+fmt(e.value)+' '+esc(e.unit)+'</td></tr>').join("")+'</tbody></table></div>' : empty("Las lecturas aparecerán aquí cuando el dispositivo envíe datos.");
  renderExport();
  $("thr-card").hidden = !child;
  for (const id of ["thr-min","thr-max"]) $(id).disabled = !admin();
  $("thr-save").hidden = !admin();
  loadThresholds();
  loadTelegram();
  renderControlShell(child);
  if (tab === "audit") renderAuditFilters();
  scheduleHistory();
  draw();
}
// --- SCRUM-54: temperatura y humedad juntas, con rango histórico y umbrales marcados -------------------------------
const COLOR_TEMP = "#b4532a", COLOR_HUM = "#2563a8";
const isTemp = e => e.metric === "Temperatura" && e.unit === "°C";
const isHum = e => e.metric === "Humedad" && e.unit === "%";
const day = value => new Date(value).toLocaleDateString("sv-SE");
let appliedRange = {}, histError = null;
function niceScale(lo, hi) {
  if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
  const rough = (hi - lo) / 4, mag = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(v => v >= rough);
  const min = Math.floor(lo / step + 1e-9) * step, max = Math.ceil(hi / step - 1e-9) * step;
  const count = Math.round((max - min) / step), ticks = [];
  for (let i = 0; i <= count; i++) ticks.push(+(min + i * step).toFixed(6));
  return {min: ticks[0], max: ticks.at(-1), ticks};
}
function chartSeries() {
  if (rangeMode) return histData && histData.child === chosen ? histData : null;
  const ascending = [...state.events].reverse();  // el estado trae lo más reciente primero
  return {temp: ascending.filter(isTemp), hum: ascending.filter(isHum), truncated: false};
}
function extent(values) {
  let lo = Infinity, hi = -Infinity;
  for (const v of values) { if (v < lo) lo = v; if (v > hi) hi = v; }
  return [lo, hi];
}
function draw() {
  if (!state || tab !== "profile") return;
  const canvas = $("graph"), info = $("graph-info");
  info.classList.toggle("error", !!histError);
  const lastTemp = state.events.find(isTemp), lastHum = state.events.find(isHum);
  $("latest").innerHTML = lastTemp || lastHum
    ? [lastTemp && '<span class="metric-value">'+fmt(lastTemp.value)+'</span> '+esc(lastTemp.unit),
       lastHum && '<span class="metric-value">'+fmt(lastHum.value)+'</span> '+esc(lastHum.unit)].filter(Boolean).join(' <span class="muted">·</span> ')
    : empty("Sin lecturas para este perfil.");
  const thr = chartThr && chartThr.child === chosen ? chartThr : null;
  $("graph-legend").innerHTML = '<span><span class="swatch"></span>Temperatura (°C, eje izquierdo)</span><span><span class="swatch hum"></span>Humedad (%, eje derecho)</span>'+
    (thr ? '<span><span class="swatch band"></span>Umbrales '+esc(fmt(thr.min))+' a '+esc(fmt(thr.max))+' °C</span>' : "");
  const series = chartSeries();
  const points = series ? [...series.temp, ...series.hum] : [];
  canvas.hidden = !points.length;
  if (!points.length) {
    info.textContent = histError || (rangeMode ? (series ? "No hay lecturas de temperatura ni humedad en este rango." : "Cargando el histórico…") : "");
    return;
  }
  const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth || 640, h = canvas.clientHeight || 280;
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const m = {l: 50, r: 50, t: 18, b: 30}, pw = w - m.l - m.r, ph = h - m.t - m.b;
  let [t0, t1] = extent(points.map(e => Date.parse(e.captured_at)));
  if (t1 === t0) { t0 -= 30000; t1 += 30000; }
  const x = t => m.l + (t - t0) / (t1 - t0) * pw;
  const tv = series.temp.map(e => e.value), hv = series.hum.map(e => e.value);
  const tempValues = [...tv, ...(thr ? [thr.min, thr.max] : [])];
  const ts = tempValues.length ? niceScale(...extent(tempValues)) : null;
  const hs = hv.length ? niceScale(...extent(hv)) : null;
  if (hs) { hs.min = Math.max(0, hs.min); hs.max = Math.min(100, hs.max); hs.ticks = hs.ticks.filter(v => v >= hs.min && v <= hs.max); }
  const yT = v => m.t + (1 - (v - ts.min) / (ts.max - ts.min)) * ph;
  const yH = v => m.t + (1 - (v - hs.min) / (hs.max - hs.min)) * ph;
  ctx.font = "12px sans-serif"; ctx.textBaseline = "middle";
  if (ts && thr) {
    ctx.fillStyle = "rgba(40,118,96,.12)";
    ctx.fillRect(m.l, yT(thr.max), pw, yT(thr.min) - yT(thr.max));
  }
  ctx.strokeStyle = "#dce6de"; ctx.lineWidth = 1;
  for (const y of ts ? ts.ticks.map(yT) : hs.ticks.map(yH)) {
    ctx.beginPath(); ctx.moveTo(m.l, y); ctx.lineTo(w - m.r, y); ctx.stroke();
  }
  ctx.textAlign = "right"; ctx.fillStyle = COLOR_TEMP;
  if (ts) { for (const v of ts.ticks) ctx.fillText(fmt(v), m.l - 6, yT(v)); ctx.fillText("°C", m.l - 6, 7); }
  ctx.textAlign = "left"; ctx.fillStyle = COLOR_HUM;
  if (hs) { for (const v of hs.ticks) ctx.fillText(fmt(v), w - m.r + 6, yH(v)); ctx.fillText("% HR", w - m.r + 6, 7); }
  if (ts && thr) {
    ctx.setLineDash([6, 4]); ctx.strokeStyle = "#287660"; ctx.lineWidth = 1.5; ctx.fillStyle = "#175c4d";
    ctx.textAlign = "left";
    for (const [label, value, dy] of [["máx " + fmt(thr.max) + " °C", thr.max, -8], ["mín " + fmt(thr.min) + " °C", thr.min, 8]]) {
      ctx.beginPath(); ctx.moveTo(m.l, yT(value)); ctx.lineTo(w - m.r, yT(value)); ctx.stroke();
      ctx.fillText(label, m.l + 6, yT(value) + dy);
    }
    ctx.setLineDash([]);
  }
  ctx.fillStyle = "#536b62"; ctx.textBaseline = "top";
  const span = t1 - t0;
  for (let i = 0; i <= 4; i++) {
    const t = t0 + span * i / 4, d = new Date(t);
    const label = span <= 36 * 3600e3 ? d.toLocaleTimeString("es-CR", {hour: "2-digit", minute: "2-digit"}) : d.toLocaleDateString("es-CR", {day: "2-digit", month: "2-digit"});
    ctx.textAlign = i === 0 ? "left" : i === 4 ? "right" : "center";
    ctx.fillText(label, x(t), h - m.b + 8);
  }
  const line = (list, y, color) => {
    if (!list.length) return;
    ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 2; ctx.beginPath();
    list.forEach((e, i) => { const px = x(Date.parse(e.captured_at)), py = y(e.value); i ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
    ctx.stroke();
    if (list.length <= 120) for (const e of list) { ctx.beginPath(); ctx.arc(x(Date.parse(e.captured_at)), y(e.value), 2.5, 0, Math.PI * 2); ctx.fill(); }
  };
  if (hs) line(series.hum, yH, COLOR_HUM);
  if (ts) line(series.temp, yT, COLOR_TEMP);
  const [tlo, thi] = tv.length ? extent(tv) : [], [hlo, hhi] = hv.length ? extent(hv) : [];
  canvas.setAttribute("aria-label", "Temperatura en °C" + (tv.length ? ", de " + fmt(tlo) + " a " + fmt(thi) : ", sin lecturas") +
    "; humedad en %" + (hv.length ? ", de " + fmt(hlo) + " a " + fmt(hhi) : ", sin lecturas") +
    (thr ? "; umbrales de temperatura " + fmt(thr.min) + " a " + fmt(thr.max) + " °C" : ""));
  info.textContent = (histError ? histError + " " : "") + (rangeMode ? "Histórico: " : "En vivo: ") + tv.length + " lecturas de temperatura y " + hv.length + " de humedad · " +
    date(t0) + " – " + date(t1) + (series.truncated ? ". Se muestran solo las primeras lecturas del rango; acórtalo para ver el resto." : "");
}
async function fetchSeries(child, metric, epoch) {
  const points = [];
  let after = null, pages = 0;
  do {
    const query = new URLSearchParams({child, metric, limit: "2000"});
    if (appliedRange.desde) query.set("desde", appliedRange.desde);
    if (appliedRange.hasta) query.set("hasta", appliedRange.hasta);
    if (after) query.set("after", after);
    const page = await api("/api/history?" + query);
    if (epoch !== histEpoch) return null;
    for (const item of page.items) points.push(item);
    after = page.next; pages++;
  } while (after && pages < 5);
  return {points, truncated: !!after};
}
async function loadHistory() {
  if (!rangeMode || !chosen) return;
  const child = chosen, epoch = ++histEpoch;
  try {
    const [t, h] = await Promise.all([fetchSeries(child, "Temperatura", epoch), fetchSeries(child, "Humedad", epoch)]);
    if (!t || !h || epoch !== histEpoch || child !== chosen) return;
    histError = null;
    histData = {child, temp: t.points.filter(isTemp), hum: h.points.filter(isHum), truncated: t.truncated || h.truncated};
    histLast = Date.now();
  } catch (err) {
    if (epoch !== histEpoch) return;
    histData = null; histError = err.message;
  }
  draw();
}
function scheduleHistory() {
  if (!rangeMode || tab !== "profile" || !chosen) return;
  const loaded = histData && histData.child === chosen;
  const open = !appliedRange.hasta || appliedRange.hasta >= day(Date.now() - 864e5);  // el rango llega hasta hoy: sigue creciendo
  if (loaded && !open) return;
  clearTimeout(histTimer);
  histTimer = setTimeout(loadHistory, loaded ? Math.max(0, 5000 - (Date.now() - histLast)) : 0);
}
$("range-form").onsubmit = ev => {
  ev.preventDefault();
  const desde = $("range-from").value, hasta = $("range-to").value;
  if (!desde && !hasta) { histError = "Elige al menos una fecha o vuelve a en vivo."; return draw(); }
  if (desde && hasta && desde > hasta) { histError = "La fecha inicial no puede ser posterior a la final."; return draw(); }
  histError = null; appliedRange = {desde, hasta}; rangeMode = true; histData = null;
  $("range-live").hidden = false;
  loadHistory(); draw();
};
$("range-live").onclick = () => {
  rangeMode = false; histData = null; histError = null; histEpoch++;
  $("range-live").hidden = true;
  draw();
};
function tgMsg(text, error=false) { $("tg-msg").textContent = text; $("tg-msg").classList.toggle("error", error); }
function renderTelegram(t) {
  const linked = t.vinculado;
  $("tg-state").textContent = linked
    ? (t.bloqueado ? "Vinculado, pero Telegram indica que bloqueaste el bot. Desbloquéalo y envía una prueba." : "Telegram vinculado.")
    : "Telegram no está vinculado.";
  $("tg-link-btn").hidden = linked;
  $("tg-test-btn").hidden = !linked;
  $("tg-unlink-btn").hidden = !linked;
  if (linked || !t.codigo_expira) $("tg-code").hidden = true;
  if (linked) {
    const last = t.ultima_prueba;
    tgMsg(t.procesador_activo ? "El procesador de alertas está activo." : "El procesador de alertas no está activo. Inicia telegram_worker.py.", !t.procesador_activo);
    if (last) $("tg-msg").textContent += " Última prueba: " + ({pendiente:"en cola",procesando:"enviando",enviada:"enviada",error_recuperable:"reintentando",error_permanente:"fallida",sin_vinculo:"sin vínculo"}[last.estado] || last.estado) + ".";
  }
}
async function loadTelegram() {
  try { renderTelegram(await api("/api/telegram")); }
  catch (err) { $("tg-state").textContent = err.message; }
}
async function tgAction(button, path, done) {
  button.disabled = true;
  try { const res = await api(path, {}); done?.(res); await loadTelegram(); }
  catch (err) { tgMsg(err.message, true); }
  finally { button.disabled = false; }
}
$("tg-link-btn").onclick = () => tgAction($("tg-link-btn"), "/api/telegram/link", res => {
  $("tg-code-value").textContent = res.codigo;
  $("tg-code").hidden = false;
  const link = $("tg-link");
  link.hidden = !res.enlace;
  if (res.enlace) link.href = res.enlace;
  tgMsg("Código generado. Envíalo al bot y pulsa Actualizar estado.");
});
$("tg-unlink-btn").onclick = () => tgAction($("tg-unlink-btn"), "/api/telegram/unlink", () => tgMsg("Telegram desvinculado."));
$("tg-test-btn").onclick = () => tgAction($("tg-test-btn"), "/api/telegram/test", () => tgMsg("Prueba en cola. Llegará en unos segundos."));
$("tg-refresh-btn").onclick = loadTelegram;
let thrFor = null, thrDirty = false, settingsDirty = false;
function thrMsg(text, error=false) { $("thr-msg").textContent = text; $("thr-msg").classList.toggle("error", error); }
async function loadThresholds(force=false) {
  const id = chosen;
  if (!id || (!force && thrFor === id && thrDirty)) return;  // render() corre con cada lectura nueva: se refresca desde el servidor salvo que haya algo sin guardar
  try {
    const t = await api("/api/thresholds?child=" + encodeURIComponent(id));
    if (id !== chosen) return;
    thrFor = id; thrDirty = false;
    $("thr-min").value = t.temp_min; $("thr-max").value = t.temp_max;
    chartThr = {child: id, min: t.temp_min, max: t.temp_max}; draw();
    thrMsg(!admin() ? "Solo un administrador puede cambiar los umbrales." : t.default ? "Usando los valores por defecto." : "");
  } catch (err) { thrMsg(err.message, true); }
}
$("thr-form").onsubmit = async ev => {
  ev.preventDefault();
  const min = $("thr-min").value.trim(), max = $("thr-max").value.trim();
  if (!min || !max || !Number.isFinite(Number(min)) || !Number.isFinite(Number(max))) return thrMsg("Escribe el mínimo y el máximo como números.", true);
  $("thr-save").disabled = true;
  try {
    await api("/api/thresholds", {child_id: chosen, temp_min: Number(min), temp_max: Number(max)});
    thrMsg("Umbrales guardados. Se aplican desde la próxima lectura.");
    thrFor = null; await loadThresholds(true);
    thrMsg("Umbrales guardados. Se aplican desde la próxima lectura.");
  } catch (err) { thrMsg(err.message, true); }
  finally { $("thr-save").disabled = false; }
};
function renderExport() {
  $("export-card").hidden = !admin();
  if (!admin()) return;
  const child = $("export-child"), device = $("export-device");
  const first = !child.options.length, prevChild = child.value, prevDevice = device.value;
  child.innerHTML = '<option value="">Todos los bebés</option>' + options(state.children, null);
  device.innerHTML = '<option value="">Todos los dispositivos</option>' +
    state.devices.map(d => '<option value="'+esc(d.id)+'">'+esc(d.name)+' · '+esc(d.id)+'</option>').join("");
  child.value = first ? (chosen || "") : prevChild;
  device.value = prevDevice;
  if (child.value !== (first ? (chosen || "") : prevChild)) child.value = "";
}
function exportStatus(msg, error=false) {
  $("export-status").textContent = msg;
  $("export-status").classList.toggle("error", error);
}
function exportName(res) {
  const found = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "");
  return found ? found[1] : "reporte-nido.xlsx";
}
async function downloadExport(values, report, btn) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value) params.set(key, value);
  btn.disabled = true;
  report("Generando el archivo. Esto puede tardar si hay muchos registros…");
  try {
    let res;
    try { res = await fetch("/api/export/xlsx" + (params.size ? "?" + params : "")); }
    catch { throw Error("No se pudo conectar con el servidor."); }
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      if (res.status === 401) showLogin();
      throw Error(data.error || "No se pudo generar el archivo.");
    }
    const name = exportName(res), url = URL.createObjectURL(await res.blob());
    const link = document.createElement("a");
    link.href = url; link.download = name;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    report("Descarga lista: " + name);
  } catch (err) { report(err.message, true); }
  finally { btn.disabled = false; }
}
$("export-form").onsubmit = async e => {
  e.preventDefault();
  const values = Object.fromEntries(new FormData(e.target));
  if (values.desde && values.hasta && values.desde > values.hasta) {
    exportStatus("La fecha inicial no puede ser posterior a la final.", true);
    $("export-from").focus();
    return;
  }
  await downloadExport(values, exportStatus, $("export-go"));
};
window.addEventListener("resize",draw);
function modal(title, html, save, label="Guardar") {
  $("modal-title").textContent=title; $("fields").innerHTML=html; $("form-error").hidden=true;
  $("confirm").textContent=label; onSave=save;
  if (!$("modal").open) $("modal").showModal();
}
$("close").onclick=()=>$("modal").close();
$("modal").addEventListener("close",()=>{ $("fields").replaceChildren(); onSave=null; });
$("editor").onsubmit=async e=>{
  e.preventDefault(); const btn=$("confirm"); btn.disabled=true;
  try {
    const values=Object.fromEntries(new FormData(e.target));
    const keep=await onSave(values);
    if (!keep && $("modal").open) $("modal").close();
    await refresh();
  } catch(err) {
    $("form-error").textContent=err.message; $("form-error").hidden=false;
    if (!$("modal").open) notice(err.message,true);
  } finally {btn.disabled=false;}
};
const input=(name,label,value="",type="text",attrs="")=>'<label>'+esc(label)+'<input name="'+name+'" type="'+type+'" value="'+esc(value)+'" required '+attrs+'></label>';
const change=(action,data)=>api("/api/change/"+action,data);
$("add-child").onclick=()=>modal("Nuevo perfil",input("name","Nombre del bebé","","text",'maxlength="80"'),async data=>{
  chosen=(await change("child",data)).id; notice("Perfil creado.");
});
$("rename").onclick=()=>{
  const child=state.children.find(c=>c.id===chosen);
  modal("Editar perfil",input("name","Nombre del bebé",child.name,"text",'maxlength="80"'),async data=>{
    await change("rename",{...data,child_id:child.id}); notice("Nombre actualizado.");
  });
};
$("add-device").onclick=()=>modal("Registrar dispositivo",input("device_id","Device_ID","","text",'maxlength="64"')+input("name","Nombre del dispositivo","","text",'maxlength="80"'),async data=>{
  const result=await change("device",data);
  modal("Clave del dispositivo",'<p class="hint">Copia esta clave ahora para configurar el simulador o la Raspberry Pi. Solo se muestra una vez.</p><label>Device_ID<input readonly value="'+esc(data.device_id)+'"></label><label>Clave privada<textarea class="secret" readonly>'+esc(result.secret)+'</textarea></label>',async()=>{}, "Listo");
  return true;
});
$("add-member").onclick=()=>{
  modal("Crear cuenta familiar",input("email","Correo electrónico","","email",'autocomplete="off"')+
    input("password","Contraseña inicial","","password",'minlength="10" autocomplete="new-password"')+
    '<label>Rol<select id="new-role" name="role"><option value="caregiver">Cuidador</option><option value="admin">Padre / administrador</option></select></label>'+
    '<label id="operate-option" class="check"><input type="checkbox" name="operate"> Permitir operar dispositivos</label>',async d=>{
      await change("member",{email:d.email,password:d.password,role:d.role,can_operate:d.operate==="on"});
      notice("Cuenta vinculada a la familia.");
    },"Crear cuenta");
  $("new-role").onchange=e=>$("operate-option").hidden=e.target.value==="admin";
};
document.addEventListener("click",async e=>{
  const b=e.target.closest("[data-do]"); if (!b) return;
  const {do:action,id}=b.dataset;
  const d=state.devices.find(row=>row.id===id);
  if (action==="assign") {
    const origin=state.children.find(c=>c.id===d.child_id);
    const choices=state.children.filter(c=>c.id!==d.child_id);
    if (origin) choices.push({id:"",name:"Dejar sin asignar"});
    if (!choices.length) {notice("Primero crea un perfil de bebé.");return;}
    modal("Asignación de "+d.id,'<label>Destino<select name="child_id">'+options(choices,null)+'</select></label>'+
      (origin ? '<p class="hint">Los datos previos quedan archivados en la ficha de '+esc(origin.name)+'. Las lecturas capturadas a partir del traslado se guardarán en el perfil de destino.</p>' : ""),async v=>{
        await change("assign",{device_id:id,expected:d.assignment_id,child_id:v.child_id||null}); notice("Asignación actualizada.");
      },"Confirmar asignación");
  } else if(action==="operate") {
    try {b.disabled=true;await change("operate",{device_id:id,expected:d.assignment_id,enabled:!d.enabled});await refresh();}
    catch(err){notice(err.message,true);await refresh().catch(()=>{});}finally{b.disabled=false;}
  } else if(action==="permission") {
    const m=state.members.find(row=>row.id===id);
    modal("Permiso del cuidador",'<p>'+esc(m.email)+'</p><label class="check"><input type="checkbox" name="operate" '+(m.can_operate?"checked":"")+'> Permitir operar dispositivos</label>',async v=>{
      await change("permission",{member_id:id,enabled:v.operate==="on"});notice("Permiso actualizado.");
    });
  }
});
// --- SCRUM-55: consola de auditoría (solo administradores) ---------------------------------------------------------
const SEVERITIES = {info: "Info", warning: "Aviso", critical: "Crítico"};
let auditApplied = "", auditLoaded = false, auditSig = "";
function auditStatus(msg, error=false) {
  $("audit-status").textContent = msg;
  $("audit-status").classList.toggle("error", error);
}
function auditFilters() {
  const values = {child: $("audit-child").value, device: $("audit-device").value, desde: $("audit-from").value, hasta: $("audit-to").value};
  for (const key of Object.keys(values)) if (!values[key]) delete values[key];
  return values;
}
function renderAuditFilters() {
  const signature = JSON.stringify([state.children.map(c => [c.id, c.name]), state.devices.map(d => [d.id, d.name])]);
  if (signature !== auditSig) {  // no reconstruir los selectores con cada lectura nueva
    const child = $("audit-child"), device = $("audit-device"), prevChild = child.value, prevDevice = device.value;
    child.innerHTML = '<option value="">Todos los bebés</option>' + options(state.children, null);
    device.innerHTML = '<option value="">Todos los dispositivos</option>' +
      state.devices.map(d => '<option value="'+esc(d.id)+'">'+esc(d.name)+' · '+esc(d.id)+'</option>').join("");
    child.value = prevChild; device.value = prevDevice;
    auditSig = signature;
  }
  if (!auditLoaded) { auditLoaded = true; loadAudit(false); }
}
function renderAuditTable() {
  const names = new Map(state.children.map(c => [c.id, c.name]));
  $("audit-table").innerHTML = auditRows.length
    ? '<table><thead><tr><th>Fecha</th><th>Hora</th><th>Bebé</th><th>Device_ID</th><th>Tipo</th><th>Detalle</th><th>Severidad</th></tr></thead><tbody>' +
      auditRows.map(e => '<tr><td>'+esc(e.event_date)+'</td><td>'+esc(e.event_time)+'</td><td>'+esc(names.get(e.child_id) ?? e.child_id)+
        '</td><td>'+esc(e.device_id)+'</td><td>'+esc(e.event_type)+'</td><td class="detail">'+esc(e.detail)+
        '</td><td class="sev-'+esc(e.severity)+'">'+esc(SEVERITIES[e.severity] ?? e.severity)+'</td></tr>').join("") + '</tbody></table>'
    : empty("No hay eventos con estos filtros.");
  $("audit-more").hidden = !auditNext;
  auditStatus(auditRows.length + (auditRows.length === 1 ? " evento" : " eventos") + (auditNext ? " · hay más" : " · fin del registro"));
}
async function loadAudit(more) {
  if (!state || !admin()) return;
  const filters = auditFilters();
  if (!more) {
    if (filters.desde && filters.hasta && filters.desde > filters.hasta) return auditStatus("La fecha inicial no puede ser posterior a la final.", true);
    auditApplied = new URLSearchParams(filters).toString();
    auditRows = []; auditNext = null;
  } else if (!auditNext) return;
  const epoch = more ? auditEpoch : ++auditEpoch;
  const query = new URLSearchParams(auditApplied);  // "Cargar más" repite la búsqueda aplicada, no lo que se esté editando
  query.set("limit", "100");
  if (more) query.set("after", auditNext);
  $("audit-go").disabled = $("audit-more").disabled = true;
  auditStatus("Buscando…");
  try {
    const page = await api("/api/audit?" + query);
    if (epoch !== auditEpoch) return;
    auditRows = auditRows.concat(page.items); auditNext = page.next;
    renderAuditTable();
  } catch (err) {
    if (epoch === auditEpoch) auditStatus(err.message, true);
  } finally {
    $("audit-go").disabled = $("audit-more").disabled = false;
  }
}
$("audit-form").onsubmit = ev => { ev.preventDefault(); loadAudit(false); };
$("audit-more").onclick = () => loadAudit(true);
$("audit-export").onclick = () => {
  const values = auditFilters();
  if (values.desde && values.hasta && values.desde > values.hasta) return auditStatus("La fecha inicial no puede ser posterior a la final.", true);
  downloadExport(values, auditStatus, $("audit-export"));
};

// --- SCRUM-35, 51, 56 y 57: control del bebé -------------------------------------------------------------------
// Todo lo que se guarda aquí es estado lógico: ningún dispositivo lo lee ni ningún comando llega al hardware.
const AUDIOS = {"ninguno": "Sin audio", "ruido_blanco": "Ruido blanco", "lluvia": "Lluvia", "olas": "Olas", "nana": "Nana"};
const MODULES = {
  temperatura: ["Sensor de temperatura", "Hay un sensor real (DHT22), pero este interruptor es lógico: no lo enciende ni lo apaga."],
  luz: ["Luz", "Sin hardware: solo se guarda el estado."],
  audio: ["Audio", "Sin hardware: solo se guarda el estado."],
  colchon: ["Colchón", "Sin hardware: solo se guarda el estado."]
};
const MODES = {manual: "Manual", programado: "Programado"};
$("audio-select").innerHTML = Object.entries(AUDIOS).map(([id, name]) => '<option value="'+id+'">'+esc(name)+'</option>').join("");
const canOperate = () => !!state?.me.can_operate;
function uuid4() {
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
  const h = [...b].map(v => v.toString(16).padStart(2, "0")).join("");
  return h.slice(0, 8)+"-"+h.slice(8, 12)+"-"+h.slice(12, 16)+"-"+h.slice(16, 20)+"-"+h.slice(20);
}
function renderControlShell(child) {
  $("control-selector").innerHTML = options(state.children, chosen);
  $("control-box").hidden = !child;
  $("control-empty").hidden = !!child; $("control-panel").hidden = !child;
  feedRender();
  if (tab === "control" && child) loadControl();
}
async function loadControl() {
  const id = chosen, marker = ++controlEpoch, query = "?child=" + encodeURIComponent(id);
  try {
    const [sleep, settings, modules] = await Promise.all([api("/api/sleep" + query), api("/api/settings" + query), api("/api/modules" + query)]);
    if (marker !== controlEpoch || id !== chosen) return;
    renderSleep(sleep); renderModules(modules);
    if (settingsFor !== id || !settingsDirty) { fillSettings(settings); settingsFor = id; }  // otra cuenta cambió los ajustes: se refleja, salvo que aquí haya algo sin guardar
  } catch (err) {
    if (marker === controlEpoch) notice(err.message, true);
  }
}
function renderSleep(st) {
  const ops = canOperate(), active = st.active, mass = st.mass, base = st.session?.baseline_g;
  $("sleep-status").textContent = active ? "Sesión activa desde " + date(st.session.started_at) : "Sin sesión activa";
  $("baseline-value").textContent = active && base != null ? fmt(base) + " g = 100 %" : "—";
  $("mass-value").textContent = !mass ? "Sin lecturas" : fmt(mass.value) + " g" + (st.current_pct != null ? " · " + fmt(st.current_pct) + " %" : "");
  $("sleep-start").disabled = !ops || active;
  $("snapshot-btn").disabled = !ops || !active;
  $("disarm-btn").disabled = !ops || !active;
  $("sleep-msg").classList.remove("error");
  $("sleep-msg").textContent = !ops ? "Necesitas permiso para operar dispositivos para iniciar, tomar el snapshot o desarmar."
    : !active ? "Inicia una sesión de sueño para poder tomar el snapshot."
    : !mass ? "Aún no llegan lecturas de masa (Masa, g). Sin ellas no se puede tomar el snapshot."
    : !mass.fresh ? "La última lectura de masa es antigua (" + date(mass.captured_at) + "). Revisa el colchón."
    : base == null ? "Listo para tomar el snapshot." : "Snapshot guardado " + date(st.session.baseline_at) + ".";
}
async function sleepAction(name, done) {
  const buttons = ["sleep-start", "snapshot-btn", "disarm-btn"].map($), id = chosen;
  buttons.forEach(b => b.disabled = true);
  try {
    const st = await api("/api/sleep/" + name, {child_id: id});
    if (id === chosen) renderSleep(st);
    notice(done(st));
  } catch (err) {
    notice(err.message, true);
    $("sleep-msg").textContent = err.message; $("sleep-msg").classList.add("error");
    if (id === chosen) await loadControl();
  }
}
$("sleep-start").onclick = () => sleepAction("start", () => "Sesión de sueño iniciada.");
$("snapshot-btn").onclick = () => sleepAction("snapshot", st => "Snapshot guardado: " + fmt(st.session.baseline_g) + " g = 100 %.");
$("disarm-btn").onclick = () => sleepAction("disarm", () => "Sesión de sueño desarmada. No se envió ningún comando al hardware.");

function renderModules(data) {
  const ops = canOperate();
  $("modules-list").innerHTML = data.modules.map(m => {
    const [name, hint] = MODULES[m.module] || [m.module, ""];
    return '<div class="row module-row"><div><h3>'+esc(name)+'</h3><small>'+esc(hint)+'</small></div><div class="row-actions">'+
      '<label><input type="checkbox" data-module="'+esc(m.module)+'"'+(m.enabled ? " checked" : "")+(ops ? "" : " disabled")+'> Encendido</label>'+
      '<label>Modo<select data-module="'+esc(m.module)+'"'+(ops ? "" : " disabled")+'>'+
      Object.entries(MODES).map(([id, label]) => '<option value="'+id+'"'+(m.mode === id ? " selected" : "")+'>'+esc(label)+'</option>').join("")+
      '</select></label></div></div>';
  }).join("");
  $("modules-msg").textContent = ops ? "" : "Necesitas permiso para operar dispositivos para cambiar los módulos.";
}
$("modules-list").addEventListener("change", async e => {
  const control = e.target.closest("[data-module]");
  if (!control) return;
  const row = control.closest(".module-row"), id = chosen;
  const body = {child_id: id, module: control.dataset.module, enabled: row.querySelector("input").checked, mode: row.querySelector("select").value};
  const fields = row.querySelectorAll("input,select");
  fields.forEach(f => f.disabled = true);
  try {
    const data = await api("/api/modules", body);
    if (id === chosen) renderModules(data);
  } catch (err) {
    notice(err.message, true);
    if (id === chosen) loadControl();
  }
});

function settingsMsg(text, error=false) { $("settings-msg").textContent = text; $("settings-msg").classList.toggle("error", error); }
function fillSettings(s) {
  settingsDirty = false;
  $("led-speed").value = s.led_speed_s; $("audio-select").value = s.audio;
  $("volume").value = s.volume; $("volume-out").textContent = s.volume;
  for (const id of ["led-speed", "audio-select", "volume"]) $(id).disabled = !admin();
  $("settings-save").hidden = !admin();
  settingsMsg(!admin() ? "Solo un administrador puede cambiar estos ajustes." : s.default ? "Usando los valores iniciales." : "");
}
$("volume").oninput = () => $("volume-out").textContent = $("volume").value;
$("settings-form").addEventListener("input", () => { settingsDirty = true; });
$("thr-form").addEventListener("input", () => { thrDirty = true; });
$("settings-form").onsubmit = async ev => {
  ev.preventDefault();
  const raw = $("led-speed").value.trim(), speed = Number(raw), volume = Number($("volume").value);
  if (!raw || !Number.isFinite(speed) || speed < 0.5 || speed > 60) return settingsMsg("La velocidad del LED va de 0,5 a 60 segundos.", true);
  $("settings-save").disabled = true;
  try {
    const saved = await api("/api/settings", {child_id: chosen, led_speed_s: speed, audio: $("audio-select").value, volume});
    fillSettings(saved); settingsFor = saved.child_id;
    settingsMsg("Ajustes guardados. Ningún dispositivo los lee todavía.");
  } catch (err) { settingsMsg(err.message, true); }
  finally { $("settings-save").disabled = false; }
};

// Registro de tomas (SCRUM-51): el cronómetro vive en la página; solo al guardar viaja la duración y el volumen.
let feed = {phase: "idle"};
const clock = seconds => {
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : String(m).padStart(2, "0")) + ":" + String(s).padStart(2, "0");
};
function feedMsg(text, error=false) { $("feed-msg").textContent = text; $("feed-msg").classList.toggle("error", error); }
function feedRender() {
  const p = feed.phase;
  $("feed-start").hidden = p !== "idle"; $("feed-stop").hidden = p !== "running";
  $("feed-save").hidden = $("feed-discard").hidden = p !== "stopped";
  $("feed-volume").disabled = $("feed-unit").disabled = p === "idle";
  $("feed-start").disabled = !canOperate();
  const elapsed = p === "running" ? Math.floor((Date.now() - feed.started) / 1000) : p === "stopped" ? feed.elapsed : 0;
  $("feed-clock").textContent = clock(elapsed);
  const child = state?.children.find(c => c.id === feed.child);
  $("feed-baby").textContent = p !== "idle" && child ? "Toma de " + child.name : "";
  if (p === "idle" && !$("feed-msg").classList.contains("error")) feedMsg(canOperate() ? "" : "Necesitas permiso para operar dispositivos para registrar tomas.");
}
function feedReset() {
  clearInterval(feed.timer);
  feed = {phase: "idle"};
  if ($("feed-clock")) { feedMsg(""); feedRender(); }
}
$("feed-start").onclick = () => {
  if (!chosen || !canOperate()) return;
  feedMsg("");
  feed = {phase: "running", child: chosen, started: Date.now(), timer: setInterval(feedRender, 1000)};
  feedRender();
};
$("feed-stop").onclick = () => {
  clearInterval(feed.timer);
  feed = {...feed, phase: "stopped", elapsed: Math.max(1, Math.round((Date.now() - feed.started) / 1000)), event: uuid4()};
  feedRender();
  $("feed-volume").focus();
};
$("feed-discard").onclick = () => { feedReset(); $("feed-volume").value = ""; };
$("feed-form").onsubmit = async ev => {
  ev.preventDefault();
  if (feed.phase !== "stopped") return;
  const raw = $("feed-volume").value.trim(), volume = Number(raw), unit = $("feed-unit").value;
  if (!raw || !Number.isFinite(volume) || volume <= 0 || volume > 2000) return feedMsg("Escribe un volumen mayor que 0 y de hasta 2000.", true);
  if (!["g", "ml"].includes(unit)) return feedMsg("Elige gramos (g) o mililitros (ml).", true);
  $("feed-save").disabled = true;
  try {
    // El mismo event_id sirve para reintentar: si la respuesta se pierde, el servidor no duplica la toma.
    await api("/api/feeding", {child_id: feed.child, event_id: feed.event, duration_s: feed.elapsed, volume, unit});
    const summary = clock(feed.elapsed) + " · " + fmt(volume) + " " + unit;
    feedReset(); $("feed-volume").value = "";
    feedMsg("Toma guardada: " + summary + ".");
    notice("Toma registrada.");
  } catch (err) { feedMsg(err.message, true); }
  finally { $("feed-save").disabled = false; }
};
refresh().catch(e=>{showLogin();if(e.status!==401)notice(e.message,true);});

