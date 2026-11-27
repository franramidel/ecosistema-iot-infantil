"""Sprint 2 sobre la API HTTP real y una base SQLite temporal: SCRUM-55 (contrato que usa la consola de auditoría),
SCRUM-54 (historial), SCRUM-35 (sesión de sueño y snapshot de masa), SCRUM-51 (tomas), SCRUM-57 (ajustes) y
SCRUM-56 (panel de control). Todo es estado lógico: ninguna prueba toca hardware.
"""
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from apoyo import Browser, Base
from cuna.common import instant, uid

NEST = Path(__file__).resolve().parent.parent


def hace(segundos):
    return (datetime.now(timezone.utc) - timedelta(seconds=segundos)).isoformat(timespec="microseconds")


class PanelBase(Base):
    def setUp(self):
        super().setUp()
        self.bebe = self.child("Bebé de prueba")
        self.seed_device(self.bebe, "CUNA-1")

    def lectura(self, valor, metrica="Temperatura", unidad="°C", at=None, device="CUNA-1"):
        evento = dict(event_id=uid(), device_id=device, captured_at=at or instant(), metric=metrica, value=valor, unit=unidad)
        codigo, cuerpo = self.send(evento)
        self.assertEqual(codigo, 200, cuerpo)
        return evento

    def masa(self, valor, at=None, unidad="g"):
        return self.lectura(valor, "Masa", unidad, at)

    def get(self, ruta, cliente=None):
        return (cliente or self.admin).call(ruta)

    def post(self, ruta, cuerpo, cliente=None):
        return (cliente or self.admin).call(ruta, cuerpo)

    def contar(self, tabla):
        with self.db.connection() as db:
            return db.execute(f"select count(*) from {tabla}").fetchone()[0]


# --- SCRUM-55: lo que la consola de auditoría da por hecho de GET /api/audit ----------------------------------

class TestContratoAuditoria(PanelBase):
    def test_01_solo_administradores_y_orden_cronologico(self):
        for i in range(5):
            self.lectura(20.0 + i, at=hace(3000 - i * 60))
        self.assertEqual(self.audit(client=self.caregiver())[0], 403)
        self.assertEqual(Browser(self.url).call("/api/audit")[0], 401)
        codigo, cuerpo = self.audit("child=" + self.bebe)
        self.assertEqual(codigo, 200)
        momentos = [(i["event_date"], i["event_time"]) for i in cuerpo["items"]]
        self.assertEqual(momentos, sorted(momentos))
        self.assertTrue({"event_date", "event_time", "event_type", "detail", "child_id", "device_id", "severity"}
                        <= set(cuerpo["items"][0]))

    def test_02_paginacion_after_recorre_todo_sin_repetir(self):
        for i in range(7):
            self.lectura(20.0 + i, at=hace(3000 - i * 60))
        vistos, cursor = [], None
        while True:
            codigo, cuerpo = self.audit("limit=3" + ("&after=" + quote(cursor) if cursor else ""))
            self.assertEqual(codigo, 200)
            vistos += [i["id"] for i in cuerpo["items"]]
            cursor = cuerpo["next"]
            if not cursor:
                break
        total = self.audit("limit=500")[1]["items"]
        self.assertEqual(vistos, [i["id"] for i in total])
        self.assertEqual(len(set(vistos)), len(vistos))

    def test_03_filtros_de_bebe_dispositivo_y_fechas_y_parametros_invalidos(self):
        otro = self.child("Otro")
        self.seed_device(otro, "CUNA-2")
        self.lectura(21.0, at="2026-03-04T12:00:00+00:00")
        self.lectura(22.0, at="2026-03-06T12:00:00+00:00", device="CUNA-1")
        self.lectura(23.0, at="2026-03-04T13:00:00+00:00", device="CUNA-2")
        filtrar = lambda q: [i["detail"] for i in self.audit(q)[1]["items"] if i["event_type"] == "LECTURA"]
        self.assertEqual(filtrar("device=CUNA-2"), ["Temperatura: 23.0 °C"])
        self.assertEqual(sorted(filtrar("child=" + self.bebe + "&desde=2026-03-05&hasta=2026-03-06")), ["Temperatura: 22.0 °C"])
        self.assertEqual(len(filtrar("desde=2026-03-04&hasta=2026-03-04")), 2)
        for malo in ("child=no-es-uuid", "desde=2026-13-01", "desde=2026-03-05&hasta=2026-03-01", "limit=0", "limit=501", "after=basura"):
            self.assertEqual(self.audit(malo)[0], 400, malo)
        ajena, _, _ = self.second_family_admin()
        self.assertEqual(self.audit("child=" + self.bebe, client=ajena)[0], 404)


# --- SCRUM-54: historial de lecturas ---------------------------------------------------------------------------

class TestHistorial(PanelBase):
    def historial(self, query="", cliente=None):
        return self.get("/api/history?child=" + self.bebe + ("&" + query if query else ""), cliente)

    def test_01_rango_de_fechas_inclusivo_en_hora_de_costa_rica(self):
        self.lectura(20.0, at="2026-03-04T05:59:59+00:00")   # 3 de marzo, 23:59:59 en Costa Rica
        self.lectura(21.0, at="2026-03-04T06:00:00+00:00")   # 4 de marzo, 00:00:00
        self.lectura(22.0, at="2026-03-05T05:59:59+00:00")   # 4 de marzo, 23:59:59
        self.lectura(23.0, at="2026-03-05T06:00:00+00:00")   # 5 de marzo, 00:00:00
        valores = lambda q: [i["value"] for i in self.historial(q)[1]["items"]]
        self.assertEqual(valores("desde=2026-03-04&hasta=2026-03-04"), [21.0, 22.0])
        self.assertEqual(valores("desde=2026-03-04"), [21.0, 22.0, 23.0])
        self.assertEqual(valores("hasta=2026-03-03"), [20.0])
        self.assertEqual(valores(""), [20.0, 21.0, 22.0, 23.0])

    def test_02_devuelve_valores_numericos_y_filtra_por_medida(self):
        self.lectura(21.5, at=hace(120))
        self.lectura(55.0, "Humedad", "%", at=hace(120))
        codigo, cuerpo = self.historial("metric=Humedad")
        self.assertEqual(codigo, 200)
        self.assertEqual([(i["metric"], i["value"], i["unit"]) for i in cuerpo["items"]], [("Humedad", 55.0, "%")])
        self.assertEqual(cuerpo["next"], None)

    def test_03_paginacion_con_after_sin_repetir_ni_perder(self):
        for i in range(7):
            self.lectura(20.0 + i, at=hace(3000 - i * 30))
        vistos, cursor, paginas = [], None, 0
        while True:
            codigo, cuerpo = self.historial("limit=3" + ("&after=" + quote(cursor) if cursor else ""))
            self.assertEqual(codigo, 200)
            vistos += [i["value"] for i in cuerpo["items"]]
            paginas += 1
            cursor = cuerpo["next"]
            if not cursor:
                break
        self.assertEqual(vistos, [20.0 + i for i in range(7)])
        self.assertEqual(paginas, 3)

    def test_04_aislamiento_entre_bebes_y_familias_y_acceso_de_cuidadores(self):
        otro = self.child("Otro")
        self.seed_device(otro, "CUNA-2")
        self.lectura(30.0, at=hace(60), device="CUNA-2")
        self.lectura(21.0, at=hace(60))
        self.assertEqual([i["value"] for i in self.historial()[1]["items"]], [21.0])
        self.assertEqual(self.historial(cliente=self.caregiver())[0], 200)  # los cuidadores ya ven las lecturas en el estado
        ajena, _, _ = self.second_family_admin()
        self.assertEqual(self.historial(cliente=ajena)[0], 404)
        self.assertEqual(self.historial(cliente=Browser(self.url))[0], 401)

    def test_05_parametros_invalidos(self):
        for malo in ("desde=ayer", "desde=2026-02-30", "desde=2026-03-05&hasta=2026-03-01", "limit=0", "limit=2001", "limit=x",
                     "after=basura", "metric=" + "x" * 41):
            self.assertEqual(self.historial(malo)[0], 400, malo)
        self.assertEqual(self.get("/api/history")[0], 400)
        self.assertEqual(self.get("/api/history?child=no-es-uuid")[0], 400)
        self.assertEqual(self.get("/api/history?child=" + uid())[0], 404)

    def test_06_los_umbrales_siguen_disponibles_para_marcarlos_en_la_grafica(self):
        self.assertEqual(self.get("/api/thresholds?child=" + self.bebe)[1]["temp_min"], 19.0)
        self.post("/api/thresholds", dict(child_id=self.bebe, temp_min=20, temp_max=25))
        u = self.get("/api/thresholds?child=" + self.bebe)[1]
        self.assertEqual((u["temp_min"], u["temp_max"], u["default"]), (20.0, 25.0, False))


# --- SCRUM-35 y la parte de sueño de SCRUM-57 ------------------------------------------------------------------

class TestSnapshotDeMasa(PanelBase):
    def sueno(self, cliente=None):
        return self.get("/api/sleep?child=" + self.bebe, cliente)

    def accion(self, nombre, cliente=None, child=None):
        return self.post("/api/sleep/" + nombre, dict(child_id=child or self.bebe), cliente)

    def test_01_sin_sesion_activa_el_snapshot_se_rechaza_con_mensaje_claro(self):
        self.masa(5000.0)
        codigo, cuerpo = self.accion("snapshot")
        self.assertEqual(codigo, 409)
        self.assertIn("sesión de sueño activa", cuerpo["error"])
        self.assertEqual(self.sueno()[1]["active"], False)

    def test_02_sin_lectura_de_masa_no_se_inventa_ningun_peso(self):
        self.assertEqual(self.accion("start")[0], 200)
        codigo, cuerpo = self.accion("snapshot")
        self.assertEqual(codigo, 409)
        self.assertIn("lecturas de masa", cuerpo["error"])
        self.assertIsNone(self.sueno()[1]["session"]["baseline_g"])
        self.masa(1.2, unidad="kg")  # otra unidad: no cuenta como masa en gramos
        self.assertEqual(self.accion("snapshot")[0], 409)
        self.assertIsNone(self.sueno()[1]["session"]["baseline_g"])

    def test_03_el_snapshot_guarda_la_linea_base_y_la_muestra_como_100_por_ciento(self):
        self.accion("start")
        self.masa(5000.0, at=hace(30))
        antes = self.contar("events")
        codigo, estado = self.accion("snapshot")
        self.assertEqual(codigo, 200, estado)
        self.assertEqual(estado["session"]["baseline_g"], 5000.0)
        self.assertEqual(estado["current_pct"], 100.0)
        self.masa(4950.0)
        estado = self.sueno()[1]
        self.assertEqual((estado["session"]["baseline_g"], estado["mass"]["value"], estado["current_pct"]), (5000.0, 4950.0, 99.0))
        self.assertEqual(self.contar("events"), antes + 1)  # el snapshot no escribe ni cambia lecturas

    def test_04_la_linea_base_sobrevive_a_un_reinicio_del_servidor(self):
        self.accion("start")
        self.masa(4800.0)
        self.accion("snapshot")
        self.reiniciar()
        estado = self.sueno()[1]
        self.assertEqual((estado["active"], estado["session"]["baseline_g"], estado["current_pct"]), (True, 4800.0, 100.0))

    def test_05_una_lectura_vieja_o_no_positiva_se_rechaza_y_no_cambia_nada(self):
        self.accion("start")
        self.masa(5000.0, at=hace(1200))  # 20 minutos
        codigo, cuerpo = self.accion("snapshot")
        self.assertEqual(codigo, 409)
        self.assertIn("10 minutos", cuerpo["error"])
        self.masa(0.0)
        self.assertEqual(self.accion("snapshot")[0], 409)
        self.assertIsNone(self.sueno()[1]["session"]["baseline_g"])

    def test_06_desarmar_cierra_la_sesion_y_cada_sesion_tiene_su_propia_linea_base(self):
        self.accion("start")
        self.assertEqual(self.accion("start")[0], 409)  # una sola sesión activa por bebé
        self.masa(5000.0)
        self.accion("snapshot")
        codigo, estado = self.accion("disarm")
        self.assertEqual((codigo, estado["active"], estado["session"]), (200, False, None))
        self.assertEqual(self.accion("disarm")[0], 409)
        self.assertEqual(self.accion("snapshot")[0], 409)
        self.accion("start")
        self.assertIsNone(self.sueno()[1]["session"]["baseline_g"])
        self.assertEqual(self.contar("sleep_sessions"), 2)  # la primera sesión queda guardada

    def test_07_permisos_aislamiento_y_seguridad_de_la_peticion(self):
        solo_lectura = self.caregiver(False)
        self.assertEqual(self.accion("start", solo_lectura)[0], 403)
        self.assertEqual(self.sueno(solo_lectura)[0], 200)
        self.assertEqual(Browser(self.url).call("/api/sleep/start", dict(child_id=self.bebe))[0], 401)
        ajena, _, _ = self.second_family_admin()
        self.assertEqual(self.accion("start", ajena)[0], 404)
        self.assertEqual(self.sueno(ajena)[0], 404)
        self.assertEqual(self.accion("start", child="no-es-uuid")[0], 400)
        codigo, _ = self.admin.call("/api/sleep/start", dict(child_id=self.bebe), {"Origin": "http://otro.example"})
        self.assertEqual(codigo, 403)
        codigo, _ = self.admin.call("/api/sleep/start", dict(child_id=self.bebe), {"Content-Type": "text/plain"})
        self.assertEqual(codigo, 415)
        self.assertEqual(self.contar("sleep_sessions"), 0)

    def test_08_un_cuidador_con_permiso_de_operar_puede_iniciar_y_tomar_snapshot(self):
        operador = self.caregiver(True)
        self.assertEqual(self.accion("start", operador)[0], 200)
        self.masa(4700.0)
        self.assertEqual(self.accion("snapshot", operador)[0], 200)

    def test_09_sesiones_de_dos_bebes_no_se_mezclan(self):
        otro = self.child("Otro")
        self.seed_device(otro, "CUNA-2")
        self.accion("start")
        self.accion("start", child=otro)
        self.masa(5000.0)
        self.lectura(3000.0, "Masa", "g", device="CUNA-2")
        self.accion("snapshot")
        self.assertEqual(self.get("/api/sleep?child=" + otro)[1]["session"]["baseline_g"], None)
        self.accion("snapshot", child=otro)
        self.assertEqual(self.get("/api/sleep?child=" + otro)[1]["session"]["baseline_g"], 3000.0)
        self.assertEqual(self.sueno()[1]["session"]["baseline_g"], 5000.0)


# --- SCRUM-51: registro de tomas --------------------------------------------------------------------------------

class TestTomas(PanelBase):
    def toma(self, cliente=None, **extra):
        cuerpo = dict(child_id=self.bebe, event_id=uid(), duration_s=750, volume=120, unit="ml")
        cuerpo.update(extra)
        return self.post("/api/feeding", cuerpo, cliente)

    def test_01_la_toma_queda_en_la_auditoria_del_bebe_con_duracion_y_volumen(self):
        codigo, cuerpo = self.toma()
        self.assertEqual(codigo, 200, cuerpo)
        filas = self.audit_rows(event_type="TOMA")
        self.assertEqual(len(filas), 1)
        self.assertEqual((filas[0]["detail"], filas[0]["child_id"], filas[0]["device_id"], filas[0]["severity"]),
                         ("Toma: 12 min 30 s · 120.0 ml", self.bebe, "CUNA-1", "info"))
        visible = [i for i in self.audit("tipo=TOMA&child=" + self.bebe)[1]["items"]]
        self.assertEqual([i["detail"] for i in visible], ["Toma: 12 min 30 s · 120.0 ml"])

    def test_02_gramos_y_mililitros_y_formatos_de_duracion(self):
        self.toma(duration_s=45, volume=30.55, unit="g")
        self.toma(duration_s=3725, volume=2.5, unit="ml")
        detalles = sorted(f["detail"] for f in self.audit_rows(event_type="TOMA"))
        self.assertEqual(detalles, ["Toma: 1 h 02 min 05 s · 2.5 ml", "Toma: 45 s · 30.6 g"])

    def test_03_reintentar_con_el_mismo_event_id_no_duplica(self):
        evento = uid()
        primero = self.toma(event_id=evento)[1]
        segundo = self.toma(event_id=evento)[1]
        self.assertEqual((primero["duplicado"], segundo["duplicado"], primero["id"]), (False, True, segundo["id"]))
        self.assertEqual(len(self.audit_rows(event_type="TOMA")), 1)
        otro = self.child("Otro")
        self.assertEqual(self.toma(event_id=evento, child_id=otro)[0], 409)  # el mismo id no sirve para otro bebé

    def test_04_validacion_de_entradas(self):
        malos = [dict(unit="oz"), dict(unit="L"), dict(unit=None), dict(volume=0), dict(volume=-5), dict(volume=2001),
                 dict(volume="120"), dict(volume=True), dict(volume=float("nan")), dict(volume=float("inf")),
                 dict(duration_s=0), dict(duration_s=21601), dict(duration_s=12.5), dict(duration_s="750"), dict(duration_s=True),
                 dict(event_id="no-es-uuid"), dict(child_id="no-es-uuid")]
        for extra in malos:
            codigo, _ = self.toma(**extra)
            self.assertEqual(codigo, 400, extra)
        for faltante in ("event_id", "duration_s", "volume", "unit", "child_id"):
            cuerpo = dict(child_id=self.bebe, event_id=uid(), duration_s=750, volume=120, unit="ml")
            del cuerpo[faltante]
            self.assertEqual(self.post("/api/feeding", cuerpo)[0], 400, faltante)
        self.assertEqual(self.audit_rows(event_type="TOMA"), [])

    def test_05_autenticacion_permisos_y_aislamiento(self):
        self.assertEqual(self.toma(cliente=Browser(self.url))[0], 401)
        self.assertEqual(self.toma(cliente=self.caregiver(False))[0], 403)
        ajena, _, _ = self.second_family_admin()
        self.assertEqual(self.toma(cliente=ajena)[0], 404)
        codigo, _ = self.admin.call("/api/feeding", dict(child_id=self.bebe, event_id=uid(), duration_s=60, volume=10, unit="g"),
                                    {"Origin": "http://otro.example"})
        self.assertEqual(codigo, 403)
        self.assertEqual(self.audit_rows(event_type="TOMA"), [])

    def test_06_un_cuidador_con_permiso_puede_registrar(self):
        self.assertEqual(self.toma(cliente=self.caregiver(True))[0], 200)

    def test_07_sin_dispositivo_asignado_responde_409_claro(self):
        sin = self.child("Sin dispositivo")
        codigo, cuerpo = self.toma(child_id=sin)
        self.assertEqual(codigo, 409)
        self.assertIn("dispositivo", cuerpo["error"])

    def test_08_un_dispositivo_no_puede_fabricar_tomas_por_la_api_de_auditoria(self):
        evento = self.audit_event("CUNA-1", "TOMA", "Toma falsa")
        self.assertEqual(self.send_audit(evento)[0], 403)


# --- SCRUM-57: ajustes de luz y audio --------------------------------------------------------------------------

class TestAjustes(PanelBase):
    def guardar(self, cliente=None, **extra):
        cuerpo = dict(child_id=self.bebe, led_speed_s=8, audio="lluvia", volume=55)
        cuerpo.update(extra)
        return self.post("/api/settings", cuerpo, cliente)

    def leer(self, cliente=None, bebe=None):
        return self.get("/api/settings?child=" + (bebe or self.bebe), cliente)

    def test_01_valores_iniciales_y_persistencia_tras_reiniciar(self):
        inicial = self.leer()[1]
        self.assertEqual((inicial["led_speed_s"], inicial["audio"], inicial["volume"], inicial["default"]), (5.0, "ruido_blanco", 30, True))
        self.assertEqual(self.guardar()[0], 200)
        self.reiniciar()
        guardado = self.leer()[1]
        self.assertEqual((guardado["led_speed_s"], guardado["audio"], guardado["volume"], guardado["default"]), (8.0, "lluvia", 55, False))

    def test_02_los_ajustes_son_por_bebe(self):
        otro = self.child("Otro")
        self.guardar()
        self.assertEqual(self.leer(bebe=otro)[1]["default"], True)

    def test_03_validacion(self):
        for extra in (dict(led_speed_s=0.4), dict(led_speed_s=61), dict(led_speed_s="5"), dict(led_speed_s=float("nan")),
                      dict(audio="rock"), dict(audio=None), dict(volume=-1), dict(volume=101), dict(volume=50.5),
                      dict(volume="50"), dict(volume=True), dict(child_id="x")):
            self.assertEqual(self.guardar(**extra)[0], 400, extra)
        for limite in (dict(led_speed_s=0.5, volume=0, audio="ninguno"), dict(led_speed_s=60, volume=100, audio="nana")):
            self.assertEqual(self.guardar(**limite)[0], 200, limite)

    def test_04_solo_administradores_cambian_pero_todos_leen(self):
        operador = self.caregiver(True)
        self.assertEqual(self.guardar(cliente=operador)[0], 403)
        self.assertEqual(self.leer(cliente=operador)[0], 200)
        ajena, _, _ = self.second_family_admin()
        self.assertEqual(self.guardar(cliente=ajena)[0], 404)
        self.assertEqual(self.leer(cliente=ajena)[0], 404)
        self.assertEqual(self.guardar(cliente=Browser(self.url))[0], 401)

    def test_05_los_umbrales_y_su_rango_por_defecto_no_cambian(self):
        self.guardar()
        u = self.get("/api/thresholds?child=" + self.bebe)[1]
        self.assertEqual((u["temp_min"], u["temp_max"], u["default"]), (19.0, 22.0, True))

    def test_06_la_interfaz_ofrece_los_mismos_audios_que_valida_el_servidor(self):
        from cuna import panel
        js = (NEST / "web" / "app.js").read_text(encoding="utf-8")
        for audio in panel.AUDIOS:
            self.assertIn(f'"{audio}"', js, audio)


# --- SCRUM-56: panel de control --------------------------------------------------------------------------------

class TestPanelDeControl(PanelBase):
    def cambiar(self, modulo, encendido, modo, cliente=None, **extra):
        return self.post("/api/modules", dict(child_id=self.bebe, module=modulo, enabled=encendido, mode=modo, **extra), cliente)

    def leer(self, cliente=None, bebe=None):
        return self.get("/api/modules?child=" + (bebe or self.bebe), cliente)

    def estado(self, bebe=None):
        return {m["module"]: (m["enabled"], m["mode"]) for m in self.leer(bebe=bebe)[1]["modules"]}

    def test_01_hay_una_fila_por_modulo_con_valores_iniciales(self):
        codigo, cuerpo = self.leer()
        self.assertEqual(codigo, 200)
        self.assertEqual([m["module"] for m in cuerpo["modules"]], ["temperatura", "luz", "audio", "colchon"])
        self.assertEqual(self.estado(), {"temperatura": (True, "manual"), "luz": (False, "manual"),
                                         "audio": (False, "manual"), "colchon": (False, "manual")})

    def test_02_encender_apagar_y_cambiar_de_modo_persiste_tras_reiniciar(self):
        self.assertEqual(self.cambiar("luz", True, "programado")[0], 200)
        self.assertEqual(self.cambiar("temperatura", False, "manual")[0], 200)
        self.assertEqual(self.cambiar("colchon", True, "manual")[0], 200)
        self.assertEqual(self.cambiar("colchon", False, "programado")[0], 200)
        self.reiniciar()
        self.assertEqual(self.estado(), {"temperatura": (False, "manual"), "luz": (True, "programado"),
                                         "audio": (False, "manual"), "colchon": (False, "programado")})

    def test_03_cada_bebe_tiene_su_propio_estado(self):
        otro = self.child("Otro")
        self.cambiar("audio", True, "programado")
        self.assertEqual(self.estado(otro)["audio"], (False, "manual"))

    def test_04_validacion(self):
        for modulo, enc, modo in (("camara", True, "manual"), ("luz", "si", "manual"), ("luz", 1, "manual"), ("luz", True, "auto"),
                                  ("luz", True, None), (None, True, "manual")):
            self.assertEqual(self.cambiar(modulo, enc, modo)[0], 400, (modulo, enc, modo))
        self.assertEqual(self.post("/api/modules", dict(child_id="x", module="luz", enabled=True, mode="manual"))[0], 400)
        self.assertEqual(self.estado()["luz"], (False, "manual"))

    def test_05_permisos_y_aislamiento(self):
        solo_lectura = self.caregiver(False)
        self.assertEqual(self.cambiar("luz", True, "manual", cliente=solo_lectura)[0], 403)
        self.assertEqual(self.leer(cliente=solo_lectura)[0], 200)
        self.assertEqual(self.cambiar("luz", True, "manual", cliente=Browser(self.url))[0], 401)
        ajena, _, _ = self.second_family_admin()
        self.assertEqual(self.cambiar("luz", True, "manual", cliente=ajena)[0], 404)
        self.assertEqual(self.leer(cliente=ajena)[0], 404)
        self.assertEqual(self.estado()["luz"], (False, "manual"))

    def test_06_un_cuidador_con_permiso_de_operar_puede_cambiar(self):
        self.assertEqual(self.cambiar("audio", True, "manual", cliente=self.caregiver(True))[0], 200)
        self.assertEqual(self.estado()["audio"], (True, "manual"))

    def test_07_es_estado_logico_no_toca_la_asignacion_del_dispositivo(self):
        antes = self.state()["devices"]
        self.cambiar("temperatura", False, "programado")
        self.assertEqual(self.state()["devices"], antes)

    def test_08_los_cambios_avisan_a_otras_pantallas(self):
        with self.db.connection() as db:
            antes = db.execute("select version from families").fetchone()[0]
        self.cambiar("luz", True, "manual")
        with self.db.connection() as db:
            despues = db.execute("select version from families").fetchone()[0]
        self.assertEqual(despues, antes + 1)


# --- Migración -------------------------------------------------------------------------------------------------

class TestMigracion(PanelBase):
    def test_01_las_tablas_nuevas_existen_y_repetir_la_migracion_no_pierde_datos(self):
        self.post("/api/sleep/start", dict(child_id=self.bebe))
        self.post("/api/modules", dict(child_id=self.bebe, module="luz", enabled=True, mode="programado"))
        from cuna.local import Local
        Local(self.path)  # vuelve a migrar el mismo archivo
        self.assertEqual(self.contar("sleep_sessions"), 1)
        self.assertEqual(self.contar("module_states"), 1)

    def test_02_las_sesiones_de_sueno_no_se_pueden_borrar(self):
        self.post("/api/sleep/start", dict(child_id=self.bebe))
        with self.assertRaises(sqlite3.IntegrityError), self.db.connection(True) as db:
            db.execute("delete from sleep_sessions")


# --- Interfaz: requisitos de seguridad de los archivos estáticos -----------------------------------------------

class TestArchivosWeb(Base):
    def test_01_sin_estilos_ni_scripts_en_linea_y_con_los_nuevos_elementos(self):
        html = (NEST / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\sstyle\s*=", html))
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html))
        # Dos scripts, ambos externos y del mismo origen: app.js y p5.js (Persona 5). Ninguno en línea.
        etiquetas = re.findall(r"<script\b[^>]*>", html)
        self.assertEqual(len(etiquetas), 2)
        self.assertTrue(all(re.search(r'\ssrc="/[\w.-]+\.js"', e) for e in etiquetas), etiquetas)
        self.assertNotIn("<script>", html)
        for ident in ("audit-tab", "audit-table", "audit-more", "audit-export", "snapshot-btn", "disarm-btn", "feed-start",
                      "feed-stop", "modules-list", "led-speed", "voice-btn", "graph"):
            self.assertIn(f'id="{ident}"', html, ident)
        js = (NEST / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\.style\.|setAttribute\(\s*[\"']style", js))
        self.assertIsNone(re.search(r"eval\(|new Function|document\.write", js))

    def test_02_la_cabecera_csp_sigue_prohibiendo_lo_en_linea(self):
        codigo, cuerpo, cabeceras = Browser(self.url).call("/", raw=True)
        csp = cabeceras["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertIn("style-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
