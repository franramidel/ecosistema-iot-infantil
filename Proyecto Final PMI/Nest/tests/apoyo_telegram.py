"""Piezas compartidas por las pruebas de Telegram: un servidor falso de la API de bots y un reloj controlable.

Ningún servidor de aquí es Telegram real. El token se arma en tiempo de ejecución y no sirve en Telegram.
"""
import json
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "".join(["90001", "2345:", "PRUEBA_", "x" * 24, "_falso"])


class TelegramFalso:
    """Servidor HTTP que imita lo mínimo de la API de bots y registra todo lo que recibe."""

    def __init__(self, token=TOKEN):
        self.token, self.enviados, self.llamadas, self.guion = token, [], [], deque()
        self.actualizaciones, self.demora, self.cerrado = [], 0, False
        falso = self

        class Manejador(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                largo = int(self.headers.get("Content-Length", 0))
                datos = json.loads(self.rfile.read(largo) or b"{}")
                partes = self.path.strip("/").split("/")
                metodo = partes[-1]
                with falso.cerrojo:
                    falso.llamadas.append((metodo, datos))
                if partes[0] != "bot" + falso.token:
                    return self.responder(401, {"ok": False, "error_code": 401, "description": "Unauthorized " + partes[0]})
                if falso.demora:
                    time.sleep(falso.demora)
                if metodo == "sendMessage":
                    with falso.cerrojo:
                        if falso.guion:
                            estado, cuerpo = falso.guion.popleft()
                            if estado != 200:
                                return self.responder(estado, cuerpo)
                        falso.enviados.append(datos)
                        numero = len(falso.enviados)
                    return self.responder(200, {"ok": True, "result": {"message_id": numero}})
                if metodo == "getUpdates":
                    desde = datos.get("offset") or 0
                    with falso.cerrojo:
                        cola = [u for u in falso.actualizaciones if u["update_id"] >= desde]
                    return self.responder(200, {"ok": True, "result": cola})
                if metodo == "getMe":
                    return self.responder(200, {"ok": True, "result": {"username": "cuna_prueba_bot"}})
                return self.responder(404, {"ok": False, "description": "no existe"})

            def responder(self, estado, cuerpo):
                bruto = json.dumps(cuerpo).encode()
                self.send_response(estado)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(bruto)))
                self.end_headers()
                try:
                    self.wfile.write(bruto)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # el cliente ya se fue, por ejemplo en la prueba de tiempo agotado

        self.cerrojo = threading.Lock()
        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), Manejador)
        self.servidor.daemon_threads = True
        self.hilo = threading.Thread(target=self.servidor.serve_forever, daemon=True)
        self.hilo.start()
        self.base = f"http://127.0.0.1:{self.servidor.server_port}"

    def mensaje(self, texto, chat=5001, usuario=None, tipo="private", bot=False):
        numero = len(self.actualizaciones) + 1
        self.actualizaciones.append({"update_id": 100 + numero, "message": {
            "text": texto, "chat": {"id": chat, "type": tipo},
            "from": {"id": usuario if usuario is not None else chat, "is_bot": bot}}})

    def textos(self):
        return [d["text"] for d in self.enviados]

    def cerrar(self):
        self.servidor.shutdown()
        self.servidor.server_close()


class Reloj:
    def __init__(self):
        self.t = datetime.now(timezone.utc)

    def __call__(self):
        return self.t.isoformat(timespec="microseconds")

    def avanzar(self, segundos):
        self.t += timedelta(seconds=segundos)
