"""Alertas de temperatura por servidor (RF-NOT-02) con umbrales por bebé (RF-CUN-01) y monitoreo ambiental (RF-CUN-02).

El dispositivo solo informa lecturas. El servidor decide si hay alerta, de modo que el criterio es único y
cambiar un umbral no exige tocar el firmware. La regla es la del SRS:

* Rango por defecto 19 a 22 °C. Hay alerta solo si se SUPERA el máximo o se baja del mínimo (`>` y `<`).
* Se avisa solo al CAMBIAR de estado (normal -> calor, normal -> frío, calor -> frío...). Mientras la
  temperatura siga fuera de rango no se repite el aviso. Volver a la normalidad no genera aviso.
* La humedad se guarda como lectura, pero no genera alerta.

Todo ocurre en la misma transacción que guarda la lectura: lectura, estado, registro de auditoría y
solicitud de Telegram se confirman juntos o no se confirma nada. La solicitud entra a la cola de SCRUM-24
y el procesador la envía aparte, así que ninguna alerta depende de que el navegador esté abierto.
"""
import math
import uuid

from . import auditoria, notificaciones
from .common import Problem, instant

METRICA_TEMPERATURA = "Temperatura"
UNIDAD_TEMPERATURA = "°C"
METRICA_HUMEDAD = "Humedad"
TEMP_MIN_DEFECTO = 19.0
TEMP_MAX_DEFECTO = 22.0
LIMITE_INFERIOR = -20.0  # fuera de lo que un cuarto puede medir: casi seguro un error de captura
LIMITE_SUPERIOR = 60.0
ESPACIO_NOMBRES = uuid.UUID("5e0f2a1c-7a43-4c8e-9a52-0c8c5f6f7b11")  # UUID v5: la alerta de una lectura siempre tiene el mismo id

TIPOS = {"calor": "ALERTA_TEMPERATURA_ALTA", "frio": "ALERTA_TEMPERATURA_BAJA"}

SCHEMA = """
create table if not exists thresholds(
 child_id text primary key references children,
 family_id text not null references families,
 temp_min real not null, temp_max real not null,
 updated_at text not null, updated_by text references members,
 check(temp_min < temp_max));
create table if not exists alert_state(
 child_id text not null references children,
 device_id text not null references devices,
 state text not null check(state in ('normal','calor','frio')),
 since text not null,
 primary key(child_id, device_id));
"""


def clasificar(valor, minimo=TEMP_MIN_DEFECTO, maximo=TEMP_MAX_DEFECTO):
    """'calor' si supera el máximo, 'frio' si baja del mínimo, 'normal' en cualquier otro caso (incluidos los límites)."""
    if valor > maximo:
        return "calor"
    if valor < minimo:
        return "frio"
    return "normal"


def validar_umbrales(minimo, maximo):
    for valor in (minimo, maximo):
        if type(valor) not in (int, float) or not math.isfinite(valor):
            raise Problem("Los umbrales deben ser números.")
        if not LIMITE_INFERIOR <= valor <= LIMITE_SUPERIOR:
            raise Problem(f"Los umbrales deben estar entre {LIMITE_INFERIOR:g} y {LIMITE_SUPERIOR:g} °C.")
    if not minimo < maximo:
        raise Problem("El mínimo debe ser menor que el máximo.")
    return float(minimo), float(maximo)


def leer_umbrales(db, child):
    fila = db.execute("select temp_min,temp_max from thresholds where child_id=?", (child,)).fetchone()
    if fila:
        return fila["temp_min"], fila["temp_max"], False
    return TEMP_MIN_DEFECTO, TEMP_MAX_DEFECTO, True


def guardar_umbrales(db, family, child, minimo, maximo, member, ahora):
    minimo, maximo = validar_umbrales(minimo, maximo)
    db.execute("""insert into thresholds(child_id,family_id,temp_min,temp_max,updated_at,updated_by) values (?,?,?,?,?,?)
        on conflict(child_id) do update set temp_min=excluded.temp_min, temp_max=excluded.temp_max,
        updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
               (child, family, minimo, maximo, ahora, member))
    return {"child_id": child, "temp_min": minimo, "temp_max": maximo, "default": False}


def titulo(estado, valor, minimo, maximo):
    if estado == "calor":
        return f"Temperatura alta: {valor:.1f} °C (máximo {maximo:g} °C)"
    return f"Temperatura baja: {valor:.1f} °C (mínimo {minimo:g} °C)"


def evaluar(db, family, device, child, at, event_id, valor, ahora=None):
    """Compara la lectura con los umbrales del bebé y, si cambió de estado hacia fuera de rango, alerta.

    Devuelve None si no hubo alerta, o {"estado", "evento", "solicitudes"} con el registro de auditoría y las
    solicitudes encoladas. Debe llamarse dentro de la transacción que guardó la lectura.
    """
    minimo, maximo, _ = leer_umbrales(db, child)
    nuevo = clasificar(valor, minimo, maximo)
    previo = db.execute("select state,since from alert_state where child_id=? and device_id=?", (child, device)).fetchone()
    if previo and at < previo["since"]:
        return None  # lectura atrasada (reintento viejo): no puede deshacer un estado más reciente
    anterior = previo["state"] if previo else "normal"
    if previo is None:
        db.execute("insert into alert_state(child_id,device_id,state,since) values (?,?,?,?)", (child, device, nuevo, at))
    elif nuevo != anterior:
        db.execute("update alert_state set state=?, since=? where child_id=? and device_id=?", (nuevo, at, child, device))
    if nuevo == anterior or nuevo == "normal":
        return None
    ahora = ahora or instant()
    detalle = titulo(nuevo, valor, minimo, maximo)
    fuente = str(uuid.uuid5(ESPACIO_NOMBRES, event_id))
    registro = auditoria.registrar(db, family, device, (fuente, at, TIPOS[nuevo], detalle, "critical"))
    solicitudes = []
    for miembro in db.execute("select id from members where family_id=? order by id", (family,)).fetchall():
        mensaje = notificaciones.formatear_alerta(detalle, device, at, "Crítico")
        solicitudes.append(notificaciones.encolar(db, miembro["id"], f"temp:{event_id}:{miembro['id']}",
                                                  mensaje, registro["id"], ahora))
    return {"estado": nuevo, "evento": registro["id"], "solicitudes": solicitudes}
