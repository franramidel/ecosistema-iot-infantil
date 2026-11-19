const {JSDOM} = require("jsdom");
const fs = require("fs");
const root = process.argv[2], base = "http://127.0.0.1:8765";
const html = fs.readFileSync(root + "/web/index.html", "utf8"), js = fs.readFileSync(root + "/web/app.js", "utf8");
const sleep = ms => new Promise(r => setTimeout(r, ms));
let fallos = 0;
const ok = (c, m) => { console.log((c ? "OK   " : "FALLA") + " " + m); if (!c) fallos++; };

function cliente() {
  let cookie = "";
  const f = async (path, opts = {}) => {
    const res = await fetch(base + path, {...opts, headers: {...(opts.headers || {}), cookie}});
    const sc = res.headers.get("set-cookie"); if (sc) cookie = sc.split(";")[0];
    return res;
  };
  f.post = (path, body) => f(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  return f;
}

async function pantalla(email, password) {
  const descargas = [], llamadas = [];
  const f = cliente();
  const dom = new JSDOM(html, {url: base + "/", runScripts: "outside-only", pretendToBeVisual: true});
  const w = dom.window;
  w.fetch = (p, o) => { llamadas.push(p); return f(p, o); };
  w.EventSource = class { constructor() {} close() {} addEventListener() {} };
  w.URL.createObjectURL = b => { descargas.push({blob: b}); return "blob:prueba"; };
  w.URL.revokeObjectURL = () => {};
  const lienzo = new Proxy({}, {get: (t, k) => (k in t ? t[k] : () => {}), set: (t, k, v) => { t[k] = v; return true; }});  // el gráfico usa más métodos del canvas
  w.HTMLCanvasElement.prototype.getContext = () => lienzo;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLAnchorElement.prototype.click = function () { if (this.download) descargas.at(-1).nombre = this.download; };
  w.eval(js);
  await sleep(300);
  w.document.querySelector("#signin [name=email]").value = email;
  w.document.querySelector("#signin [name=password]").value = password;
  w.document.getElementById("signin").dispatchEvent(new w.Event("submit", {cancelable: true, bubbles: true}));
  await sleep(600);
  return {w, d: w.document, descargas, llamadas, f};
}

(async () => {
  const admin = cliente();
  await admin.post("/api/login", {email: "padre@example.test", password: "Demo_prueba01"});
  const hijo = (await (await admin.post("/api/change/child", {name: "Luna"})).json()).id;
  await admin.post("/api/change/device", {device_id: "TEMP-01", name: "Sensor"});
  await admin.post("/api/change/assign", {expected: null, child_id: hijo, device_id: "TEMP-01"});
  await admin.post("/api/change/member", {email: "cuida@example.test", password: "Demo_segura02", role: "caregiver", can_operate: false});

  const a = await pantalla("padre@example.test", "Demo_prueba01");
  const tarjeta = a.d.getElementById("export-card");
  ok(!a.d.getElementById("dashboard").hidden && !tarjeta.hidden, "el administrador ve el formulario de exportación");
  ok(a.d.getElementById("export-child").value === hijo, "el bebé seleccionado es el predeterminado");
  ok([...a.d.getElementById("export-device").options].map(o => o.value).join() === ",TEMP-01", "selector de dispositivos con Device_ID");
  ok(a.d.getElementById("report") !== null, "el enlace de CSV existente sigue presente");

  a.d.getElementById("export-from").value = "2026-03-10";
  a.d.getElementById("export-to").value = "2026-03-01";
  const antes = a.llamadas.length;
  a.d.getElementById("export-form").dispatchEvent(new a.w.Event("submit", {cancelable: true, bubbles: true}));
  await sleep(100);
  ok(a.llamadas.length === antes && /posterior/.test(a.d.getElementById("export-status").textContent), "rango invertido: mensaje y sin petición");
  ok(a.d.getElementById("export-status").classList.contains("error"), "el error se marca como error");

  a.d.getElementById("export-from").value = "2026-03-01";
  a.d.getElementById("export-to").value = "2026-03-10";
  a.d.getElementById("export-device").value = "TEMP-01";
  a.d.getElementById("export-form").dispatchEvent(new a.w.Event("submit", {cancelable: true, bubbles: true}));
  await sleep(40);
  ok(a.d.getElementById("export-go").disabled && /Generando/.test(a.d.getElementById("export-status").textContent), "indicador de generación y botón bloqueado");
  await sleep(800);
  const peticion = a.llamadas.at(-1);
  ok(/^\/api\/export\/xlsx\?/.test(peticion) && peticion.includes("child=" + hijo) && peticion.includes("device=TEMP-01") &&
     peticion.includes("desde=2026-03-01") && peticion.includes("hasta=2026-03-10"), "la petición lleva los cuatro filtros: " + peticion);
  const d = a.descargas.at(-1);
  const bytes = Buffer.from(await d.blob.arrayBuffer());
  ok(bytes.subarray(0, 2).toString() === "PK" && /^nido-reporte-bebe-.*\.xlsx$/.test(d.nombre), "se descarga un .xlsx con nombre descriptivo: " + d.nombre);
  ok(!a.d.getElementById("export-go").disabled && /Descarga lista/.test(a.d.getElementById("export-status").textContent), "el botón se libera y se informa la descarga");

  a.d.getElementById("export-from").value = ""; a.d.getElementById("export-to").value = "";
  a.d.getElementById("export-child").innerHTML += '<option value="00000000-0000-4000-8000-000000000000">Falso</option>';
  a.d.getElementById("export-child").value = "00000000-0000-4000-8000-000000000000";
  a.d.getElementById("export-form").dispatchEvent(new a.w.Event("submit", {cancelable: true, bubbles: true}));
  await sleep(700);
  ok(/no disponible/i.test(a.d.getElementById("export-status").textContent) && a.d.getElementById("export-status").classList.contains("error"), "error del servidor se muestra: " + a.d.getElementById("export-status").textContent);

  const c = await pantalla("cuida@example.test", "Demo_segura02");
  ok(!c.d.getElementById("dashboard").hidden && c.d.getElementById("export-card").hidden, "el cuidador no ve la tarjeta de exportación");
  const directo = await c.f("/api/export/xlsx");
  ok(directo.status === 403, "la llamada directa del cuidador devuelve 403 (" + directo.status + ")");
  const anonimo = await fetch(base + "/api/export/xlsx");
  ok(anonimo.status === 401, "sin sesión devuelve 401 (" + anonimo.status + ")");
  console.log(fallos ? "HAY FALLOS: " + fallos : "UI OK");
  process.exit(fallos ? 1 : 0);
})();
