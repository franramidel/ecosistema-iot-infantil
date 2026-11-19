// Verifica con jsdom las pantallas del Sprint 2 contra el servidor de tests/ui/servidor.py:
// consola de auditoría (SCRUM-55), gráfica (SCRUM-54), sesión de sueño y snapshot (SCRUM-35), tomas (SCRUM-51),
// ajustes (SCRUM-57) y panel de control (SCRUM-56). jsdom es un DOM simulado, no un navegador real: no mide el dibujo
// real del canvas (se registran las llamadas de texto) ni la CSP del navegador.
const {JSDOM} = require("jsdom");
const fs = require("fs");
const crypto = require("crypto");
const root = process.argv[2], base = "http://127.0.0.1:8765";
const html = fs.readFileSync(root + "/web/index.html", "utf8"), js = fs.readFileSync(root + "/web/app.js", "utf8");
const sleep = ms => new Promise(r => setTimeout(r, ms));
let fallos = 0;
const ok = (c, m) => { console.log((c ? "OK   " : "FALLA") + " " + m); if (!c) fallos++; };
const uuid = () => crypto.randomUUID();

function cliente() {
  let cookie = "";
  const f = async (p, o = {}) => {
    const res = await fetch(base + p, {...o, headers: {...(o.headers || {}), cookie}});
    const sc = res.headers.get("set-cookie"); if (sc) cookie = sc.split(";")[0];
    return res;
  };
  f.json = async (p, body, headers = {}) => {
    const res = await f(p, body === undefined ? {} : {method: "POST", body: JSON.stringify(body),
      headers: {"Content-Type": "application/json", Origin: base, ...headers}});
    return {status: res.status, data: await res.json().catch(() => ({}))};
  };
  return f;
}
async function pantalla(email, password, f = cliente()) {
  const dom = new JSDOM(html, {url: base + "/", runScripts: "outside-only", pretendToBeVisual: true});
  const w = dom.window, pedidos = [], texto = [];
  w.fetch = (p, o) => { pedidos.push(p); return f(p, o && o.method === "POST" ? {...o, headers: {...o.headers, Origin: base}} : o); };
  w.EventSource = class { constructor() {} close() {} addEventListener() {} };
  const lienzo = new Proxy({}, {get: (t, k) => k === "fillText" ? txt => texto.push(String(txt)) : (k in t ? t[k] : () => {}), set: (t, k, v) => { t[k] = v; return true; }});
  w.HTMLCanvasElement.prototype.getContext = () => lienzo;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.URL.createObjectURL = () => "blob:prueba"; w.URL.revokeObjectURL = () => {};
  w.HTMLAnchorElement.prototype.click = function () { pedidos.push("descarga:" + this.download); };
  w.eval(js);
  await sleep(300);
  w.document.querySelector("#signin [name=email]").value = email;
  w.document.querySelector("#signin [name=password]").value = password;
  w.document.getElementById("signin").dispatchEvent(new w.Event("submit", {cancelable: true, bubbles: true}));
  await sleep(900);
  return {w, d: w.document, pedidos, texto, f};
}
const ev = (p, id, tipo = "click") => p.d.getElementById(id).dispatchEvent(new p.w.Event(tipo, {bubbles: true, cancelable: true}));
const click = async (p, id, ms = 500) => { ev(p, id); await sleep(ms); };
const tab = async (p, nombre) => { p.d.querySelector(`[data-tab=${nombre}]`).dispatchEvent(new p.w.Event("click", {bubbles: true})); await sleep(600); };
const poner = (p, id, valor) => { p.d.getElementById(id).value = valor; };
const texto = (p, id) => p.d.getElementById(id).textContent;
const visible = (p, id) => !p.d.getElementById(id).hidden;

(async () => {
  const admin = await pantalla("padre@example.test", "Demo_prueba01");
  const api = admin.f.json;
  // --- datos: un bebé, un dispositivo asignado y lecturas
  const bebe = (await api("/api/change/child", {name: "Bebé <b>UI</b>"})).data.id;
  const clave = (await api("/api/change/device", {device_id: "UI-1", name: "Sensor UI"})).data.secret;
  await api("/api/change/assign", {device_id: "UI-1", expected: null, child_id: bebe});
  const dispositivo = async (ruta, cuerpo) => (await fetch(base + ruta, {method: "POST", body: JSON.stringify(cuerpo),
    headers: {"Content-Type": "application/json", Authorization: "Bearer " + clave}})).status;
  const ahora = () => new Date().toISOString().replace("Z", "+00:00");
  for (const [metrica, valor, unidad] of [["Temperatura", 21.5, "°C"], ["Humedad", 55, "%"], ["Temperatura", 23.4, "°C"], ["Humedad", 60, "%"]]) {
    await dispositivo("/api/events", {event_id: uuid(), device_id: "UI-1", captured_at: ahora(), metric: metrica, value: valor, unit: unidad});
  }
  await dispositivo("/api/audit/events", {event_id: uuid(), device_id: "UI-1", event_type: "PRUEBA_XSS", detail: '<img src=x id="xss"><script>window.hackeado=1</script>', occurred_at: ahora()});
  for (let i = 0; i < 105; i++) await dispositivo("/api/audit/events", {event_id: uuid(), device_id: "UI-1", event_type: "RELLENO", detail: "evento " + i, occurred_at: ahora()});
  await sleep(300);
  admin.w.location.reload = () => {};

  const a = await pantalla("padre@example.test", "Demo_prueba01", admin.f);

  // --- SCRUM-55: consola de auditoría
  ok(visible(a, "audit-tab"), "el administrador ve la pestaña Auditoría");
  await tab(a, "audit");
  ok(visible(a, "audit"), "la sección de auditoría se muestra");
  const filas = () => a.d.querySelectorAll("#audit-table tbody tr").length;
  ok(filas() === 100, "primera página de 100 eventos (" + filas() + ")");
  ok(visible(a, "audit-more"), "hay botón Cargar más cuando existe cursor next");
  ok(a.d.getElementById("xss") === null && a.w.hackeado === undefined, "el detalle con HTML se muestra como texto (escapado)");
  await click(a, "audit-more", 700);
  ok(filas() > 100 && !visible(a, "audit-more"), "Cargar más agrega la siguiente página y se oculta al terminar (" + filas() + ")");
  ok(a.d.getElementById("xss") === null, "el HTML sigue escapado en la segunda página");
  ok(/PRUEBA_XSS/.test(texto(a, "audit-table")) && /&lt;|<img/.test(a.d.getElementById("audit-table").textContent), "el texto del evento aparece literal");
  const fechas = [...a.d.querySelectorAll("#audit-table tbody tr")].map(r => r.cells[0].textContent + r.cells[1].textContent);
  ok(JSON.stringify(fechas) === JSON.stringify([...fechas].sort()), "orden cronológico ascendente");
  ok(/Bebé <b>UI<\/b>/.test(a.d.querySelector("#audit-table tbody tr").cells[2].textContent) && a.d.querySelector("#audit-table b") === null, "el nombre del bebé también se escapa");
  poner(a, "audit-child", bebe); poner(a, "audit-device", "UI-1"); poner(a, "audit-from", "2026-01-01"); poner(a, "audit-to", "2999-01-01");
  await a.d.getElementById("audit-form").dispatchEvent(new a.w.Event("submit", {cancelable: true, bubbles: true})); await sleep(600);
  const consulta = a.pedidos.filter(p => String(p).startsWith("/api/audit?")).at(-1);
  ok(/child=/.test(consulta) && /device=UI-1/.test(consulta) && /desde=2026-01-01/.test(consulta) && /hasta=2999-01-01/.test(consulta) && /limit=100/.test(consulta), "la búsqueda envía bebé, dispositivo y rango: " + consulta);
  poner(a, "audit-from", "2026-05-01"); poner(a, "audit-to", "2026-04-01");
  a.d.getElementById("audit-form").dispatchEvent(new a.w.Event("submit", {cancelable: true, bubbles: true})); await sleep(300);
  ok(/no puede ser posterior/.test(texto(a, "audit-status")), "rango invertido se rechaza en la pantalla");
  poner(a, "audit-from", ""); poner(a, "audit-to", "");
  await click(a, "audit-export", 800);
  ok(a.pedidos.some(p => String(p).startsWith("/api/export/xlsx?") && /child=/.test(p)) && a.pedidos.some(p => String(p).startsWith("descarga:")), "Exportar a Excel usa el mismo endpoint con los filtros");

  // --- SCRUM-54: gráfica
  await tab(a, "profile");
  ok(visible(a, "graph"), "la gráfica se muestra con lecturas");
  ok(a.texto.some(t => t === "°C") && a.texto.some(t => /% HR/.test(t)), "ejes con unidades °C y % HR");
  ok(a.texto.some(t => /máx 22 °C/.test(t)) && a.texto.some(t => /mín 19 °C/.test(t)), "umbrales 19 y 22 marcados en la gráfica");
  ok(/Umbrales 19 a 22 °C/.test(texto(a, "graph-legend")) && /Temperatura/.test(texto(a, "graph-legend")) && /Humedad/.test(texto(a, "graph-legend")), "leyenda con ambas series y los umbrales");
  ok(/En vivo: 2 lecturas de temperatura y 2 de humedad/.test(texto(a, "graph-info")), "modo en vivo cuenta ambas series: " + texto(a, "graph-info"));
  a.texto.length = 0;
  poner(a, "range-from", "2026-01-01"); poner(a, "range-to", "2999-01-01");
  a.d.getElementById("range-form").dispatchEvent(new a.w.Event("submit", {cancelable: true, bubbles: true})); await sleep(900);
  const hist = a.pedidos.filter(p => String(p).startsWith("/api/history?"));
  ok(hist.some(p => /metric=Temperatura/.test(p)) && hist.some(p => /metric=Humedad/.test(p)) && hist.every(p => /desde=2026-01-01/.test(p)), "el histórico pide temperatura y humedad por rango a /api/history");
  ok(/Histórico: 2 lecturas de temperatura y 2 de humedad/.test(texto(a, "graph-info")) && visible(a, "range-live"), "se muestra el histórico: " + texto(a, "graph-info"));
  poner(a, "range-from", "2026-05-01"); poner(a, "range-to", "2026-04-01");
  a.d.getElementById("range-form").dispatchEvent(new a.w.Event("submit", {cancelable: true, bubbles: true})); await sleep(200);
  ok(/no puede ser posterior/.test(texto(a, "graph-info")), "rango histórico invertido se rechaza");
  await click(a, "range-live");
  ok(!visible(a, "range-live") && /En vivo/.test(texto(a, "graph-info")), "volver a en vivo");

  // --- SCRUM-35 / SCRUM-57: sesión de sueño
  await tab(a, "control");
  ok(a.d.getElementById("snapshot-btn").disabled && a.d.getElementById("disarm-btn").disabled && !a.d.getElementById("sleep-start").disabled, "sin sesión: Snapshot y Desarmar deshabilitados");
  ok(/Sin sesión activa/.test(texto(a, "sleep-status")), "estado sin sesión");
  ok(a.d.getElementById("disarm-btn").classList.contains("danger"), "el botón de desarme tiene el estilo prioritario");
  await click(a, "sleep-start", 700);
  ok(/Sesión activa/.test(texto(a, "sleep-status")) && !a.d.getElementById("snapshot-btn").disabled, "la sesión queda activa");
  await click(a, "snapshot-btn", 700);
  ok(/lecturas de masa/.test(texto(a, "sleep-msg")) && !/g = 100/.test(texto(a, "baseline-value")), "sin lectura de masa: mensaje claro y ninguna línea base inventada: " + texto(a, "sleep-msg"));
  await dispositivo("/api/events", {event_id: uuid(), device_id: "UI-1", captured_at: ahora(), metric: "Masa", value: 5000, unit: "g"});
  await click(a, "snapshot-btn", 800);
  ok(/5.?000 g = 100 %/.test(texto(a, "baseline-value")), "Snapshot guarda 5000 g = 100 %: " + texto(a, "baseline-value"));
  await dispositivo("/api/events", {event_id: uuid(), device_id: "UI-1", captured_at: ahora(), metric: "Masa", value: 4950, unit: "g"});
  a.d.dispatchEvent(new a.w.Event("visibilitychange")); await sleep(700);
  ok(/4.?950 g/.test(texto(a, "mass-value")) && /99/.test(texto(a, "mass-value")), "masa actual y porcentaje de la línea base: " + texto(a, "mass-value"));
  await click(a, "disarm-btn", 700);
  ok(/Sin sesión activa/.test(texto(a, "sleep-status")) && a.d.getElementById("disarm-btn").disabled, "Desarmar cierra la sesión");
  ok(/no envió ningún comando al hardware|no env/.test(a.d.getElementById("notice").textContent) || /no envía ningún comando/.test(a.d.getElementById("sleep-card").textContent), "la interfaz aclara que no se acciona hardware");

  // --- SCRUM-56: panel de control
  const filasModulos = a.d.querySelectorAll("#modules-list .module-row");
  ok(filasModulos.length === 4, "cuatro módulos: sensor de temperatura, luz, audio y colchón");
  const luz = a.d.querySelector('#modules-list input[type=checkbox][data-module=luz]');
  luz.checked = true; luz.dispatchEvent(new a.w.Event("change", {bubbles: true})); await sleep(600);
  const modo = a.d.querySelector('#modules-list select[data-module=luz]');
  modo.value = "programado"; modo.dispatchEvent(new a.w.Event("change", {bubbles: true})); await sleep(600);
  const estadoLuz = (await (await admin.f("/api/modules?child=" + bebe)).json()).modules.find(m => m.module === "luz");
  ok(estadoLuz.enabled === true && estadoLuz.mode === "programado", "encendido y modo programado quedan guardados en el servidor");
  const b = await pantalla("padre@example.test", "Demo_prueba01", admin.f);
  await tab(b, "control");
  ok(b.d.querySelector('#modules-list input[data-module=luz]').checked && b.d.querySelector('#modules-list select[data-module=luz]').value === "programado", "tras recargar la pantalla el estado se conserva");

  // --- SCRUM-57: luces y audio
  ok(a.d.getElementById("voice-btn").disabled && /no hay llamada real/.test(texto(a, "voice-msg")), "el botón de voz es solo interfaz y lo dice");
  poner(a, "led-speed", "12"); poner(a, "audio-select", "olas"); poner(a, "volume", "65");
  a.d.getElementById("settings-form").dispatchEvent(new a.w.Event("submit", {cancelable: true, bubbles: true})); await sleep(700);
  const ajustes = await (await admin.f("/api/settings?child=" + bebe)).json();
  ok(ajustes.led_speed_s === 12 && ajustes.audio === "olas" && ajustes.volume === 65, "los ajustes se guardan en el servidor");
  poner(a, "led-speed", "99");
  a.d.getElementById("settings-form").dispatchEvent(new a.w.Event("submit", {cancelable: true, bubbles: true})); await sleep(300);
  ok(/0,5 a 60/.test(texto(a, "settings-msg")) && (await (await admin.f("/api/settings?child=" + bebe)).json()).led_speed_s === 12, "velocidad inválida se rechaza sin guardar");

  // --- SCRUM-51: tomas
  ok(visible(a, "feed-start") && !visible(a, "feed-stop"), "inicio: solo Start Feeding");
  await click(a, "feed-start", 1300);
  ok(visible(a, "feed-stop") && !visible(a, "feed-start") && /^00:0[1-9]$/.test(texto(a, "feed-clock")), "el cronómetro corre: " + texto(a, "feed-clock"));
  await click(a, "feed-stop");
  ok(visible(a, "feed-save") && !a.d.getElementById("feed-volume").disabled, "al detener aparece Guardar toma");
  poner(a, "feed-volume", "0");
  a.d.getElementById("feed-form").dispatchEvent(new a.w.Event("submit", {cancelable: true, bubbles: true})); await sleep(300);
  ok(/mayor que 0/.test(texto(a, "feed-msg")) && !a.pedidos.some(p => p === "/api/feeding"), "volumen inválido no se envía");
  poner(a, "feed-volume", "120"); poner(a, "feed-unit", "ml");
  a.d.getElementById("feed-form").dispatchEvent(new a.w.Event("submit", {cancelable: true, bubbles: true})); await sleep(800);
  ok(visible(a, "feed-start") && /Toma guardada/.test(texto(a, "feed-msg")), "la toma se guarda y el cronómetro vuelve a cero");
  const tomas = await (await admin.f("/api/audit?tipo=TOMA&child=" + bebe)).json();
  ok(tomas.items.length === 1 && /Toma: \d+ s · 120\.0 ml/.test(tomas.items[0].detail), "la toma está en la auditoría del bebé: " + (tomas.items[0] || {}).detail);

  // --- cuidador sin permiso: sin auditoría y con controles bloqueados
  await api("/api/change/member", {email: "cuida@example.test", password: "Demo_segura02", role: "caregiver", can_operate: false});
  const c = await pantalla("cuida@example.test", "Demo_segura02");
  ok(!visible(c, "audit-tab"), "el cuidador no ve la pestaña Auditoría");
  await tab(c, "control");
  ok(c.d.getElementById("snapshot-btn").disabled && c.d.getElementById("sleep-start").disabled && c.d.getElementById("feed-start").disabled, "el cuidador sin permiso no puede operar");
  ok(c.d.querySelector("#modules-list input").disabled && c.d.getElementById("led-speed").disabled, "módulos y ajustes quedan en solo lectura");
  await tab(c, "audit");
  ok(!visible(c, "audit"), "forzar la pestaña de auditoría no la muestra a un cuidador");

  console.log(fallos ? "UI FALLÓ: " + fallos : "UI OK");
  process.exit(fallos ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
