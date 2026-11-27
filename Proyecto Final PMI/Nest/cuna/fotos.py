"""Almacenamiento y galería de fotos del kit de seguridad (SCRUM-43 parte B, RF-SEG-02, RNF-SEG-03/04).

La cámara (parte A, Persona 4) sube la imagen cruda con POST /api/photos y la clave del dispositivo. Aquí se valida,
se guarda en un almacén PRIVADO y se registra en auditoría un evento FOTO_SEGURIDAD con la ruta del objeto.
Nunca hay URL pública: la app recibe URL firmadas con HMAC que vencen en 5 minutos, y además exigen la sesión.

Alcance honesto: el almacén es una carpeta local (datos/fotos). La interfaz `Almacen` es la que se sustituiría por
Supabase Storage (bucket privado + createSignedUrl); ese adaptador no está escrito.
"""
import hashlib
import hmac
import os
import re
import uuid
from pathlib import Path

from .common import Problem, instant

MAX_BYTES = 5 * 1024 * 1024
TIPOS = {"image/jpeg": ".jpg", "image/png": ".png"}
FIRMAS = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"))
TIPO_EVENTO = "FOTO_SEGURIDAD"
VIGENCIA_URL_S = 300
LIMITE_GALERIA, LIMITE_MAXIMO = 24, 100

SCHEMA = """
create table if not exists photos(
 id text primary key, family_id text not null references families, child_id text not null references children,
 device_id text not null references devices, captured_at text not null, content_type text not null,
 size_bytes integer not null check(size_bytes > 0), sha256 text not null, storage_path text not null,
 audit_id text not null references audit_log, created_at text not null);
create index if not exists photos_timeline on photos(child_id, captured_at, id);
create trigger if not exists keep_photo_update before update on photos begin select raise(abort,'Foto inmutable'); end;
create trigger if not exists keep_photo_delete before delete on photos begin select raise(abort,'Foto inmutable'); end;
"""


def tipo_por_firma(datos):
    """El tipo lo decide el contenido, no lo que declare el cliente."""
    for firma, tipo in FIRMAS:
        if datos.startswith(firma):
            return tipo
    raise Problem("El archivo no es una imagen JPEG o PNG.", 415)


def validar_subida(headers, datos):
    """Devuelve (photo_id, captured_at, content_type). El id de la foto es el X-Event-Id: reintentar no duplica."""
    if not datos:
        raise Problem("La imagen está vacía.")
    try:
        ident = str(uuid.UUID(headers.get("X-Event-Id", "")))
    except ValueError:
        raise Problem("Se requiere el encabezado X-Event-Id con un UUID.")
    at = instant(headers.get("X-Captured-At", ""))
    if at > instant():
        raise Problem("La captura está en el futuro. Revisa el reloj del dispositivo.")
    real = tipo_por_firma(datos)
    declarado = (headers.get_content_type() or "").lower()
    if declarado != real:
        raise Problem("El Content-Type no coincide con el contenido de la imagen.", 415)
    return ident, at, real


def detalle(photo_id, tipo):
    return f"Foto capturada. Objeto privado: fotos/{photo_id}{TIPOS[tipo]}"


class Almacen:
    """Carpeta privada de objetos. Los nombres salen de un UUID validado: no hay forma de escapar de la carpeta."""

    def __init__(self, raiz):
        self.raiz = Path(raiz)

    def ruta(self, photo_id, tipo):
        return self.raiz / f"{uuid.UUID(photo_id)}{TIPOS[tipo]}"

    def guardar(self, photo_id, tipo, datos):
        destino = self.ruta(photo_id, tipo)
        self.raiz.mkdir(parents=True, exist_ok=True)
        temporal = destino.with_suffix(destino.suffix + ".parcial")
        temporal.write_bytes(datos)
        os.replace(temporal, destino)
        return f"fotos/{destino.name}"

    def leer(self, photo_id, tipo):
        try:
            return self.ruta(photo_id, tipo).read_bytes()
        except FileNotFoundError:
            raise Problem("La imagen ya no está en el almacén.", 404)


def firma(secreto, photo_id, expira):
    return hmac.new(secreto, f"{photo_id}.{expira}".encode(), hashlib.sha256).hexdigest()[:40]


def url_firmada(secreto, photo_id, ahora_ts, ttl=VIGENCIA_URL_S):
    expira = int(ahora_ts) + ttl
    return f"/api/photo?id={photo_id}&exp={expira}&sig={firma(secreto, photo_id, expira)}", expira


def verificar_url(secreto, photo_id, expira, sig, ahora_ts):
    try:
        expira = int(expira)
    except (TypeError, ValueError):
        raise Problem("Enlace de imagen no válido.", 403)
    if not isinstance(sig, str) or not re.fullmatch(r"[0-9a-f]{40}", sig) \
            or not hmac.compare_digest(sig, firma(secreto, photo_id, expira)):
        raise Problem("Enlace de imagen no válido.", 403)
    if expira < ahora_ts:
        raise Problem("El enlace de la imagen venció. Actualiza la galería.", 403)


def consulta_galeria(query):
    """Parámetros de GET /api/photos. Devuelve (child, limite, antes)."""
    from .panel import uuid_texto
    uno = lambda k: query.get(k, [None])[0] or None
    if not uno("child"):
        raise Problem("Indica el bebé con ?child=.")
    try:
        limite = int(uno("limit") or LIMITE_GALERIA)
    except ValueError:
        raise Problem("limit debe ser un número.")
    if not 1 <= limite <= LIMITE_MAXIMO:
        raise Problem(f"limit va de 1 a {LIMITE_MAXIMO}.")
    antes = None
    if uno("before"):
        try:
            at, ident = uno("before").replace(" ", "+").rsplit("|", 1)
            antes = (instant(at), str(uuid.UUID(ident)))
        except (ValueError, Problem):
            raise Problem("El cursor before no es válido.")
    return uuid_texto(uno("child")), limite, antes
