"""Pruebas de RF-NOT-02, RF-CUN-01 y RF-CUN-02: alertas de temperatura por servidor y umbrales por bebé.

Corren contra un servidor real y una base SQLite temporal. Telegram es un servidor HTTP falso: ninguna prueba
sale a Internet ni usa un token real.
"""
import io
from datetime import timedelta

from apoyo import ADMIN, Base
from apoyo_telegram import TOKEN, Reloj, TelegramFalso
from cuna import alertas
from cuna.common import Problem, instant, uid
from cuna.procesador import Procesador
from cuna.telegram import ClienteTelegram


def hace(segundos):
    from datetime import datetime
    return (datetime.fromisoformat(instant()) - timedelta(seconds=segundos)).isoformat(timespec="microseconds")


class AlertasBase(Base):
    def setUp(self):
        super().setUp()
        self.bebe = self.child("Bebé de prueba")
        self.device(self.bebe, "CUNA-1")

    def temperatura(self, valor, at=None, device="CUNA-1", metrica="Temperatura", unidad="°C"):
        evento = dict(event_id=uid(), device_id=device, captured_at=at or instant(), metric=metrica, value=valor, unit=unidad)
        codigo, cuerpo = self.send(evento)
        self.assertEqual(codigo, 200, cuerpo)
        return evento

    def cola(self):
        with self.db.connection() as db:
            return [dict(r) for r in db.execute("select * from notification_queue order by created_at,rowid")]

    def alertas_auditoria(self):
        with self.db.connection() as db:
            return [dict(r) for r in db.execute(
                "select * from audit_log where event_type like 'ALERTA_TEMPERATURA%' order by occurred_at,rowid")]

    def estado_alerta(self, device="CUNA-1"):
        with self.db.connection() as db:
            fila = db.execute("select state from alert_state where device_id=?", (device,)).fetchone()
            return fila["state"] if fila else None

    def miembro(self, correo=ADMIN[0]):
        with self.db.connection() as db:
            return db.execute("select id from members where email=?", (correo,)).fetchone()[0]

    def umbrales(self, client=None, bebe=None):
        return (client or self.admin).call("/api/thresholds?child=" + (bebe or self.bebe))

    def fijar(self, minimo, maximo, client=None, bebe=None):
        return (client or self.admin).call("/api/thresholds", dict(child_id=bebe or self.bebe, temp_min=minimo, temp_max=maximo))


class TestRegla(Base):
    def test_01_los_limites_no_alertan(self):
        self.assertEqual(alertas.clasificar(22.0), "normal")
        self.assertEqual(alertas.clasificar(19.0), "normal")
        self.assertEqual(alertas.clasificar(20.5), "normal")

    def test_02_solo_se_alerta_al_superar_o_bajar(self):
        self.assertEqual(alertas.clasificar(22.1), "calor")
        self.assertEqual(alertas.clasificar(22.5), "calor")
        self.assertEqual(alertas.clasificar(18.9), "frio")
        self.assertEqual(alertas.clasificar(18.5), "frio")

    def test_03_umbrales_propios(self):
        self.assertEqual(alertas.clasificar(24.0, 20, 25), "normal")
        self.assertEqual(alertas.clasificar(25.1, 20, 25), "calor")

    def test_04_validacion_de_umbrales(self):
        self.assertEqual(alertas.validar_umbrales(19, 22), (19.0, 22.0))
        for malo in ((22, 19), (20, 20), ("a", 22), (19, None), (float("nan"), 22), (19, float("inf")), (-50, 22), (19, 90), (True, 22)):
            with self.assertRaises(Problem, msg=str(malo)):
                alertas.validar_umbrales(*malo)


class TestAlertas(AlertasBase):
    def test_01_dentro_del_rango_no_hay_alerta(self):
        for valor in (19.0, 20.5, 22.0):
            self.temperatura(valor)
        self.assertEqual(self.cola(), [])
        self.assertEqual(self.alertas_auditoria(), [])
        self.assertEqual(self.estado_alerta(), "normal")

    def test_02_superar_el_maximo_alerta_una_vez(self):
        self.temperatura(22.5)
        cola = self.cola()
        self.assertEqual(len(cola), 1)
        self.assertEqual(cola[0]["member_id"], self.miembro())
        self.assertIn("Temperatura alta: 22.5", cola[0]["message"])
        self.assertIn("Estado: Crítico", cola[0]["message"])
        self.assertIn("Dispositivo: CUNA-1", cola[0]["message"])
        self.assertNotIn("Bebé de prueba", cola[0]["message"])  # el mensaje no lleva datos del bebé
        registros = self.alertas_auditoria()
        self.assertEqual(len(registros), 1)
        self.assertEqual((registros[0]["event_type"], registros[0]["severity"], registros[0]["child_id"]),
                         ("ALERTA_TEMPERATURA_ALTA", "critical", self.bebe))
        self.assertEqual(cola[0]["event_id"], registros[0]["id"])
        self.assertEqual(self.estado_alerta(), "calor")

    def test_03_bajar_del_minimo_alerta(self):
        self.temperatura(18.5)
        cola = self.cola()
        self.assertEqual(len(cola), 1)
        self.assertIn("Temperatura baja: 18.5", cola[0]["message"])
        self.assertEqual(self.alertas_auditoria()[0]["event_type"], "ALERTA_TEMPERATURA_BAJA")
        self.assertEqual(self.estado_alerta(), "frio")

    def test_04_no_se_repite_mientras_siga_fuera_de_rango(self):
        for valor in (22.5, 23.0, 24.0, 22.5):
            self.temperatura(valor)
        self.assertEqual(len(self.cola()), 1)

    def test_05_volver_a_la_normalidad_no_avisa_pero_rearma(self):
        self.temperatura(22.5)
        self.temperatura(21.0)
        self.assertEqual(self.estado_alerta(), "normal")
        self.assertEqual(len(self.cola()), 1)
        self.temperatura(23.0)
        self.assertEqual(len(self.cola()), 2)

    def test_06_de_calor_a_frio_es_otro_cambio_de_estado(self):
        self.temperatura(23.0)
        self.temperatura(18.0)
        tipos = [r["event_type"] for r in self.alertas_auditoria()]
        self.assertEqual(tipos, ["ALERTA_TEMPERATURA_ALTA", "ALERTA_TEMPERATURA_BAJA"])
        self.assertEqual(len(self.cola()), 2)

    def test_07_la_primera_lectura_fuera_de_rango_tambien_alerta(self):
        self.temperatura(30.0)
        self.assertEqual(len(self.cola()), 1)

    def test_08_el_reintento_de_una_lectura_no_duplica_la_alerta(self):
        evento = self.temperatura(25.0)
        for _ in range(3):
            self.assertEqual(self.send(evento)[0], 200)
        self.assertEqual(len(self.cola()), 1)
        self.assertEqual(len(self.alertas_auditoria()), 1)

    def test_09_la_humedad_se_guarda_pero_no_alerta(self):
        for valor in (10.0, 55.0, 95.0):
            self.temperatura(valor, metrica="Humedad", unidad="%")
        self.assertEqual(self.cola(), [])
        self.assertEqual(self.alertas_auditoria(), [])
        with self.db.connection() as db:
            self.assertEqual(db.execute("select count(*) from events where metric='Humedad'").fetchone()[0], 3)

    def test_10_una_lectura_atrasada_no_deshace_un_estado_mas_reciente(self):
        self.seed_device(self.bebe, "CUNA-9")  # asignado desde hace tiempo, para poder enviar lecturas del pasado
        self.temperatura(24.0, at=hace(5), device="CUNA-9")
        self.assertEqual(self.estado_alerta("CUNA-9"), "calor")
        self.temperatura(20.0, at=hace(30), device="CUNA-9")  # reintento viejo, anterior al estado actual
        self.assertEqual(self.estado_alerta("CUNA-9"), "calor")
        self.temperatura(24.5, device="CUNA-9")
        self.assertEqual(len(self.cola()), 1)  # sigue en calor: no hay aviso repetido

    def test_11_avisa_a_todos_los_miembros_de_la_familia(self):
        self.caregiver()
        self.temperatura(25.0)
        miembros = {r["member_id"] for r in self.cola()}
        self.assertEqual(miembros, {self.miembro(), self.miembro("cuidador@example.test")})

    def test_12_no_avisa_a_otras_familias(self):
        _, _, _ = self.second_family_admin()
        self.temperatura(25.0)
        self.assertEqual({r["member_id"] for r in self.cola()}, {self.miembro()})

    def test_13_cada_bebe_y_dispositivo_tienen_su_propio_estado(self):
        otro = self.child("Otro bebé")
        self.device(otro, "CUNA-2")
        self.temperatura(25.0, device="CUNA-1")
        self.temperatura(25.0, device="CUNA-2")
        self.assertEqual(len(self.cola()), 2)
        self.temperatura(26.0, device="CUNA-2")
        self.assertEqual(len(self.cola()), 2)

    def test_14_solo_la_temperatura_en_grados_celsius_se_evalua(self):
        self.temperatura(80.0, unidad="°F")
        self.temperatura(80.0, metrica="Peso", unidad="g")
        self.assertEqual(self.cola(), [])

    def test_15_la_alerta_es_atomica_con_la_lectura(self):
        """Si la solicitud falla, la lectura tampoco se guarda: no queda una lectura crítica sin aviso."""
        original = alertas.notificaciones.encolar

        def roto(*args, **kwargs):
            raise Problem("falla simulada", 500)

        alertas.notificaciones.encolar = roto
        try:
            evento = dict(event_id=uid(), device_id="CUNA-1", captured_at=instant(), metric="Temperatura", value=25.0, unit="°C")
            codigo, _ = self.send(evento)
            self.assertEqual(codigo, 500)
        finally:
            alertas.notificaciones.encolar = original
        with self.db.connection() as db:
            self.assertEqual(db.execute("select count(*) from events where id=?", (evento["event_id"],)).fetchone()[0], 0)
        self.assertEqual(self.estado_alerta(), None)
        self.assertEqual(self.send(evento)[0], 200)  # el reintento sí alerta
        self.assertEqual(len(self.cola()), 1)


class TestUmbrales(AlertasBase):
    def test_01_por_defecto_son_19_y_22(self):
        codigo, datos = self.umbrales()
        self.assertEqual(codigo, 200)
        self.assertEqual(datos, {"child_id": self.bebe, "temp_min": 19.0, "temp_max": 22.0, "default": True})

    def test_02_el_administrador_los_cambia(self):
        self.assertEqual(self.fijar(20, 25)[0], 200)
        self.assertEqual(self.umbrales()[1], {"child_id": self.bebe, "temp_min": 20.0, "temp_max": 25.0, "default": False})

    def test_03_el_cambio_se_aplica_a_las_lecturas_siguientes(self):
        self.fijar(20, 25)
        self.temperatura(24.0)
        self.assertEqual(self.cola(), [])
        self.temperatura(25.5)
        self.assertEqual(len(self.cola()), 1)
        self.temperatura(19.5)
        self.assertEqual(len(self.cola()), 2)

    def test_04_los_umbrales_son_por_bebe(self):
        otro = self.child("Otro bebé")
        self.device(otro, "CUNA-2")
        self.fijar(10, 40, bebe=otro)
        self.temperatura(30.0, device="CUNA-2")
        self.assertEqual(self.cola(), [])
        self.temperatura(30.0, device="CUNA-1")
        self.assertEqual(len(self.cola()), 1)

    def test_05_un_cuidador_puede_leer_pero_no_cambiar(self):
        cuidador = self.caregiver(allowed=True)
        self.assertEqual(self.umbrales(cuidador)[0], 200)
        self.assertEqual(self.fijar(10, 40, cuidador)[0], 403)
        self.assertEqual(self.umbrales()[1]["default"], True)

    def test_06_valores_invalidos_se_rechazan(self):
        for minimo, maximo in ((22, 19), (20, 20), ("x", 22), (None, 22), (19, 99), (-99, 22)):
            codigo, _ = self.fijar(minimo, maximo)
            self.assertEqual(codigo, 400, (minimo, maximo))
        self.assertEqual(self.umbrales()[1]["default"], True)

    def test_07_otra_familia_no_ve_ni_cambia_los_umbrales(self):
        otro, _, _ = self.second_family_admin()
        self.assertEqual(self.umbrales(otro)[0], 404)
        self.assertEqual(self.fijar(10, 40, otro)[0], 404)
        self.assertEqual(self.umbrales()[1]["default"], True)

    def test_08_requiere_sesion_y_bebe(self):
        from apoyo import Browser
        anonimo = Browser(self.url)
        self.assertIn(anonimo.call("/api/thresholds?child=" + self.bebe)[0], (401, 403))
        self.assertEqual(anonimo.call("/api/thresholds", dict(child_id=self.bebe, temp_min=10, temp_max=40))[0], 401)
        self.assertEqual(self.admin.call("/api/thresholds")[0], 400)
        self.assertEqual(self.fijar(20, 25, bebe=uid())[0], 404)

    def test_09_los_umbrales_sobreviven_a_un_reinicio(self):
        self.fijar(20, 25)
        self.temperatura(25.5)
        self.reiniciar()
        self.assertEqual(self.umbrales()[1]["temp_max"], 25.0)
        self.assertEqual(self.estado_alerta(), "calor")
        self.temperatura(26.0)
        self.assertEqual(len(self.cola()), 1)


class TestDeExtremoAExtremo(AlertasBase):
    """Lectura del sensor -> alerta del servidor -> cola -> procesador -> Telegram falso."""

    def setUp(self):
        super().setUp()
        self.falso = TelegramFalso()
        self.addCleanup(self.falso.cerrar)
        self.reloj = Reloj()
        self.proc = Procesador(self.db, ClienteTelegram(TOKEN, self.falso.base, timeout=2), "proc-1", self.reloj, salida=io.StringIO())

    def vincular(self, correo=ADMIN[0], chat=5001):
        miembro = self.miembro(correo)
        codigo = self.db.telegram_codigo({"id": miembro}, ahora=self.reloj())["codigo"]
        self.falso.mensaje("/start " + codigo, chat=chat)
        self.proc.procesar_actualizaciones(0)

    def test_01_la_alerta_llega_a_telegram(self):
        self.vincular()
        self.temperatura(23.5)
        self.reloj.avanzar(5)
        self.assertEqual(self.proc.procesar_cola()["enviadas"], 1)
        mensajes = [m for m in self.falso.enviados if "Alerta:" in m.get("text", "")]
        self.assertEqual(len(mensajes), 1)
        self.assertIn("Temperatura alta: 23.5", mensajes[0]["text"])
        self.assertEqual(str(mensajes[0]["chat_id"]), "5001")

    def test_02_sin_cambio_de_estado_no_se_envia_nada_mas(self):
        self.vincular()
        self.temperatura(23.5)
        self.temperatura(24.0)
        self.temperatura(23.0)
        self.reloj.avanzar(5)
        self.proc.procesar_cola()
        self.assertEqual(len([m for m in self.falso.enviados if "Alerta:" in m.get("text", "")]), 1)

    def test_03_un_miembro_sin_telegram_queda_registrado_como_sin_vinculo(self):
        self.caregiver()
        self.vincular()
        self.temperatura(25.0)
        estados = {r["member_id"]: r["status"] for r in self.cola()}
        self.assertEqual(estados[self.miembro()], "pendiente")
        self.assertEqual(estados[self.miembro("cuidador@example.test")], "sin_vinculo")

    def test_04_telegram_caido_no_pierde_la_alerta(self):
        self.vincular()
        self.falso.guion.extend([(500, {"ok": False, "error_code": 500, "description": "caído"})])
        self.temperatura(25.0)
        self.reloj.avanzar(5)
        self.proc.procesar_cola()
        self.assertEqual([r["status"] for r in self.cola()], ["error_recuperable"])
        self.reloj.avanzar(600)
        self.assertEqual(self.proc.procesar_cola()["enviadas"], 1)
        self.assertEqual([r["status"] for r in self.cola()], ["enviada"])
