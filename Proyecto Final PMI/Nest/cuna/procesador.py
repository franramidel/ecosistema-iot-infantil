"""Procesador de Telegram: envía la cola y recibe los mensajes del bot (SCRUM-24).

Corre en su propio proceso (`python telegram_worker.py`), sin depender del navegador ni del servidor web.
Cada ciclo hace dos cosas:

1. Reclama solicitudes pendientes de la cola y las envía una por una.
2. Consulta a Telegram por long polling los mensajes nuevos del bot, para completar vinculaciones.

Solo un procesador a la vez puede consultar los mensajes del bot: lo garantiza un arrendamiento guardado
en la base. Varios procesadores sí podrían enviar la cola sin pisarse, porque cada solicitud se reclama
de forma atómica, pero el diseño espera uno solo.
"""
import sys
import threading
import time

from .common import Problem, instant, uid
from .notificaciones import normalizar_codigo
from .telegram import ErrorTelegram

BIENVENIDA = ("Hola. Para vincular tu cuenta de Ecosistema IoT Infantil, abre la plataforma, pulsa "
              "Vincular Telegram y envíame aquí el código que aparece.")
RESPUESTAS = {
    "vinculado": "Cuenta vinculada. Recibirás aquí las alertas de Ecosistema IoT Infantil.",
    "invalido": "Ese código no es válido. Genera uno nuevo en la plataforma.",
    "expirado": "Ese código expiró. Genera uno nuevo en la plataforma.",
    "bloqueado": "Demasiados intentos fallidos. Espera unos minutos.",
    "chat_en_uso": "Este Telegram ya está vinculado a otra cuenta. Desvincúlalo primero en la plataforma.",
}


class ProcesadorOcupado(Exception):
    pass


class Procesador:
    def __init__(self, store, cliente, dueno=None, reloj=instant, salida=sys.stderr):
        self.store, self.cliente, self.dueno = store, cliente, dueno or uid()
        self.reloj, self.salida = reloj, salida
        self.token_invalido = False
        self.almacen_rechazado = False

    def registrar(self, texto):
        """Escribe en el registro del proceso. Nunca incluye el token ni el contenido de los mensajes."""
        print(f"[{self.reloj()}] {self.cliente.limpiar(texto)}", file=self.salida, flush=True)

    def procesar_cola(self, limite=10):
        cuenta = {"enviadas": 0, "reintentos": 0, "permanentes": 0}
        trabajos = self.store.cola_reclamar(uid(), limite, ahora=self.reloj())
        for trabajo in trabajos:
            if self.token_invalido:
                break
            if not trabajo["chat_id"]:
                self.store.cola_resultado(trabajo, "permanente", "El usuario ya no tiene Telegram vinculado.", ahora=self.reloj())
                cuenta["permanentes"] += 1
                continue
            try:
                self.cliente.enviar(trabajo["chat_id"], trabajo["message"])
            except ErrorTelegram as exc:
                if exc.tipo == "token":
                    self.token_invalido = True
                bloqueado = exc.codigo == 403
                final = self.store.cola_resultado(trabajo, "temporal" if exc.tipo in ("temporal", "token") else "permanente",
                                                  str(exc), exc.reintentar_en, bloqueado, ahora=self.reloj())
                cuenta["permanentes" if final == "error_permanente" else "reintentos"] += 1
                self.registrar(f"Solicitud {trabajo['id']}: {final} ({exc.tipo})")
            else:
                final = self.store.cola_resultado(trabajo, ahora=self.reloj())
                cuenta["enviadas"] += final == "enviada"
                self.registrar(f"Solicitud {trabajo['id']}: {final}")
        return cuenta

    def responder(self, chat_id, texto):
        try:
            self.cliente.enviar(chat_id, texto, formato=None)
        except ErrorTelegram as exc:
            self.registrar(f"No se pudo responder al chat: {exc}")

    def procesar_actualizaciones(self, espera=5):
        actualizaciones = self.cliente.actualizaciones(self.store.telegram_offset(), espera)
        for dato in sorted(actualizaciones, key=lambda u: u["update_id"]):
            siguiente = dato["update_id"] + 1
            mensaje = dato.get("message") or {}
            chat, origen, texto = mensaje.get("chat") or {}, mensaje.get("from") or {}, mensaje.get("text") or ""
            # Solo chats privados cuyo remitente es el propio dueño del chat. Un grupo o un bot no vinculan.
            if chat.get("type") != "private" or origen.get("is_bot") or origen.get("id") != chat.get("id") or not texto:
                self.store.telegram_offset(siguiente)
                continue
            if texto.startswith("/start"):
                partes = texto.split(None, 1)
                carga = partes[1] if len(partes) > 1 else ""
            else:
                carga = texto if normalizar_codigo(texto) else ""
            if not carga:
                self.store.telegram_offset(siguiente)
                self.responder(chat["id"], BIENVENIDA)
                continue
            estado, _ = self.store.telegram_vincular(carga, chat["id"], origen["id"], siguiente_offset=siguiente, ahora=self.reloj())
            self.registrar(f"Vinculación: {estado}")
            self.responder(chat["id"], RESPUESTAS[estado])
        return len(actualizaciones)

    def ciclo(self, espera=5):
        if not self.store.telegram_procesador(self.dueno, ahora=self.reloj()):
            raise ProcesadorOcupado("Ya hay otro procesador de Telegram activo.")
        cuenta = self.procesar_cola()
        self.procesar_actualizaciones(espera)
        return cuenta

    def ejecutar(self, parar, espera=5):
        """Bucle principal. Se detiene con el evento `parar`, si Telegram rechaza el token o si el almacén rechaza la clave.

        Una falla de red hacia Telegram o hacia Supabase no detiene el bucle: se registra y se reintenta.
        """
        try:
            while not parar.is_set() and not self.token_invalido and not self.almacen_rechazado:
                try:
                    self.ciclo(espera)
                except ErrorTelegram as exc:
                    self.registrar(f"Ciclo con error ({exc.tipo}): {exc}")
                    if exc.tipo == "token":
                        self.token_invalido = True
                    else:
                        parar.wait(5)
                except Problem as exc:
                    self.registrar(f"El almacén de datos respondió con error ({exc.status}): {exc}")
                    if exc.status in (401, 403):
                        self.almacen_rechazado = True
                    else:
                        parar.wait(5)
        finally:
            try:
                self.store.telegram_procesador(self.dueno, ahora=self.reloj(), liberar=True)
            except Problem:
                pass  # el arrendamiento vence solo
