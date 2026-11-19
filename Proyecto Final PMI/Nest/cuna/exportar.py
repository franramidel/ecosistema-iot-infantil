"""Exportación del registro unificado a Excel (RF-DAT-04, SCRUM-23).

El libro tiene tres hojas. Auditoría guarda todos los eventos salvo las lecturas. Telemetría guarda
las lecturas de los sensores con su medida, valor y unidad. Resumen documenta los filtros y los totales.
Una lectura vive en Telemetría y no se repite en Auditoría, aunque la tabla audit_log tenga un
renglón LECTURA por cada una.

El archivo se arma por lotes con openpyxl en modo write_only, así que la memoria no crece con el
número de filas. Todas las páginas se consultan con un corte fijo tomado al empezar, para que los
eventos que lleguen durante la exportación no la mezclen ni la dupliquen.
"""
import os
import re
import tempfile
from datetime import date, datetime, time, timedelta, timezone

from .auditoria import LIMITE_MAXIMO, decodificar
from .common import Problem

COSTA_RICA = timezone(timedelta(hours=-6))
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_FILAS_POR_HOJA = 1_048_575  # el límite de Excel menos la fila de encabezados
MAX_TEXTO = 32_000  # Excel admite 32 767 caracteres por celda

BASE = ["Fecha", "Hora", "Tipo_de_Evento", "Descripción_Detalle", "ID_Bebé", "ID_Dispositivo"]
COLUMNAS_AUDITORIA = BASE + ["Device_ID", "Severidad"]
COLUMNAS_TELEMETRIA = BASE + ["Device_ID", "Medida", "Valor", "Unidad"]

ILEGALES = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
PELIGROSO = re.compile(r"^[\s]*[=+\-@]|^[\t\r\n]")


def texto_seguro(valor):
    """Neutraliza fórmulas. Un texto que empiece por = + - @ (tras espacios) o por tabulador o salto
    de línea se guarda con una comilla delante, como recomienda OWASP, y siempre como texto."""
    if valor is None:
        return ""
    valor = ILEGALES.sub("", str(valor))[:MAX_TEXTO]
    return "'" + valor if PELIGROSO.match(valor) else valor


def celda(hoja, valor, formato=None):
    from openpyxl.cell import WriteOnlyCell
    c = WriteOnlyCell(hoja, value=valor)
    if formato:
        c.number_format = formato
    elif isinstance(valor, str):
        c.data_type = "s"
    return c


def fila_base(hoja, f):
    return [celda(hoja, date.fromisoformat(f["event_date"]), "yyyy-mm-dd"),
            celda(hoja, time.fromisoformat(f["event_time"][:8]), "hh:mm:ss"),
            celda(hoja, texto_seguro(f["event_type"])), celda(hoja, texto_seguro(f["detail"])),
            celda(hoja, texto_seguro(f["child_id"])), celda(hoja, texto_seguro(f["device_uuid"])),
            celda(hoja, texto_seguro(f["device_id"]))]


def fila_auditoria(hoja, f):
    return fila_base(hoja, f) + [celda(hoja, texto_seguro(f["severity"]))]


def fila_telemetria(hoja, f):
    valor = f.get("value")
    return fila_base(hoja, f) + [celda(hoja, texto_seguro(f.get("metric"))),
                                 celda(hoja, None if valor is None else float(valor), "General"),
                                 celda(hoja, texto_seguro(f.get("unit")))]


def paginas(store, session, filtros, hoja, corte):
    """Recorre todas las filas de una hoja. Termina cuando una página llega sin cursor siguiente."""
    after = None
    while True:
        pagina = store.audit_page(session, {**filtros, "hoja": hoja, "corte": corte}, after, LIMITE_MAXIMO)
        yield from pagina["items"]
        if not pagina["next"]:
            return
        after = decodificar(pagina["next"])


def nombre_de_archivo(filtros, ahora):
    partes = ["nido-reporte"]
    if "child" in filtros:
        partes.append("bebe-" + filtros["child"][:8])
    if "device" in filtros:
        partes.append("disp-" + re.sub(r"[^A-Za-z0-9_.-]", "", filtros["device"])[:20])
    if "desde" in filtros or "hasta" in filtros:
        partes.append(f"{filtros.get('desde', 'inicio')}_{filtros.get('hasta', 'hoy')}")
    partes.append(f"{ahora:%Y%m%d-%H%M%S}")
    return "-".join(partes) + ".xlsx"


class Exportacion:
    def __init__(self, ruta, nombre, auditoria, telemetria):
        self.ruta, self.nombre, self.auditoria, self.telemetria = ruta, nombre, auditoria, telemetria

    def eliminar(self):
        try:
            os.remove(self.ruta)
        except FileNotFoundError:
            pass


def escribir_hoja(libro, nombre, columnas, anchos, filas, construir):
    hoja = libro.create_sheet(nombre)
    hoja.freeze_panes = "A2"
    for letra, ancho in zip("ABCDEFGHIJ", anchos):
        hoja.column_dimensions[letra].width = ancho
    hoja.auto_filter.ref = f"A1:{chr(64 + len(columnas))}1"
    hoja.append(columnas)
    total = 0
    for f in filas:
        if total >= MAX_FILAS_POR_HOJA:
            raise Problem("Demasiadas filas para una hoja de Excel. Acota el rango de fechas.", 413)
        hoja.append(construir(hoja, f))
        total += 1
    return total


def generar(store, session, filtros, carpeta=None, ahora=None):
    """Valida, consulta por lotes y escribe el libro en un archivo temporal privado.

    La autorización y los filtros se comprueban en `audit_corte`, antes de crear ningún archivo.
    Quien llama debe enviar el archivo y luego llamar a `eliminar()`.
    """
    try:
        from openpyxl import Workbook
    except ImportError:
        raise Problem("Falta instalar openpyxl (pip install -r requirements.txt).", 503)
    ahora = ahora or datetime.now(COSTA_RICA)
    corte = store.audit_corte(session, filtros)
    descriptor, ruta = tempfile.mkstemp(prefix="nido-", suffix=".xlsx", dir=carpeta)
    os.close(descriptor)
    libro = None
    try:
        libro = Workbook(write_only=True)
        resumen = libro.create_sheet("Resumen")
        n_auditoria = escribir_hoja(libro, "Auditoría", COLUMNAS_AUDITORIA, (12, 10, 18, 50, 38, 38, 16, 11),
                                    paginas(store, session, filtros, "auditoria", corte), fila_auditoria)
        n_telemetria = escribir_hoja(libro, "Telemetría", COLUMNAS_TELEMETRIA, (12, 10, 14, 40, 38, 38, 16, 18, 12, 10),
                                     paginas(store, session, filtros, "telemetria", corte), fila_telemetria)
        resumen.column_dimensions["A"].width = 30
        resumen.column_dimensions["B"].width = 80
        for linea in (
            ("Reporte", "Nido · Registro unificado de eventos (RF-DAT-04)"),
            ("Generado", f"{ahora:%Y-%m-%d %H:%M:%S} (hora de Costa Rica, UTC-6)"),
            ("Bebé (ID_Bebé)", filtros.get("child", "Todos los bebés de la familia")),
            ("Dispositivo", filtros.get("device", "Todos los dispositivos")),
            ("Fecha desde", filtros.get("desde", "Sin límite")),
            ("Fecha hasta", filtros.get("hasta", "Sin límite")),
            ("Criterio de fechas", "Ambos extremos incluidos. La fecha es la local de Costa Rica: un evento a las "
                                   "00:00:00 cuenta en el día que empieza y uno a las 23:59:59 en el día que termina."),
            ("Filas en Auditoría", n_auditoria),
            ("Filas en Telemetría", n_telemetria),
            ("Sin duplicados", "Las lecturas aparecen solo en Telemetría. Auditoría contiene los demás eventos."),
            ("Corte de la consulta", f"Registro {corte}. Los eventos posteriores no se incluyen."),
        ):
            resumen.append([celda(resumen, texto_seguro(linea[0])),
                            celda(resumen, linea[1] if isinstance(linea[1], int) else texto_seguro(linea[1]))])
        libro.save(ruta)
    except BaseException:
        if libro is not None:
            libro.close()
        try:
            os.remove(ruta)
        except FileNotFoundError:
            pass
        raise
    libro.close()
    return Exportacion(ruta, nombre_de_archivo(filtros, ahora), n_auditoria, n_telemetria)
