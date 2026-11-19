"""SCRUM-20 · RF-DAT-01. Registro unificado de eventos."""
import concurrent.futures
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from apoyo import Base, Browser
from cuna.common import device_secret, fingerprint, instant, uid
from cuna.local import Local


class Auditoria(Base):
    def test_ca1_lectura_valida_guarda_todos_los_campos(self):
        child = self.child("Luna")
        self.device(child)
        reading = self.reading(value=22.4)
        self.assertEqual(self.send(reading), (200, {"child_id": child}))
        row = self.audit_rows(source_event_id=reading["event_id"])[0]
        self.assertEqual((row["event_type"], row["detail"], row["child_id"], row["device_id"], row["severity"]),
                         ("LECTURA", "Temperatura: 22.4 °C", child, "TEST-1", "info"))
        self.assertRegex(row["event_date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(row["event_time"], r"^\d{2}:\d{2}:\d{2}$")
        code, page = self.audit("device=TEST-1&tipo=LECTURA")
        self.assertEqual(code, 200)
        item = page["items"][0]
        for key in ("event_date", "event_time", "event_type", "detail", "child_id", "device_id"):
            self.assertIn(key, item)

    def test_fecha_y_hora_en_horario_de_costa_rica(self):
        child = self.child("Luna")
        self.seed_device(child, "OLD-1")
        antes = self.reading("OLD-1", "2026-11-13T05:59:59+00:00")
        despues = self.reading("OLD-1", "2026-11-13T06:00:00+00:00")
        self.assertEqual(self.send(antes)[0], 200)
        self.assertEqual(self.send(despues)[0], 200)
        a = self.audit_rows(source_event_id=antes["event_id"])[0]
        b = self.audit_rows(source_event_id=despues["event_id"])[0]
        self.assertEqual((a["event_date"], a["event_time"]), ("2026-11-12", "23:59:59"))
        self.assertEqual((b["event_date"], b["event_time"]), ("2026-11-13", "00:00:00"))
        solo_b = self.audit("device=OLD-1&tipo=LECTURA&desde=2026-11-13&hasta=2026-11-13")[1]["items"]
        self.assertEqual([i["event_time"] for i in solo_b], ["00:00:00"])
        solo_a = self.audit("device=OLD-1&tipo=LECTURA&desde=2026-11-12&hasta=2026-11-12")[1]["items"]
        self.assertEqual([i["event_time"] for i in solo_a], ["23:59:59"])

    def test_ca2_el_bebe_sale_de_la_asignacion_y_no_del_cuerpo(self):
        a, b = self.child("Luna"), self.child("Sol")
        first = self.device(a)
        enviado = self.audit_event(child_id=b)  # un dispositivo no puede elegir bebé
        status, result = self.send_audit(enviado)
        self.assertEqual((status, result["child_id"]), (200, a))
        self.assertEqual(self.audit_rows(source_event_id=enviado["event_id"])[0]["child_id"], a)
        tardio = self.reading()
        self.change("assign", device_id="TEST-1", expected=first, child_id=b)
        self.assertEqual(self.send(tardio), (200, {"child_id": a}))
        nuevo = self.audit_event(detail="Después del traslado")
        self.assertEqual(self.send_audit(nuevo)[1]["child_id"], b)
        tipos_a = sorted(r["event_type"] for r in self.audit_rows(child_id=a))
        tipos_b = sorted(r["event_type"] for r in self.audit_rows(child_id=b))
        self.assertEqual(tipos_a, ["ALERTA_MANUAL", "ALERTA_TEMPERATURA_ALTA", "ASIGNACION", "DESASIGNACION", "LECTURA"])
        self.assertEqual(tipos_b, ["ALERTA_MANUAL", "ASIGNACION"])

    def test_cambios_de_estado_dejan_rastro(self):
        child = self.child("Luna")
        assignment = self.device(child)
        self.change("operate", device_id="TEST-1", expected=assignment, enabled=True)
        self.change("operate", device_id="TEST-1", expected=assignment, enabled=False)
        tipos = [r["event_type"] for r in self.audit_rows(child_id=child)]
        self.assertEqual(sorted(tipos), ["ACTIVACION", "ASIGNACION", "DESACTIVACION"])

    def test_campos_obligatorios_y_valores_invalidos(self):
        self.device(self.child("Luna"))
        base = self.audit_event()
        for roto in (
            {k: v for k, v in base.items() if k != "event_id"},
            {k: v for k, v in base.items() if k != "detail"},
            {k: v for k, v in base.items() if k != "event_type"},
            {k: v for k, v in base.items() if k != "occurred_at"},
            dict(base, event_id="no-es-uuid"),
            dict(base, event_type="minúscula"),
            dict(base, event_type="A" * 51),
            dict(base, detail="   "),
            dict(base, detail="x" * 1001),
            dict(base, severity="grave"),
            dict(base, occurred_at="2026-11-13T10:00:00"),
            dict(base, occurred_at="2999-01-01T00:00:00+00:00"),
        ):
            status, _ = self.send_audit(roto)
            self.assertEqual(status, 400, roto)
        for reservado in ("LECTURA", "ASIGNACION", "ACTIVACION"):
            self.assertEqual(self.send_audit(dict(base, event_type=reservado))[0], 403)
        self.assertEqual(self.audit_rows(event_type="ALERTA_MANUAL"), [])

    def test_claves_foraneas_y_restricciones_en_la_base(self):
        child = self.child("Luna")
        self.device(child)
        familia = self.family_of_admin()
        with self.db.connection() as db:
            valor = db.execute("select uuid from devices where id='TEST-1'").fetchone()[0]
        columnas = "id,family_id,occurred_at,event_type,detail,child_id,device_id,device_uuid"
        insertar = f"insert into audit_log({columnas}) values (?,?,?,?,?,?,?,?)"
        with self.db.connection(True) as db:  # control positivo: la fila válida entra
            db.execute(insertar, (uid(), familia, instant(), "X", "d", child, "TEST-1", valor))
        casos = {
            "bebé inexistente": (uid(), familia, instant(), "X", "d", uid(), "TEST-1", valor),
            "dispositivo inexistente": (uid(), familia, instant(), "X", "d", child, "NO-EXISTE", valor),
            "tipo de más de 50 caracteres": (uid(), familia, instant(), "T" * 51, "d", child, "TEST-1", valor),
            "fecha inválida": (uid(), familia, "no es fecha", "X", "d", child, "TEST-1", valor),
        }
        for causa, fila in casos.items():
            with self.assertRaises(sqlite3.IntegrityError, msg=causa), self.db.connection(True) as db:
                db.execute(insertar, fila)
        for sentencia in ("update audit_log set detail='alterado'", "delete from audit_log"):
            with self.assertRaises(sqlite3.IntegrityError, msg=sentencia), self.db.connection(True) as db:
                db.execute(sentencia)

    def test_identificadores_inexistentes_dan_errores_controlados(self):
        self.device(self.child("Luna"))
        evento = self.audit_event(device="NO-EXISTE")
        self.assertEqual(self.send_audit(evento, key="cualquiera")[0], 403)
        self.assertEqual(self.send_audit(self.audit_event(), key="clave-equivocada")[0], 403)
        self.assertEqual(self.audit("child=" + uid())[0], 404)
        self.assertEqual(self.audit("device=NO-EXISTE")[0], 404)
        for consulta in ("child=no-uuid", "desde=13-11-2026", "desde=2026-02-31", "desde=2026-12-01&hasta=2026-11-01",
                         "limit=0", "limit=501", "limit=abc", "after=basura", "tipo=minuscula", "device=con%20espacio"):
            self.assertEqual(self.audit(consulta)[0], 400, consulta)

    def test_dispositivo_sin_asignar_no_pierde_ni_inventa_bebe(self):
        child = self.child("Luna")
        assignment = self.device(child)
        self.change("assign", device_id="TEST-1", expected=assignment, child_id=None)
        status, result = self.send_audit(self.audit_event())
        self.assertEqual(status, 409, result)
        self.assertEqual(self.audit_rows(event_type="ALERTA_MANUAL"), [])

    def test_reintento_idempotente_y_conflicto(self):
        self.device(self.child("Luna"))
        evento = self.audit_event()
        primero = self.send_audit(evento)
        self.assertEqual(self.send_audit(evento), primero)
        self.assertEqual(len(self.audit_rows(event_type="ALERTA_MANUAL")), 1)
        self.assertEqual(self.send_audit(dict(evento, detail="otro contenido"))[0], 409)

    def test_un_error_de_base_no_se_pierde_en_silencio(self):
        child = self.child("Luna")
        self.device(child)
        with self.db.connection(True) as db:
            db.execute("create trigger falla before insert on audit_log begin select raise(abort,'disco lleno'); end")
        lectura = self.reading()
        self.assertEqual(self.send(lectura)[0], 500)
        with self.db.connection() as db:
            self.assertEqual(db.execute("select count(*) from events").fetchone()[0], 0)
        evento = self.audit_event()
        self.assertEqual(self.send_audit(evento)[0], 500)
        with self.db.connection(True) as db:
            db.execute("drop trigger falla")
        self.assertEqual(self.send(lectura)[0], 200)
        self.assertEqual(self.send_audit(evento)[0], 200)
        self.assertEqual(len(self.audit_rows(source_event_id=lectura["event_id"])), 1)

    def test_autorizacion_y_aislamiento(self):
        child = self.child("Luna")
        self.device(child)
        self.send(self.reading())
        ajeno, ajeno_child, ajeno_key = self.second_family_admin()
        self.send(self.reading("AJENO-1"), key=ajeno_key)
        self.assertEqual(Browser(self.url).call("/api/audit")[0], 401)
        self.assertEqual(self.audit(client=self.caregiver(allowed=True))[0], 403)
        propios = {i["child_id"] for i in self.audit()[1]["items"]}
        self.assertEqual(propios, {child})
        self.assertEqual(self.audit("child=" + ajeno_child)[0], 404)
        self.assertEqual(self.audit("device=AJENO-1")[0], 404)
        self.assertEqual({i["child_id"] for i in self.audit(client=ajeno)[1]["items"]}, {ajeno_child})
        self.assertEqual(self.send_audit(self.audit_event("AJENO-1"), key=self.keys["TEST-1"])[0], 403)

    def test_paginacion_con_cursor_y_orden_cronologico(self):
        child = self.child("Luna")
        self.seed_device(child, "OLD-1")
        for hora in ("12:00:03", "12:00:01", "12:00:02", "12:00:01", "12:00:05"):
            self.assertEqual(self.send(self.reading("OLD-1", f"2026-11-13T{hora}+00:00"))[0], 200)
        vistos, cursor = [], ""
        for _ in range(10):
            page = self.audit("tipo=LECTURA&limit=2" + ("&after=" + quote(cursor) if cursor else ""))[1]
            vistos += [i["event_time"] for i in page["items"]]
            cursor = page["next"]
            if not cursor:
                break
        self.assertEqual(vistos, ["06:00:01", "06:00:01", "06:00:02", "06:00:03", "06:00:05"])

    def test_concurrencia_no_mezcla_bebes(self):
        a, b = self.child("Luna"), self.child("Sol")
        first = self.device(a)
        eventos = [self.audit_event(detail=f"n{i}") for i in range(24)]
        # Todos se capturaron antes del traslado, así que todos pertenecen a Luna.
        movimiento = lambda: self.admin.call("/api/change/assign", dict(device_id="TEST-1", expected=first, child_id=b))[0]
        envio = lambda e: self.send_audit(e)[0]
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            traslado = pool.submit(movimiento)
            codigos = list(pool.map(envio, eventos))
            self.assertEqual(traslado.result(), 200)
        self.assertEqual(codigos, [200] * 24)
        filas = self.audit_rows(event_type="ALERTA_MANUAL")
        self.assertEqual(len(filas), 24)
        self.assertEqual({f["child_id"] for f in filas}, {a})
        self.assertEqual(self.audit_rows(child_id=b, event_type="ALERTA_MANUAL"), [])

    def test_migracion_rellena_el_historial_una_sola_vez(self):
        with tempfile.TemporaryDirectory() as folder:
            ruta = Path(folder) / "antigua.db"
            familia, child, key = uid(), uid(), device_secret()
            lectura, asignacion = uid(), uid()
            db = sqlite3.connect(ruta)
            db.executescript((Path(__file__).parent / "fixtures" / "esquema_scrum21.sql").read_text(encoding="utf-8"))
            db.execute("insert into families(id) values (?)", (familia,))
            db.execute("insert into children values (?,?,?)", (child, familia, "Luna"))
            db.execute("insert into devices values ('OLD-1',?,?,?)", (familia, "Sensor", fingerprint(key)))
            db.execute("insert into assignments values (?,?,?,?,?,0)",
                       (asignacion, "OLD-1", child, "2026-01-01T00:00:00.000000+00:00", "2026-06-01T00:00:00.000000+00:00"))
            db.execute("insert into events values (?,?,?,?,?,?,?)",
                       (lectura, "OLD-1", child, "2026-03-01T12:00:00.000000+00:00", "Temperatura", 21.5, "°C"))
            db.commit()
            db.close()
            for _ in range(2):  # abrir dos veces no duplica
                Local(ruta)
            db = sqlite3.connect(ruta)
            tipos = sorted(r[0] for r in db.execute("select event_type from audit_log"))
            db.close()
            self.assertEqual(tipos, ["ASIGNACION", "DESASIGNACION", "LECTURA"])


if __name__ == "__main__":
    unittest.main()
