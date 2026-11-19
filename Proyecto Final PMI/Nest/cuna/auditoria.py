"""Registro unificado de eventos (RF-DAT-01, SCRUM-20).

La tabla audit_log guarda un renglón por cada hecho relevante de un dispositivo:
lecturas, asignaciones, retiros y cambios de estado. Los renglones de esos hechos
los crean triggers de la base, en la misma transacción que el hecho. Así ningún
camino de código puede guardar una lectura sin dejar su rastro. Los dispositivos
también pueden aportar eventos propios mediante `registrar`.

Fecha y hora se calculan en hora de Costa Rica, que es UTC-6 todo el año. Se usa
el desplazamiento fijo y no una zona con nombre para no depender de tzdata en Windows.
"""
import re
import uuid
from datetime import date

from .common import Problem, device_id, field, instant, uid

SEVERIDADES = ("info", "warning", "critical")
# Los escribe el sistema. Un dispositivo no puede fabricarlos por la API.
RESERVADOS = {"LECTURA", "ASIGNACION", "DESASIGNACION", "ACTIVACION", "DESACTIVACION",
              "NOTIFICACION_ENVIADA", "NOTIFICACION_FALLIDA"}
LIMITE_MAXIMO = 500

UUID_SQL = ("lower(hex(randomblob(4)))||'-'||lower(hex(randomblob(2)))||'-4'||substr(lower(hex(randomblob(2))),2)"
            "||'-'||substr('89ab',abs(random())%4+1,1)||substr(lower(hex(randomblob(2))),2)||'-'||lower(hex(randomblob(6)))")

def tabla(nombre="audit_log"):
    """device_id es el identificador operativo. device_uuid es el ID_Dispositivo del SRS.
    La clave compuesta impide que ambos apunten a dispositivos distintos."""
    return f"""
create table {nombre}(
 id text primary key default ({UUID_SQL}),
 family_id text not null references families,
 occurred_at text not null,
 event_date text not null generated always as (date(occurred_at,'-6 hours')) stored,
 event_time text not null generated always as (time(occurred_at,'-6 hours')) stored,
 event_type text not null check(length(event_type) between 1 and 50),
 detail text not null,
 child_id text not null references children,
 device_id text not null references devices,
 device_uuid text not null references devices(uuid),
 severity text not null default 'info' check(severity in ('info','warning','critical')),
 source_event_id text,
 foreign key (device_id,device_uuid) references devices(id,uuid));
"""


INDICES = """
create unique index if not exists audit_source on audit_log(source_event_id) where source_event_id is not null;
create index if not exists audit_family_time on audit_log(family_id,occurred_at);
create index if not exists audit_child_time on audit_log(child_id,occurred_at);
"""

TRIGGERS = """
create trigger audit_keep_update before update on audit_log begin select raise(abort,'Registro de auditoría inmutable'); end;
create trigger audit_keep_delete before delete on audit_log begin select raise(abort,'Registro de auditoría inmutable'); end;
create trigger audit_reading after insert on events begin
 insert into audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,device_uuid,source_event_id)
 select d.family_id,new.captured_at,'LECTURA',new.metric||': '||new.value||' '||new.unit,new.child_id,new.device_id,d.uuid,new.id
 from devices d where d.id=new.device_id;
end;
create trigger audit_assigned after insert on assignments begin
 insert into audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,device_uuid,source_event_id)
 select d.family_id,new.started_at,'ASIGNACION','Dispositivo asignado al bebé',new.child_id,new.device_id,d.uuid,new.id||':inicio'
 from devices d where d.id=new.device_id;
end;
create trigger audit_unassigned after update of ended_at on assignments
 when old.ended_at is null and new.ended_at is not null begin
 insert into audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,device_uuid,source_event_id)
 select d.family_id,new.ended_at,'DESASIGNACION','Dispositivo retirado del bebé',new.child_id,new.device_id,d.uuid,new.id||':fin'
 from devices d where d.id=new.device_id;
end;
create trigger audit_toggled after update of enabled on assignments when old.enabled<>new.enabled begin
 insert into audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,device_uuid)
 select d.family_id,strftime('%Y-%m-%dT%H:%M:%f000+00:00','now'),
  case when new.enabled then 'ACTIVACION' else 'DESACTIVACION' end,
  case when new.enabled then 'Sensor activado' else 'Sensor desactivado' end,new.child_id,new.device_id,d.uuid
 from devices d where d.id=new.device_id;
end;
"""

NOMBRES_TRIGGERS = ("audit_keep_update", "audit_keep_delete", "audit_reading", "audit_assigned",
                    "audit_unassigned", "audit_toggled")

# Rellena el historial anterior a la migración. source_event_id evita duplicados.
BACKFILL = """
insert into audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,device_uuid,source_event_id)
 select d.family_id,e.captured_at,'LECTURA',e.metric||': '||e.value||' '||e.unit,e.child_id,e.device_id,d.uuid,e.id
 from events e join devices d on d.id=e.device_id
 where not exists(select 1 from audit_log l where l.source_event_id=e.id);
insert into audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,device_uuid,source_event_id)
 select d.family_id,a.started_at,'ASIGNACION','Dispositivo asignado al bebé',a.child_id,a.device_id,d.uuid,a.id||':inicio'
 from assignments a join devices d on d.id=a.device_id
 where not exists(select 1 from audit_log l where l.source_event_id=a.id||':inicio');
insert into audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,device_uuid,source_event_id)
 select d.family_id,a.ended_at,'DESASIGNACION','Dispositivo retirado del bebé',a.child_id,a.device_id,d.uuid,a.id||':fin'
 from assignments a join devices d on d.id=a.device_id
 where a.ended_at is not null and not exists(select 1 from audit_log l where l.source_event_id=a.id||':fin');
"""

COLUMNAS = ("a.id,a.rowid as seq,a.occurred_at,a.event_date,a.event_time,a.event_type,a.detail,"
            "a.child_id,a.device_id,a.device_uuid,a.severity")


def evento(data):
    """Valida el cuerpo de POST /api/audit/events. El bebé NUNCA se toma del cuerpo."""
    try:
        event_id = str(uuid.UUID(data["event_id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise Problem("Se requiere un event_id UUID.")
    tipo = data.get("event_type")
    if not isinstance(tipo, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,49}", tipo):
        raise Problem("event_type usa mayúsculas, números y guion bajo, hasta 50 caracteres.")
    if tipo in RESERVADOS:
        raise Problem("Ese tipo de evento lo registra el sistema.", 403)
    severidad = data.get("severity", "info")
    if severidad not in SEVERIDADES:
        raise Problem("severity debe ser info, warning o critical.")
    # La hora la fija el dispositivo. Si la pusiera el servidor, un reintento cambiaría el contenido.
    at = instant(field(data, "occurred_at"))
    if at > instant():
        raise Problem("El evento está en el futuro. Revisa el reloj del dispositivo.")
    return event_id, at, tipo, field(data, "detail", 1000), severidad


def consulta(query):
    """Valida los filtros de consulta y exportación. Recibe el resultado de parse_qs."""
    def one(key):
        return query.get(key, [None])[0] or None

    filtros = {}
    if one("child"):
        try:
            filtros["child"] = str(uuid.UUID(one("child")))
        except ValueError:
            raise Problem("El identificador del bebé no es válido.")
    if one("device"):
        filtros["device"] = device_id({"device_id": one("device")})
    if one("tipo"):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,49}", one("tipo")):
            raise Problem("El tipo de evento no es válido.")
        filtros["tipo"] = one("tipo")
    for key in ("desde", "hasta"):
        if one(key):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", one(key)):
                raise Problem(f"{key} usa el formato AAAA-MM-DD.")
            try:
                date.fromisoformat(one(key))
            except ValueError:
                raise Problem(f"{key} no es una fecha real.")
            filtros[key] = one(key)
    if filtros.get("desde") and filtros.get("hasta") and filtros["desde"] > filtros["hasta"]:
        raise Problem("desde no puede ser posterior a hasta.")
    try:
        limite = int(one("limit") or 100)
    except ValueError:
        raise Problem("limit debe ser un número.")
    if not 1 <= limite <= LIMITE_MAXIMO:
        raise Problem(f"limit va de 1 a {LIMITE_MAXIMO}.")
    return filtros, decodificar(one("after")), limite


def codificar(row):
    return f"{row['occurred_at']}|{row['seq']}"


def decodificar(token):
    if not token:
        return None
    try:
        at, seq = token.rsplit("|", 1)
        return instant(at), int(seq)
    except (ValueError, Problem):
        raise Problem("El cursor after no es válido.")


def resolver_filtros(db, family, filtros):
    """Comprueba que el bebé y el dispositivo del filtro pertenecen a la familia.

    El dispositivo puede darse como Device_ID o como UUID. Devuelve los filtros con el Device_ID operativo.
    """
    resultado = dict(filtros)
    if "child" in filtros and not db.execute("select 1 from children where family_id=? and id=?",
                                             (family, filtros["child"])).fetchone():
        raise Problem("Perfil no disponible.", 404)
    if "device" in filtros:
        fila = db.execute("select id from devices where family_id=? and (id=? or uuid=?) order by id=? desc",
                          (family, filtros["device"], filtros["device"], filtros["device"])).fetchone()
        if not fila:
            raise Problem("Dispositivo no disponible.", 404)
        resultado["device"] = fila["id"]
    return resultado


def corte(db, family):
    """Último registro existente. Una exportación solo incluye hasta aquí, aunque lleguen eventos nuevos."""
    return db.execute("select coalesce(max(rowid),0) from audit_log where family_id=?", (family,)).fetchone()[0]


def pagina(db, family, filtros, after, limite):
    """Un lote ordenado por instante y orden de inserción. Los filtros de fecha usan la fecha local
    de Costa Rica e incluyen ambos extremos. Los valores van siempre como parámetros.

    Las claves `hoja` y `corte` las usa solo el exportador. Consulta() nunca las produce."""
    where, args = ["a.family_id=?"], [family]
    for columna, clave in (("a.child_id", "child"), ("a.device_id", "device"), ("a.event_type", "tipo")):
        if clave in filtros:
            where.append(columna + "=?")
            args.append(filtros[clave])
    if "desde" in filtros:
        where.append("a.event_date>=?")
        args.append(filtros["desde"])
    if "hasta" in filtros:
        where.append("a.event_date<=?")
        args.append(filtros["hasta"])
    if "corte" in filtros:
        where.append("a.rowid<=?")
        args.append(int(filtros["corte"]))
    origen, columnas = "audit_log a", COLUMNAS
    if filtros.get("hoja") == "telemetria":
        origen += " left join events e on e.id=a.source_event_id"
        columnas += ",e.metric,e.value,e.unit"
        where.append("a.event_type='LECTURA'")
    elif filtros.get("hoja") == "auditoria":
        where.append("a.event_type<>'LECTURA'")
    if after:
        where.append("(a.occurred_at,a.rowid)>(?,?)")
        args.extend(after)
    rows = [dict(r) for r in db.execute(
        f"select {columnas} from {origen} where {' and '.join(where)} order by a.occurred_at,a.rowid limit ?",
        (*args, limite + 1))]
    siguiente = codificar(rows[limite - 1]) if len(rows) > limite else None
    return {"items": [{k: v for k, v in r.items() if k != "seq"} for r in rows[:limite]], "next": siguiente}


def bebe_activo(db, device, at):
    """Bebé asignado al dispositivo en ese instante. Es la única fuente de ID_Bebé (CA2)."""
    row = db.execute("""select child_id from assignments where device_id=? and started_at<=?
        and (ended_at is null or ?<ended_at)""", (device, at, at)).fetchone()
    return row["child_id"] if row else None


def registrar(db, family, device, observation):
    """Guarda un evento aportado por un dispositivo ya autenticado. Es idempotente por event_id."""
    event_id, at, tipo, detalle, severidad = observation
    prior = db.execute("select * from audit_log where source_event_id=?", (event_id,)).fetchone()
    if prior:
        if (prior["device_id"], prior["occurred_at"], prior["event_type"], prior["detail"], prior["severity"]) \
                != (device, at, tipo, detalle, severidad):
            raise Problem("El identificador del evento ya tiene otro contenido.", 409)
        return {"child_id": prior["child_id"], "id": prior["id"]}
    child = bebe_activo(db, device, at)
    if not child:
        raise Problem("El dispositivo estaba sin asignar en esa fecha.", 409)
    new_id = uid()
    db.execute("""insert into audit_log(id,family_id,occurred_at,event_type,detail,child_id,device_id,device_uuid,severity,source_event_id)
        select ?,?,?,?,?,?,d.id,d.uuid,?,? from devices d where d.id=?""",
               (new_id, family, at, tipo, detalle, child, severidad, event_id, device))
    return {"child_id": child, "id": new_id}
