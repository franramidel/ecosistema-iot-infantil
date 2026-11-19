"""Pruebas de SCRUM-24: alertas por Telegram.

Todas son simulaciones locales. Un servidor HTTP falso hace el papel de api.telegram.org. Ninguna prueba
sale a Internet ni usa un token real. El token de abajo se arma en tiempo de ejecución y no sirve en Telegram.
"""
import io
import json
import re
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from apoyo import ADMIN, Base, Browser
from apoyo_telegram import TOKEN, Reloj, TelegramFalso
from app import Application
from cuna import notificaciones
from cuna.common import Problem, uid
from cuna.local import Local
from cuna.procesador import Procesador, ProcesadorOcupado
from cuna.telegram import ClienteTelegram, ConfigError, ErrorTelegram, base_valida, token_desde

NEST = Path(__file__).resolve().parent.parent


class TelegramBase(Base):
    def setUp(self):
        super().setUp()
        self.falso = TelegramFalso()
        self.addCleanup(self.falso.cerrar)
        self.reloj = Reloj()
        self.cliente = ClienteTelegram(TOKEN, self.falso.base, timeout=2)
        self.proc = Procesador(self.db, self.cliente, "proc-1", self.reloj, salida=io.StringIO())
        self.admin_id = self.miembro(ADMIN[0])

    def miembro(self, correo):
        with self.db.connection() as db:
            return db.execute("select id from members where email=?", (correo,)).fetchone()[0]

    def vincular(self, miembro=None, chat=5001):
        """Vincula de verdad: código por el almacén, mensaje al bot falso, procesador lo recibe."""
        miembro = miembro or self.admin_id
        codigo = self.db.telegram_codigo({"id": miembro}, ahora=self.reloj())["codigo"]
        self.falso.mensaje("/start " + codigo, chat=chat)
        self.proc.procesar_actualizaciones(0)
        return codigo

    def pedir(self, clave="k1", usuario=None, **extra):
        datos = dict(titulo="Temperatura alta", dispositivo="CUNA-1", estado="Crítico", ocurrido=self.reloj())
        datos.update(extra)
        return self.db.solicitar_notificacion(usuario or self.admin_id, clave, ahora=self.reloj(), **datos)

    def fila(self, numero):
        with self.db.connection() as db:
            return dict(db.execute("select * from notification_queue where id=?", (numero,)).fetchone())

    def enlace(self, miembro=None):
        with self.db.connection() as db:
            return db.execute("select * from telegram_links where member_id=?", (miembro or self.admin_id,)).fetchone()


class TestConfiguracion(TelegramBase):
    def test_01_token_configurado(self):
        self.assertEqual(token_desde({"TELEGRAM_BOT_TOKEN": TOKEN}), TOKEN)

    def test_02_token_ausente_o_mal_formado(self):
        with self.assertRaises(ConfigError) as ctx:
            token_desde({})
        self.assertIn("TELEGRAM_BOT_TOKEN", str(ctx.exception))
        with self.assertRaises(ConfigError):
            token_desde({"TELEGRAM_BOT_TOKEN": "no-es-un-token"})
        with self.assertRaises(ConfigError):
            ClienteTelegram("basura")

    def test_03_solo_https_salvo_localhost(self):
        self.assertTrue(base_valida("https://api.telegram.org"))
        self.assertTrue(base_valida("http://127.0.0.1:9999"))
        for mala in ("http://api.telegram.org", "ftp://127.0.0.1", "http://evil.example"):
            with self.assertRaises(ConfigError):
                base_valida(mala)

    def test_04_ni_el_ejemplo_ni_el_codigo_contienen_tokens(self):
        patron = re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{30,}\b")
        revisados = 0
        for carpeta in ("cuna", "web", "docs", "supabase"):
            for archivo in (NEST / carpeta).rglob("*"):
                if archivo.is_file() and archivo.suffix in (".py", ".js", ".html", ".md", ".sql", ".css"):
                    revisados += 1
                    self.assertIsNone(patron.search(archivo.read_text(encoding="utf-8", errors="ignore")), archivo.name)
        for suelto in ("app.py", "telegram_worker.py", ".env.example"):
            revisados += 1
            self.assertIsNone(patron.search((NEST / suelto).read_text(encoding="utf-8")), suelto)
        self.assertGreater(revisados, 20)
        self.assertIn("TELEGRAM_BOT_TOKEN=", (NEST / ".env.example").read_text(encoding="utf-8"))

    def test_05_el_token_no_sale_en_errores_ni_en_la_cola(self):
        malo = ClienteTelegram(TOKEN, self.falso.base)
        malo._token = TOKEN
        self.falso.token = "otro:" + "z" * 30
        with self.assertRaises(ErrorTelegram) as ctx:
            malo.enviar(1, "hola")
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.falso.token = TOKEN
        self.vincular()
        self.falso.guion.append((400, {"ok": False, "description": "bad " + TOKEN}))
        numero = self.pedir()["id"]
        self.proc.procesar_cola()
        self.assertNotIn(TOKEN, json.dumps(self.fila(numero)))
        self.assertNotIn(TOKEN, self.proc.salida.getvalue())


class TestVinculacion(TelegramBase):
    def test_06_vinculacion_valida(self):
        self.vincular()
        self.assertEqual(self.enlace()["chat_id"], "5001")
        self.assertIn("Cuenta vinculada", self.falso.textos()[-1])
        self.assertTrue(self.db.telegram_estado({"id": self.admin_id})["vinculado"])

    def test_07_codigo_expirado(self):
        codigo = self.db.telegram_codigo({"id": self.admin_id}, ahora=self.reloj())["codigo"]
        self.reloj.avanzar(601)
        self.falso.mensaje("/start " + codigo)
        self.proc.procesar_actualizaciones(0)
        self.assertIsNone(self.enlace()["chat_id"])
        self.assertIn("expiró", self.falso.textos()[-1])

    def test_08_codigo_de_un_solo_uso(self):
        codigo = self.vincular()
        self.falso.mensaje("/start " + codigo, chat=6002)
        self.proc.procesar_actualizaciones(0)
        self.assertIn("no es válido", self.falso.textos()[-1])
        with self.db.connection() as db:
            self.assertEqual(db.execute("select count(*) from telegram_links where chat_id='6002'").fetchone()[0], 0)

    def test_09_codigo_incorrecto_y_bloqueo_por_intentos(self):
        self.db.telegram_codigo({"id": self.admin_id}, ahora=self.reloj())
        for _ in range(notificaciones.MAX_FALLOS):
            self.falso.mensaje("ABCDEFGH", chat=7001)
        self.proc.procesar_actualizaciones(0)
        self.assertEqual(self.falso.textos().count("Ese código no es válido. Genera uno nuevo en la plataforma."), notificaciones.MAX_FALLOS)
        real = self.db.telegram_codigo({"id": self.admin_id}, ahora=self.reloj())["codigo"]
        estado, _ = self.db.telegram_vincular(real, 7001, 7001, ahora=self.reloj())
        self.assertEqual(estado, "bloqueado")
        self.reloj.avanzar(16 * 60)
        real = self.db.telegram_codigo({"id": self.admin_id}, ahora=self.reloj())["codigo"]
        estado, _ = self.db.telegram_vincular(real, 7001, 7001, ahora=self.reloj())
        self.assertEqual(estado, "vinculado")

    def test_10_sin_sesion_no_se_puede_vincular_ni_consultar(self):
        anonimo = Browser(self.url)
        for ruta, cuerpo in (("/api/telegram", None), ("/api/telegram/link", {}), ("/api/telegram/unlink", {}), ("/api/telegram/test", {})):
            self.assertEqual(anonimo.call(ruta, cuerpo)[0], 401, ruta)

    def test_11_el_codigo_solo_vincula_a_su_dueno(self):
        otro, _, _ = self.second_family_admin()
        ajeno = self.miembro("otra.familia@example.test")
        codigo = self.db.telegram_codigo({"id": self.admin_id}, ahora=self.reloj())["codigo"]
        self.falso.mensaje("/start " + codigo, chat=8001)
        self.proc.procesar_actualizaciones(0)
        self.assertEqual(self.enlace()["chat_id"], "8001")
        self.assertIsNone(self.enlace(ajeno))
        # Quien ya tiene un chat vinculado no puede apropiarse del chat de otro.
        codigo2 = self.db.telegram_codigo({"id": ajeno}, ahora=self.reloj())["codigo"]
        estado, _ = self.db.telegram_vincular(codigo2, 8001, 8001, ahora=self.reloj())
        self.assertEqual(estado, "chat_en_uso")
        self.assertIsNone(self.enlace(ajeno)["chat_id"])

    def test_12_desvincular(self):
        self.vincular()
        codigo, = [self.db.telegram_codigo({"id": self.admin_id}, ahora=self.reloj())["codigo"]]
        estado, datos = self.admin.call("/api/telegram/unlink", {})
        self.assertEqual(estado, 200)
        self.assertIsNone(self.enlace()["chat_id"])
        self.assertFalse(self.admin.call("/api/telegram")[1]["vinculado"])
        estado, _ = self.db.telegram_vincular(codigo, 5001, 5001, ahora=self.reloj())
        self.assertEqual(estado, "invalido", "desvincular revoca los códigos abiertos")
        self.assertEqual(self.pedir("tras-desvincular")["estado"], "sin_vinculo")

    def test_13_nuevo_codigo_anula_el_anterior_y_solo_se_guarda_el_hash(self):
        primero = self.db.telegram_codigo({"id": self.admin_id}, ahora=self.reloj())["codigo"]
        segundo = self.db.telegram_codigo({"id": self.admin_id}, ahora=self.reloj())["codigo"]
        self.assertNotEqual(primero, segundo)
        self.assertEqual(len(segundo), 8)
        with self.db.connection() as db:
            guardado = json.dumps([tuple(r) for r in db.execute("select * from telegram_codes")])
        self.assertNotIn(primero, guardado)
        self.assertNotIn(segundo, guardado)
        self.assertEqual(self.db.telegram_vincular(primero, 1, 1, ahora=self.reloj())[0], "invalido")
        self.assertEqual(self.db.telegram_vincular(segundo, 1, 1, ahora=self.reloj())[0], "vinculado")

    def test_14_solo_chats_privados_de_personas(self):
        codigo = self.db.telegram_codigo({"id": self.admin_id}, ahora=self.reloj())["codigo"]
        self.falso.mensaje("/start " + codigo, chat=-900, tipo="group")
        self.falso.mensaje("/start " + codigo, chat=900, bot=True)
        self.falso.mensaje("/start " + codigo, chat=901, usuario=902)
        self.proc.procesar_actualizaciones(0)
        self.assertIsNone(self.enlace()["chat_id"])
        self.assertEqual(self.falso.enviados, [])

    def test_15_offset_persistente_y_sin_reproceso(self):
        self.vincular()
        self.assertEqual(self.db.telegram_offset(), 102)
        enviados = len(self.falso.enviados)
        self.proc.procesar_actualizaciones(0)
        self.assertEqual(len(self.falso.enviados), enviados)

    def test_16_solo_un_procesador_escucha_al_bot(self):
        otro = Procesador(self.db, self.cliente, "proc-2", self.reloj, salida=self.proc.salida)
        self.proc.ciclo(0)
        with self.assertRaises(ProcesadorOcupado):
            otro.ciclo(0)
        self.db.telegram_procesador("proc-1", ahora=self.reloj(), liberar=True)
        otro.ciclo(0)


class TestEnvio(TelegramBase):
    def test_17_envio_exitoso_con_datos_reales_y_html_escapado(self):
        self.vincular()
        n = len(self.falso.enviados)
        r = self.pedir(titulo="Temp <b>alta</b> & riesgo", dispositivo="CUNA<1>")
        self.assertEqual((r["estado"], r["vinculado"], r["duplicado"]), ("pendiente", True, False))
        self.assertEqual(self.proc.procesar_cola()["enviadas"], 1)
        texto = self.falso.enviados[n]["text"]
        self.assertEqual(self.falso.enviados[n]["chat_id"], "5001")
        self.assertEqual(self.falso.enviados[n]["parse_mode"], "HTML")
        self.assertIn("Temp &lt;b&gt;alta&lt;/b&gt; &amp; riesgo", texto)
        self.assertIn("Dispositivo: CUNA&lt;1&gt;", texto)
        self.assertIn("Estado: Crítico", texto)
        self.assertEqual(self.fila(r["id"])["status"], "enviada")

    def test_18_hora_de_costa_rica_y_sin_datos_del_bebe(self):
        self.child("Bebé Secreto")
        texto = notificaciones.formatear_alerta("Aviso", "D1", "2026-03-04T18:30:00+00:00", "Informativo")
        self.assertIn("Fecha: 04/03/2026", texto)
        self.assertIn("Hora: 12:30", texto)
        self.assertNotIn("Secreto", texto)
        for malo in (dict(titulo=""), dict(titulo="x" * 151), dict(titulo="a", estado="Rojo"), dict(titulo="a", dispositivo="d" * 65)):
            with self.assertRaises(Problem):
                notificaciones.formatear_alerta(**malo)

    def test_19_error_de_red_es_recuperable(self):
        self.vincular()
        r = self.pedir()
        caido = Procesador(self.db, ClienteTelegram(TOKEN, "http://127.0.0.1:9", timeout=1), "p", self.reloj, salida=self.proc.salida)
        self.assertEqual(caido.procesar_cola()["reintentos"], 1)
        fila = self.fila(r["id"])
        self.assertEqual((fila["status"], fila["attempts"]), ("error_recuperable", 1))
        self.assertGreater(fila["next_attempt_at"], self.reloj())

    def test_20_tiempo_agotado_es_recuperable(self):
        self.vincular()
        r = self.pedir()
        self.falso.demora = 1.2
        lento = Procesador(self.db, ClienteTelegram(TOKEN, self.falso.base, timeout=0.3), "p", self.reloj, salida=self.proc.salida)
        lento.procesar_cola()
        self.assertEqual(self.fila(r["id"])["status"], "error_recuperable")
        self.assertIn("tiempo de espera", self.fila(r["id"])["last_error"])

    def test_21_token_invalido_detiene_el_envio_sin_perder_la_cola(self):
        self.vincular()
        a, b = self.pedir("a")["id"], self.pedir("b")["id"]
        self.falso.token = "123456:" + "q" * 30
        antes = len(self.falso.llamadas)
        self.proc.procesar_cola()
        self.assertTrue(self.proc.token_invalido)
        self.assertEqual(len(self.falso.llamadas) - antes, 1, "tras el 401 no se insiste con las demás")
        self.assertEqual(sorted([self.fila(a)["status"], self.fila(b)["status"]]), ["error_recuperable", "procesando"])
        self.falso.token = TOKEN
        self.proc.token_invalido = False
        self.reloj.avanzar(3600)
        self.assertEqual(self.proc.procesar_cola()["enviadas"], 2, "con el token corregido nada se perdió")

    def test_22_usuario_sin_telegram(self):
        r = self.pedir()
        self.assertEqual((r["estado"], r["vinculado"]), ("sin_vinculo", False))
        self.assertEqual(self.proc.procesar_cola(), {"enviadas": 0, "reintentos": 0, "permanentes": 0})
        self.assertEqual(self.falso.enviados, [])
        self.assertIn("no tiene Telegram", self.fila(r["id"])["last_error"])

    def test_23_error_temporal_y_recuperacion(self):
        self.vincular()
        r = self.pedir()
        self.falso.guion.append((503, {"ok": False, "description": "caído"}))
        self.proc.procesar_cola()
        self.assertEqual(self.fila(r["id"])["status"], "error_recuperable")
        self.assertEqual(self.proc.procesar_cola()["enviadas"], 0, "aún no toca reintentar")
        self.reloj.avanzar(31)
        self.assertEqual(self.proc.procesar_cola()["enviadas"], 1)
        fila = self.fila(r["id"])
        self.assertEqual((fila["status"], fila["attempts"], fila["last_error"]), ("enviada", 2, None))

    def test_24_error_permanente_no_se_reintenta_y_403_marca_bloqueo(self):
        self.vincular()
        r = self.pedir()
        self.falso.guion.append((403, {"ok": False, "description": "Forbidden: bot was blocked by the user"}))
        self.proc.procesar_cola()
        self.assertEqual(self.fila(r["id"])["status"], "error_permanente")
        self.assertIsNotNone(self.enlace()["blocked_at"])
        self.assertTrue(self.db.telegram_estado({"id": self.admin_id}, ahora=self.reloj())["bloqueado"])
        self.reloj.avanzar(7200)
        n = len(self.falso.llamadas)
        self.proc.procesar_cola()
        self.assertEqual(len(self.falso.llamadas), n)

    def test_25_reintentos_limitados(self):
        self.vincular()
        r = self.pedir()
        for _ in range(notificaciones.MAX_INTENTOS + 3):
            self.falso.guion.append((500, {"ok": False}))
        for _ in range(notificaciones.MAX_INTENTOS + 3):
            self.proc.procesar_cola()
            self.reloj.avanzar(3600)
        fila = self.fila(r["id"])
        self.assertEqual((fila["status"], fila["attempts"]), ("error_permanente", notificaciones.MAX_INTENTOS))
        self.assertIn("Reintentos agotados", fila["last_error"])

    def test_26_429_respeta_retry_after(self):
        self.vincular()
        r = self.pedir()
        self.falso.guion.append((429, {"ok": False, "parameters": {"retry_after": 600}}))
        self.proc.procesar_cola()
        espera = (datetime.fromisoformat(self.fila(r["id"])["next_attempt_at"]) - datetime.fromisoformat(self.reloj())).total_seconds()
        self.assertGreaterEqual(espera, 599)

    def test_27_solicitudes_duplicadas(self):
        self.vincular()
        a = self.pedir("evento-77")
        b = self.pedir("evento-77")
        self.assertEqual((a["duplicado"], b["duplicado"], a["id"]), (False, True, b["id"]))
        with self.assertRaises(Problem) as ctx:
            self.pedir("evento-77", titulo="Otro contenido")
        self.assertEqual(ctx.exception.status, 409)
        n = len(self.falso.enviados)
        self.proc.procesar_cola()
        self.proc.procesar_cola()
        self.assertEqual(len(self.falso.enviados) - n, 1)
        with self.assertRaises(Problem):
            self.pedir("")
        with self.assertRaises(Problem):
            self.pedir("k" * 129)

    def test_28_solicitud_pendiente_sobrevive_al_reinicio(self):
        self.vincular()
        r = self.pedir()
        self.reiniciar()
        self.assertEqual(self.fila(r["id"])["status"], "pendiente")
        nuevo = Procesador(self.db, self.cliente, "p2", self.reloj, salida=self.proc.salida)
        self.assertEqual(nuevo.procesar_cola()["enviadas"], 1)

    def test_29_reinicio_del_procesador_recupera_solicitudes_abandonadas(self):
        self.vincular()
        r = self.pedir()
        tomado = self.db.cola_reclamar("proceso-muerto", ahora=self.reloj())
        self.assertEqual(len(tomado), 1)
        self.assertEqual(self.proc.procesar_cola()["enviadas"], 0, "el arrendamiento sigue vigente")
        self.reloj.avanzar(notificaciones.ARRENDAMIENTO.total_seconds() + 1)
        self.assertEqual(self.proc.procesar_cola()["enviadas"], 1)
        self.assertEqual(self.fila(r["id"])["attempts"], 2)
        # El proceso viejo ya no puede cerrar la solicitud con un resultado tardío.
        self.assertIsNone(self.db.cola_resultado(tomado[0], tipo="permanente", error="tarde", ahora=self.reloj()))
        self.assertEqual(self.fila(r["id"])["status"], "enviada")

    def test_30_interrupciones_repetidas_no_generan_bucle_infinito(self):
        self.vincular()
        r = self.pedir()
        for _ in range(notificaciones.MAX_INTENTOS + 2):
            self.db.cola_reclamar("muere", ahora=self.reloj())
            self.reloj.avanzar(notificaciones.ARRENDAMIENTO.total_seconds() + 1)
        self.db.cola_reclamar("otro", ahora=self.reloj())
        self.assertEqual(self.fila(r["id"])["status"], "error_permanente")

    def test_31_procesamiento_concurrente_no_duplica(self):
        self.vincular()
        ids = [self.pedir(f"c{i}")["id"] for i in range(24)]
        n = len(self.falso.enviados)
        reclamados, fallos = [], []

        def trabajar(nombre):
            try:
                reclamados.extend(t["id"] for t in self.db.cola_reclamar(nombre, 24, ahora=self.reloj()))
            except Exception as exc:
                fallos.append(exc)
        hilos = [threading.Thread(target=trabajar, args=(f"h{i}",)) for i in range(6)]
        [h.start() for h in hilos]
        [h.join() for h in hilos]
        self.assertEqual(fallos, [])
        self.assertEqual(sorted(reclamados), sorted(ids))
        self.assertEqual(len(set(reclamados)), len(reclamados))
        # Dos procesadores completos a la vez tampoco repiten mensajes.
        self.reloj.avanzar(notificaciones.ARRENDAMIENTO.total_seconds() + 1)
        otro = Procesador(self.db, self.cliente, "p9", self.reloj, salida=self.proc.salida)
        hilos = [threading.Thread(target=p.procesar_cola, args=(24,)) for p in (self.proc, otro)]
        [h.start() for h in hilos]
        [h.join() for h in hilos]
        self.assertEqual(len(self.falso.enviados) - n, 24)

    def test_32_funciona_sin_servidor_web_ni_navegador(self):
        self.vincular()
        r = self.pedir()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        independiente = Procesador(Local(self.path), self.cliente, "solo", self.reloj, salida=self.proc.salida)
        self.assertEqual(independiente.procesar_cola()["enviadas"], 1)
        self.assertEqual(self.fila(r["id"])["status"], "enviada")
        self.arrancar()  # para que tearDown encuentre un servidor vivo


class TestIntegracion(TelegramBase):
    def evento(self):
        niño = self.child("Bebé")
        self.device(niño, "CUNA-1")
        self.assertEqual(self.send_audit(self.audit_event("CUNA-1", "ALERTA_MANUAL", "Aviso"))[0], 200)
        return self.audit_rows(event_type="ALERTA_MANUAL")[0]["id"]

    def test_33_auditoria_registra_envio_y_fallo(self):
        self.vincular()
        evento = self.evento()
        ok = self.pedir("ok", evento_id=evento)["id"]
        self.proc.procesar_cola()
        enviado = self.audit_rows(event_type="NOTIFICACION_ENVIADA")
        self.assertEqual(len(enviado), 1)
        self.assertEqual(enviado[0]["device_id"], "CUNA-1")
        self.assertNotIn("Aviso", enviado[0]["detail"])
        self.assertEqual(self.fila(ok)["event_id"], evento)
        self.falso.guion.append((400, {"ok": False, "description": "chat not found"}))
        self.pedir("mal", evento_id=evento)
        self.proc.procesar_cola()
        self.assertEqual(len(self.audit_rows(event_type="NOTIFICACION_FALLIDA")), 1)
        self.assertEqual(self.audit(f"tipo=NOTIFICACION_ENVIADA")[0], 200)

    def test_34_un_dispositivo_no_puede_falsificar_eventos_de_notificacion(self):
        self.evento()
        for tipo in ("NOTIFICACION_ENVIADA", "NOTIFICACION_FALLIDA"):
            codigo, _ = self.send_audit(self.audit_event("CUNA-1", tipo, "falso"))
            self.assertEqual(codigo, 403, tipo)

    def test_35_no_toda_auditoria_genera_notificacion(self):
        self.vincular()
        self.evento()
        with self.db.connection() as db:
            self.assertEqual(db.execute("select count(*) from notification_queue").fetchone()[0], 0)

    def test_36_interfaz_para_scrum_25_con_evento_de_ejemplo(self):
        self.vincular()
        evento = self.evento()
        r = self.db.solicitar_notificacion(self.admin_id, "scrum25:" + evento, titulo="Temperatura fuera de rango",
                                           dispositivo="CUNA-1", ocurrido="2026-11-18T20:00:00+00:00",
                                           estado="Crítico", evento_id=evento, ahora=self.reloj())
        self.assertEqual(set(r), {"id", "estado", "duplicado", "vinculado"})
        n = len(self.falso.enviados)
        self.proc.procesar_cola()
        self.assertIn("Hora: 14:00", self.falso.enviados[n]["text"])
        with self.assertRaises(Problem) as ctx:
            self.db.solicitar_notificacion("no-existe", "x", titulo="a")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(Problem):
            self.db.solicitar_notificacion(self.admin_id, "x", titulo="a", estado="Rojo")

    def test_37_evento_de_otra_familia_se_rechaza(self):
        otro, hijo, llave = self.second_family_admin()
        self.assertEqual(self.send_audit(self.audit_event("AJENO-1", "ALERTA_MANUAL", "x"), llave)[0], 200)
        ajeno = self.audit_rows(device_id="AJENO-1")[0]["id"]
        with self.assertRaises(Problem) as ctx:
            self.pedir("cruce", evento_id=ajeno)
        self.assertEqual(ctx.exception.status, 404)

    def test_38_aislamiento_entre_familias_en_estado_y_cola(self):
        otro, _, _ = self.second_family_admin()
        self.vincular()
        self.pedir("solo-mia")
        self.assertFalse(otro.call("/api/telegram")[1]["vinculado"])
        self.assertEqual(otro.call("/api/telegram/test", {})[0], 409)
        mismo = self.pedir("clave-comun")["id"]
        ajeno = self.db.solicitar_notificacion(self.miembro("otra.familia@example.test"), "clave-comun", titulo="Otra",
                                               ahora=self.reloj())
        self.assertNotEqual(mismo, ajeno["id"], "la clave de idempotencia es por familia")


class TestProcesoAparte(TelegramBase):
    def correr(self, **entorno):
        import os
        import subprocess
        import sys
        base = {k: v for k, v in os.environ.items() if not k.startswith(("TELEGRAM_", "CUNA_"))}
        base.update(CUNA_DB=str(self.path), PYTHONDONTWRITEBYTECODE="1", **entorno)
        return subprocess.run([sys.executable, str(NEST / "telegram_worker.py"), "--una-vez"], env=base,
                              capture_output=True, text=True, timeout=60, cwd=str(NEST))

    def test_47_telegram_worker_es_un_proceso_independiente_del_servidor(self):
        self.vincular()
        r = self.pedir("proceso")
        resultado = self.correr(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_API_BASE=self.falso.base)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertEqual(self.fila(r["id"])["status"], "enviada")
        self.assertIn("Procesador iniciado para @cuna_prueba_bot", resultado.stderr)
        self.assertNotIn(TOKEN, resultado.stdout + resultado.stderr)

    def test_48_sin_token_el_procesador_no_arranca_y_lo_dice(self):
        resultado = self.correr()
        self.assertEqual(resultado.returncode, 2)
        self.assertIn("TELEGRAM_BOT_TOKEN", resultado.stderr)
        self.assertNotIn("Traceback", resultado.stderr)

    def test_49_con_token_rechazado_termina_sin_traza_ni_filtrar_el_token(self):
        self.falso.token = "123456:" + "w" * 30
        resultado = self.correr(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_API_BASE=self.falso.base)
        self.assertEqual(resultado.returncode, 2)
        self.assertNotIn(TOKEN, resultado.stdout + resultado.stderr)
        self.assertNotIn("Traceback", resultado.stderr)


class TestSolicitarAlerta(TelegramBase):
    def correr(self, *args, **entorno):
        import os
        import subprocess
        import sys
        base = {k: v for k, v in os.environ.items() if not k.startswith(("TELEGRAM_", "CUNA_", "SUPABASE_"))}
        base.update(CUNA_DB=str(self.path), PYTHONDONTWRITEBYTECODE="1", **entorno)
        return subprocess.run([sys.executable, str(NEST / "solicitar_alerta.py"), *args], env=base,
                              capture_output=True, text=True, timeout=60, cwd=str(NEST))

    def test_50_solicitar_alerta_sin_navegador_la_envia_el_procesador(self):
        self.vincular()
        r = self.correr("--correo", ADMIN[0])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("'estado': 'pendiente'", r.stdout)
        n = len(self.falso.enviados)
        self.reloj.avanzar(30)  # el proceso aparte usó el reloj real, que ya pasó al del procesador de la prueba
        self.assertEqual(self.proc.procesar_cola()["enviadas"], 1)
        self.assertIn("Alerta de prueba sin navegador", self.falso.enviados[n]["text"])

    def test_51_solicitar_alerta_sin_vinculo_o_con_datos_malos_avisa_sin_traza(self):
        sin = self.correr("--correo", ADMIN[0])
        self.assertEqual(sin.returncode, 3)
        self.assertNotIn("Traceback", sin.stderr)
        self.assertEqual(self.correr("--correo", "nadie@example.test").returncode, 2)
        self.assertEqual(self.correr().returncode, 2)
        self.assertEqual(self.correr("--supabase", "--usuario", uid()).returncode, 2)

    def test_52_con_supabase_exige_confirmacion_antes_de_escribir_y_no_imprime_secretos(self):
        entorno = dict(SUPABASE_URL="http://127.0.0.1:9", SUPABASE_PUBLISHABLE_KEY="clave-publica-sintetica",
                       SUPABASE_SECRET_KEY="clave-secreta-sintetica")
        sin = self.correr("--supabase", "--usuario", uid(), **entorno)
        self.assertEqual(sin.returncode, 4, sin.stderr)
        salida = sin.stdout + sin.stderr
        self.assertIn("127.0.0.1", salida)  # muestra solo el servidor
        self.assertNotIn("clave-secreta-sintetica", salida)
        self.assertNotIn("clave-publica-sintetica", salida)
        self.assertNotIn("Traceback", salida)
        con = self.correr("--supabase", "--usuario", uid(), "--confirmo-escribir-en-supabase", **entorno)
        # Con la confirmación sí intenta escribir. El servidor no existe, así que falla sin traza y sin secretos.
        self.assertEqual(con.returncode, 2, con.stderr)
        self.assertNotIn("clave-secreta-sintetica", con.stdout + con.stderr)
        self.assertNotIn("Traceback", con.stderr)


class TestIntegracionExcel(TelegramBase):
    def test_46_los_eventos_de_notificacion_salen_en_el_excel_de_scrum_23(self):
        import openpyxl
        self.vincular()
        niño = self.child("Bebé")
        self.device(niño, "CUNA-1")
        self.send_audit(self.audit_event("CUNA-1", "ALERTA_MANUAL", "Aviso"))
        evento = self.audit_rows(event_type="ALERTA_MANUAL")[0]["id"]
        self.pedir("excel", evento_id=evento)
        self.proc.procesar_cola()
        estado, contenido, _ = self.admin.call("/api/export/xlsx", raw=True)
        self.assertEqual(estado, 200)
        libro = openpyxl.load_workbook(io.BytesIO(contenido), read_only=True)
        celdas = [c for hoja in libro.worksheets for fila in hoja.iter_rows(values_only=True) for c in fila]
        self.assertIn("NOTIFICACION_ENVIADA", celdas)
        self.assertNotIn(TOKEN, " ".join(str(c) for c in celdas))


class TestEndpoints(TelegramBase):
    def test_39_endpoints_vincular_probar_y_limitar(self):
        self.server.bot_username = "cuna_prueba_bot"
        codigo, datos = self.admin.call("/api/telegram/link", {})
        self.assertEqual(codigo, 200)
        self.assertEqual(len(datos["codigo"]), 8)
        self.assertEqual(datos["enlace"], f"https://t.me/cuna_prueba_bot?start={datos['codigo']}")
        self.assertEqual(self.admin.call("/api/telegram/test", {})[0], 409, "sin vínculo no hay prueba")
        self.falso.mensaje("/start " + datos["codigo"])
        self.proc.reloj = lambda: __import__("cuna.common", fromlist=["instant"]).instant()
        self.proc.procesar_actualizaciones(0)
        self.assertTrue(self.admin.call("/api/telegram")[1]["vinculado"])
        self.assertEqual(self.admin.call("/api/telegram/test", {})[0], 200)
        self.assertEqual(self.admin.call("/api/telegram/test", {})[0], 429)
        self.proc.procesar_cola()
        self.assertEqual(self.admin.call("/api/telegram")[1]["ultima_prueba"]["estado"], "enviada")

    def test_40_proteccion_csrf_y_formato(self):
        status, _ = self.admin.call("/api/telegram/link", {}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.admin.call("/api/telegram/unlink", {}, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertNotIn(self.admin.call("/api/telegram")[0], (500,))

    def test_41_el_token_nunca_aparece_en_respuestas_http(self):
        self.server.bot_username = "cuna_prueba_bot"
        cuerpos = [self.admin.call("/api/telegram")[1], self.admin.call("/api/telegram/link", {})[1], self.admin.call("/api/state")[1]]
        self.assertNotIn(TOKEN, json.dumps(cuerpos))
        self.assertNotIn("TELEGRAM_BOT_TOKEN", json.dumps(cuerpos))

    def test_42_con_base_en_la_nube_responde_501(self):
        class SinTelegram:
            pass
        servidor = Application(("127.0.0.1", 0), self.db)
        servidor.store = SinTelegram()
        servidor.session = lambda sid: {"id": self.admin_id}
        hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
        hilo.start()
        try:
            cliente = Browser("http://127.0.0.1:" + str(servidor.server_port))
            cliente.opener.addheaders = [("Cookie", "cuna=falsa")]
            self.assertEqual(cliente.call("/api/telegram")[0], 501)
        finally:
            servidor.shutdown()
            servidor.server_close()


class TestMigracion(TelegramBase):
    def test_43_migracion_nueva_y_desde_version_anterior(self):
        with self.db.connection() as db:
            self.assertEqual(db.execute("pragma user_version").fetchone()[0], 4)
            tablas = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
        self.assertTrue({"telegram_links", "telegram_codes", "telegram_attempts", "telegram_state", "telegram_worker", "notification_queue"} <= tablas)
        self.vincular()
        r = self.pedir()
        # Se simula una base en la versión 3: sin tablas de Telegram.
        conexion = sqlite3.connect(self.path)
        for tabla in ("notification_queue", "telegram_worker", "telegram_state", "telegram_attempts", "telegram_codes", "telegram_links"):
            conexion.execute(f"drop table {tabla}")
        conexion.execute("pragma user_version=3")
        conexion.commit()
        conexion.close()
        migrada = Local(self.path)
        with migrada.connection() as db:
            self.assertEqual(db.execute("pragma user_version").fetchone()[0], 4)
            self.assertEqual(db.execute("select count(*) from notification_queue").fetchone()[0], 0)
            self.assertGreater(db.execute("select count(*) from members").fetchone()[0], 0)
        self.assertIsNotNone(r)

    def test_44_restricciones_de_la_base(self):
        with self.db.connection(True) as db:
            familia = db.execute("select family_id from members limit 1").fetchone()[0]
            for estado, intentos in (("inventado", 0),):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("insert into notification_queue(id,family_id,member_id,idempotency_key,message,status,next_attempt_at,created_at) values (?,?,?,?,?,?,?,?)",
                               (uid(), familia, self.admin_id, "k", "m", estado, "x", "x"))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("insert into notification_queue(id,family_id,member_id,idempotency_key,message,status,next_attempt_at,created_at) values (?,?,?,?,?,?,?,?)",
                           (uid(), "no-existe", self.admin_id, "k", "m", "pendiente", "x", "x"))

    def test_45_el_script_de_supabase_cubre_todas_las_tablas_con_rls(self):
        sql = (NEST / "supabase" / "007_telegram.sql").read_text(encoding="utf-8").lower()
        for tabla in ("telegram_links", "telegram_codes", "telegram_attempts", "telegram_state", "telegram_worker", "notification_queue"):
            self.assertIn(f"alter table public.{tabla} enable row level security", sql, tabla)
        self.assertNotRegex(sql, r"delete\s+from|drop\s+table|truncate")
