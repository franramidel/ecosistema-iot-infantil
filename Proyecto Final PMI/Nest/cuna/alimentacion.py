"""Sistema Inteligente de Alimentación, parte de software (Persona 5, Sprint 2).

* SCRUM-50 / RF-ALI-01: temperatura del biberón. El dispositivo solo informa lecturas (métrica `TemperaturaBiberon`
  en °C, por el mismo POST /api/events de siempre). El servidor decide: rango seguro por bebé (36 a 40 °C por
  defecto, objetivo 37 °C), alerta «muy frío» / «muy caliente» solo al CAMBIAR de estado, registro en auditoría y
  solicitud de Telegram, todo en la misma transacción que guarda la lectura (mismo criterio que alertas.py).
* SCRUM-52 / RF-ALI-03: recordatorios de toma recurrentes por intervalo o a horas fijas, enviados a Telegram.
  Los dispara `disparar()`, que ejecuta el procesador de Telegram en cada ciclo; no depende del navegador.

Estado: la lectura y las alertas son reales de extremo a extremo. Lo que sigue simulado es el sensor físico
(ver simular.py, que ya admite --medida/--unidad).
"""
import math
import re
import uuid
from datetime import datetime, timedelta

from . import auditoria, notificaciones
from .common import Problem, instant, uid

METRICA = "TemperaturaBiberon"
UNIDAD = "°C"
MIN_DEFECTO, MAX_DEFECTO, OBJETIVO = 36.0, 40.0, 37.0
RANGO_FISICO = (0.0, 100.0)     # fuera de esto la lectura es un error del sensor, no una temperatura de leche
VIGENCIA_S = 300                # una lectura más vieja no describe el biberón de ahora
ESPACIO = uuid.UUID("b1b3f0de-52c4-4e0d-8d2e-6a0f9a6c3d21")
TIPOS = {"caliente": "ALERTA_BIBERON_CALIENTE", "fria": "ALERTA_BIBERON_FRIO"}
SEVERIDAD = {"caliente": "critical", "fria": "warning"}
ESTADO_TELEGRAM = {"caliente": "Crítico", "fria": "Advertencia"}

INTERVALO_MIN, INTERVALO_MAX = 15, 24 * 60
MAX_RECORDATORIOS = 10
MAX_HORAS = 12

SCHEMA = """
create table if not exists bottle_thresholds(
 child_id text primary key references children, family_id text not null references families,
 temp_min real not null, temp_max real not null, updated_at text not null, updated_by text references members,
 check(temp_min < temp_max));
create table if not exists bottle_state(
 child_id text not null references children, device_id text not null references devices,
 state text not null check(state in ('normal','caliente','fria')), since text not null,
 primary key(child_id, device_id));
create table if not exists feed_reminders(
 id text primary key, family_id text not null references families, child_id text not null references children,
 kind text not null check(kind in ('interval','fixed')), interval_min integer, times text,
 enabled integer not null check(enabled in (0,1)), next_due_at text not null,
 created_at text not null, created_by text references members, updated_at text not null, deleted_at text,
 check((kind='interval' and interval_min between 15 and 1440) or (kind='fixed' and times is not null)));
create index if not exists reminders_due on feed_reminders(enabled, next_due_at);
"""


# --- Temperatura del biberón (SCRUM-50) ------------------------------------------------------------------------

def es_biberon(metrica, unidad):
    return metrica == METRICA and unidad == UNIDAD


def clasificar(valor, minimo=MIN_DEFECTO, maximo=MAX_DEFECTO):
    """'caliente' si SUPERA el máximo, 'fria' si baja del mínimo; los límites exactos son normales."""
    if valor > maximo:
        return "caliente"
    if valor < minimo:
        return "fria"
    return "normal"


def validar_lectura(valor):
    if not RANGO_FISICO[0] <= valor <= RANGO_FISICO[1]:
        raise Problem("La lectura del biberón debe estar entre 0 y 100 °C. Revisa el sensor.")


def validar_umbrales(minimo, maximo):
    for valor in (minimo, maximo):
        if type(valor) not in (int, float) or not math.isfinite(valor):
            raise Problem("Los límites del biberón deben ser números.")
        if not RANGO_FISICO[0] <= valor <= RANGO_FISICO[1]:
            raise Problem("Los límites del biberón deben estar entre 0 y 100 °C.")
    if not minimo < maximo:
        raise Problem("El mínimo debe ser menor que el máximo.")
    return float(minimo), float(maximo)


def leer_umbrales(db, child):
    fila = db.execute("select temp_min,temp_max from bottle_thresholds where child_id=?", (child,)).fetchone()
    if fila:
        return fila["temp_min"], fila["temp_max"], False
    return MIN_DEFECTO, MAX_DEFECTO, True


def guardar_umbrales(db, family, child, minimo, maximo, miembro, ahora):
    minimo, maximo = validar_umbrales(minimo, maximo)
    db.execute("""insert into bottle_thresholds(child_id,family_id,temp_min,temp_max,updated_at,updated_by) values (?,?,?,?,?,?)
        on conflict(child_id) do update set temp_min=excluded.temp_min, temp_max=excluded.temp_max,
        updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
               (child, family, minimo, maximo, ahora, miembro))
    return {"child_id": child, "temp_min": minimo, "temp_max": maximo, "target": OBJETIVO, "default": False}


def titulo(estado, valor, minimo, maximo):
    if estado == "caliente":
        return f"Biberón muy caliente: {valor:.1f} °C (máximo {maximo:g} °C)"
    return f"Biberón muy frío: {valor:.1f} °C (mínimo {minimo:g} °C)"


def evaluar(db, family, device, child, at, event_id, valor, ahora=None):
    """Compara la lectura con el rango del bebé. Debe llamarse dentro de la transacción que guardó la lectura.

    Avisa solo al pasar de un estado a uno fuera de rango (no repite mientras siga igual; volver a la normalidad
    no avisa). Una lectura atrasada no puede deshacer un estado más reciente. Devuelve None o {estado, evento, solicitudes}.
    """
    minimo, maximo, _ = leer_umbrales(db, child)
    nuevo = clasificar(valor, minimo, maximo)
    previo = db.execute("select state,since from bottle_state where child_id=? and device_id=?", (child, device)).fetchone()
    if previo and at < previo["since"]:
        return None
    anterior = previo["state"] if previo else "normal"
    if previo is None:
        db.execute("insert into bottle_state(child_id,device_id,state,since) values (?,?,?,?)", (child, device, nuevo, at))
    elif nuevo != anterior:
        db.execute("update bottle_state set state=?, since=? where child_id=? and device_id=?", (nuevo, at, child, device))
    if nuevo == anterior or nuevo == "normal":
        return None
    ahora = ahora or instant()
    detalle = titulo(nuevo, valor, minimo, maximo)
    registro = auditoria.registrar(db, family, device,
                                   (str(uuid.uuid5(ESPACIO, event_id)), at, TIPOS[nuevo], detalle, SEVERIDAD[nuevo]))
    solicitudes = []
    for miembro in db.execute("select id from members where family_id=? order by id", (family,)).fetchall():
        mensaje = notificaciones.formatear_alerta(detalle, device, at, ESTADO_TELEGRAM[nuevo])
        solicitudes.append(notificaciones.encolar(db, miembro["id"], f"biberon:{event_id}:{miembro['id']}",
                                                  mensaje, registro["id"], ahora))
    return {"estado": nuevo, "evento": registro["id"], "solicitudes": solicitudes}


def estado_biberon(db, child, ahora):
    """Lo que muestra la tarjeta de la app: última lectura, estado visual y rango vigente."""
    minimo, maximo, defecto = leer_umbrales(db, child)
    fila = db.execute("""select id,device_id,value,captured_at from events where child_id=? and metric=? and unit=?
        order by captured_at desc,id desc limit 1""", (child, METRICA, UNIDAD)).fetchone()
    salida = {"child_id": child, "temp_min": minimo, "temp_max": maximo, "target": OBJETIVO, "default": defecto,
              "reading": None, "status": "sin_lectura", "fresh": False}
    if fila:
        limite = (datetime.fromisoformat(ahora) - timedelta(seconds=VIGENCIA_S)).isoformat(timespec="microseconds")
        salida["reading"] = {"value": fila["value"], "captured_at": fila["captured_at"], "device_id": fila["device_id"]}
        salida["fresh"] = fila["captured_at"] >= limite
        salida["status"] = clasificar(fila["value"], minimo, maximo)
    return salida


# --- Recordatorios de toma (SCRUM-52) --------------------------------------------------------------------------

def validar_recordatorio(data):
    """Cuerpo de POST /api/reminders con accion 'save'. Devuelve (id|None, tipo, intervalo, horas, activo)."""
    ident = data.get("id")
    if ident is not None:
        try:
            ident = str(uuid.UUID(ident))
        except (ValueError, TypeError, AttributeError):
            raise Problem("El identificador del recordatorio no es válido.")
    tipo = data.get("kind")
    activo = data.get("enabled", True)
    if not isinstance(activo, bool):
        raise Problem("El estado del recordatorio debe ser booleano.")
    if tipo == "interval":
        minutos = data.get("interval_min")
        if type(minutos) is not int or not INTERVALO_MIN <= minutos <= INTERVALO_MAX:
            raise Problem("El intervalo va de 15 minutos a 24 horas.")
        return ident, tipo, minutos, None, activo
    if tipo == "fixed":
        horas = data.get("times")
        if not isinstance(horas, list) or not 1 <= len(horas) <= MAX_HORAS or not all(isinstance(h, str) for h in horas):
            raise Problem(f"Indica de 1 a {MAX_HORAS} horas con formato HH:MM.")
        if not all(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", h) for h in horas):
            raise Problem("Las horas usan el formato HH:MM de 24 horas.")
        return ident, tipo, None, sorted(set(horas)), activo
    raise Problem("El recordatorio es por intervalo (interval) o por horas fijas (fixed).")


def siguiente(tipo, intervalo, horas, desde):
    """Próximo vencimiento ESTRICTAMENTE posterior a `desde` (ISO UTC). Las horas fijas son hora de Costa Rica."""
    base = datetime.fromisoformat(desde)
    if tipo == "interval":
        return (base + timedelta(minutes=intervalo)).isoformat(timespec="microseconds")
    local = base.astimezone(notificaciones.COSTA_RICA)
    candidatos = []
    for dia in (0, 1):
        for hora in horas:
            h, m = map(int, hora.split(":"))
            candidatos.append(local.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=dia))
    return min(c for c in candidatos if c > local).astimezone(base.tzinfo).isoformat(timespec="microseconds")


def fila_recordatorio(fila):
    return {"id": fila["id"], "kind": fila["kind"], "interval_min": fila["interval_min"],
            "times": fila["times"].split(",") if fila["times"] else None,
            "enabled": bool(fila["enabled"]), "next_due_at": fila["next_due_at"]}


def listar_recordatorios(db, child):
    filas = db.execute("select * from feed_reminders where child_id=? and deleted_at is null order by created_at,id", (child,))
    return {"child_id": child, "reminders": [fila_recordatorio(f) for f in filas]}


def guardar_recordatorio(db, family, child, miembro, datos, ahora):
    ident, tipo, intervalo, horas, activo = datos
    texto = ",".join(horas) if horas else None
    proximo = siguiente(tipo, intervalo, horas, ahora)
    if ident:
        cambios = db.execute("""update feed_reminders set kind=?, interval_min=?, times=?, enabled=?, next_due_at=?, updated_at=?
            where id=? and child_id=? and family_id=? and deleted_at is null""", (tipo, intervalo, texto, int(activo), proximo, ahora, ident, child, family)).rowcount
        if not cambios:
            raise Problem("Recordatorio no encontrado.", 404)
    else:
        total = db.execute("select count(*) from feed_reminders where child_id=? and deleted_at is null", (child,)).fetchone()[0]
        if total >= MAX_RECORDATORIOS:
            raise Problem(f"Un bebé admite hasta {MAX_RECORDATORIOS} recordatorios.", 409)
        db.execute("""insert into feed_reminders(id,family_id,child_id,kind,interval_min,times,enabled,next_due_at,created_at,created_by,updated_at)
            values (?,?,?,?,?,?,?,?,?,?,?)""", (uid(), family, child, tipo, intervalo, texto, int(activo), proximo, ahora, miembro, ahora))
    return listar_recordatorios(db, child)


def borrar_recordatorio(db, family, child, ident, ahora):
    """Borrado lógico: el recordatorio deja de listarse y de dispararse, pero la fila se conserva (política de retención)."""
    try:
        ident = str(uuid.UUID(ident))
    except (ValueError, TypeError, AttributeError):
        raise Problem("El identificador del recordatorio no es válido.")
    if not db.execute("update feed_reminders set deleted_at=?, enabled=0, updated_at=? where id=? and child_id=? and family_id=? and deleted_at is null",
                      (ahora, ahora, ident, child, family)).rowcount:
        raise Problem("Recordatorio no encontrado.", 404)
    return listar_recordatorios(db, child)


def disparar(db, ahora):
    """Envía a Telegram los recordatorios vencidos y calcula el siguiente. Devuelve cuántos se dispararon.

    Idempotente: la clave de la cola incluye el vencimiento, así que reintentar tras un fallo no duplica. Si el
    servicio estuvo apagado varias horas se envía UN aviso (no una ráfaga) y el siguiente se calcula desde ahora."""
    disparados = 0
    for r in db.execute("select * from feed_reminders where enabled=1 and next_due_at<=? order by next_due_at,id", (ahora,)).fetchall():
        mensaje = notificaciones.formatear_alerta("Recordatorio: es hora de la toma del bebé", None, r["next_due_at"], "Informativo")
        for miembro in db.execute("select id from members where family_id=? order by id", (r["family_id"],)).fetchall():
            notificaciones.encolar(db, miembro["id"], f"recordatorio:{r['id']}:{r['next_due_at']}:{miembro['id']}", mensaje, None, ahora)
        proximo = r["next_due_at"]
        horas = r["times"].split(",") if r["times"] else None
        while proximo <= ahora:
            proximo = siguiente(r["kind"], r["interval_min"], horas, proximo if r["kind"] == "interval" else ahora)
        db.execute("update feed_reminders set next_due_at=? where id=?", (proximo, r["id"]))
        db.execute("update families set version=version+1 where id=?", (r["family_id"],))
        disparados += 1
    return disparados
