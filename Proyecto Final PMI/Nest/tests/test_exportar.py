"""SCRUM-23 · RF-DAT-04. Exportación a Excel: los .xlsx se leen de vuelta con openpyxl."""
import io
import os
import re
import sqlite3
import tempfile
import time
import tracemalloc
import unittest
from contextlib import closing
from datetime import date, datetime, time as hora
from urllib.parse import parse_qs, urlencode, urlsplit

from openpyxl import load_workbook

from apoyo import ADMIN, Base
from cuna import exportar
from cuna.cloud import Cloud
from cuna.common import Problem, uid

XLSX = exportar.MIME
SRS = ["Fecha", "Hora", "Tipo_de_Evento", "Descripción_Detalle", "ID_Bebé", "ID_Dispositivo"]
V_LUNA = [("2026-03-01T05:59:59.000000+00:00", 20.0), ("2026-03-01T06:00:00.000000+00:00", 21.0),
          ("2026-03-05T12:00:00.000000+00:00", 22.0), ("2026-03-10T12:00:00.000000+00:00", 23.0)]


class ExportBase(Base):
    def uuid_de(self, device):
        with self.db.connection() as db:
            return db.execute("select uuid from devices where id=?", (device,)).fetchone()[0]

    def sesion(self):
        with self.db.connection() as db:
            return {"id": db.execute("select id from members where email=?", (ADMIN[0],)).fetchone()[0]}

    def lectura(self, device, child, at, valor, metrica="Temperatura", unidad="°C"):
        with self.db.connection(True) as db:
            db.execute("insert into events values (?,?,?,?,?,?,?)", (uid(), device, child, at, metrica, valor, unidad))

    def evento(self, device, child, at, tipo, detalle, severidad="info"):
        with self.db.connection(True) as db:
            db.execute("""insert into audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,device_uuid,severity)
                select family_id,?,?,?,?,id,uuid,? from devices where id=?""", (at, tipo, detalle, child, severidad, device))

    def sembrar(self):
        """Luna con TEMP-01 (4 lecturas y una alerta) y Sol con TEMP-02 (1 lectura)."""
        self.luna, self.sol = self.child("Luna"), self.child("Sol")
        self.seed_device(self.luna, "TEMP-01")
        self.seed_device(self.sol, "TEMP-02")
        for at, valor in V_LUNA:
            self.lectura("TEMP-01", self.luna, at, valor)
        self.lectura("TEMP-02", self.sol, "2026-03-05T13:00:00.000000+00:00", 30.0)
        self.evento("TEMP-01", self.luna, "2026-03-05T12:30:00.000000+00:00", "ALERTA_MANUAL", "Temperatura alta", "warning")

    def descargar(self, client=None, **params):
        consulta = urlencode(params)
        return (client or self.admin).call("/api/export/xlsx" + ("?" + consulta if consulta else ""), raw=True)

    def libro(self, **params):
        estado, cuerpo, cabeceras = self.descargar(**params)
        self.assertEqual(estado, 200, cuerpo[:200])
        return load_workbook(io.BytesIO(cuerpo)), cabeceras

    def filas(self, libro, hoja):
        return list(libro[hoja].iter_rows(min_row=2, values_only=True))

    def encabezados(self, libro, hoja):
        return [c.value for c in libro[hoja][1]]


class ExportacionXlsx(ExportBase):
    def test_exportacion_vacia_es_un_libro_valido_con_encabezados(self):
        libro, cabeceras = self.libro()
        self.assertEqual(libro.sheetnames, ["Resumen", "Auditoría", "Telemetría"])
        for hoja in ("Auditoría", "Telemetría"):
            self.assertEqual(self.encabezados(libro, hoja)[:6], SRS)
            self.assertEqual(self.filas(libro, hoja), [])
        resumen = dict(self.filas(libro, "Resumen"))
        self.assertEqual((resumen["Filas en Auditoría"], resumen["Filas en Telemetría"]), (0, 0))

    def test_un_solo_registro(self):
        luna = self.child("Luna")
        self.seed_device(luna, "TEMP-01")
        self.lectura("TEMP-01", luna, "2026-03-05T12:00:00.000000+00:00", 22.5)
        libro, _ = self.libro()
        self.assertEqual(len(self.filas(libro, "Telemetría")), 1)
        self.assertEqual(len(self.filas(libro, "Auditoría")), 1)  # la asignación

    def test_muchos_registros_abarcan_varias_paginas_sin_perder_ni_repetir(self):
        luna = self.child("Luna")
        self.seed_device(luna, "TEMP-01")
        with self.db.connection(True) as db:
            db.executemany("insert into events values (?,?,?,?,?,?,?)",
                           [(uid(), "TEMP-01", luna, f"2026-03-05T12:{i // 60:02d}:{i % 60:02d}.000000+00:00", "Temperatura", i, "°C")
                            for i in range(1300)])
        libro, _ = self.libro()
        valores = [f[8] for f in self.filas(libro, "Telemetría")]
        self.assertEqual(valores, [float(i) for i in range(1300)])

    def test_filtro_por_bebe(self):
        self.sembrar()
        libro, _ = self.libro(child=self.sol)
        self.assertEqual({f[4] for f in self.filas(libro, "Telemetría")}, {self.sol})
        self.assertEqual(len(self.filas(libro, "Telemetría")), 1)
        self.assertEqual({f[4] for f in self.filas(libro, "Auditoría")}, {self.sol})

    def test_filtro_por_dispositivo_con_id_o_con_uuid(self):
        self.sembrar()
        por_id, _ = self.libro(device="TEMP-02")
        por_uuid, _ = self.libro(device=self.uuid_de("TEMP-02"))
        self.assertEqual(self.filas(por_id, "Telemetría"), self.filas(por_uuid, "Telemetría"))
        self.assertEqual({f[6] for f in self.filas(por_id, "Telemetría")}, {"TEMP-02"})

    def test_fecha_inicial_y_final_son_inclusivas(self):
        self.sembrar()
        desde, _ = self.libro(desde="2026-03-05")
        self.assertEqual(sorted(f[8] for f in self.filas(desde, "Telemetría")), [22.0, 23.0, 30.0])
        hasta, _ = self.libro(hasta="2026-03-05")
        self.assertEqual(sorted(f[8] for f in self.filas(hasta, "Telemetría")), [20.0, 21.0, 22.0, 30.0])
        dia, _ = self.libro(desde="2026-03-05", hasta="2026-03-05")
        self.assertEqual(sorted(f[8] for f in self.filas(dia, "Telemetría")), [22.0, 30.0])

    def test_medianoche_local_pertenece_al_dia_que_empieza(self):
        self.sembrar()
        antes, _ = self.libro(hasta="2026-02-28")
        self.assertEqual([f[8] for f in self.filas(antes, "Telemetría")], [20.0])
        self.assertEqual(self.filas(antes, "Telemetría")[0][1], hora(23, 59, 59))
        despues, _ = self.libro(desde="2026-03-01", hasta="2026-03-01")
        fila = self.filas(despues, "Telemetría")[0]
        self.assertEqual((fila[0].date(), fila[1], fila[8]), (date(2026, 3, 1), hora(0, 0, 0), 21.0))

    def test_todos_los_filtros_a_la_vez(self):
        self.sembrar()
        libro, _ = self.libro(child=self.luna, device="TEMP-01", desde="2026-03-05", hasta="2026-03-31")
        self.assertEqual([f[8] for f in self.filas(libro, "Telemetría")], [22.0, 23.0])
        self.assertEqual([f[3] for f in self.filas(libro, "Auditoría")], ["Temperatura alta"])

    def test_filtros_que_no_coinciden_dan_libro_vacio(self):
        self.sembrar()
        libro, _ = self.libro(child=self.sol, device="TEMP-01")
        self.assertEqual((self.filas(libro, "Telemetría"), self.filas(libro, "Auditoría")), ([], []))
        libro, _ = self.libro(desde="2030-01-01")
        self.assertEqual(self.filas(libro, "Telemetría"), [])

    def test_valores_vacios_se_ignoran(self):
        self.sembrar()
        libro, _ = self.libro(child="", device="", desde="", hasta="")
        self.assertEqual(len(self.filas(libro, "Telemetría")), 5)

    def test_fechas_invalidas_y_rango_invertido(self):
        for params in ({"desde": "ayer"}, {"hasta": "2026-13-01"}, {"desde": "2026-02-30"}, {"desde": "2026-3-1"},
                       {"desde": "2026-03-10", "hasta": "2026-03-01"}):
            estado, cuerpo, _ = self.descargar(**params)
            self.assertEqual(estado, 400, params)
            self.assertNotIn(b"Traceback", cuerpo)

    def test_identificadores_mal_formados_o_inexistentes(self):
        self.sembrar()
        self.assertEqual(self.descargar(child="no-es-uuid")[0], 400)
        self.assertEqual(self.descargar(device="mal id!")[0], 400)
        self.assertEqual(self.descargar(child=uid())[0], 404)
        self.assertEqual(self.descargar(device="NO-EXISTE")[0], 404)
        self.assertEqual(self.descargar(device=uid())[0], 404)

    def test_sin_sesion_o_sin_permiso_no_hay_archivo(self):
        from apoyo import Browser
        self.sembrar()
        estado, cuerpo, _ = self.descargar(client=Browser(self.url))
        self.assertEqual(estado, 401)
        cuidador = self.caregiver(allowed=True)
        estado, cuerpo, _ = self.descargar(client=cuidador)
        self.assertEqual(estado, 403)
        self.assertNotIn(b"PK", cuerpo[:2])

    def test_otra_familia_no_se_ve_ni_manipulando_parametros(self):
        self.sembrar()
        otro, hijo_ajeno, _ = self.second_family_admin()
        ajeno_uuid = self.uuid_de("AJENO-1")
        with self.db.connection(True) as db:
            db.execute("insert into events values (?,?,?,?,?,?,?)",
                       (uid(), "AJENO-1", hijo_ajeno, "2026-03-05T12:00:00.000000+00:00", "Temperatura", 99.0, "°C"))
        self.assertEqual(self.descargar(child=hijo_ajeno)[0], 404)
        self.assertEqual(self.descargar(device="AJENO-1")[0], 404)
        self.assertEqual(self.descargar(device=ajeno_uuid)[0], 404)
        self.assertEqual(self.descargar(child=self.luna, device="AJENO-1")[0], 404)
        libro, _ = self.libro()
        todo = [c for hoja in ("Auditoría", "Telemetría") for f in self.filas(libro, hoja) for c in f]
        self.assertNotIn(hijo_ajeno, todo)
        self.assertNotIn("AJENO-1", todo)
        self.assertNotIn(99.0, todo)
        estado, cuerpo, _ = self.descargar(client=otro)
        propio = load_workbook(io.BytesIO(cuerpo))
        self.assertEqual({f[4] for f in self.filas(propio, "Telemetría")}, {hijo_ajeno})

    def test_archivo_xlsx_valido_con_tipo_nombre_y_cabeceras_de_seguridad(self):
        self.sembrar()
        estado, cuerpo, cabeceras = self.descargar(child=self.luna, desde="2026-03-01", hasta="2026-03-31")
        self.assertEqual(estado, 200)
        self.assertEqual(cuerpo[:2], b"PK")
        self.assertTrue(cabeceras["Content-Type"].startswith(XLSX))
        self.assertEqual(int(cabeceras["Content-Length"]), len(cuerpo))
        self.assertEqual(cabeceras["Cache-Control"], "no-store")
        self.assertRegex(cabeceras["Content-Disposition"],
                         r'^attachment; filename="nido-reporte-bebe-' + self.luna[:8] + r'-2026-03-01_2026-03-31-\d{8}-\d{6}\.xlsx"$')
        self.assertEqual(cabeceras["X-Content-Type-Options"], "nosniff")
        load_workbook(io.BytesIO(cuerpo)).close()

    def test_encabezados_del_srs_en_ambas_hojas(self):
        libro, _ = self.libro()
        self.assertEqual(self.encabezados(libro, "Auditoría"), SRS + ["Device_ID", "Severidad"])
        self.assertEqual(self.encabezados(libro, "Telemetría"), SRS + ["Device_ID", "Medida", "Valor", "Unidad"])
        self.assertEqual(libro["Auditoría"].freeze_panes, "A2")
        self.assertEqual(libro["Telemetría"].auto_filter.ref, "A1:J1")

    def test_id_bebe_e_id_dispositivo_son_uuid_y_el_id_operativo_va_aparte(self):
        self.sembrar()
        libro, _ = self.libro(device="TEMP-01")
        patron = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        esperado = self.uuid_de("TEMP-01")
        for hoja in ("Auditoría", "Telemetría"):
            for f in self.filas(libro, hoja):
                self.assertRegex(f[4], patron)
                self.assertRegex(f[5], patron)
                self.assertEqual(f[5], esperado)
                self.assertEqual(f[6], "TEMP-01")
                self.assertEqual(f[4], self.luna)

    def test_fechas_y_horas_tienen_tipo_y_formato_de_excel(self):
        self.sembrar()
        libro, _ = self.libro(desde="2026-03-05", hasta="2026-03-05", device="TEMP-01")
        fila = next(libro["Telemetría"].iter_rows(min_row=2))
        self.assertIsInstance(fila[0].value, datetime)
        self.assertEqual(fila[0].number_format, "yyyy-mm-dd")
        self.assertEqual(fila[0].value.date(), date(2026, 3, 5))
        self.assertIsInstance(fila[1].value, hora)
        self.assertEqual(fila[1].number_format, "hh:mm:ss")
        self.assertEqual(fila[1].value, hora(6, 0, 0))
        self.assertIsInstance(fila[8].value, (int, float))

    def test_cada_hecho_aparece_una_sola_vez_y_en_orden_cronologico(self):
        self.sembrar()
        libro, _ = self.libro()
        tipos_auditoria = [f[2] for f in self.filas(libro, "Auditoría")]
        self.assertNotIn("LECTURA", tipos_auditoria)
        self.assertEqual(sorted(tipos_auditoria), ["ALERTA_MANUAL", "ASIGNACION", "ASIGNACION"])
        self.assertEqual({f[2] for f in self.filas(libro, "Telemetría")}, {"LECTURA"})
        for hoja in ("Auditoría", "Telemetría"):
            marcas = [(f[0], f[1]) for f in self.filas(libro, hoja)]
            self.assertEqual(marcas, sorted(marcas))
        resumen = dict(self.filas(libro, "Resumen"))
        self.assertEqual((resumen["Filas en Auditoría"], resumen["Filas en Telemetría"]), (3, 5))
        with self.db.connection() as db:
            total = db.execute("select count(*) from audit_log").fetchone()[0]
        self.assertEqual(total, 3 + 5)

    def test_telemetria_lleva_medida_valor_y_unidad(self):
        self.sembrar()
        libro, _ = self.libro(device="TEMP-02")
        fila = self.filas(libro, "Telemetría")[0]
        self.assertEqual((fila[7], fila[8], fila[9]), ("Temperatura", 30.0, "°C"))
        self.assertEqual(fila[3], "Temperatura: 30.0 °C")

    def test_acentos_y_caracteres_especiales(self):
        luna = self.child("Luna")
        self.seed_device(luna, "TEMP-01")
        texto = "Llanto fuerte ¿qué pasó? ñandú · 日本 😀 <b>&amp;</b>"
        self.evento("TEMP-01", luna, "2026-03-05T12:00:00.000000+00:00", "ALERTA_MANUAL", texto)
        self.evento("TEMP-01", luna, "2026-03-05T12:01:00.000000+00:00", "ALERTA_MANUAL", "con\x07control\x00ilegal")
        libro, _ = self.libro()
        detalles = [f[3] for f in self.filas(libro, "Auditoría") if f[2] == "ALERTA_MANUAL"]
        self.assertEqual(detalles, [texto, "concontrolilegal"])

    def test_formulas_maliciosas_se_guardan_como_texto(self):
        luna = self.child("Luna")
        self.seed_device(luna, "TEMP-01")
        ataques = ['=HYPERLINK("http://x.test","clic")', "+SUM(1+1)", "-2+3", "@SUM(A1)", "\t=1+1", "  =cmd|' /C calc'!A0",
                   "\r=1+1"]
        for i, ataque in enumerate(ataques):
            self.evento("TEMP-01", luna, f"2026-03-05T12:0{i}:00.000000+00:00", "ALERTA_MANUAL", ataque)
        self.lectura("TEMP-01", luna, "2026-03-05T13:00:00.000000+00:00", 1.0, metrica="=1+1", unidad="@x")
        libro, _ = self.libro()
        for hoja in libro.worksheets:
            for fila in hoja.iter_rows():
                for c in fila:
                    self.assertNotEqual(c.data_type, "f", f"{hoja.title}!{c.coordinate} es una fórmula")
        guardados = [f[3] for f in self.filas(libro, "Auditoría") if f[2] == "ALERTA_MANUAL"]
        self.assertEqual(len(guardados), len(ataques))
        for original, guardado in zip(ataques, guardados):
            self.assertEqual(guardado, "'" + original)
        medida = self.filas(libro, "Telemetría")[0]
        self.assertEqual((medida[7], medida[9]), ("'=1+1", "'@x"))

    def test_resumen_documenta_filtros_sin_datos_personales_del_bebe(self):
        self.sembrar()
        libro, _ = self.libro(child=self.luna, desde="2026-03-01", hasta="2026-03-31")
        resumen = dict(self.filas(libro, "Resumen"))
        self.assertEqual(resumen["Bebé (ID_Bebé)"], self.luna)
        self.assertEqual((resumen["Fecha desde"], resumen["Fecha hasta"]), ("2026-03-01", "2026-03-31"))
        self.assertIn("Ambos extremos incluidos", resumen["Criterio de fechas"])
        texto = " ".join(str(v) for v in resumen.values())
        self.assertNotIn("Luna", texto)
        self.assertNotIn("Sol", texto.replace("Solo", ""))

    def test_no_quedan_archivos_temporales(self):
        self.sembrar()
        with tempfile.TemporaryDirectory() as carpeta:
            anterior, tempfile.tempdir = tempfile.tempdir, carpeta
            try:
                self.assertEqual(self.descargar()[0], 200)
                self.assertEqual(self.descargar(desde="mal")[0], 400)
                self.assertEqual(self.descargar(device="NO-EXISTE")[0], 404)
                self.assertEqual(os.listdir(carpeta), [])
            finally:
                tempfile.tempdir = anterior

    def test_un_error_a_mitad_de_la_exportacion_limpia_y_se_propaga(self):
        self.sembrar()

        class Roto:
            def __init__(self, store):
                self.store, self.llamadas = store, 0

            def audit_corte(self, *a):
                return self.store.audit_corte(*a)

            def audit_page(self, *a):
                self.llamadas += 1
                if self.llamadas == 2:
                    raise Problem("fallo simulado", 500)
                return self.store.audit_page(*a)

        with tempfile.TemporaryDirectory() as carpeta:
            with self.assertRaises(Problem):
                exportar.generar(Roto(self.db), self.sesion(), {}, carpeta=carpeta)
            self.assertEqual(os.listdir(carpeta), [])

    def test_eventos_que_llegan_durante_la_exportacion_no_la_alteran(self):
        luna = self.child("Luna")
        self.seed_device(luna, "TEMP-01")
        with self.db.connection(True) as db:
            db.executemany("insert into events values (?,?,?,?,?,?,?)",
                           [(uid(), "TEMP-01", luna, f"2026-03-05T12:{i // 60:02d}:{i % 60:02d}.000000+00:00", "Temperatura", i, "°C")
                            for i in range(1100)])
        test = self

        class Intruso:
            def __init__(self, store):
                self.store, self.hecho = store, False

            def audit_corte(self, *a):
                return self.store.audit_corte(*a)

            def audit_page(self, *a):
                pagina = self.store.audit_page(*a)
                if not self.hecho and a[1].get("hoja") == "telemetria":
                    self.hecho = True
                    test.lectura("TEMP-01", luna, "2026-03-05T12:00:00.500000+00:00", 5000.0)  # entre filas ya leídas
                    test.lectura("TEMP-01", luna, "2026-03-05T18:00:00.000000+00:00", 6000.0)  # después de lo leído
                    test.lectura("TEMP-01", luna, "2026-03-05T11:00:00.000000+00:00", 7000.0)  # antes de lo leído
                return pagina

        with tempfile.TemporaryDirectory() as carpeta:
            resultado = exportar.generar(Intruso(self.db), self.sesion(), {}, carpeta=carpeta)
            try:
                libro = load_workbook(resultado.ruta)
                valores = [f[8] for f in libro["Telemetría"].iter_rows(min_row=2, values_only=True)]
            finally:
                resultado.eliminar()
        self.assertEqual(valores, [float(i) for i in range(1100)])
        despues, _ = self.libro()
        self.assertEqual(len(self.filas(despues, "Telemetría")), 1103)

    def test_volumen_grande_no_consume_memoria_proporcional(self):
        luna = self.child("Luna")
        self.seed_device(luna, "TEMP-01")

        def sembrar(desde, n):
            with self.db.connection(True) as db:
                db.executemany("insert into events values (?,?,?,?,?,?,?)",
                               [(uid(), "TEMP-01", luna, f"2026-03-05T00:00:00.{i:06d}+00:00".replace("T00:00:00", f"T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}"),
                                 "Temperatura", i, "°C") for i in range(desde, desde + n)])

        picos = {}
        sembrar(0, 2000)
        tamanos = {}
        for etiqueta, total in (("pequeno", 2000), ("grande", 24000)):
            if etiqueta == "grande":
                sembrar(2000, 22000)
            with tempfile.TemporaryDirectory() as carpeta:
                tracemalloc.start()
                resultado = exportar.generar(self.db, self.sesion(), {}, carpeta=carpeta)
                picos[etiqueta] = tracemalloc.get_traced_memory()[1]
                tracemalloc.stop()
                tamanos[etiqueta] = os.path.getsize(resultado.ruta)
                self.assertEqual(resultado.telemetria, total)
                resultado.eliminar()
        self.assertGreater(tamanos["grande"], 5 * tamanos["pequeno"])
        self.assertLess(picos["grande"], 2.5 * picos["pequeno"], picos)
        self.assertLess(picos["grande"], 60 * 1024 * 1024, picos)

    def test_la_api_anterior_sigue_funcionando(self):
        self.sembrar()
        estado, cuerpo = self.admin.call("/api/report?child=" + self.luna)
        self.assertEqual(estado, 200)
        self.assertTrue(cuerpo.startswith("id,child_id,device_id,captured_at,metric,value,unit"))
        self.assertEqual(len(cuerpo.strip().splitlines()), 5)
        estado, datos = self.admin.call("/api/audit?limit=2")
        self.assertEqual((estado, len(datos["items"])), (200, 2))
        self.assertTrue(datos["next"])
        self.assertIn("device_uuid", datos["items"][0])

    def test_la_api_publica_no_acepta_los_parametros_internos(self):
        self.sembrar()
        _, datos = self.admin.call("/api/audit?hoja=telemetria&corte=1&limit=500")
        self.assertEqual(len(datos["items"]), 8)


class NubeFalsa(Cloud):
    """Imita lo necesario de PostgREST sobre los datos de una base SQLite: filtros, orden, cursor y el
    tope de filas max-rows. No es Supabase: solo prueba la forma de las consultas de Cloud."""

    def __init__(self, ruta, tope):
        super().__init__("https://ejemplo.supabase.co", "pub", "sec")
        self.ruta, self.tope, self.consultas = ruta, tope, []

    def access(self, ctx):
        return "token"

    def datos(self):
        with closing(sqlite3.connect(self.ruta)) as db:
            db.row_factory = sqlite3.Row
            filas = [dict(r) for r in db.execute("""select a.rowid as seq,a.id,a.family_id,a.occurred_at,a.event_date,a.event_time,
                a.event_type,a.detail,a.child_id,a.device_id,a.device_uuid,a.severity,
                case when a.event_type='LECTURA' then 'telemetria' else 'auditoria' end as hoja,e.metric,e.value,e.unit
                from audit_log a left join events e on e.id=a.source_event_id""")]
            hijos = [dict(r) for r in db.execute("select id from children")]
            dispositivos = [dict(r) for r in db.execute("select id,uuid from devices")]
            miembro = db.execute("select id,family_id,role from members where role='admin' limit 1").fetchone()
        return filas, hijos, dispositivos, dict(miembro)

    def request(self, path, data=None, token=None, admin=False):
        url = urlsplit(path)
        tabla, q = url.path.rsplit("/", 1)[-1], {k: v[0] for k, v in parse_qs(url.query).items()}
        self.consultas.append((tabla, q))
        filas, hijos, dispositivos, miembro = self.datos()
        if tabla == "members":
            return [miembro]
        if tabla == "children":
            return [h for h in hijos if h["id"] == q["id"][3:]]
        if tabla == "devices":
            return [d for d in dispositivos if ("id" in q and d["id"] == q["id"][3:]) or ("uuid" in q and d["uuid"] == q["uuid"][3:])]
        assert tabla in ("audit_log", "audit_export"), tabla
        for columna in ("child_id", "device_id", "event_type", "hoja"):
            if columna in q:
                assert q[columna].startswith("eq.")
                filas = [f for f in filas if f[columna] == q[columna][3:]]
        if "and" in q:
            for campo, op, valor in re.findall(r"(\w+)\.(gte|lte)\.([^,)]+)", q["and"]):
                if campo == "event_date":
                    filas = [f for f in filas if (f[campo] >= valor if op == "gte" else f[campo] <= valor)]
                else:
                    assert (campo, op) == ("seq", "lte")
                    filas = [f for f in filas if f["seq"] <= int(valor)]
        if "or" in q:
            at, seq = re.search(r'occurred_at\.gt\."([^"]+)".*seq\.gt\.(\d+)', q["or"]).groups()
            filas = [f for f in filas if (f["occurred_at"], f["seq"]) > (at, int(seq))]
        filas.sort(key=lambda f: (f["occurred_at"], f["seq"]) if q.get("order") != "seq.desc" else -f["seq"])
        return filas[:min(int(q["limit"]), self.tope)]


class SqliteContraSupabase(ExportBase):
    def test_nube_y_sqlite_exportan_lo_mismo_aunque_postgrest_recorte_las_paginas(self):
        luna = self.child("Luna")
        self.seed_device(luna, "TEMP-01")
        with self.db.connection(True) as db:
            db.executemany("insert into events values (?,?,?,?,?,?,?)",
                           [(uid(), "TEMP-01", luna, f"2026-03-05T12:{i // 60:02d}:{i % 60:02d}.000000+00:00", "Temperatura", i, "°C")
                            for i in range(730)])
        self.evento("TEMP-01", luna, "2026-03-05T12:30:30.000000+00:00", "ALERTA_MANUAL", "Alerta")
        nube = NubeFalsa(self.path, tope=100)
        sesion = self.sesion()
        with tempfile.TemporaryDirectory() as carpeta:
            ahora = datetime(2026, 11, 1, 10, 0, 0, tzinfo=exportar.COSTA_RICA)
            local = exportar.generar(self.db, sesion, {"child": luna}, carpeta=carpeta, ahora=ahora)
            remoto = exportar.generar(nube, sesion, {"child": luna}, carpeta=carpeta, ahora=ahora)
            try:
                a, b = load_workbook(local.ruta), load_workbook(remoto.ruta)
                for hoja in ("Resumen", "Auditoría", "Telemetría"):
                    self.assertEqual(list(a[hoja].values), list(b[hoja].values), hoja)
                self.assertEqual(remoto.telemetria, 730)
                self.assertEqual(local.nombre, remoto.nombre)
            finally:
                local.eliminar()
                remoto.eliminar()
        paginas = [q for t, q in nube.consultas if t == "audit_export" and q["hoja"] == "eq.telemetria"]
        self.assertGreater(len(paginas), 7)  # 730 filas con tope de 100 por respuesta
        self.assertTrue(all(q["and"].endswith(")") and "seq.lte." in q["and"] for q in paginas))

    def test_nube_resuelve_dispositivo_por_uuid_y_rechaza_sin_ser_administrador(self):
        luna = self.child("Luna")
        self.seed_device(luna, "TEMP-01")
        nube = NubeFalsa(self.path, tope=1000)
        valor = self.uuid_de("TEMP-01")
        pagina = nube.audit_page(self.sesion(), {"device": valor}, None, 50)
        self.assertEqual({f["device_id"] for f in pagina["items"]}, {"TEMP-01"})
        self.assertIsNone(pagina["next"])
        with self.assertRaises(Problem) as ctx:
            nube.audit_page(self.sesion(), {"device": uid()}, None, 50)
        self.assertEqual(ctx.exception.status, 404)

    def uuid_de(self, device):
        with self.db.connection() as db:
            return db.execute("select uuid from devices where id=?", (device,)).fetchone()[0]

    def sesion(self):
        with self.db.connection() as db:
            return {"id": db.execute("select id from members where email=?", (ADMIN[0],)).fetchone()[0]}


if __name__ == "__main__":
    unittest.main()
