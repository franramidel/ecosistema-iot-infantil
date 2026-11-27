"""Servidor del prototipo. Ejecutar con Python 3.10 o posterior."""
import argparse
import csv
import io
import json
import os
import re
import secrets
import threading
import time
from getpass import getpass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cuna import alimentacion, exportar, fotos, panel
from cuna import voz as canal_voz
from cuna.auditoria import consulta, evento
from cuna.cloud import Cloud
from cuna.common import Problem, device_id, field, member_input, reading
from cuna.local import Local

ROOT = Path(__file__).resolve().parent
STATIC = {"/": ("index.html","text/html"), "/app.js": ("app.js","text/javascript"), "/estilo.css": ("estilo.css","text/css"),
          "/p5.js": ("p5.js","text/javascript"), "/cuna-voz": ("cuna-voz.html","text/html"), "/cuna-voz.js": ("cuna-voz.js","text/javascript")}


def settings():
    cfg = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                cfg[key.strip()] = value.strip()
    return {**cfg, **os.environ}


def payload(action, data):
    if action == "member":
        email, password, role, allowed = member_input(data)
        return dict(email=email,password=password,role=role,can_operate=allowed)
    out = {}
    required = {"child":["name"], "rename":["name","child_id"], "device":["name"], "assign":[],
                "operate":[], "permission":["member_id"]}
    if action not in required:
        raise Problem("Operación desconocida.", 404)
    for key in required[action]:
        out[key] = field(data,key)
    if action in ("device","assign","operate"):
        out["device_id"] = device_id(data)
    if action in ("assign","operate"):
        if "expected" not in data:
            raise Problem("Falta la asignación que muestra el panel.")
        out["expected"] = None if data["expected"] is None else field(data,"expected")
    if action == "assign":
        if "child_id" not in data:
            raise Problem("Elige el destino.")
        out["child_id"] = None if data["child_id"] is None else field(data,"child_id")
    if action in ("operate","permission"):
        if not isinstance(data.get("enabled"), bool):
            raise Problem("El permiso o estado debe ser booleano.")
        out["enabled"] = data["enabled"]
    return out


class Application(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, secure=False):
        super().__init__(address, Handler)
        self.store, self.secure = store, secure
        self.bot_username = None  # solo para mostrar el enlace t.me; el servidor nunca conoce el token
        self.canales = canal_voz.Canales()   # señalización de voz (SCRUM-31 B), solo en memoria
        self.ice_servers = []                # STUN/TURN opcionales; vacío = solo red local
        self.photo_secret = os.environ.get("CUNA_PHOTO_SECRET", "").encode() or secrets.token_bytes(32)
        self.sessions = {}
        self.condition = threading.Condition()
        self.revision = 0
        self.closing = False

    def server_close(self):
        with self.condition:
            self.closing = True
            self.condition.notify_all()
        super().server_close()

    def create_session(self, ctx):
        with self.condition:
            now = time.monotonic()
            self.sessions = {k:v for k,v in self.sessions.items() if v[1] > now}
            sid = secrets.token_urlsafe(32)
            self.sessions[sid] = (ctx, now + 8*3600)
            return sid

    def session(self, sid):
        with self.condition:
            current = self.sessions.get(sid)
            if not current or current[1] <= time.monotonic():
                self.sessions.pop(sid,None)
                raise Problem("Inicia sesión para continuar.", 401)
            return current[0]

    def changed(self):
        with self.condition:
            self.revision += 1
            self.condition.notify_all()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Las solicitudes pueden contener identificadores de menores.

    def response(self, data, mime="application/json", status=200, headers=None):
        if mime == "application/json":
            data = json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length",str(len(data)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Referrer-Policy","same-origin")
        connect = "'self'"
        realtime_origin = getattr(self.server.store, "realtime_origin", None)
        if realtime_origin:
            connect += " " + realtime_origin
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; connect-src " + connect +
                         "; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        for key,value in (headers or {}).items():
            self.send_header(key,value)
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, mime, filename):
        """Envía un archivo por bloques. Los encabezados salen solo cuando el archivo ya está completo.
        Repite los encabezados de seguridad de response() para no modificar ese método, que es de otro módulo."""
        with open(path,"rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length",str(size))
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("Referrer-Policy","same-origin")
            self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
            self.send_header("Content-Disposition",f'attachment; filename="{filename}"')
            self.end_headers()
            while block := handle.read(65536):
                self.wfile.write(block)

    def sid(self):
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie",""))
        return cookie["cuna"].value if "cuna" in cookie else ""

    def cookie(self, value, age):
        return f"cuna={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}" + ("; Secure" if self.server.secure else "")

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def dispatch(self, post):
        try:
            parsed = urlsplit(self.path)
            if post:
                self.post(parsed.path)
            else:
                self.get(parsed.path, parse_qs(parsed.query))
        except Problem as exc:
            self.response({"error":str(exc)},status=exc.status)
        except (ConnectionError, TimeoutError):
            return
        except Exception:
            import traceback
            traceback.print_exc()
            self.response({"error":"Error interno al procesar la solicitud."},status=500)

    def get(self,path,query):
        if path in STATIC:
            name,mime = STATIC[path]
            self.response((ROOT/"web"/name).read_bytes(),mime)
            return
        session = self.server.session(self.sid())
        child = query.get("child",[None])[0]
        if path == "/api/state":
            self.response(self.server.store.state(session,child))
        elif path == "/api/sync":
            self.response(self.server.store.sync_config(session))
        elif path == "/api/thresholds":
            if not child:
                raise Problem("Indica el bebé con ?child=.")
            self.response(self.umbrales().umbrales(session,child))
        elif path == "/api/audit":
            self.response(self.server.store.audit_page(session,*consulta(query)))
        elif path == "/api/history":
            self.response(self.server.store.history(session,*panel.consulta_historial(query)))
        elif path in ("/api/sleep","/api/settings","/api/modules"):
            if not child:
                raise Problem("Indica el bebé con ?child=.")
            child = panel.uuid_texto(child)
            store = self.server.store
            self.response({"/api/sleep":store.sleep_state,"/api/settings":store.settings,"/api/modules":store.modules}[path](session,child))
        elif path == "/api/stream":
            if self.server.store.sync_mode != "sse":
                raise Problem("Esta sesión usa Supabase Realtime.",404)
            self.stream()
        elif path == "/api/report" and child:
            out = io.StringIO(newline="")
            writer = csv.writer(out)
            keys = ["id","child_id","device_id","captured_at","metric","value","unit"]
            writer.writerow(keys)
            after = None
            while True:
                rows = self.server.store.export_page(session,child,after)
                for row in rows:
                    writer.writerow([safe_csv(row[k]) for k in keys])
                if not rows:
                    break
                after = rows[-1]["id"]
            self.response(out.getvalue().encode("utf-8-sig"),"text/csv",headers={
                "Content-Disposition":'attachment; filename="historial.csv"'})
        elif path == "/api/bottle":
            self.response(self.p5("bottle_state")(session, panel.uuid_texto(child) if child else self.pedir_bebe()))
        elif path == "/api/reminders":
            self.response(self.p5("reminders")(session, panel.uuid_texto(child) if child else self.pedir_bebe()))
        elif path == "/api/photos":
            hijo, limite, antes = fotos.consulta_galeria(query)
            salida = self.p5("photo_list")(session, hijo, limite, antes)
            ahora = time.time()
            for item in salida["items"]:   # URL firmadas y con vencimiento: no hay ninguna URL pública (RNF-SEG-04)
                item["url"], item["expires"] = fotos.url_firmada(self.server.photo_secret, item["id"], ahora)
            self.response(salida)
        elif path == "/api/photo":
            ident = panel.uuid_texto(query.get("id", [None])[0], "Enlace de imagen no válido.")
            fotos.verificar_url(self.server.photo_secret, ident, query.get("exp", [None])[0], query.get("sig", [None])[0], time.time())
            tipo, datos = self.p5("photo_read")(session, ident)
            self.response(datos, tipo, headers={"Content-Disposition": "inline"})
        elif path == "/api/voice/config":
            self.response({"ice_servers": [{"urls": self.server.ice_servers}] if self.server.ice_servers else []})
        elif path == "/api/telegram":
            self.response({**self.telegram().telegram_estado(session),"bot":self.server.bot_username})
        elif path == "/api/export/xlsx":
            filtros = {k:v for k,v in consulta(query)[0].items() if k in ("child","device","desde","hasta")}
            export = exportar.generar(self.server.store,session,filtros)
            try:
                self.send_file(export.ruta,exportar.MIME,export.nombre)
            finally:
                export.eliminar()
        else:
            raise Problem("Ruta desconocida.",404)

    def stream(self):
        sid = self.sid()
        store = self.server.store
        version = store.version(self.server.session(sid))
        self.send_response(200)
        self.send_header("Content-Type","text/event-stream")
        self.send_header("Cache-Control","no-cache")
        self.send_header("X-Accel-Buffering","no")
        self.end_headers()
        # El primer evento obliga a releer el snapshot tras conectar o reconectar.
        self.wfile.write(f"data: {version}\n\n".encode())
        self.wfile.flush()
        while True:
            with self.server.condition:
                revision = self.server.revision
                self.server.condition.wait_for(lambda: self.server.closing or self.server.revision != revision, timeout=3)
                if self.server.closing:
                    return
            try:
                latest = store.version(self.server.session(sid))
            except Problem:
                self.wfile.write(b"event: revisar\ndata: {}\n\n")
                self.wfile.flush()
                return
            self.wfile.write(f"data: {latest}\n\n".encode() if latest != version else b": pulso\n\n")
            self.wfile.flush()
            version = latest

    def umbrales(self):
        store = self.server.store
        if not hasattr(store,"umbrales"):
            raise Problem("Este almacén de datos no admite umbrales de temperatura.",501)
        return store

    def telegram(self):
        store = self.server.store
        if not hasattr(store,"telegram_estado"):
            raise Problem("Este almacén de datos no admite Telegram.",501)
        return store

    def p5(self, nombre):
        """Métodos de la Persona 5. Solo los implementa la demo local; Supabase responde 501."""
        metodo = getattr(self.server.store, nombre, None)
        if metodo is None:
            raise Problem("Este almacén de datos aún no admite esta función.", 501)
        return metodo

    @staticmethod
    def pedir_bebe():
        raise Problem("Indica el bebé con ?child=.")

    def subir_foto(self):
        """POST /api/photos: imagen cruda de la cámara. Encabezados: Authorization (clave del dispositivo), X-Device-Id,
        X-Event-Id (UUID, idempotencia) y X-Captured-At (ISO 8601 con zona)."""
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() != self.headers.get("Host", "").lower():
            raise Problem("Origen no permitido.", 403)
        clave = self.device_key()
        try:
            largo = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            largo = 0
        if not 1 <= largo <= fotos.MAX_BYTES:
            raise Problem("La imagen debe pesar entre 1 byte y 5 MB.", 413)
        datos = self.rfile.read(largo)
        dispositivo = device_id({"device_id": self.headers.get("X-Device-Id", "")})
        foto, cuando, tipo = fotos.validar_subida(self.headers, datos)
        return self.p5("photo_ingest")(dispositivo, clave, foto, cuando, tipo, datos)

    def voz(self, path, data):
        """Señalización WebRTC. La app usa su sesión; una cuna real, la clave del dispositivo. Nunca pasa audio por aquí."""
        autorizacion = self.headers.get("Authorization", "")
        con_clave = autorizacion.startswith("Bearer ") and bool(autorizacion[7:])
        if con_clave:
            bebe = self.p5("voice_device")(device_id(data), autorizacion[7:])
        else:
            sesion = self.server.session(self.sid())
            bebe = panel.uuid_texto(data.get("child_id"))
            self.p5("voice_session")(sesion, bebe)
        if path == "/api/voice/signal":
            rol, tipo, datos = canal_voz.validar_senal(data, con_clave)
            return self.server.canales.enviar(bebe, rol, tipo, datos)
        rol = "cuna" if con_clave else data.get("role")
        despues, espera = canal_voz.validar_espera(data)
        return self.server.canales.esperar(bebe, rol, despues, espera)

    def device_key(self):
        """Clave del dispositivo para /api/audit/events. /api/events conserva su propia comprobación original."""
        authorization = self.headers.get("Authorization","")
        if not authorization.startswith("Bearer ") or not authorization[7:]:
            raise Problem("Se requiere la clave del dispositivo.",403)
        return authorization[7:]

    def post(self,path):
        if path == "/api/photos":
            result = self.subir_foto()
            self.server.changed()
            self.response(result)
            return
        if self.headers.get_content_type() != "application/json":
            raise Problem("Se requiere JSON.",415)
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() != self.headers.get("Host","").lower():
            raise Problem("Origen no permitido.",403)
        try:
            length = int(self.headers.get("Content-Length","0"))
            if length < 1 or length > 16384:
                raise ValueError()
            data = json.loads(self.rfile.read(length))
            if not isinstance(data,dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise Problem("Cuerpo JSON inválido.")
        store = self.server.store
        if path in ("/api/voice/signal", "/api/voice/poll"):
            self.response(self.voz(path, data))   # sin changed(): la señalización no es un cambio de datos
            return
        if path == "/api/login":
            email = data.get("email")
            password = data.get("password")
            if not isinstance(email,str) or not isinstance(password,str) or len(password)>1024:
                raise Problem("Correo o contraseña incorrectos.",401)
            ctx = store.login(email.strip().lower(),password)
            sid = self.server.create_session(ctx)
            self.response({},headers={"Set-Cookie":self.cookie(sid,8*3600)})
            return
        if path == "/api/logout":
            with self.server.condition:
                self.server.sessions.pop(self.sid(),None)
            self.server.changed()
            self.response({},headers={"Set-Cookie":self.cookie("",0)})
            return
        if path == "/api/events":
            authorization = self.headers.get("Authorization","")
            if not authorization.startswith("Bearer ") or not authorization[7:]:
                raise Problem("Se requiere la clave del dispositivo.",403)
            result = store.ingest(device_id(data),authorization[7:],reading(data))
        elif path == "/api/thresholds":
            session = self.server.session(self.sid())
            result = self.umbrales().umbrales_guardar(session,field(data,"child_id"),data.get("temp_min"),data.get("temp_max"))
        elif path == "/api/device/thresholds":
            result = self.umbrales().umbrales_dispositivo(device_id(data),self.device_key())
        elif path == "/api/audit/events":
            result = store.record(device_id(data),self.device_key(),evento(data))
        elif path in ("/api/telegram/link","/api/telegram/unlink","/api/telegram/test"):
            session = self.server.session(self.sid())
            store = self.telegram()
            if path.endswith("/link"):
                result = store.telegram_codigo(session)
                bot = self.server.bot_username
                result = {**result,"bot":bot,"enlace":f"https://t.me/{bot}?start={result['codigo']}" if bot else None}
            elif path.endswith("/unlink"):
                result = store.telegram_desvincular(session)
            else:
                result = store.telegram_probar(session)
        elif path == "/api/bottle/thresholds":
            session = self.server.session(self.sid())
            result = self.p5("bottle_thresholds_save")(session, panel.uuid_texto(data.get("child_id")), data.get("temp_min"), data.get("temp_max"))
        elif path == "/api/reminders":
            session = self.server.session(self.sid())
            hijo, accion = panel.uuid_texto(data.get("child_id")), data.get("action")
            if accion == "delete":
                result = self.p5("reminders_change")(session, hijo, "delete", data.get("id"))
            elif accion == "save":
                result = self.p5("reminders_change")(session, hijo, "save", alimentacion.validar_recordatorio(data))
            else:
                raise Problem("action debe ser save o delete.")
        elif path == "/api/feeding":
            session = self.server.session(self.sid())
            result = store.feeding_log(session,*panel.validar_toma(data))
        elif path in ("/api/sleep/start","/api/sleep/snapshot","/api/sleep/disarm"):
            session = self.server.session(self.sid())
            result = store.sleep_action(session,panel.uuid_texto(data.get("child_id")),path.rsplit("/",1)[1])
        elif path == "/api/settings":
            session = self.server.session(self.sid())
            result = store.settings_save(session,*panel.validar_ajustes(data))
        elif path == "/api/modules":
            session = self.server.session(self.sid())
            result = store.modules_save(session,*panel.validar_modulo(data))
        elif path.startswith("/api/change/"):
            session = self.server.session(self.sid())
            action = path.removeprefix("/api/change/")
            result = store.change(session,action,payload(action,data))
        else:
            raise Problem("Ruta desconocida.",404)
        self.server.changed()
        self.response(result)


def safe_csv(value):
    if isinstance(value,str) and (value.lstrip().startswith(("=","+","-","@")) or value.startswith(("\t","\r","\n"))):
        return "'" + value
    return value


def main():
    parser = argparse.ArgumentParser(description="Cuna inteligente · Usuarios y cuentas")
    parser.add_argument("--supabase",action="store_true",help="Usar el proyecto configurado en .env")
    parser.add_argument("--host",default="127.0.0.1")
    parser.add_argument("--port",default=8000,type=int)
    args = parser.parse_args()
    cfg = settings()
    if args.supabase:
        try:
            store = Cloud(cfg["SUPABASE_URL"],cfg["SUPABASE_PUBLISHABLE_KEY"],cfg["SUPABASE_SECRET_KEY"])
        except (KeyError,ValueError) as exc:
            parser.exit(2,f"Revisa .env y la guía de Supabase ({exc}).\n")
    else:
        dbpath = Path(cfg.get("CUNA_DB", ROOT/"datos"/"demo.sqlite3"))
        dbpath.parent.mkdir(parents=True,exist_ok=True)
        store = Local(dbpath)
        with store.connection() as db:
            first = not db.execute("select 1 from members limit 1").fetchone()
        if first:
            print("Primer inicio de la demo. Crea tu cuenta de administrador.")
            try:
                email,pw,_,_ = member_input(dict(email=input("Correo: "),password=getpass("Contraseña (10 caracteres o más): "),role="admin"))
            except Problem as exc:
                parser.exit(2,str(exc)+"\n")
            store.bootstrap(email,pw)
    server = Application((args.host,args.port),store,cfg.get("CUNA_SECURE_COOKIE")=="1")
    username = (cfg.get("TELEGRAM_BOT_USERNAME") or "").lstrip("@")
    server.ice_servers = [u.strip() for u in (cfg.get("CUNA_ICE_SERVERS") or "").split(",") if u.strip().startswith(("stun:", "turn:", "turns:"))]
    server.bot_username = username if re.fullmatch(r"[A-Za-z0-9_]{5,32}",username) else None
    print(f"Cuna inteligente · {'Supabase' if args.supabase else 'Demo local'} · http://{args.host}:{server.server_port}",flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

