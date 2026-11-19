"""Migraciones de la base SQLite.

La versión 3 agrega un UUID persistente a cada dispositivo y lo enlaza con la auditoría.
`devices.id` sigue siendo el identificador operativo (por ejemplo TEMP-01). `devices.uuid` es
el ID_Dispositivo del SRS. La versión 4 agrega las tablas de Telegram (SCRUM-24). La migración corre en una sola transacción: si algo falla, la base
queda exactamente como estaba. Ejecutarla de nuevo no cambia ningún UUID ni duplica registros.
"""
import sqlite3

from . import alertas, auditoria, notificaciones, retencion

VERSION = 4


class MigracionError(Exception):
    pass


UUID_CHECK = ("length(uuid)=36 and uuid not glob '*[^0-9a-f-]*' and substr(uuid,9,1)='-' "
              "and substr(uuid,14,1)='-' and substr(uuid,19,1)='-' and substr(uuid,24,1)='-'")


def tabla_dispositivos(nombre="devices"):
    return f"""
create table {nombre}(
 id text primary key, family_id text not null references families, name text not null, secret_hash text not null,
 uuid text not null unique default ({auditoria.UUID_SQL}) check({UUID_CHECK}),
 unique(id,uuid));
"""


TRIGGER_UUID = """
create trigger devices_keep_uuid before update of uuid on devices when old.uuid<>new.uuid begin
 select raise(abort,'El UUID del dispositivo no puede cambiar');
end;
"""


def ejecutar(db, script):
    """executescript confirma la transacción en curso. Esta versión ejecuta sentencia por sentencia."""
    pendiente = ""
    for linea in script.splitlines(keepends=True):
        pendiente += linea
        if sqlite3.complete_statement(pendiente):
            db.execute(pendiente)
            pendiente = ""
    if pendiente.strip():
        db.execute(pendiente)


def columnas(db, tabla):
    return [r[1] for r in db.execute(f"pragma table_info({tabla})")]


def existe(db, tabla):
    return db.execute("select 1 from sqlite_master where type='table' and name=?", (tabla,)).fetchone() is not None


def contar(db, tabla):
    return db.execute(f"select count(*) from {tabla}").fetchone()[0]


def reconstruir_dispositivos(db):
    antes = contar(db, "devices")
    for nombre in auditoria.NOMBRES_TRIGGERS:
        db.execute(f"drop trigger if exists {nombre}")
    ejecutar(db, tabla_dispositivos("devices_nueva"))
    db.execute("insert into devices_nueva(id,family_id,name,secret_hash) select id,family_id,name,secret_hash from devices")
    if contar(db, "devices_nueva") != antes:
        raise MigracionError("La copia de dispositivos no conserva todas las filas.")
    db.execute("drop table devices")
    db.execute("alter table devices_nueva rename to devices")


def reconstruir_auditoria(db):
    antes = contar(db, "audit_log")
    ejecutar(db, auditoria.tabla("audit_log_nueva"))
    db.execute("""insert into audit_log_nueva(rowid,id,family_id,occurred_at,event_type,detail,child_id,device_id,
            device_uuid,severity,source_event_id)
        select a.rowid,a.id,a.family_id,a.occurred_at,a.event_type,a.detail,a.child_id,a.device_id,
            d.uuid,a.severity,a.source_event_id
        from audit_log a left join devices d on d.id=a.device_id order by a.rowid""")
    if contar(db, "audit_log_nueva") != antes:
        raise MigracionError("La copia de la auditoría no conserva todas las filas.")
    db.execute("drop table audit_log")
    db.execute("alter table audit_log_nueva rename to audit_log")


def migrar(ruta, esquema_base, antes_de_confirmar=None):
    """Lleva la base a la versión actual. `antes_de_confirmar` solo lo usan las pruebas para simular un fallo."""
    db = sqlite3.connect(str(ruta), timeout=10, isolation_level=None)
    try:
        db.execute("pragma foreign_keys=off")
        db.execute("begin immediate")
        try:
            if not existe(db, "devices"):
                ejecutar(db, tabla_dispositivos())
            elif "uuid" not in columnas(db, "devices"):
                reconstruir_dispositivos(db)
            ejecutar(db, esquema_base)
            db.execute("drop trigger if exists devices_keep_uuid")
            ejecutar(db, TRIGGER_UUID)
            if not existe(db, "audit_log"):
                ejecutar(db, auditoria.tabla())
            elif "device_uuid" not in columnas(db, "audit_log"):
                reconstruir_auditoria(db)
            for nombre in auditoria.NOMBRES_TRIGGERS:
                db.execute(f"drop trigger if exists {nombre}")
            ejecutar(db, auditoria.INDICES)
            ejecutar(db, auditoria.TRIGGERS)
            ejecutar(db, retencion.SCHEMA)
            ejecutar(db, notificaciones.SCHEMA)
            ejecutar(db, alertas.SCHEMA)
            ejecutar(db, auditoria.BACKFILL)
            rotas = db.execute("pragma foreign_key_check").fetchall()
            if rotas:
                raise MigracionError(f"{len(rotas)} filas con relaciones rotas tras migrar.")
            if db.execute("pragma user_version").fetchone()[0] < VERSION:
                db.execute(f"pragma user_version={VERSION}")
            if antes_de_confirmar:
                antes_de_confirmar(db)
            db.execute("commit")
        except BaseException as exc:
            if db.in_transaction:
                db.execute("rollback")
            if isinstance(exc, MigracionError):
                raise
            if isinstance(exc, sqlite3.Error):
                raise MigracionError(f"La migración falló y se revirtió: {exc}") from exc
            raise
    finally:
        db.close()
