"""Cliente mínimo de la API de Telegram Bot (SCRUM-24, RF-NOT-01).

Usa solo la biblioteca estándar. El token se recibe por parámetro, nunca está en el código, y se
elimina de cualquier mensaje de error antes de que salga de este módulo. Los errores se clasifican en
tres tipos para que la cola decida qué hacer:

- temporal: red caída, tiempo agotado, 429 o 5xx. Se puede reintentar.
- permanente: el mensaje no se puede entregar nunca, por ejemplo el usuario bloqueó al bot.
- token: el token es inválido o fue revocado. Es un problema de configuración, no del mensaje.
"""
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

API = "https://api.telegram.org"
TOKEN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")
LIMITE_TEXTO = 4096


class ConfigError(Exception):
    pass


class ErrorTelegram(Exception):
    def __init__(self, tipo, mensaje, reintentar_en=None, codigo=None):
        super().__init__(mensaje)
        self.tipo, self.reintentar_en, self.codigo = tipo, reintentar_en, codigo


def token_desde(entorno):
    """Lee TELEGRAM_BOT_TOKEN del diccionario dado. Falla con un mensaje claro si falta o no tiene forma de token."""
    valor = (entorno.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not valor:
        raise ConfigError("Falta la variable TELEGRAM_BOT_TOKEN. Créala con BotFather y defínela en el entorno o en .env.")
    if not TOKEN.fullmatch(valor):
        raise ConfigError("TELEGRAM_BOT_TOKEN no tiene el formato de un token de Telegram.")
    return valor


def base_valida(base):
    """Solo HTTPS, salvo un servidor local de pruebas."""
    partes = urlsplit(base)
    if partes.scheme == "https" and partes.hostname:
        return base.rstrip("/")
    if partes.scheme == "http" and partes.hostname in ("127.0.0.1", "localhost", "::1"):
        return base.rstrip("/")
    raise ConfigError("TELEGRAM_API_BASE debe usar HTTPS. Solo se admite HTTP hacia localhost, para pruebas.")


class ClienteTelegram:
    def __init__(self, token, base=API, timeout=15):
        if not TOKEN.fullmatch(token or ""):
            raise ConfigError("El token de Telegram no tiene un formato válido.")
        self._token = token
        self.base = base_valida(base)
        self.timeout = timeout
        local = urlsplit(self.base).hostname in ("127.0.0.1", "localhost", "::1")
        # Un servidor local de pruebas no debe pasar por el proxy del sistema.
        self._abrir = (urllib.request.build_opener(urllib.request.ProxyHandler({})) if local
                       else urllib.request.build_opener()).open

    def limpiar(self, texto):
        return str(texto).replace(self._token, "***")

    def llamar(self, metodo, datos, timeout=None):
        solicitud = urllib.request.Request(
            f"{self.base}/bot{self._token}/{metodo}", data=json.dumps(datos).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self._abrir(solicitud, timeout=timeout or self.timeout) as respuesta:
                cuerpo = json.loads(respuesta.read())
        except urllib.error.HTTPError as exc:
            raise self.error_http(exc) from None
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            motivo = "tiempo de espera agotado" if isinstance(exc, TimeoutError) or "timed out" in str(exc) else "error de red"
            raise ErrorTelegram("temporal", f"Telegram no respondió: {motivo}.") from None
        except ValueError:
            raise ErrorTelegram("temporal", "Telegram devolvió una respuesta que no es JSON.") from None
        if not isinstance(cuerpo, dict) or not cuerpo.get("ok"):
            raise ErrorTelegram("permanente", "Telegram rechazó la solicitud.")
        return cuerpo.get("result")

    def error_http(self, exc):
        try:
            cuerpo = json.loads(exc.read())
        except (ValueError, OSError):
            cuerpo = {}
        descripcion = self.limpiar(cuerpo.get("description", "")) if isinstance(cuerpo, dict) else ""
        espera = (cuerpo.get("parameters") or {}).get("retry_after") if isinstance(cuerpo, dict) else None
        codigo = exc.code
        if codigo == 401:
            return ErrorTelegram("token", "Telegram rechazó el token del bot (401). Revisa TELEGRAM_BOT_TOKEN.", codigo=codigo)
        if codigo == 429:
            return ErrorTelegram("temporal", "Telegram pidió esperar (429).", reintentar_en=espera if isinstance(espera, int) else 30, codigo=codigo)
        if codigo >= 500:
            return ErrorTelegram("temporal", f"Telegram no está disponible ({codigo}).", codigo=codigo)
        # 400, 403, 404 y demás: el chat no existe, el usuario bloqueó al bot, el texto es inválido...
        return ErrorTelegram("permanente", f"Telegram rechazó el envío ({codigo}): {descripcion[:150]}", codigo=codigo)

    def enviar(self, chat_id, texto, formato="HTML"):
        if not isinstance(texto, str) or not texto.strip() or len(texto) > LIMITE_TEXTO:
            raise ErrorTelegram("permanente", "El texto del mensaje es inválido.")
        datos = {"chat_id": chat_id, "text": texto, "disable_web_page_preview": True}
        if formato:
            datos["parse_mode"] = formato
        return self.llamar("sendMessage", datos)["message_id"]

    def actualizaciones(self, offset=None, espera=25):
        datos = {"timeout": espera, "allowed_updates": ["message"]}
        if offset is not None:
            datos["offset"] = offset
        return self.llamar("getUpdates", datos, timeout=espera + self.timeout)

    def identidad(self):
        return self.llamar("getMe", {})
