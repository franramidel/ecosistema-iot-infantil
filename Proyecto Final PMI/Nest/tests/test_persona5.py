"""Persona 5, Sprint 2: SCRUM-50 (biberón), SCRUM-52 (recordatorios), SCRUM-43 parte B (fotos), SCRUM-31 parte B (voz).

Corren contra un servidor real y una base SQLite temporal, como las demás pruebas. No usan Internet ni Telegram real.
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from apoyo import ADMIN, Base
from cuna import alimentacion, fotos
from cuna.common import instant, uid
from cuna.voz import Canales

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def hace(segundos):
    return (datetime.fromisoformat(instant()) - timedelta(seconds=segundos)).isoformat(timespec="microseconds")


class P5(Base):
    def setUp(self):
        super().setUp()
        self.bebe = self.child("Bebé de prueba")
        self.device(self.bebe, "CUNA-1")

    def biberon(self, valor, at=None, device="CUNA-1"):
        evento = dict(event_id=uid(), device_id=device, captured_at=at or instant(), metric=alimentacion.METRICA, value=valor, unit="°C")
        return self.send(evento), evento

    def cola(self):
        with self.db.connection() as db:
            return [dict(r) for r in db.execute("select * from notification_queue order by created_at,rowid")]

    def alertas(self):
        with self.db.connection() as db:
            return [dict(r) for r in db.execute("select * from audit_log where event_type like 'ALERTA_BIBERON%' order by rowid")]

    def get(self, ruta, client=None):
        return (client or self.admin).call(ruta)

    def post(self, ruta, datos, client=None):
        return (client or self.admin).call(ruta, datos)

    def subir(self, datos=JPEG, tipo="image/jpeg", device="CUNA-1", key=None, evento=None, at=None, url=None):
        cab = {"Content-Type": tipo, "Authorization": "Bearer " + (key or self.keys.get(device, "x")), "X-Device-Id": device,
               "X-Event-Id": evento or uid(), "X-Captured-At": at or instant()}
        try:
            with urlopen(Request((url or self.url) + "/api/photos", data=datos, headers=cab), timeout=8) as r:
                return r.status, json.load(r)
        except HTTPError as err:
            return err.code, json.load(err)


class TestBiberon(P5):
    def test_01_regla_y_limites(self):
        self.assertEqual([alimentacion.clasificar(v) for v in (35.9, 36, 37, 40, 40.1)],
                         ["fria", "normal", "normal", "normal", "caliente"])

    def test_02_lectura_normal_no_alerta(self):
        (codigo, _), _ = self.biberon(37.0)
        self.assertEqual(codigo, 200)
        self.assertEqual(self.alertas(), [])
        self.assertEqual(self.get("/api/bottle?child=" + self.bebe)[1]["status"], "normal")

    def test_03_caliente_alerta_una_sola_vez_y_encola_telegram(self):
        self.biberon(41.5)
        self.biberon(42.0)  # sigue fuera de rango: no repite
        filas = self.alertas()
        self.assertEqual([f["event_type"] for f in filas], ["ALERTA_BIBERON_CALIENTE"])
        self.assertEqual(filas[0]["severity"], "critical")
        self.assertIn("muy caliente", filas[0]["detail"])
        cola = self.cola()
        self.assertEqual(len(cola), 1)
        self.assertIn("muy caliente", cola[0]["message"])
        self.assertIn("Crítico", cola[0]["message"])

    def test_04_frio_y_cambio_de_estado(self):
        self.biberon(30.0)
        self.biberon(37.0)  # volver a la normalidad no avisa
        self.biberon(44.0)  # frío -> normal -> caliente: avisa de nuevo
        self.assertEqual([f["event_type"] for f in self.alertas()], ["ALERTA_BIBERON_FRIO", "ALERTA_BIBERON_CALIENTE"])
        self.assertEqual(self.alertas()[0]["severity"], "warning")

    def test_05_reintento_con_el_mismo_uuid_no_duplica(self):
        (_, _), evento = self.biberon(45.0)
        self.assertEqual(self.send(evento)[0], 200)
        self.assertEqual(len(self.alertas()), 1)
        self.assertEqual(len(self.cola()), 1)

    def test_06_lectura_atrasada_no_deshace_un_estado_mas_reciente(self):
        self.biberon(45.0)
        self.biberon(37.0, at=hace(3600))
        self.assertEqual(self.get("/api/bottle?child=" + self.bebe)[1]["status"], "caliente")
        self.assertEqual(len(self.alertas()), 1)

    def test_07_lectura_imposible_se_rechaza_y_no_se_guarda(self):
        (codigo, cuerpo), _ = self.biberon(250.0)
        self.assertEqual(codigo, 400, cuerpo)
        self.assertEqual(self.get("/api/bottle?child=" + self.bebe)[1]["status"], "sin_lectura")

    def test_08_no_mezcla_con_la_temperatura_ambiente(self):
        self.send(self.reading("CUNA-1", value=30.0))  # temperatura de la cuna, rango 19-22
        self.assertEqual(self.alertas(), [])

    def test_09_umbrales_configurables_solo_admin(self):
        c, d = self.get("/api/bottle?child=" + self.bebe)
        self.assertEqual((d["temp_min"], d["temp_max"], d["target"], d["default"]), (36.0, 40.0, 37.0, True))
        self.assertEqual(self.post("/api/bottle/thresholds", dict(child_id=self.bebe, temp_min=35, temp_max=39))[0], 200)
        self.biberon(39.5)  # ahora es normal... 39.5 < 39? no: 39.5 > 39 -> caliente
        self.assertEqual(self.alertas()[0]["event_type"], "ALERTA_BIBERON_CALIENTE")
        self.assertEqual(self.post("/api/bottle/thresholds", dict(child_id=self.bebe, temp_min=40, temp_max=36))[0], 400)
        self.assertEqual(self.post("/api/bottle/thresholds", dict(child_id=self.bebe, temp_min="a", temp_max=36))[0], 400)
        cuidador = self.caregiver(True)
        self.assertEqual(self.post("/api/bottle/thresholds", dict(child_id=self.bebe, temp_min=30, temp_max=41), cuidador)[0], 403)
        self.assertEqual(self.get("/api/bottle?child=" + self.bebe, cuidador)[0], 200)

    def test_10_otra_familia_no_ve_el_estado(self):
        otro, _, _ = self.second_family_admin()
        self.assertEqual(self.get("/api/bottle?child=" + self.bebe, otro)[0], 404)


class TestRecordatorios(P5):
    def crear(self, client=None, **datos):
        return self.post("/api/reminders", dict(child_id=self.bebe, action="save", **datos), client)

    def test_01_intervalo_y_horas_fijas(self):
        c, d = self.crear(kind="interval", interval_min=180)
        self.assertEqual(c, 200, d)
        c, d = self.crear(kind="fixed", times=["20:00", "08:00", "08:00"])
        self.assertEqual(c, 200, d)
        lista = self.get("/api/reminders?child=" + self.bebe)[1]["reminders"]
        self.assertEqual([r["kind"] for r in lista], ["interval", "fixed"])
        self.assertEqual(lista[1]["times"], ["08:00", "20:00"])

    def test_02_validacion(self):
        for malo in (dict(kind="interval", interval_min=5), dict(kind="interval", interval_min=1.5), dict(kind="interval", interval_min=2000),
                     dict(kind="fixed", times=[]), dict(kind="fixed", times=["25:00"]), dict(kind="fixed", times="08:00"),
                     dict(kind="otro"), dict(kind="interval", interval_min=60, enabled="si")):
            self.assertEqual(self.crear(**malo)[0], 400, malo)

    def test_03_permiso_para_operar(self):
        sin = self.caregiver(False)
        self.assertEqual(self.crear(sin, kind="interval", interval_min=60)[0], 403)
        self.assertEqual(self.get("/api/reminders?child=" + self.bebe, sin)[0], 200)

    def test_04_editar_y_borrar(self):
        r = self.crear(kind="interval", interval_min=60)[1]["reminders"][0]
        c, d = self.crear(id=r["id"], kind="interval", interval_min=120)
        self.assertEqual((c, d["reminders"][0]["interval_min"]), (200, 120))
        self.assertEqual(self.post("/api/reminders", dict(child_id=self.bebe, action="delete", id=r["id"]))[1]["reminders"], [])
        self.assertEqual(self.post("/api/reminders", dict(child_id=self.bebe, action="delete", id=r["id"]))[0], 404)
        with self.db.connection() as db:     # borrado lógico: la fila se conserva y ya no dispara
            fila = db.execute("select enabled,deleted_at from feed_reminders where id=?", (r["id"],)).fetchone()
        self.assertEqual((fila["enabled"], fila["deleted_at"] is not None), (0, True))

    def test_05_limite_por_bebe(self):
        for _ in range(alimentacion.MAX_RECORDATORIOS):
            self.assertEqual(self.crear(kind="interval", interval_min=60)[0], 200)
        self.assertEqual(self.crear(kind="interval", interval_min=60)[0], 409)

    def test_06_disparo_por_intervalo_idempotente_y_sin_rafaga(self):
        self.crear(kind="interval", interval_min=180)
        ahora = instant()
        self.assertEqual(self.db.recordatorios_disparar(ahora), 0)
        pasado = (datetime.fromisoformat(ahora) + timedelta(hours=10)).isoformat(timespec="microseconds")
        self.assertEqual(self.db.recordatorios_disparar(pasado), 1)   # 10 h apagado: UN aviso, no tres
        self.assertEqual(self.db.recordatorios_disparar(pasado), 0)   # repetir no duplica
        cola = self.cola()
        self.assertEqual(len(cola), 1)
        self.assertIn("hora de la toma", cola[0]["message"])
        self.assertNotIn("Bebé de prueba", cola[0]["message"])
        proximo = self.get("/api/reminders?child=" + self.bebe)[1]["reminders"][0]["next_due_at"]
        self.assertGreater(proximo, pasado)

    def test_07_horas_fijas_en_hora_de_costa_rica(self):
        # 2026-11-20 12:00 UTC = 06:00 en Costa Rica (UTC-6). La próxima hora fija de 08:00 CR es 14:00 UTC.
        prox = alimentacion.siguiente("fixed", None, ["08:00"], "2026-11-20T12:00:00.000000+00:00")
        self.assertEqual(prox, "2026-11-20T14:00:00.000000+00:00")
        prox = alimentacion.siguiente("fixed", None, ["08:00"], "2026-11-20T14:00:00.000000+00:00")  # justo en la hora: la siguiente es mañana
        self.assertEqual(prox, "2026-11-21T14:00:00.000000+00:00")

    def test_08_desactivado_no_dispara(self):
        self.crear(kind="interval", interval_min=15, enabled=False)
        futuro = (datetime.fromisoformat(instant()) + timedelta(days=2)).isoformat(timespec="microseconds")
        self.assertEqual(self.db.recordatorios_disparar(futuro), 0)


class TestFotos(P5):
    def test_01_subida_galeria_y_descarga_firmada(self):
        evento = uid()
        c, d = self.subir(evento=evento)
        self.assertEqual((c, d["duplicado"], d["child_id"]), (200, False, self.bebe), d)
        c, g = self.get("/api/photos?child=" + self.bebe)
        self.assertEqual(c, 200)
        self.assertEqual([i["id"] for i in g["items"]], [evento])
        self.assertTrue(g["items"][0]["url"].startswith("/api/photo?id=" + evento))
        c, contenido, cab = self.admin.call(g["items"][0]["url"], raw=True)
        self.assertEqual((c, contenido, cab["Content-Type"].split(";")[0]), (200, JPEG, "image/jpeg"))
        self.assertEqual(cab["Cache-Control"], "no-store")

    def test_02_queda_en_auditoria_con_la_ruta_del_objeto_y_sin_url_publica(self):
        evento = uid()
        self.subir(evento=evento)
        filas = self.audit_rows(event_type="FOTO_SEGURIDAD")
        self.assertEqual(len(filas), 1)
        self.assertIn(f"fotos/{evento}.jpg", filas[0]["detail"])
        self.assertNotIn("http", filas[0]["detail"])
        self.assertTrue((self.db.fotos.raiz / f"{evento}.jpg").exists())

    def test_03_reintento_idempotente_y_conflicto(self):
        evento = uid()
        at = instant()
        self.assertFalse(self.subir(evento=evento, at=at)[1]["duplicado"])
        self.assertTrue(self.subir(evento=evento, at=at)[1]["duplicado"])
        self.assertEqual(len(self.audit_rows(event_type="FOTO_SEGURIDAD")), 1)
        self.assertEqual(self.subir(datos=JPEG + b"\x01", evento=evento, at=at)[0], 409)

    def test_04_validaciones(self):
        self.assertEqual(self.subir(datos=b"MZ-no-es-imagen", tipo="image/jpeg")[0], 415)
        self.assertEqual(self.subir(datos=PNG, tipo="image/jpeg")[0], 415)   # el contenido manda, no lo declarado
        self.assertEqual(self.subir(datos=PNG, tipo="image/png")[0], 200)
        self.assertEqual(self.subir(at="2999-01-01T00:00:00+00:00")[0], 400)
        self.assertEqual(self.subir(evento="no-uuid")[0], 400)
        limite, fotos.MAX_BYTES = fotos.MAX_BYTES, 1000     # el servidor responde 413 sin leer el cuerpo
        try:
            self.assertEqual(self.subir(datos=JPEG + b"0" * 2000)[0], 413)
        finally:
            fotos.MAX_BYTES = limite

    def test_05_credenciales_y_asignacion(self):
        self.assertEqual(self.subir(key="clave-falsa")[0], 403)
        self.seed_device(self.bebe, "OTRA-1", started="2026-01-01T00:00:00.000000+00:00")
        self.assertEqual(self.subir(device="OTRA-1")[0], 200)
        self.assertEqual(self.subir(at="2025-01-01T00:00:00+00:00")[0], 409)  # antes de la asignación

    def test_06_aislamiento_entre_familias(self):
        evento = uid()
        self.subir(evento=evento)
        otro, hijo_ajeno, _ = self.second_family_admin()
        url = self.get("/api/photos?child=" + self.bebe)[1]["items"][0]["url"]
        self.assertEqual(self.get("/api/photos?child=" + self.bebe, otro)[0], 404)
        self.assertEqual(otro.call(url)[0], 404)                       # ni con la URL firmada
        self.assertEqual(self.get("/api/photos?child=" + hijo_ajeno)[0], 404)

    def test_07_url_sin_sesion_alterada_o_vencida(self):
        evento = uid()
        self.subir(evento=evento)
        url = self.get("/api/photos?child=" + self.bebe)[1]["items"][0]["url"]
        from apoyo import Browser
        self.assertEqual(Browser(self.url).call(url)[0], 401)         # la firma sola no basta
        self.assertEqual(self.admin.call(url[:-1] + ("0" if url[-1] != "0" else "1"))[0], 403)
        exp = int(time.time()) - 1
        vieja = f"/api/photo?id={evento}&exp={exp}&sig={fotos.firma(self.server.photo_secret, evento, exp)}"
        c, d = self.admin.call(vieja)
        self.assertEqual(c, 403)
        self.assertIn("venció", d["error"])

    def test_08_cuidador_sin_permiso_puede_ver(self):
        self.subir()
        self.assertEqual(self.get("/api/photos?child=" + self.bebe, self.caregiver(False))[0], 200)

    def test_09_paginacion(self):
        self.seed_device(self.bebe, "VIEJA-1")             # asignada desde enero: admite capturas con fecha pasada
        for i in range(3):
            self.assertEqual(self.subir(device="VIEJA-1", at=hace(100 - i))[0], 200)
        c, p1 = self.get("/api/photos?child=" + self.bebe + "&limit=2")
        self.assertEqual((len(p1["items"]), bool(p1["next"])), (2, True))
        c, p2 = self.get("/api/photos?child=" + self.bebe + "&limit=2&before=" + p1["next"].replace("+", "%2B"))
        self.assertEqual((c, len(p2["items"]), p2["next"]), (200, 1, None))
        self.assertEqual(self.get("/api/photos?child=" + self.bebe + "&limit=0")[0], 400)

    def test_10_las_fotos_son_inmutables(self):
        import sqlite3
        self.subir()
        with self.assertRaises(sqlite3.DatabaseError):
            with self.db.connection(True) as db:
                db.execute("delete from photos")


class TestVoz(P5):
    def senal(self, rol, tipo, datos=None, client=None, hijo=None):
        return self.post("/api/voice/signal", dict(child_id=hijo or self.bebe, role=rol, type=tipo, data=datos), client)

    def sondeo(self, rol, despues=0, espera=0, client=None):
        return self.post("/api/voice/poll", dict(child_id=self.bebe, role=rol, after=despues, wait=espera), client)

    def test_01_oferta_respuesta_y_candidatos(self):
        self.assertEqual(self.senal("app", "offer", {"sdp": "oferta"})[0], 200)
        c, d = self.sondeo("cuna")
        self.assertEqual([m["type"] for m in d["messages"]], ["offer"])
        self.assertEqual(self.sondeo("app")[1]["messages"], [])        # nadie recibe sus propios mensajes
        self.senal("cuna", "answer", {"sdp": "respuesta"})
        self.senal("app", "candidate", {"candidate": "c1"})
        self.assertEqual([m["type"] for m in self.sondeo("app")[1]["messages"]], ["answer"])
        ultimo = self.sondeo("cuna")[1]["messages"][-1]["seq"]
        self.assertEqual(self.sondeo("cuna", despues=ultimo)[1]["messages"], [])

    def test_02_una_sola_llamada_y_colgar(self):
        self.senal("app", "offer", {"sdp": "a"})
        c, d = self.senal("app", "offer", {"sdp": "b"})
        self.assertEqual(c, 409)
        self.senal("app", "hangup")
        self.assertEqual(self.senal("app", "offer", {"sdp": "c"})[0], 200)
        self.senal("app", "hangup")
        mensajes = self.sondeo("cuna")[1]["messages"]
        self.assertEqual([m["type"] for m in mensajes], ["hangup"])    # colgar limpia lo pendiente y avisa al otro lado

    def test_03_respuesta_sin_llamada_y_solo_la_app_ofrece(self):
        self.assertEqual(self.senal("cuna", "answer", {"sdp": "x"})[0], 409)
        self.assertEqual(self.senal("cuna", "offer", {"sdp": "x"})[0], 403)

    def test_04_espera_larga_se_despierta_con_un_mensaje(self):
        resultado = {}
        hilo = threading.Thread(target=lambda: resultado.update(r=self.sondeo("cuna", espera=10)))
        hilo.start()
        time.sleep(0.4)
        self.senal("app", "offer", {"sdp": "x"})
        hilo.join(5)
        self.assertEqual([m["type"] for m in resultado["r"][1]["messages"]], ["offer"])

    def test_05_permisos(self):
        sin, con = self.caregiver(False), None
        self.assertEqual(self.senal("app", "offer", {"sdp": "x"}, sin)[0], 403)
        self.assertEqual(self.sondeo("cuna", client=sin)[0], 403)
        from apoyo import Browser
        self.assertEqual(self.sondeo("cuna", client=Browser(self.url))[0], 401)

    def test_06_otra_familia_no_entra_al_canal(self):
        otro, _, _ = self.second_family_admin()
        self.assertEqual(self.senal("app", "offer", {"sdp": "x"}, otro)[0], 404)
        self.assertEqual(self.sondeo("cuna", client=otro)[0], 404)

    def test_07_cuna_real_con_clave_de_dispositivo(self):
        from apoyo import Browser
        cab = {"Authorization": "Bearer " + self.keys["CUNA-1"]}
        self.senal("app", "offer", {"sdp": "x"})
        c, d = Browser(self.url).call("/api/voice/poll", dict(device_id="CUNA-1", after=0, wait=0), cab)
        self.assertEqual((c, [m["type"] for m in d["messages"]]), (200, ["offer"]))
        self.assertEqual(Browser(self.url).call("/api/voice/signal", dict(device_id="CUNA-1", type="answer", data={"sdp": "r"}), cab)[0], 200)
        self.assertEqual(Browser(self.url).call("/api/voice/poll", dict(device_id="CUNA-1"), {"Authorization": "Bearer mala"})[0], 403)

    def test_08_mensaje_grande_y_ttl(self):
        self.assertEqual(self.senal("app", "offer", {"sdp": "x" * 20000})[0], 400)  # supera el límite del cuerpo HTTP
        reloj = [0.0]
        canales = Canales(reloj=lambda: reloj[0])
        canales.enviar("b", "app", "offer", {"sdp": "x"})
        reloj[0] = 60
        self.assertEqual(canales.esperar("b", "cuna", 0, 0)["messages"], [])        # la oferta vieja ya no se entrega
        reloj[0] = 200
        self.assertEqual(canales.enviar("b", "app", "offer", {"sdp": "y"})["seq"], 2)  # y la llamada caída se libera

    def test_09_configuracion_ice(self):
        self.assertEqual(self.get("/api/voice/config")[1], {"ice_servers": []})
        self.server.ice_servers = ["stun:ejemplo.test:3478"]
        self.assertEqual(self.get("/api/voice/config")[1]["ice_servers"][0]["urls"], ["stun:ejemplo.test:3478"])


class TestEstaticos(P5):
    def test_01_paginas_y_scripts_se_sirven_con_csp_estricta(self):
        for ruta, tipo in (("/p5.js", "text/javascript"), ("/cuna-voz", "text/html"), ("/cuna-voz.js", "text/javascript")):
            c, cuerpo, cab = self.admin.call(ruta, raw=True)
            self.assertEqual((c, cab["Content-Type"].split(";")[0]), (200, tipo), ruta)
            self.assertIn("script-src 'self'", cab["Content-Security-Policy"])
        c, cuerpo, _ = self.admin.call("/", raw=True)
        for marca in (b'data-tab="feeding"', b'data-tab="photos"', b"/p5.js"):
            self.assertIn(marca, cuerpo)
        self.assertNotIn(b" style=", cuerpo)   # la CSP no permite estilos en línea

    def test_02_las_rutas_nuevas_exigen_sesion(self):
        from apoyo import Browser
        anonimo = Browser(self.url)
        for ruta in ("/api/bottle?child=" + self.bebe, "/api/reminders?child=" + self.bebe, "/api/photos?child=" + self.bebe, "/api/voice/config"):
            self.assertEqual(anonimo.call(ruta)[0], 401, ruta)
