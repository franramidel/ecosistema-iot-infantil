import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from capturar import capturar

SERVIDOR = os.getenv("SERVIDOR_URL", "http://localhost:8000").rstrip("/")
CLAVE = os.getenv("CAMARA_KEY", "")
TOKEN = os.getenv("CAMARA_TOKEN", "")
DEVICE = os.getenv("CAMARA_DEVICE_ID", "CAMARA-1")
_ultimo = 0.0


def subir_foto(ruta):
    with open(ruta, "rb") as f:
        datos = f.read()
    tipo = "image/png" if datos.startswith(b"\x89PNG") else "image/jpeg"
    capturada = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    cab = {"Authorization": "Bearer " + CLAVE, "Content-Type": tipo, "X-Device-Id": DEVICE,
           "X-Event-Id": str(uuid4()), "X-Captured-At": capturada}
    with urlopen(Request(SERVIDOR + "/api/photos", data=datos, headers=cab, method="POST"), timeout=20) as r:
        return json.load(r)


def disparar():
    ruta = str(capturar())
    return {"foto": ruta, "subida": subir_foto(ruta)}


class Disparador(BaseHTTPRequestHandler):
    def responder(self, codigo, cuerpo):
        datos = json.dumps(cuerpo).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def do_GET(self):
        global _ultimo
        url = urlparse(self.path)
        if url.path != "/disparar":
            return self.responder(404, {"error": "no existe"})
        if parse_qs(url.query).get("t", [""])[0] != TOKEN or not TOKEN:
            return self.responder(403, {"error": "token invalido"})
        if time.time() - _ultimo < 5:
            return self.responder(429, {"error": "espera 5 segundos"})
        _ultimo = time.time()
        try:
            self.responder(200, disparar())
        except HTTPError as exc:
            self.responder(502, {"error": "el servidor rechazo la foto", "codigo": exc.code})
        except (URLError, OSError, RuntimeError) as exc:
            self.responder(500, {"error": str(exc)})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--servir", action="store_true")
    if p.parse_args().servir:
        print("Escuchando en el puerto 8001. Ctrl+C para detener.")
        ThreadingHTTPServer(("0.0.0.0", 8001), Disparador).serve_forever()
    else:
        print(disparar())
