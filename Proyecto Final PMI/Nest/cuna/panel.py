"""Sprint 2: historial ambiental (SCRUM-54), sesión de sueño y snapshot de masa (SCRUM-35), registro de tomas
(SCRUM-51), ajustes de luz y audio (SCRUM-57) y panel de control (SCRUM-56) para la demo SQLite.

Alcance honesto: todo lo que guarda este módulo es ESTADO LÓGICO. No hay ninguna ruta de comandos hacia el
hardware, así que "desarmar", "apagar un módulo" o "volumen" no accionan nada físico. Lo único que viene de un
dispositivo son las lecturas (`Masa` en gramos, temperatura y humedad) que ya entran por POST /api/events.

Las reglas de validación están aquí y las usan los dos almacenes (Local y Cloud). Las funciones con `db` solo las usa
Local. El equivalente en Supabase es supabase/009_panel_sprint2.sql.
"""
import math
import re
import uuid
from datetime import date, datetime, timedelta

from . import auditoria
from .common import Problem, instant, uid

MODULOS = ("temperatura", "luz", "audio", "colchon")
MODOS = ("manual", "programado")
# Hasta que alguien lo cambie: solo el sensor de temperatura figura encendido, porque es el que ya envía lecturas.
MODULO_INICIAL = {"temperatura": True, "luz": False, "audio": False, "colchon": False}

AUDIOS = ("ninguno", "ruido_blanco", "lluvia", "olas", "nana")
AJUSTES_INICIALES = {"led_speed_s": 5.0, "audio": "ruido_blanco", "volume": 30}
LED_MIN, LED_MAX = 0.5, 60.0

MASA_METRICA, MASA_UNIDAD = "Masa", "g"
MASA_VIGENCIA = 600  # s. Una lectura de masa más vieja no es una línea base fiable.

TOMA_TIPO = "TOMA"
TOMA_UNIDADES = ("g", "ml")
TOMA_MAX_SEGUNDOS = 6 * 3600
TOMA_MAX_VOLUMEN = 2000.0

HISTORIAL_LIMITE = 500
HISTORIAL_MAXIMO = 2000

SCHEMA = """
create table if not exists sleep_sessions(
 id text primary key, family_id text not null references families, child_id text not null references children,
 started_at text not null, started_by text references members, ended_at text, ended_by text references members,
 baseline_g real, baseline_event_id text references events, baseline_captured_at text, baseline_at text,
 check(baseline_g is null or baseline_g > 0), check(ended_at is null or ended_at >= started_at));
create unique index if not exists one_sleep_active on sleep_sessions(child_id) where ended_at is null;
create trigger if not exists keep_sleep before delete on sleep_sessions begin select raise(abort,'Sesión de sueño inmutable'); end;
create table if not exists child_settings(
 child_id text primary key references children, family_id text not null references families,
 led_speed_s real not null check(led_speed_s between 0.5 and 60), audio text not null,
 volume integer not null check(volume between 0 and 100), updated_at text not null, updated_by text references members);
create table if not exists module_states(
 child_id text not null references children, family_id text not null references families,
 module text not null check(module in ('temperatura','luz','audio','colchon')),
 enabled integer not null check(enabled in (0,1)), mode text not null check(mode in ('manual','programado')),
 updated_at text not null, updated_by text references members, primary key(child_id, module));
"""


# --- Validación compartida -------------------------------------------------------------------------------------

def uuid_texto(valor, mensaje="El identificador del bebé no es válido."):
    if not isinstance(valor, str):
        raise Problem(mensaje)
    try:
        return str(uuid.UUID(valor))
    except ValueError:
        raise Problem(mensaje)


def numero(valor, nombre):
    if type(valor) not in (int, float) or not math.isfinite(valor):
        raise Problem(f"{nombre} debe ser un número finito.")
    return float(valor)


def entero(valor, nombre):
    if type(valor) is not int:
        raise Problem(f"{nombre} debe ser un número entero.")
    return valor


def duracion_texto(segundos):
    h, resto = divmod(segundos, 3600)
    m, s = divmod(resto, 60)
    if h:
        return f"{h} h {m:02d} min {s:02d} s"
    return f"{m} min {s:02d} s" if m else f"{s} s"


def volumen_texto(volumen, unidad):
    return f"{round(volumen, 1):.1f} {unidad}"


def detalle_toma(duracion, volumen, unidad):
    return f"Toma: {duracion_texto(duracion)} · {volumen_texto(volumen, unidad)}"


def validar_toma(data):
    """Cuerpo de POST /api/feeding. Devuelve (child, event_id, duracion_s, volumen, unidad).

    El instante de la toma lo pone el servidor al recibirla; el cliente solo dice cuánto duró."""
    child = uuid_texto(data.get("child_id"))
    event_id = uuid_texto(data.get("event_id"), "Se requiere un event_id UUID.")
    duracion = entero(data.get("duration_s"), "La duración")
    if not 1 <= duracion <= TOMA_MAX_SEGUNDOS:
        raise Problem("La duración debe estar entre 1 segundo y 6 horas.")
    volumen = numero(data.get("volume"), "El volumen")
    if not 0 < volumen <= TOMA_MAX_VOLUMEN:
        raise Problem("El volumen debe ser mayor que 0 y de hasta 2000.")
    unidad = data.get("unit")
    if unidad not in TOMA_UNIDADES:
        raise Problem("La unidad del volumen debe ser g o ml.")
    return child, event_id, duracion, round(volumen, 1), unidad


def validar_ajustes(data):
    """Cuerpo de POST /api/settings. Devuelve (child, velocidad_led_s, audio, volumen)."""
    child = uuid_texto(data.get("child_id"))
    velocidad = numero(data.get("led_speed_s"), "La velocidad del LED")
    if not LED_MIN <= velocidad <= LED_MAX:
        raise Problem(f"La velocidad del LED va de {LED_MIN:g} a {LED_MAX:g} segundos.")
    audio = data.get("audio")
    if audio not in AUDIOS:
        raise Problem("Elige un audio de la lista.")
    volumen = entero(data.get("volume"), "El volumen")
    if not 0 <= volumen <= 100:
        raise Problem("El volumen va de 0 a 100.")
    return child, round(velocidad, 1), audio, volumen


def validar_modulo(data):
    """Cuerpo de POST /api/modules. Devuelve (child, modulo, encendido, modo)."""
    child = uuid_texto(data.get("child_id"))
    modulo = data.get("module")
    if modulo not in MODULOS:
        raise Problem("Módulo desconocido.")
    if not isinstance(data.get("enabled"), bool):
        raise Problem("El estado del módulo debe ser booleano.")
    if data.get("mode") not in MODOS:
        raise Problem("El modo debe ser manual o programado.")
    return child, modulo, data["enabled"], data["mode"]


def fecha(texto, clave):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", texto):
        raise Problem(f"{clave} usa el formato AAAA-MM-DD.")
    try:
        return date.fromisoformat(texto)
    except ValueError:
        raise Problem(f"{clave} no es una fecha real.")


def rango_utc(desde, hasta):
    """Convierte dos fechas locales de Costa Rica (UTC-6 todo el año, ambas inclusivas) en [desde, hasta) UTC.
    Mismo criterio que los filtros de /api/audit."""
    inicio = fin = None
    if desde:
        inicio = instant(f"{fecha(desde, 'desde').isoformat()}T06:00:00+00:00")
    if hasta:
        fin = instant(f"{(fecha(hasta, 'hasta') + timedelta(days=1)).isoformat()}T06:00:00+00:00")
    if desde and hasta and fecha(desde, "desde") > fecha(hasta, "hasta"):
        raise Problem("desde no puede ser posterior a hasta.")
    return inicio, fin


def consulta_historial(query):
    """Parámetros de GET /api/history. Devuelve (child, filtros, after, limite)."""
    def uno(clave):
        return query.get(clave, [None])[0] or None

    if not uno("child"):
        raise Problem("Indica el bebé con ?child=.")
    child = uuid_texto(uno("child"))
    filtros = {}
    for clave in ("desde", "hasta"):
        if uno(clave):
            fecha(uno(clave), clave)
            filtros[clave] = uno(clave)
    rango_utc(filtros.get("desde"), filtros.get("hasta"))  # valida el orden
    if uno("metric"):
        if len(uno("metric")) > 40:
            raise Problem("La medida no es válida.")
        filtros["metric"] = uno("metric")
    try:
        limite = int(uno("limit") or HISTORIAL_LIMITE)
    except ValueError:
        raise Problem("limit debe ser un número.")
    if not 1 <= limite <= HISTORIAL_MAXIMO:
        raise Problem(f"limit va de 1 a {HISTORIAL_MAXIMO}.")
    return child, filtros, decodificar_cursor(uno("after")), limite


def codificar_cursor(fila):
    return f"{fila['captured_at']}|{fila['id']}"


def decodificar_cursor(token):
    if not token:
        return None
    try:
        at, ident = token.replace(" ", "+").rsplit("|", 1)  # un "+" sin codificar en la URL llega como espacio
        return instant(at), str(uuid.UUID(ident))
    except (ValueError, Problem):
        raise Problem("El cursor after no es válido.")


def pagina_historial(filas, limite):
    """De limite+1 filas ordenadas saca la página y el cursor siguiente."""
    siguiente = codificar_cursor(filas[limite - 1]) if len(filas) > limite else None
    return {"items": [dict(f) for f in filas[:limite]], "next": siguiente}


# --- SQLite: historial, sueño, tomas, ajustes y módulos -------------------------------------------------------

def historial(db, child, filtros, after, limite):
    inicio, fin = rango_utc(filtros.get("desde"), filtros.get("hasta"))
    where, args = ["child_id=?"], [child]
    if "metric" in filtros:
        where.append("metric=?")
        args.append(filtros["metric"])
    if inicio:
        where.append("captured_at>=?")
        args.append(inicio)
    if fin:
        where.append("captured_at<?")
        args.append(fin)
    if after:
        where.append("(captured_at,id)>(?,?)")
        args.extend(after)
    filas = [dict(r) for r in db.execute(
        f"select id,device_id,captured_at,metric,value,unit from events where {' and '.join(where)} "
        "order by captured_at,id limit ?", (*args, limite + 1))]
    return pagina_historial(filas, limite)


def antes(ahora, segundos):
    return (datetime.fromisoformat(ahora) - timedelta(seconds=segundos)).isoformat(timespec="microseconds")


def sesion_activa(db, child):
    return db.execute("select * from sleep_sessions where child_id=? and ended_at is null", (child,)).fetchone()


def ultima_masa(db, child):
    return db.execute("""select id,value,captured_at from events where child_id=? and metric=? and unit=?
        order by captured_at desc,id desc limit 1""", (child, MASA_METRICA, MASA_UNIDAD)).fetchone()


def estado_sueno(db, child, ahora):
    sesion, masa = sesion_activa(db, child), ultima_masa(db, child)
    salida = {"child_id": child, "active": sesion is not None, "session": None, "mass": None, "current_pct": None}
    if sesion:
        salida["session"] = {"id": sesion["id"], "started_at": sesion["started_at"], "baseline_g": sesion["baseline_g"],
                             "baseline_at": sesion["baseline_at"], "baseline_captured_at": sesion["baseline_captured_at"]}
    if masa:
        salida["mass"] = {"value": masa["value"], "captured_at": masa["captured_at"],
                          "fresh": masa["captured_at"] >= antes(ahora, MASA_VIGENCIA)}
        if sesion and sesion["baseline_g"]:
            salida["current_pct"] = round(masa["value"] / sesion["baseline_g"] * 100, 1)
    return salida


def iniciar_sueno(db, family, child, miembro, ahora):
    if sesion_activa(db, child):
        raise Problem("Ya hay una sesión de sueño activa.", 409)
    db.execute("insert into sleep_sessions(id,family_id,child_id,started_at,started_by) values (?,?,?,?,?)",
               (uid(), family, child, ahora, miembro))


def snapshot_masa(db, child, miembro, ahora):
    """Guarda la última lectura de masa (g) como línea base (100 %) de la sesión activa. No inventa pesos:
    sin sesión, sin lectura, con lectura vieja o con lectura no positiva responde 409 y no cambia nada."""
    sesion = sesion_activa(db, child)
    if not sesion:
        raise Problem("No hay una sesión de sueño activa. Inicia una antes de tomar el snapshot.", 409)
    masa = ultima_masa(db, child)
    if not masa:
        raise Problem("Aún no hay lecturas de masa en gramos (g) para este bebé.", 409)
    if masa["captured_at"] < antes(ahora, MASA_VIGENCIA):
        raise Problem("La última lectura de masa tiene más de 10 minutos. Revisa el colchón y vuelve a intentarlo.", 409)
    if not masa["value"] > 0:
        raise Problem("La lectura de masa no es válida: debe ser mayor que 0 g.", 409)
    db.execute("""update sleep_sessions set baseline_g=?, baseline_event_id=?, baseline_captured_at=?, baseline_at=?
        where id=?""", (masa["value"], masa["id"], masa["captured_at"], ahora, sesion["id"]))


def desarmar_sueno(db, child, miembro, ahora):
    sesion = sesion_activa(db, child)
    if not sesion:
        raise Problem("No hay una sesión de sueño activa.", 409)
    db.execute("update sleep_sessions set ended_at=?, ended_by=? where id=?", (ahora, miembro, sesion["id"]))


def registrar_toma(db, family, child, event_id, duracion, volumen, unidad, ahora):
    """Guarda la toma con el ayudante de auditoría. Reintentar con el mismo event_id no duplica la toma.

    audit_log exige un dispositivo, así que la toma queda ligada al dispositivo activo del bebé."""
    previo = db.execute("select * from audit_log where source_event_id=?", (event_id,)).fetchone()
    if previo:
        if previo["child_id"] != child or previo["event_type"] != TOMA_TIPO or previo["family_id"] != family:
            raise Problem("El identificador del evento ya tiene otro contenido.", 409)
        return {"child_id": child, "id": previo["id"], "device_id": previo["device_id"], "duplicado": True}
    fila = db.execute("select device_id from assignments where child_id=? and ended_at is null order by device_id limit 1",
                      (child,)).fetchone()
    if not fila:
        raise Problem("Asigna un dispositivo al bebé antes de registrar tomas: la auditoría guarda cada evento con su dispositivo.", 409)
    resultado = auditoria.registrar(db, family, fila["device_id"],
                                    (event_id, ahora, TOMA_TIPO, detalle_toma(duracion, volumen, unidad), "info"))
    return {"child_id": child, "id": resultado["id"], "device_id": fila["device_id"], "duplicado": False}


def leer_ajustes(db, child):
    fila = db.execute("select led_speed_s,audio,volume from child_settings where child_id=?", (child,)).fetchone()
    if fila:
        return {"child_id": child, "led_speed_s": fila["led_speed_s"], "audio": fila["audio"], "volume": fila["volume"],
                "default": False}
    return {"child_id": child, **AJUSTES_INICIALES, "default": True}


def guardar_ajustes(db, family, child, velocidad, audio, volumen, miembro, ahora):
    if not (isinstance(velocidad, (int, float)) and LED_MIN <= velocidad <= LED_MAX and audio in AUDIOS
            and isinstance(volumen, int) and 0 <= volumen <= 100):
        raise Problem("Ajustes no válidos.")
    db.execute("""insert into child_settings(child_id,family_id,led_speed_s,audio,volume,updated_at,updated_by) values (?,?,?,?,?,?,?)
        on conflict(child_id) do update set led_speed_s=excluded.led_speed_s, audio=excluded.audio, volume=excluded.volume,
        updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
               (child, family, velocidad, audio, volumen, ahora, miembro))
    return leer_ajustes(db, child)


def leer_modulos(db, child):
    guardados = {r["module"]: r for r in db.execute("select module,enabled,mode from module_states where child_id=?", (child,))}
    modulos = []
    for nombre in MODULOS:
        fila = guardados.get(nombre)
        modulos.append({"module": nombre, "enabled": bool(fila["enabled"]) if fila else MODULO_INICIAL[nombre],
                        "mode": fila["mode"] if fila else "manual", "default": fila is None})
    return {"child_id": child, "modules": modulos}


def guardar_modulo(db, family, child, modulo, encendido, modo, miembro, ahora):
    if modulo not in MODULOS or modo not in MODOS or not isinstance(encendido, bool):
        raise Problem("Módulo, modo o estado no válido.")
    db.execute("""insert into module_states(child_id,family_id,module,enabled,mode,updated_at,updated_by) values (?,?,?,?,?,?,?)
        on conflict(child_id,module) do update set enabled=excluded.enabled, mode=excluded.mode,
        updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
               (child, family, modulo, int(encendido), modo, ahora, miembro))
    return leer_modulos(db, child)
