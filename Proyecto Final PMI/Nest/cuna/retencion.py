"""Retención permanente (RF-DAT-02, SCRUM-21): verificación, respaldo y restauración de la base SQLite.

Nada aquí borra registros. El respaldo usa la API de copia en línea de SQLite, que toma una
fotografía consistente aunque el servidor esté escribiendo. Los respaldos nunca se depuran solos.
"""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

TABLAS = ("families", "members", "children", "devices", "assignments", "events", "audit_log")
TRIGGERS = ("keep_event", "keep_history", "keep_assignments", "audit_keep_update", "audit_keep_delete")

SCHEMA = """
create trigger if not exists keep_assignments before delete on assignments begin
 select raise(abort,'Asignación inmutable');
end;
"""


class RespaldoError(Exception):
    pass


def abrir_lectura(ruta):
    ruta = Path(ruta)
    if not ruta.is_file():
        raise RespaldoError(f"No existe el archivo {ruta}.")
    db = sqlite3.connect(ruta.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def verificar(ruta):
    """Revisa integridad, relaciones, triggers de inmutabilidad y consistencia con la auditoría.

    Devuelve un diccionario con `ok`, `problemas` y `conteos`. No modifica el archivo.
    """
    problemas, conteos, avisos = [], {}, []
    try:
        db = abrir_lectura(ruta)
    except sqlite3.Error as exc:
        return {"ok": False, "problemas": [f"No se pudo abrir: {exc}"], "conteos": {}, "avisos": []}
    try:
        estado = db.execute("pragma integrity_check").fetchone()[0]
        if estado != "ok":
            problemas.append(f"integrity_check: {estado}")
            return {"ok": False, "problemas": problemas, "conteos": conteos, "avisos": avisos}
        existentes = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
        faltan = [t for t in TABLAS if t not in existentes]
        if faltan:
            problemas.append("Faltan tablas: " + ", ".join(faltan))
            return {"ok": False, "problemas": problemas, "conteos": conteos, "avisos": avisos}
        triggers = {r[0] for r in db.execute("select name from sqlite_master where type='trigger'")}
        problemas += [f"Falta el trigger de inmutabilidad {t}" for t in TRIGGERS if t not in triggers]
        huerfanas = db.execute("pragma foreign_key_check").fetchall()
        if huerfanas:
            problemas.append(f"{len(huerfanas)} filas con relaciones rotas")
        for tabla in TABLAS:
            conteos[tabla] = db.execute(f"select count(*) from {tabla}").fetchone()[0]
        sin_auditoria = db.execute("""select count(*) from events e where not exists
            (select 1 from audit_log l where l.source_event_id=e.id)""").fetchone()[0]
        if sin_auditoria:
            problemas.append(f"{sin_auditoria} lecturas sin renglón de auditoría")
        if "uuid" not in [r[1] for r in db.execute("pragma table_info(devices)")]:
            avisos.append("Respaldo anterior a la versión 3: sus dispositivos no tienen UUID. "
                          "Se asignará al abrirlo con la aplicación.")
        else:
            mal = db.execute("""select count(*) from devices where uuid is null
                or length(uuid)<>36 or uuid glob '*[^0-9a-f-]*'""").fetchone()[0]
            repetidos = db.execute("select count(*)-count(distinct uuid) from devices").fetchone()[0]
            cruzados = db.execute("""select count(*) from audit_log l join devices d on d.id=l.device_id
                where l.device_uuid<>d.uuid""").fetchone()[0]
            if mal or repetidos:
                problemas.append(f"UUID de dispositivo inválido o repetido ({mal + repetidos} casos)")
            if cruzados:
                problemas.append(f"{cruzados} renglones de auditoría con UUID distinto al de su dispositivo")
    except sqlite3.Error as exc:
        problemas.append(f"Error al leer: {exc}")
    finally:
        db.close()
    return {"ok": not problemas, "problemas": problemas, "conteos": conteos, "avisos": avisos}


def nombre_de_respaldo(carpeta, ahora=None):
    ahora = ahora or datetime.now(timezone.utc)
    return Path(carpeta) / f"nido-{ahora:%Y%m%d-%H%M%S}.sqlite3"


def copiar(origen, destino):
    """Copia consistente con la API de respaldo en línea. El destino debe ser un archivo nuevo."""
    entrada = abrir_lectura(origen)
    salida = sqlite3.connect(destino)
    try:
        entrada.backup(salida)
    finally:
        salida.close()
        entrada.close()


def respaldar(origen, destino):
    """Crea un respaldo verificado. Nunca sobrescribe un archivo existente."""
    destino = Path(destino)
    if destino.exists():
        raise RespaldoError(f"{destino} ya existe. Los respaldos no se sobrescriben.")
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_name(destino.name + ".parcial")
    temporal.unlink(missing_ok=True)
    try:
        copiar(origen, temporal)
    except sqlite3.Error as exc:
        temporal.unlink(missing_ok=True)
        raise RespaldoError(f"No se pudo copiar la base: {exc}") from exc
    informe = verificar(temporal)
    if not informe["ok"]:
        temporal.unlink(missing_ok=True)
        raise RespaldoError("El respaldo no pasó la verificación: " + "; ".join(informe["problemas"]))
    os.replace(temporal, destino)
    try:
        os.chmod(destino, 0o600)
    except OSError:
        pass
    return informe


def restaurar(respaldo, destino):
    """Reemplaza la base de destino por el contenido de un respaldo verificado.

    El servidor debe estar detenido. Si el destino existe, antes se guarda una copia con el
    sufijo .antes-de-restaurar. Si algo falla, el destino queda como estaba.
    """
    respaldo, destino = Path(respaldo), Path(destino)
    informe = verificar(respaldo)
    if not informe["ok"]:
        raise RespaldoError("El respaldo no es válido: " + "; ".join(informe["problemas"]))
    copia_previa = None
    if destino.exists():
        comprobar_sin_uso(destino)
        sello = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        copia_previa = destino.with_name(f"{destino.name}.antes-de-restaurar-{sello}")
        if copia_previa.exists():
            raise RespaldoError(f"{copia_previa} ya existe. Espera un segundo y repite.")
        try:
            copiar(destino, copia_previa)
        except sqlite3.Error as exc:
            copia_previa.unlink(missing_ok=True)
            raise RespaldoError(f"No se pudo guardar la copia previa de la base actual: {exc}") from exc
    pendiente = destino.with_name(destino.name + "-journal")
    if pendiente.exists():
        raise RespaldoError(f"Hay un archivo pendiente {pendiente.name}. Detén el servidor y repite.")
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_name(destino.name + ".restaurando")
    temporal.unlink(missing_ok=True)
    try:
        copiar(respaldo, temporal)
        revision = verificar(temporal)
        if not revision["ok"]:
            raise RespaldoError("La copia restaurada no pasó la verificación: " + "; ".join(revision["problemas"]))
        os.replace(temporal, destino)
    except (sqlite3.Error, OSError) as exc:
        temporal.unlink(missing_ok=True)
        raise RespaldoError(f"No se pudo restaurar: {exc}") from exc
    except RespaldoError:
        temporal.unlink(missing_ok=True)
        raise
    return {"informe": revision, "copia_previa": copia_previa}


def comprobar_sin_uso(ruta):
    """Detecta escrituras en curso. Un servidor inactivo no se puede detectar, por eso hay que detenerlo."""
    db = sqlite3.connect(ruta, timeout=0)
    try:
        db.execute("begin exclusive")
        db.rollback()
    except sqlite3.OperationalError as exc:
        raise RespaldoError("La base está en uso. Detén el servidor antes de restaurar.") from exc
    finally:
        db.close()
