"""Umbrales y alertas de temperatura con los dos almacenes: SQLite (Local) y Supabase (Cloud).

Mismo contrato contra ambos. La parte de Supabase corre el SQL real de supabase/001 a 008 en un PostgreSQL local
desechable, detrás del puente de pruebas (NO es Supabase real). Se omite si falta PGTEST_URL; ver test_telegram_nube.py.
"""
import os
import sys
import unittest
from datetime import datetime, timezone

import test_telegram_nube as tn
from cuna.common import Problem, uid

SQL_008 = "supabase/008_alertas_temperatura.sql"


def setUpModule():
    if SQL_008 not in tn.MIGRACIONES:
        tn.MIGRACIONES = tn.MIGRACIONES + [SQL_008]
    tn.setUpModule()


def tearDownModule():
    tn.tearDownModule()


def ahora():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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

    def temp(self, valor):
        self.s.ingest("CUNA-1", self.clave, (uid(), ahora(), "Temperatura", valor, "°C"))

    def alertas(self):
        return int(self.h.sql("select count(*) from audit_log where event_type like 'ALERTA_TEMPERATURA%'")[0][0])

    def test_1_valores_por_defecto(self):
        u = self.s.umbrales(self.a, self.nino)
        self.assertEqual((float(u["temp_min"]), float(u["temp_max"])), (19.0, 22.0))

    def test_2_el_admin_cambia_y_el_cuidador_solo_lee(self):
        self.s.umbrales_guardar(self.a, self.nino, 20, 25)
        self.assertEqual(float(self.s.umbrales(self.c, self.nino)["temp_max"]), 25.0)
        with self.assertRaises(Problem) as ctx:
            self.s.umbrales_guardar(self.c, self.nino, 10, 40)
        self.assertEqual(ctx.exception.status, 403)

    def test_3_otra_familia_no_ve_ni_cambia(self):
        for f in (lambda: self.s.umbrales(self.b, self.nino), lambda: self.s.umbrales_guardar(self.b, self.nino, 10, 40)):
            with self.assertRaises(Problem) as ctx:
                f()
            self.assertEqual(ctx.exception.status, 404)

    def test_4_valores_invalidos(self):
        for mn, mx in ((25, 20), (20, 90), (None, 30)):
            with self.assertRaises(Problem, msg=(mn, mx)):
                self.s.umbrales_guardar(self.a, self.nino, mn, mx)
        self.assertEqual(float(self.s.umbrales(self.a, self.nino)["temp_max"]), 22.0)

    def test_5_el_dispositivo_lee_con_su_clave(self):
        self.s.umbrales_guardar(self.a, self.nino, 20, 25)
        self.assertEqual(float(self.s.umbrales_dispositivo("CUNA-1", self.clave)["temp_max"]), 25.0)
        with self.assertRaises(Problem) as ctx:
            self.s.umbrales_dispositivo("CUNA-1", "mala")
        self.assertEqual(ctx.exception.status, 403)

    def test_6_alerta_solo_al_cambiar_de_estado(self):
        for v in (22.0, 19.0):
            self.temp(v)
        self.assertEqual(self.alertas(), 0)
        self.temp(22.5)
        self.temp(23.0)
        self.assertEqual(self.alertas(), 1)
        self.temp(20.0)
        self.temp(18.5)
        self.assertEqual(self.alertas(), 2)

    def test_7_usa_los_umbrales_configurados(self):
        self.s.umbrales_guardar(self.a, self.nino, 20, 25)
        self.temp(24.0)
        self.assertEqual(self.alertas(), 0)
        self.temp(25.5)
        self.assertEqual(self.alertas(), 1)

    def test_8_la_humedad_no_alerta(self):
        self.s.ingest("CUNA-1", self.clave, (uid(), ahora(), "Humedad", 99.0, "%"))
        self.assertEqual(self.alertas(), 0)


class TestLocal(Contrato, unittest.TestCase):
    Harness = tn.HarnessSqlite


@unittest.skipUnless(tn.PGTEST_URL, "Defina PGTEST_URL para probar contra PostgreSQL local")
class TestSupabase(Contrato, unittest.TestCase):
    Harness = tn.HarnessSupabase


if __name__ == "__main__":
    unittest.main()
