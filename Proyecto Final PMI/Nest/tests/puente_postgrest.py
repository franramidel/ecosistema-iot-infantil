"""Puente HTTP de pruebas entre cuna/cloud.py y un PostgreSQL local. NO es Supabase ni PostgREST.

Imita solo la ruta /rest/v1/rpc/<función> de PostgREST. Cada llamada ejecuta la función real de
supabase/*.sql en PostgreSQL, dentro de una transacción, con el rol que corresponde a la credencial:

- apikey igual a la clave secreta de pruebas  -> rol service_role, sin usuario.
- Authorization: Bearer jwt-<uuid>            -> rol authenticated con ese usuario en request.jwt.claim.sub.
- apikey igual a la clave pública de pruebas  -> rol anon.

Así se prueban de verdad el SQL, los permisos de cada rol y el código de Cloud. Lo que NO se prueba:
PostgREST, Supabase Auth, los JWT firmados, la red ni los límites del plan. Requiere psql.
"""
import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit

CLAVE_PUBLICA = "sb_publishable_prueba"
CLAVE_SECRETA = "sb_secret_prueba"
NOMBRE = re.compile(r"[a-z_][a-z0-9_]*")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def url_de(base, nombre):
    """Cambia el nombre de la base en una URL libpq: postgresql://usuario@/postgres?host=...&port=..."""
    partes = urlsplit(base)
    return urlunsplit(partes._replace(path="/" + nombre))


def psql(url, script, timeout=60):
    return subprocess.run(["psql", url, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose", "-f", "-"],
                          input=script, capture_output=True, text=True, timeout=timeout)


def literal(valor):
    if valor is None:
        return "null"
    if isinstance(valor, (dict, list)):
        valor = json.dumps(valor)
    return "'" + str(valor).replace("'", "''").replace("\x00", "") + "'"


class Puente:
    def __init__(self, url_base):
        self.url_base, self.registro, self.cerrojo = url_base, [], threading.Lock()
        puente = self

        class Manejador(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                largo = int(self.headers.get("Content-Length", 0))
                try:
                    datos = json.loads(self.rfile.read(largo) or b"{}")
                except ValueError:
                    return self.responder(400, {"code": "400", "message": "JSON inválido"})
                estado, cuerpo = puente.rpc(self.path, datos, self.headers)
                self.responder(estado, cuerpo)

            def responder(self, estado, cuerpo):
                bruto = cuerpo.encode() if isinstance(cuerpo, str) else json.dumps(cuerpo).encode()
                self.send_response(estado)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(bruto)))
                self.end_headers()
                try:
                    self.wfile.write(bruto)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), Manejador)
        self.servidor.daemon_threads = True
        self.hilo = threading.Thread(target=self.servidor.serve_forever, daemon=True)
        self.hilo.start()
        self.url = f"http://127.0.0.1:{self.servidor.server_port}"

    def cerrar(self):
        self.servidor.shutdown()
        self.servidor.server_close()

    def rol(self, cabeceras):
        auth = cabeceras.get("Authorization", "")
        if auth.startswith("Bearer jwt-") and UUID.fullmatch(auth[len("Bearer jwt-"):]):
            return "authenticated", auth[len("Bearer jwt-"):]
        clave = cabeceras.get("apikey")
        if clave == CLAVE_SECRETA:
            return "service_role", None
        if clave == CLAVE_PUBLICA:
            return "anon", None
        return None, None

    def rpc(self, ruta, datos, cabeceras):
        coincide = re.fullmatch(r"/rest/v1/rpc/([a-z_][a-z0-9_]*)", ruta)
        if not coincide:
            return 404, {"code": "PGRST125", "message": "ruta no soportada por el puente"}
        rol, usuario = self.rol(cabeceras)
        if rol is None:
            return 401, {"code": "401", "message": "credenciales ausentes"}
        funcion = coincide.group(1)
        if not isinstance(datos, dict) or not all(NOMBRE.fullmatch(k) for k in datos):
            return 400, {"code": "400", "message": "argumentos inválidos"}
        args = ", ".join(f"{k} => {literal(v)}" for k, v in datos.items())
        with self.cerrojo:
            self.registro.append((funcion, rol))
        script = "begin;\nset local role %s;\n" % rol
        if usuario:
            script += "select set_config('request.jwt.claim.sub', %s, true) \\g /dev/null\n" % literal(usuario)
        script += "select 'RES:' || coalesce(to_jsonb(public.%s(%s))::text, 'null');\ncommit;\n" % (funcion, args)
        try:
            salida = psql(self.url_base, script)
        except subprocess.TimeoutExpired:
            return 504, {"code": "504", "message": "tiempo agotado"}
        if salida.returncode != 0:
            error = re.search(r"ERROR:\s+([0-9A-Z]{5}):\s*(.*)", salida.stderr)
            codigo, mensaje = (error.group(1), error.group(2).strip()) if error else ("XX000", salida.stderr[-200:])
            if codigo == "42883":
                return 404, {"code": "PGRST202", "message": mensaje}
            estado = 403 if codigo == "42501" else int(codigo[2:]) if codigo.startswith("PT") and codigo[2:].isdigit() else 409 if codigo == "23505" else 400
            return estado, {"code": codigo, "message": mensaje}
        for linea in salida.stdout.splitlines():
            if linea.startswith("RES:"):
                return 200, linea[4:]
        return 200, "null"
