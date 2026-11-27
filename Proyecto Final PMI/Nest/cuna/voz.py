"""Voz bidireccional app <-> cuna (SCRUM-31 parte B, RF-CUN-06, RNF-REN-02).

El audio viaja por WebRTC directo entre los dos extremos (el navegador de la app y la cuna). Este módulo es solo la
SEÑALIZACIÓN: un buzón en memoria por bebé donde los dos lados intercambian oferta, respuesta y candidatos ICE.
El servidor nunca ve el audio. Los mensajes viven pocos segundos y no se guardan en la base.

Roles: "app" (quien llama) y "cuna" (altavoz y micrófono). En el prototipo la cuna puede ser otra pestaña del
navegador (web/cuna-voz.html) o un dispositivo con su clave; el contrato es el mismo.
"""
import json
import threading
import time

from .common import Problem

ROLES = ("app", "cuna")
TIPOS = ("hello", "offer", "answer", "candidate", "hangup")
TTL_S = 45               # un mensaje que nadie leyó en este tiempo ya no sirve
LLAMADA_S = 90           # sin actividad durante este tiempo la llamada se considera caída
MAX_DATOS = 12288
MAX_COLA = 200
MAX_ESPERA = 20


def otro(rol):
    return "cuna" if rol == "app" else "app"


class Canales:
    def __init__(self, reloj=time.monotonic):
        self.reloj = reloj
        self.cond = threading.Condition()
        self.canales = {}

    def canal(self, child):
        return self.canales.setdefault(child, {"seq": 0, "msgs": [], "activa": False, "ultima": 0.0})

    def purgar(self, c, ahora):
        c["msgs"] = [m for m in c["msgs"] if ahora - m["ts"] <= TTL_S][-MAX_COLA:]
        if c["activa"] and ahora - c["ultima"] > LLAMADA_S:
            c["activa"] = False

    def enviar(self, child, rol, tipo, datos=None):
        if rol not in ROLES or tipo not in TIPOS:
            raise Problem("Rol o tipo de mensaje de voz no válido.")
        if datos is not None and len(json.dumps(datos)) > MAX_DATOS:
            raise Problem("El mensaje de señalización es demasiado grande.", 413)
        with self.cond:
            ahora = self.reloj()
            c = self.canal(child)
            self.purgar(c, ahora)
            if tipo == "offer":
                if rol != "app":
                    raise Problem("Solo la app inicia la llamada.", 403)
                if c["activa"]:
                    raise Problem("Ya hay una llamada de voz en curso con esta cuna.", 409)
                c["activa"] = True
                c["msgs"] = []                      # una oferta nueva descarta restos de intentos anteriores
            elif tipo == "hangup":
                c["activa"] = False
                c["msgs"] = []
            elif not c["activa"] and tipo in ("answer", "candidate"):
                raise Problem("No hay una llamada en curso.", 409)
            c["ultima"] = ahora
            c["seq"] += 1
            c["msgs"].append({"seq": c["seq"], "para": otro(rol), "type": tipo, "data": datos, "ts": ahora})
            self.cond.notify_all()
            return {"seq": c["seq"]}

    def esperar(self, child, rol, despues, espera):
        """Long polling: devuelve los mensajes para `rol` con seq > despues, esperando hasta `espera` segundos."""
        if rol not in ROLES:
            raise Problem("Rol de voz no válido.")
        limite = self.reloj() + max(0, min(espera, MAX_ESPERA))
        with self.cond:
            while True:
                c = self.canal(child)
                self.purgar(c, self.reloj())
                mios = [{"seq": m["seq"], "type": m["type"], "data": m["data"]}
                        for m in c["msgs"] if m["para"] == rol and m["seq"] > despues]
                restante = limite - self.reloj()
                if mios or restante <= 0:
                    return {"messages": mios, "last": c["seq"], "active": c["activa"]}
                self.cond.wait(timeout=restante)


def validar_senal(data, hay_clave):
    """Cuerpo de POST /api/voice/signal. Con clave de dispositivo el rol es siempre 'cuna'."""
    tipo = data.get("type")
    rol = "cuna" if hay_clave else data.get("role")
    if tipo not in TIPOS or rol not in ROLES:
        raise Problem("Indica role (app o cuna) y type (hello, offer, answer, candidate o hangup).")
    return rol, tipo, data.get("data")


def validar_espera(data):
    try:
        despues = int(data.get("after", 0))
        espera = int(data.get("wait", 15))
    except (TypeError, ValueError):
        raise Problem("after y wait deben ser números.")
    return max(0, despues), max(0, espera)
