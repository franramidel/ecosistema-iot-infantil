"""Vinculación con Telegram y cola persistente de notificaciones (SCRUM-24, RF-NOT-01).

Este módulo no habla con Telegram. Solo guarda y consulta estado en SQLite, siempre con una conexión
que le entrega el almacén. Quien envía es el procesador (`cuna/procesador.py`), en un proceso aparte,
de modo que ninguna alerta depende de que el navegador o una solicitud HTTP estén abiertos.

Estados de una solicitud:
  pendiente -> procesando -> enviada
  procesando -> error_recuperable -> procesando ...   (hasta MAX_INTENTOS)
  procesando -> error_permanente                      (error definitivo o reintentos agotados)
  sin_vinculo                                         (el usuario no tenía Telegram al solicitarla)

Nada se borra: los códigos y las vinculaciones se marcan, no se eliminan.
"""
import hashlib
import html
import re
import secrets
from datetime import datetime, timedelta, timezone

from .common import Problem, instant, uid

ALFABETO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sin 0, O, 1, I ni L, para dictarlo sin errores
LARGO_CODIGO = 8
VIGENCIA_CODIGO = timedelta(minutes=10)
MAX_FALLOS = 5
VENTANA_FALLOS = timedelta(minutes=15)
MAX_INTENTOS = 5
ARRENDAMIENTO = timedelta(minutes=2)
COSTA_RICA = timezone(timedelta(hours=-6))
ESTADOS_ALERTA = ("Informativo", "Advertencia", "Crítico")

SCHEMA = """
create table if not exists telegram_links(
 member_id text primary key references members,
 family_id text not null references families,
 chat_id text unique,
 telegram_user_id text,
 linked_at text,
 unlinked_at text,
 blocked_at text);
create table if not exists telegram_codes(
 code_hash text primary key,
 member_id text not null references members,
 family_id text not null references families,
 created_at text not null,
 expires_at text not null,
 used_at text);
create unique index if not exists telegram_one_open_code on telegram_codes(member_id) where used_at is null;
create table if not exists telegram_attempts(
 chat_id text primary key, failures integer not null, window_start text not null);
create table if not exists telegram_state(key text primary key, value text not null);
create table if not exists telegram_worker(
 id integer primary key check(id=1), owner text not null, expires_at text not null);
create table if not exists notification_queue(
 id text primary key,
 family_id text not null references families,
 member_id text not null references members,
 event_id text references audit_log(id),
 idempotency_key text not null check(length(idempotency_key) between 1 and 128),
 message text not null check(length(message) between 1 and 4096),
 status text not null check(status in ('pendiente','procesando','enviada','error_recuperable','error_permanente','sin_vinculo')),
 attempts integer not null default 0,
 max_attempts integer not null default 5,
 next_attempt_at text not null,
 locked_until text,
 lock_token text,
 last_attempt_at text,
 last_error text,
 created_at text not null,
 sent_at text,
 unique(family_id, idempotency_key));
create index if not exists queue_due on notification_queue(status, next_attempt_at);
create index if not exists queue_member on notification_queue(member_id, created_at);
"""


def sumar(momento, delta):
    return (datetime.fromisoformat(momento) + delta).isoformat(timespec="microseconds")


def hash_codigo(codigo):
    return hashlib.sha256(codigo.encode()).hexdigest()


def normalizar_codigo(texto):
    """Devuelve el código en mayúsculas sin separadores, o None si no tiene la forma de un código."""
    limpio = re.sub(r"[^A-Za-z0-9]", "", texto or "").upper()
    return limpio if len(limpio) == LARGO_CODIGO and all(c in ALFABETO for c in limpio) else None


def espera(intentos):
    """Segundos antes del siguiente intento: 30 s, 2 min, 8 min, 30 min."""
    return min(30 * 4 ** max(intentos - 1, 0), 1800)


def limpiar_error(texto):
    return re.sub(r"\d{5,}:[A-Za-z0-9_-]{20,}", "***", str(texto))[:300]


def formatear_alerta(titulo, dispositivo=None, ocurrido=None, estado="Advertencia"):
    """Compone el mensaje en HTML de Telegram. Todo valor variable se escapa. No incluye datos del bebé."""
    def texto(valor, limite, nombre):
        if not isinstance(valor, str) or not valor.strip() or len(valor) > limite:
            raise Problem(f"Revisa {nombre}.")
        return html.escape(re.sub(r"[\x00-\x1f\x7f]", " ", valor).strip(), quote=False)

    if estado not in ESTADOS_ALERTA:
        raise Problem("El estado debe ser Informativo, Advertencia o Crítico.")
    lineas = ["<b>Ecosistema IoT Infantil</b>", "", "Alerta: " + texto(titulo, 150, "el título")]
    if dispositivo is not None:
        lineas.append("Dispositivo: " + texto(dispositivo, 64, "el dispositivo"))
    if ocurrido is not None:
        momento = datetime.fromisoformat(instant(ocurrido)).astimezone(COSTA_RICA)
        lineas += [f"Fecha: {momento:%d/%m/%Y}", f"Hora: {momento:%H:%M}"]
    lineas.append("Estado: " + estado)
    return "\n".join(lineas)


def generar_codigo(db, member, family, ahora):
    """Un código nuevo anula el anterior sin uso del mismo usuario. Se guarda solo su hash."""
    db.execute("update telegram_codes set used_at=? where member_id=? and used_at is null", (ahora, member))
    codigo = "".join(secrets.choice(ALFABETO) for _ in range(LARGO_CODIGO))
    expira = sumar(ahora, VIGENCIA_CODIGO)
    db.execute("insert into telegram_codes(code_hash,member_id,family_id,created_at,expires_at) values (?,?,?,?,?)",
               (hash_codigo(codigo), member, family, ahora, expira))
    db.execute("insert into telegram_links(member_id,family_id) values (?,?) on conflict(member_id) do nothing", (member, family))
    return codigo, expira


def registrar_fallo(db, chat, ahora):
    fila = db.execute("select failures,window_start from telegram_attempts where chat_id=?", (chat,)).fetchone()
    if not fila or sumar(fila["window_start"], VENTANA_FALLOS) <= ahora:
        db.execute("insert into telegram_attempts values (?,1,?) on conflict(chat_id) do update set failures=1,window_start=excluded.window_start",
                   (chat, ahora))
    else:
        db.execute("update telegram_attempts set failures=failures+1 where chat_id=?", (chat,))


def vincular(db, texto, chat_id, user_id, ahora):
    """Completa la vinculación desde un mensaje recibido por el bot.

    Devuelve (estado, member_id). El chat y el usuario salen de Telegram, nunca del navegador.
    Estados: vinculado, invalido, expirado, bloqueado, chat_en_uso.
    """
    chat = str(chat_id)
    fila = db.execute("select failures,window_start from telegram_attempts where chat_id=?", (chat,)).fetchone()
    if fila and fila["failures"] >= MAX_FALLOS and sumar(fila["window_start"], VENTANA_FALLOS) > ahora:
        return "bloqueado", None
    codigo = normalizar_codigo(texto)
    registro = db.execute("select * from telegram_codes where code_hash=?", (hash_codigo(codigo),)).fetchone() if codigo else None
    if not registro or registro["used_at"]:
        registrar_fallo(db, chat, ahora)
        return "invalido", None
    if registro["expires_at"] <= ahora:
        registrar_fallo(db, chat, ahora)
        return "expirado", None
    otro = db.execute("select member_id from telegram_links where chat_id=? and member_id<>?", (chat, registro["member_id"])).fetchone()
    if otro:
        return "chat_en_uso", None
    db.execute("""insert into telegram_links(member_id,family_id,chat_id,telegram_user_id,linked_at)
        values (?,?,?,?,?) on conflict(member_id) do update set chat_id=excluded.chat_id,
        telegram_user_id=excluded.telegram_user_id,linked_at=excluded.linked_at,unlinked_at=null,blocked_at=null""",
               (registro["member_id"], registro["family_id"], chat, str(user_id), ahora))
    db.execute("update telegram_codes set used_at=? where code_hash=?", (ahora, registro["code_hash"]))
    db.execute("update telegram_attempts set failures=0 where chat_id=?", (chat,))
    return "vinculado", registro["member_id"]


def desvincular(db, member, ahora):
    db.execute("""update telegram_links set chat_id=null,telegram_user_id=null,unlinked_at=?,blocked_at=null
        where member_id=? and chat_id is not null""", (ahora, member))
    db.execute("update telegram_codes set used_at=? where member_id=? and used_at is null", (ahora, member))


def estado(db, member, ahora):
    fila = db.execute("select chat_id,linked_at,blocked_at from telegram_links where member_id=?", (member,)).fetchone()
    codigo = db.execute("select expires_at from telegram_codes where member_id=? and used_at is null and expires_at>?",
                        (member, ahora)).fetchone()
    ultima = db.execute("""select status,last_error,created_at from notification_queue
        where member_id=? and idempotency_key like 'prueba:%' order by created_at desc limit 1""", (member,)).fetchone()
    procesador = db.execute("select 1 from telegram_worker where expires_at>?", (ahora,)).fetchone()
    return {"vinculado": bool(fila and fila["chat_id"]), "vinculado_desde": fila["linked_at"] if fila and fila["chat_id"] else None,
            "bloqueado": bool(fila and fila["chat_id"] and fila["blocked_at"]),
            "codigo_expira": codigo["expires_at"] if codigo else None,
            "procesador_activo": procesador is not None,
            "ultima_prueba": ({"estado": ultima["status"], "error": ultima["last_error"]} if ultima else None)}


def encolar(db, member, clave, mensaje, evento, ahora):
    """Guarda la solicitud antes de responder. La clave de idempotencia evita duplicados dentro de la familia."""
    if not isinstance(clave, str) or not 1 <= len(clave) <= 128 or not clave.isprintable():
        raise Problem("La clave de idempotencia debe tener entre 1 y 128 caracteres.")
    usuario = db.execute("select family_id from members where id=?", (member,)).fetchone()
    if not usuario:
        raise Problem("El usuario destinatario no existe.", 404)
    familia = usuario["family_id"]
    if evento and not db.execute("select 1 from audit_log where id=? and family_id=?", (evento, familia)).fetchone():
        raise Problem("El evento no existe en la familia del destinatario.", 404)
    previa = db.execute("select * from notification_queue where family_id=? and idempotency_key=?", (familia, clave)).fetchone()
    if previa:
        if (previa["member_id"], previa["message"], previa["event_id"]) != (member, mensaje, evento):
            raise Problem("La clave de idempotencia ya se usó con otro contenido.", 409)
        return {"id": previa["id"], "estado": previa["status"], "duplicado": True, "vinculado": previa["status"] != "sin_vinculo"}
    vinculo = db.execute("select 1 from telegram_links where member_id=? and chat_id is not null", (member,)).fetchone()
    nuevo = uid()
    estado_inicial = "pendiente" if vinculo else "sin_vinculo"
    db.execute("""insert into notification_queue(id,family_id,member_id,event_id,idempotency_key,message,status,max_attempts,
        next_attempt_at,created_at,last_error) values (?,?,?,?,?,?,?,?,?,?,?)""",
               (nuevo, familia, member, evento, clave, mensaje, estado_inicial, MAX_INTENTOS, ahora, ahora,
                None if vinculo else "El usuario no tiene Telegram vinculado."))
    return {"id": nuevo, "estado": estado_inicial, "duplicado": False, "vinculado": bool(vinculo)}


def reclamar(db, ahora, limite, token):
    """Toma solicitudes listas. Las que quedaron 'procesando' con el arrendamiento vencido se recuperan.

    El contador de intentos sube al reclamar, no al terminar: si el proceso muere en cada intento,
    el límite se alcanza igual y no hay bucle infinito.
    """
    db.execute("""update notification_queue set status='error_permanente',lock_token=null,locked_until=null,
        last_error='Reintentos agotados tras interrupciones del procesador.'
        where status='procesando' and locked_until<=? and attempts>=max_attempts""", (ahora,))
    ids = [r["id"] for r in db.execute("""select id from notification_queue
        where (status in ('pendiente','error_recuperable') and next_attempt_at<=?) or (status='procesando' and locked_until<=?)
        order by next_attempt_at,created_at limit ?""", (ahora, ahora, limite))]
    vence = sumar(ahora, ARRENDAMIENTO)
    trabajos = []
    for numero in ids:
        db.execute("""update notification_queue set status='procesando',attempts=attempts+1,lock_token=?,locked_until=?,
            last_attempt_at=? where id=?""", (token, vence, ahora, numero))
        fila = dict(db.execute("select id,member_id,family_id,message,attempts,max_attempts,event_id from notification_queue where id=?", (numero,)).fetchone())
        enlace = db.execute("select chat_id from telegram_links where member_id=?", (fila["member_id"],)).fetchone()
        fila["chat_id"] = enlace["chat_id"] if enlace else None
        fila["token"] = token
        trabajos.append(fila)
    return trabajos


def auditar(db, fila, tipo, detalle, ahora):
    """Deja constancia en la auditoría de SCRUM-20 cuando la solicitud nació de un evento validado."""
    if not fila["event_id"]:
        return
    db.execute("""insert into audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,device_uuid,severity,source_event_id)
        select family_id,?,?,?,child_id,device_id,device_uuid,?,? from audit_log where id=?
        on conflict do nothing""",
               (ahora, tipo, detalle, "info" if tipo == "NOTIFICACION_ENVIADA" else "warning",
                f"notif:{fila['id']}:{tipo}", fila["event_id"]))


def resultado(db, trabajo, ahora, tipo=None, error=None, reintentar_en=None):
    """Cierra un intento. `tipo` es None si se envió, o 'temporal', 'permanente' o 'token' si falló.

    Solo actúa si el trabajador todavía tiene el arrendamiento. Devuelve el estado final o None.
    """
    fila = db.execute("select * from notification_queue where id=? and lock_token=? and status='procesando'",
                      (trabajo["id"], trabajo["token"])).fetchone()
    if not fila:
        return None
    error = limpiar_error(error) if error else None
    if tipo is None:
        final = "enviada"
        db.execute("update notification_queue set status='enviada',sent_at=?,last_error=null,lock_token=null,locked_until=null where id=?",
                   (ahora, fila["id"]))
        auditar(db, fila, "NOTIFICACION_ENVIADA", "Alerta enviada por Telegram", ahora)
    elif tipo == "temporal" and fila["attempts"] < fila["max_attempts"]:
        final = "error_recuperable"
        siguiente = sumar(ahora, timedelta(seconds=max(espera(fila["attempts"]), min(reintentar_en or 0, 3600))))
        db.execute("update notification_queue set status='error_recuperable',next_attempt_at=?,last_error=?,lock_token=null,locked_until=null where id=?",
                   (siguiente, error, fila["id"]))
    else:
        final = "error_permanente"
        motivo = error if tipo != "temporal" else "Reintentos agotados: " + (error or "")
        db.execute("update notification_queue set status='error_permanente',last_error=?,lock_token=null,locked_until=null where id=?",
                   (limpiar_error(motivo), fila["id"]))
        auditar(db, fila, "NOTIFICACION_FALLIDA", "Alerta por Telegram no entregada", ahora)
    return final


def marcar_bloqueado(db, member, ahora):
    db.execute("update telegram_links set blocked_at=? where member_id=? and chat_id is not null", (ahora, member))


def adquirir_procesador(db, dueno, ahora, vigencia=timedelta(seconds=90)):
    """Solo un procesador a la vez consulta las actualizaciones del bot. Es un arrendamiento que se renueva."""
    fila = db.execute("select owner,expires_at from telegram_worker where id=1").fetchone()
    if fila and fila["owner"] != dueno and fila["expires_at"] > ahora:
        return False
    db.execute("insert into telegram_worker values (1,?,?) on conflict(id) do update set owner=excluded.owner,expires_at=excluded.expires_at",
               (dueno, sumar(ahora, vigencia)))
    return True


def liberar_procesador(db, dueno, ahora):
    db.execute("update telegram_worker set expires_at=? where id=1 and owner=?", (ahora, dueno))


def leer_offset(db):
    fila = db.execute("select value from telegram_state where key='offset'").fetchone()
    return int(fila["value"]) if fila else None


def guardar_offset(db, valor):
    db.execute("insert into telegram_state values ('offset',?) on conflict(key) do update set value=excluded.value", (str(valor),))
