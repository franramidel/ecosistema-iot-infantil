"""Sprint 2 (SCRUM-54, 35, 51, 57, 56) con los dos almacenes: SQLite (Local) y Supabase (Cloud).

Mismo contrato contra ambos. La parte de Supabase corre el SQL real de supabase/001 a 009 en un PostgreSQL local
desechable, detrás del puente de pruebas (NO es Supabase real). Se omite si falta PGTEST_URL; ver test_telegram_nube.py.
"""
import unittest
from datetime import datetime, timedelta, timezone

import test_telegram_nube as tn
from cuna import panel
from cuna.common import Problem, uid

SQL = ["supabase/008_alertas_temperatura.sql", "supabase/009_panel_sprint2.sql"]


def setUpModule():
    for archivo in SQL:
        if archivo not in tn.MIGRACIONES:
            tn.MIGRACIONES = tn.MIGRACIONES + [archivo]
    tn.setUpModule()


def tearDownModule():
    tn.tearDownModule()


def ahora(segundos=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=segundos)).isoformat(timespec="microseconds")


class Contrato:
    Harness = None

    def setUp(self):
        self.h = self.Harness()
        self.addCleanup(self.h.cerrar)
        self.s = self.h.store
        self.a, self.c, self.b = (self.h.sesiones[u] for u in (tn.ADMIN_A, tn.CUIDA_A, tn.ADMIN_B))
        self.nino = self.s.change(self.a, "child", {"name": "Bebé"})["id"]
        self.clave = self.s.change(self.a, "device", {"device_id": "CUNA-1", "name": "S"})["secret"]
        self.s.change(self.a, "assign", {"device_id": "CUNA-1", "expected": None, "child_id": self.nino})
        # El dispositivo estuvo asignado desde hace dos horas: así se pueden registrar lecturas del pasado.
        self.h.sql("update assignments set started_at=? where child_id=?", ahora(-7200), self.nino)

    def lectura(self, metrica, valor, unidad, segundos=-60):
        self.s.ingest("CUNA-1", self.clave, (uid(), ahora(segundos), metrica, valor, unidad))

    def falla(self, estado, funcion, *args):
        with self.assertRaises(Problem) as ctx:
            funcion(*args)
        self.assertEqual(ctx.exception.status, estado)

    # SCRUM-54
    def test_01_historial_filtra_pagina_y_aisla_por_bebe(self):
        otro = self.s.change(self.a, "child", {"name": "Otro"})["id"]
        for i in range(5):
            self.lectura("Temperatura", 20.0 + i / 10, "°C", -50 + i)
        self.lectura("Humedad", 55.0, "%", -20)
        todo = self.s.history(self.a, self.nino, {}, None, 100)
        self.assertEqual(len(todo["items"]), 6)
        self.assertIsNone(todo["next"])
        solo = self.s.history(self.a, self.nino, {"metric": "Temperatura"}, None, 100)
        self.assertEqual({e["metric"] for e in solo["items"]}, {"Temperatura"})
        p1 = self.s.history(self.a, self.nino, {"metric": "Temperatura"}, None, 2)
        self.assertEqual(len(p1["items"]), 2)
        self.assertTrue(p1["next"])
        vistos = [e["id"] for e in p1["items"]]
        cursor, pagina = p1["next"], None
        while cursor:
            pagina = self.s.history(self.a, self.nino, {"metric": "Temperatura"}, panel.decodificar_cursor(cursor), 2)
            vistos += [e["id"] for e in pagina["items"]]
            cursor = pagina["next"]
        self.assertEqual(sorted(vistos), sorted(e["id"] for e in solo["items"]))
        self.assertEqual(len(set(vistos)), 5)
        self.assertEqual(self.s.history(self.a, otro, {}, None, 100)["items"], [])
        self.assertEqual(len(self.s.history(self.c, self.nino, {}, None, 100)["items"]), 6)

    def test_02_historial_por_rango_de_fechas_y_otra_familia(self):
        self.lectura("Temperatura", 21.0, "°C", -10)
        hoy = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-6))).date().isoformat()
        self.assertEqual(len(self.s.history(self.a, self.nino, {"desde": hoy, "hasta": hoy}, None, 100)["items"]), 1)
        self.assertEqual(self.s.history(self.a, self.nino, {"desde": "2000-01-01", "hasta": "2000-01-02"}, None, 100)["items"], [])
        self.falla(404, self.s.history, self.b, self.nino, {}, None, 10)

    # SCRUM-35
    def test_03_snapshot_sin_sesion_sin_masa_y_con_masa(self):
        self.assertFalse(self.s.sleep_state(self.a, self.nino)["active"])
        self.falla(409, self.s.sleep_action, self.a, self.nino, "snapshot")
        self.falla(409, self.s.sleep_action, self.a, self.nino, "disarm")
        self.s.sleep_action(self.a, self.nino, "start")
        self.falla(409, self.s.sleep_action, self.a, self.nino, "start")
        self.falla(409, self.s.sleep_action, self.a, self.nino, "snapshot")  # sesión sin lectura de masa
        self.assertIsNone(self.s.sleep_state(self.a, self.nino)["session"]["baseline_g"])
        self.lectura("Masa", 4200.0, "g")
        self.falla(403, self.s.sleep_action, self.c, self.nino, "snapshot")  # el cuidador sin permiso de operar solo lee
        e = self.s.sleep_action(self.a, self.nino, "snapshot")
        self.assertEqual(float(e["session"]["baseline_g"]), 4200.0)
        self.lectura("Masa", 4116.0, "g", -1)
        self.assertEqual(float(self.s.sleep_state(self.a, self.nino)["current_pct"]), 98.0)

    def test_04_masa_vieja_o_en_otra_unidad_no_sirve_y_desarmar_cierra(self):
        self.s.sleep_action(self.a, self.nino, "start")
        self.lectura("Masa", 4200.0, "g", -3600)
        self.falla(409, self.s.sleep_action, self.a, self.nino, "snapshot")
        self.lectura("Masa", 9.0, "kg", -2)
        self.falla(409, self.s.sleep_action, self.a, self.nino, "snapshot")
        self.assertFalse(self.s.sleep_action(self.a, self.nino, "disarm")["active"])
        self.s.sleep_action(self.a, self.nino, "start")  # tras desarmar se puede iniciar otra

    def test_05_sueno_aislado_entre_familias(self):
        self.falla(404, self.s.sleep_state, self.b, self.nino)
        self.falla(404, self.s.sleep_action, self.b, self.nino, "start")
        self.falla(404, self.s.sleep_action, self.a, self.nino, "otra")

    # SCRUM-51
    def test_06_toma_queda_en_auditoria_y_es_idempotente(self):
        ev = uid()
        self.falla(403, self.s.feeding_log, self.c, self.nino, ev, 750, 120.0, "ml")
        r = self.s.feeding_log(self.a, self.nino, ev, 750, 120.0, "ml")
        self.assertFalse(r["duplicado"])
        self.assertTrue(self.s.feeding_log(self.a, self.nino, ev, 750, 120.0, "ml")["duplicado"])
        filas = self.h.sql("select count(*) from audit_log where event_type='TOMA'")
        self.assertEqual(int(filas[0][0]), 1)
        detalle = self.h.sql("select detail from audit_log where event_type='TOMA'")[0][0]
        self.assertIn("12 min 30 s", detalle)
        self.assertIn("120.0 ml", detalle)

    def test_07_toma_distinto_contenido_con_mismo_id_otra_familia_y_sin_dispositivo(self):
        ev = uid()
        self.s.feeding_log(self.a, self.nino, ev, 60, 50.0, "g")
        self.falla(404, self.s.feeding_log, self.b, self.nino, uid(), 60, 50.0, "g")
        sin = self.s.change(self.a, "child", {"name": "Sin cuna"})["id"]
        self.falla(409, self.s.feeding_log, self.a, sin, uid(), 60, 50.0, "g")

    # SCRUM-57
    def test_08_ajustes_por_defecto_guardado_y_permisos(self):
        d = self.s.settings(self.c, self.nino)
        self.assertEqual((float(d["led_speed_s"]), d["audio"], int(d["volume"]), d["default"]), (5.0, "ruido_blanco", 30, True))
        g = self.s.settings_save(self.a, self.nino, 2.5, "lluvia", 60)
        self.assertEqual((float(g["led_speed_s"]), g["audio"], int(g["volume"])), (2.5, "lluvia", 60))
        r = self.s.settings(self.c, self.nino)
        self.assertEqual((float(r["led_speed_s"]), r["audio"], int(r["volume"]), r["default"]), (2.5, "lluvia", 60, False))
        self.falla(403, self.s.settings_save, self.c, self.nino, 1, "olas", 10)
        self.falla(404, self.s.settings_save, self.b, self.nino, 1, "olas", 10)
        self.falla(404, self.s.settings, self.b, self.nino)

    def test_09_ajustes_invalidos_no_cambian_nada(self):
        self.s.settings_save(self.a, self.nino, 2.5, "lluvia", 60)
        for args in ((0, "lluvia", 60), (999, "lluvia", 60), (2, "rock", 60), (2, "lluvia", 101), (2, "lluvia", -1)):
            with self.assertRaises(Problem, msg=args):
                self.s.settings_save(self.a, self.nino, *args)
        self.assertEqual(self.s.settings(self.a, self.nino)["audio"], "lluvia")

    # SCRUM-56
    def test_10_modulos_por_defecto_cambio_y_persistencia(self):
        m = self.s.modules(self.c, self.nino)["modules"]
        self.assertEqual([x["module"] for x in m], ["temperatura", "luz", "audio", "colchon"])
        self.assertTrue(all(x["default"] and x["mode"] == "manual" for x in m))
        self.falla(403, self.s.modules_save, self.c, self.nino, "luz", True, "programado")
        self.s.modules_save(self.a, self.nino, "luz", True, "programado")
        self.s.modules_save(self.a, self.nino, "audio", False, "manual")
        despues = {x["module"]: x for x in self.s.modules(self.a, self.nino)["modules"]}
        self.assertTrue(despues["luz"]["enabled"])
        self.assertEqual(despues["luz"]["mode"], "programado")
        self.assertFalse(despues["luz"]["default"])
        self.assertTrue(despues["colchon"]["default"])
        self.s.modules_save(self.a, self.nino, "luz", False, "manual")
        self.assertFalse({x["module"]: x for x in self.s.modules(self.a, self.nino)["modules"]}["luz"]["enabled"])

    def test_11_modulos_invalidos_y_aislamiento(self):
        self.falla(400, self.s.modules_save, self.a, self.nino, "turbo", True, "manual")
        self.falla(400, self.s.modules_save, self.a, self.nino, "luz", True, "raro")
        self.falla(404, self.s.modules_save, self.b, self.nino, "luz", True, "manual")
        self.falla(404, self.s.modules, self.b, self.nino)


class TestLocal(Contrato, unittest.TestCase):
    Harness = tn.HarnessSqlite


@unittest.skipUnless(tn.PGTEST_URL, "Defina PGTEST_URL para probar contra PostgreSQL local")
class TestSupabase(Contrato, unittest.TestCase):
    Harness = tn.HarnessSupabase


if __name__ == "__main__":
    unittest.main()
