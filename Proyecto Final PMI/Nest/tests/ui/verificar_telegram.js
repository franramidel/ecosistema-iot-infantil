// Verifica la tarjeta de Telegram (SCRUM-24) con jsdom contra el servidor de tests/ui/servidor.py.
const {JSDOM} = require("jsdom");
const fs = require("fs");
const {execFileSync} = require("child_process");
const os = require("os"), path = require("path");
const root = process.argv[2], base = "http://127.0.0.1:8765";
const html = fs.readFileSync(root + "/web/index.html", "utf8"), js = fs.readFileSync(root + "/web/app.js", "utf8");
const sleep = ms => new Promise(r => setTimeout(r, ms));
let fallos = 0;
const ok = (c, m) => { console.log((c ? "OK   " : "FALLA") + " " + m); if (!c) fallos++; };

function cliente() {
  let cookie = "";
  const f = async (p, o = {}) => {
    const res = await fetch(base + p, {...o, headers: {...(o.headers || {}), cookie}});
    const sc = res.headers.get("set-cookie"); if (sc) cookie = sc.split(";")[0];
    return res;
  };
  return f;
}
async function pantalla(email, password) {
  const f = cliente();
  const dom = new JSDOM(html, {url: base + "/", runScripts: "outside-only", pretendToBeVisual: true});
  const w = dom.window;
  w.fetch = (p, o) => f(p, o && o.method === "POST" ? {...o, headers: {...o.headers, Origin: base}} : o);
  w.EventSource = class { constructor() {} close() {} addEventListener() {} };
  const lienzo = new Proxy({}, {get: (t, k) => (k in t ? t[k] : () => {}), set: (t, k, v) => { t[k] = v; return true; }});  // el gráfico usa más métodos del canvas
  w.HTMLCanvasElement.prototype.getContext = () => lienzo;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.eval(js);
  await sleep(300);
  w.document.querySelector("#signin [name=email]").value = email;
  w.document.querySelector("#signin [name=password]").value = password;
  w.document.getElementById("signin").dispatchEvent(new w.Event("submit", {cancelable: true, bubbles: true}));
  await sleep(800);
  return {w, d: w.document};
}
const click = async (p, id) => { p.d.getElementById(id).dispatchEvent(new p.w.Event("click", {bubbles: true})); await sleep(500); };

(async () => {
  const a = await pantalla("padre@example.test", "Demo_prueba01");
  const $ = id => a.d.getElementById(id);
  ok(!$("telegram-card").hidden, "la tarjeta de Telegram se muestra");
  ok($("tg-state").textContent.includes("no está vinculado"), "estado inicial: no vinculado");
  ok(!$("tg-link-btn").hidden && $("tg-test-btn").hidden && $("tg-unlink-btn").hidden, "solo se ofrece vincular");
  await click(a, "tg-link-btn");
  const codigo = $("tg-code-value").textContent;
  ok(/^[A-HJ-NP-Z2-9]{8}$/.test(codigo) && !$("tg-code").hidden, "se muestra un código de 8 caracteres");
  ok($("tg-link").href === "https://t.me/cuna_prueba_bot?start=" + codigo, "el enlace abre el bot con el código");
  ok($("tg-link").rel.includes("noopener"), "el enlace externo usa noopener");

  // Se simula que el bot ya recibió el código: se escribe el vínculo directamente en la base de prueba.
  execFileSync("python3", ["-c", "import sqlite3,sys;d=sqlite3.connect(sys.argv[1]);m=d.execute(\"select id,family_id from members where email='padre@example.test'\").fetchone();d.execute(\"update telegram_links set chat_id='777',linked_at='2026-01-01T00:00:00+00:00' where member_id=?\",(m[0],));d.commit()", path.join(os.tmpdir(), "nido-ui-prueba.db")]);
  await click(a, "tg-refresh-btn");
  ok($("tg-state").textContent === "Telegram vinculado.", "tras actualizar aparece vinculado");
  ok($("tg-link-btn").hidden && !$("tg-test-btn").hidden && !$("tg-unlink-btn").hidden && $("tg-code").hidden, "se ofrece probar y desvincular, el código se oculta");
  ok($("tg-msg").textContent.includes("no está activo"), "avisa que el procesador no está activo");
  await click(a, "tg-test-btn");
  ok($("tg-msg").textContent.includes("Última prueba: en cola") || $("tg-msg").textContent.includes("Prueba en cola"), "la prueba queda en cola");
  await click(a, "tg-test-btn");
  ok($("tg-msg").classList.contains("error") || $("tg-msg").textContent.includes("Espera"), "una segunda prueba inmediata se limita");
  await click(a, "tg-unlink-btn");
  ok($("tg-state").textContent.includes("no está vinculado") && !$("tg-link-btn").hidden, "desvincular vuelve al estado inicial");
  const html2 = a.d.documentElement.outerHTML;
  ok(!/style="/.test(html2.replace(/<canvas[^>]*>/g, "")), "la tarjeta no usa estilos en línea (respeta la CSP)");
  ok(!html2.includes("javascript:"), "sin enlaces javascript:");
  console.log(fallos ? "UI FALLÓ" : "UI OK");
  process.exit(fallos ? 1 : 0);
})();
