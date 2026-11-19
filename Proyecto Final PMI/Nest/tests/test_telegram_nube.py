"""SCRUM-24 con los dos almacenes: SQLite (Local) y Supabase (Cloud).

Un mismo contrato se ejecuta contra ambos y los resultados deben coincidir. La parte de Supabase corre el
SQL real de supabase/001 a 007 en un PostgreSQL local, con los roles reales authenticated y service_role,
y llega a él por un puente HTTP de pruebas (tests/puente_postgrest.py). Eso NO es Supabase: no hay PostgREST,
ni Supabase Auth, ni JWT firmados, ni red real. Telegram es siempre el servidor falso.

Las pruebas de Supabase se omiten si no existe la variable PGTEST_URL, por ejemplo:
    PGTEST_URL="postgresql://postgres@/postgres?host=/tmp/pgsock&port=54329"
El usuario de esa URL debe poder crear bases y roles. Cada prueba usa una base propia y desechable.
"""
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from apoyo import Browser
from apoyo_telegram import TOKEN, TelegramFalso
from puente_postgrest import CLAVE_PUBLICA, CLAVE_SECRETA, Puente, psql, url_de
from app import Application
from cuna import notificaciones
from cuna.cloud import Cloud
from cuna.common import Problem, instant, uid
from cuna.local import Local, password_hash
from cuna.procesador import Procesador, ProcesadorOcupado
from cuna.telegram import ClienteTelegram

NEST = Path(__file__).resolve().parent.parent
PGTEST_URL = os.environ.get("PGTEST_URL")
MIGRACIONES = ["tests/postgres/stub_supabase.sql", "supabase/001_usuarios.sql", "supabase/003_auditoria.sql",
               "supabase/004_retencion.sql", "supabase/005_device_uuid.sql", "supabase/007_telegram.sql"]
ADMIN_A, CUIDA_A, ADMIN_B = ("a1000000-0000-4000-8000-00000000000%d" % n for n in (1, 2, 3))
FAM_A, FAM_B = "fa000000-0000-4000-8000-00000000000a", "fb000000-0000-4000-8000-00000000000b"
PLANTILLA = f"cuna_tg_plantilla_{os.getpid()}"
contador = iter(range(1, 10_000))
LEJANO = "2000-01-01T00:00:00.000000+00:00"
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"


def setUpModule():
    if not PGTEST_URL:
        return
    if any(x in PGTEST_URL for x in ("supabase.co", "supabase.com", "pooler")):
        raise RuntimeError("PGTEST_URL parece de Supabase. Estas pruebas son solo para PostgreSQL local desechable.")
    psql(PGTEST_URL, f'create database "{PLANTILLA}";')
    for archivo in MIGRACIONES:
        r = subprocess.run(["psql", url_de(PGTEST_URL, PLANTILLA), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(NEST / archivo)],
                           capture_output=True, text=True,
                           env={**os.environ, "PGOPTIONS": "-c cuna.entorno_desechable=si"})
        if r.returncode:
            raise RuntimeError(f"Falló {archivo}: {r.stderr[-400:]}")


def tearDownModule():
    if PGTEST_URL:
        psql(PGTEST_URL, f'drop database if exists "{PLANTILLA}" with (force);')


class HarnessSqlite:
    nombre = "sqlite"

    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "c.db"
        self.store = Local(self.path)
        self.store.bootstrap("padre@example.test", "Demo_prueba01")
        with self.store.connection(True) as db:
            fam_a = db.execute("select family_id from members").fetchone()[0]
            db.execute("update members set id=? where family_id=?", (ADMIN_A, fam_a))
            db.execute("insert into members values (?,?,?,?,?,0)", (CUIDA_A, fam_a, "cuida@example.test", password_hash("Demo_segura02"), "caregiver"))
            db.execute("insert into families(id) values (?)", (FAM_B,))
            db.execute("insert into members values (?,?,?,?,?,1)", (ADMIN_B, FAM_B, "b@example.test", password_hash("Demo_segura02"), "admin"))
        self.sesiones = {u: {"id": u} for u in (ADMIN_A, CUIDA_A, ADMIN_B)}

    def sql(self, consulta, *args):
        with self.store.connection(True) as db:
            return [tuple(r) for r in db.execute(consulta, args)]

    def reabrir(self):
        return Local(self.path)

    def vencer(self, n):
        self.sql("update notification_queue set next_attempt_at=? where id=?", LEJANO, n)

    def expirar(self, n):
        self.sql("update notification_queue set locked_until=? where id=?", LEJANO, n)

    def expirar_codigo(self, miembro):
        self.sql("update telegram_codes set expires_at=? where member_id=? and used_at is null", LEJANO, miembro)

    def fila(self, n):
        r = self.sql("select status,attempts,last_error from notification_queue where id=?", n)[0]
        return dict(status=r[0], attempts=r[1], last_error=r[2])

    def espera(self, n):
        from datetime import datetime, timezone
        sig = self.sql("select next_attempt_at from notification_queue where id=?", n)[0][0]
        return (datetime.fromisoformat(sig) - datetime.now(timezone.utc)).total_seconds()

    def cerrar(self):
        self.dir.cleanup()


class HarnessSupabase:
    nombre = "supabase"

    def __init__(self):
        self.db = f"cuna_tg_{os.getpid()}_{next(contador)}"
        psql(PGTEST_URL, f'create database "{self.db}" template "{PLANTILLA}";')
        self.url = url_de(PGTEST_URL, self.db)
        self.script(f"""
            insert into public.families(id) values ('{FAM_A}'),('{FAM_B}');
            insert into auth.users(id,email) values ('{ADMIN_A}','a@example.test'),('{CUIDA_A}','c@example.test'),('{ADMIN_B}','b@example.test');
            insert into public.members values ('{ADMIN_A}','{FAM_A}','a@example.test','admin',true),
              ('{CUIDA_A}','{FAM_A}','c@example.test','caregiver',false),('{ADMIN_B}','{FAM_B}','b@example.test','admin',true);""")
        self.puente = Puente(self.url)
        self.store = self.nuevo_cloud()
        self.sesiones = {u: self.sesion(u) for u in (ADMIN_A, CUIDA_A, ADMIN_B)}

    def nuevo_cloud(self, secreta=CLAVE_SECRETA):
        return Cloud(self.puente.url, CLAVE_PUBLICA, secreta)

    @staticmethod
    def sesion(miembro):
        import time
        return {"id": miembro, "lock": threading.Lock(), "access": "jwt-" + miembro, "refresh": "x", "expires": time.time() + 3600}

    def script(self, texto):
        r = psql(self.url, texto)
        if r.returncode:
            raise RuntimeError(r.stderr[-300:])
        return r.stdout

    def sql(self, consulta, *args):
        for a in args:
            consulta = consulta.replace("?", "'" + str(a).replace("'", "''") + "'", 1)
        salida = self.script(consulta + ";")
        return [tuple(linea.split("|")) for linea in salida.splitlines() if linea]

    def reabrir(self):
        return self.nuevo_cloud()

    def vencer(self, n):
        self.sql("update notification_queue set next_attempt_at=now()-interval '1 second' where id=?", n)

    def expirar(self, n):
        self.sql("update notification_queue set locked_until=now()-interval '1 second' where id=?", n)

    def expirar_codigo(self, miembro):
        self.sql("update telegram_codes set created_at=now()-interval '20 minutes', expires_at=now()-interval '10 minutes' where member_id=? and used_at is null", miembro)

    def fila(self, n):
        r = self.sql("select status,attempts,coalesce(last_error,'') from notification_queue where id=?", n)[0]
        return dict(status=r[0], attempts=int(r[1]), last_error=r[2] or None)

    def espera(self, n):
        return float(self.sql("select extract(epoch from next_attempt_at-now()) from notification_queue where id=?", n)[0][0])

    def cerrar(self):
        self.puente.cerrar()
        psql(PGTEST_URL, f'drop database if exists "{self.db}" with (force);')


class ContratoComun:
    """Pruebas que se ejecutan idénticas con SQLite y con Supabase."""
    Harness = None

    def setUp(self):
        self.h = self.Harness()
        self.addCleanup(self.h.cerrar)
        self.store = self.h.store
        self.falso = TelegramFalso()
        self.addCleanup(self.falso.cerrar)
        self.reloj = instant
        self.cliente = ClienteTelegram(TOKEN, self.falso.base, timeout=2)
        self.proc = Procesador(self.store, self.cliente, "proc-1", self.reloj, salida=io.StringIO())
        self.a, self.c, self.b = (self.h.sesiones[u] for u in (ADMIN_A, CUIDA_A, ADMIN_B))

    def vincular(self, sesion=None, miembro=ADMIN_A, chat=5001):
        codigo = self.store.telegram_codigo(sesion or self.a)["codigo"]
        self.falso.mensaje("/start " + codigo, chat=chat)
        self.proc.procesar_actualizaciones(0)
        return codigo

    def pedir(self, clave="k1", usuario=ADMIN_A, **extra):
        datos = dict(titulo="Temperatura alta", dispositivo="CUNA-1", estado="Crítico", ocurrido="2026-03-04T18:30:00+00:00")
        datos.update(extra)
        return self.store.solicitar_notificacion(usuario, clave, **datos)

    # --- vinculación -------------------------------------------------------------------------------------
    def test_01_vinculacion_valida_y_consulta_de_estado(self):
        antes = self.store.telegram_estado(self.a)
        self.assertEqual({k: antes[k] for k in ("vinculado", "bloqueado", "codigo_expira", "ultima_prueba")},
                         {"vinculado": False, "bloqueado": False, "codigo_expira": None, "ultima_prueba": None})
        codigo = self.store.telegram_codigo(self.a)
        self.assertEqual(len(codigo["codigo"]), 8)
        self.assertIsNotNone(self.store.telegram_estado(self.a)["codigo_expira"])
        self.falso.mensaje("/start " + codigo["codigo"])
        self.proc.procesar_actualizaciones(0)
        despues = self.store.telegram_estado(self.a)
        self.assertEqual((despues["vinculado"], despues["bloqueado"], despues["codigo_expira"]), (True, False, None))
        self.assertIsNotNone(despues["vinculado_desde"])
        self.assertIn("Cuenta vinculada", self.falso.textos()[-1])
        self.assertEqual(set(despues), {"vinculado", "vinculado_desde", "bloqueado", "codigo_expira", "procesador_activo", "ultima_prueba"})

    def test_02_codigo_reutilizado_incorrecto_y_bloqueo(self):
        codigo = self.vincular()
        self.assertEqual(self.store.telegram_vincular(codigo, 6002, 6002)[0], "invalido")
        for _ in range(notificaciones.MAX_FALLOS):
            self.assertEqual(self.store.telegram_vincular("ABCDEFGH", 7001, 7001)[0], "invalido")
        nuevo = self.store.telegram_codigo(self.b)["codigo"]
        self.assertEqual(self.store.telegram_vincular(nuevo, 7001, 7001)[0], "bloqueado")
        self.assertFalse(self.store.telegram_estado(self.b)["vinculado"])
        self.assertEqual(self.store.telegram_vincular("   ", 7002, 7002)[0], "invalido")

    def test_03_codigo_expirado(self):
        codigo = self.store.telegram_codigo(self.a)["codigo"]
        self.h.expirar_codigo(ADMIN_A)
        self.assertEqual(self.store.telegram_vincular(codigo, 5001, 5001)[0], "expirado")
        self.assertFalse(self.store.telegram_estado(self.a)["vinculado"])

    def test_04_nuevo_codigo_anula_el_anterior(self):
        primero = self.store.telegram_codigo(self.a)["codigo"]
        segundo = self.store.telegram_codigo(self.a)["codigo"]
        self.assertEqual(self.store.telegram_vincular(primero, 5001, 5001)[0], "invalido")
        self.assertEqual(self.store.telegram_vincular(segundo, 5001, 5001)[0], "vinculado")

    def test_05_chat_ya_vinculado_a_otra_cuenta(self):
        self.vincular(chat=8001)
        codigo = self.store.telegram_codigo(self.b)["codigo"]
        self.assertEqual(self.store.telegram_vincular(codigo, 8001, 8001)[0], "chat_en_uso")
        self.assertFalse(self.store.telegram_estado(self.b)["vinculado"])
        self.assertEqual(self.store.telegram_vincular(codigo, 8002, 8002)[0], "vinculado", "el código no se consumió")

    def test_06_desvincular_revoca_codigos_y_deja_sin_vinculo(self):
        self.vincular()
        abierto = self.store.telegram_codigo(self.a)["codigo"]
        self.assertEqual(self.store.telegram_desvincular(self.a), {})
        self.assertFalse(self.store.telegram_estado(self.a)["vinculado"])
        self.assertEqual(self.store.telegram_vincular(abierto, 5001, 5001)[0], "invalido")
        self.assertEqual(self.pedir("tras-desvincular")["estado"], "sin_vinculo")
        self.assertEqual(self.store.telegram_desvincular(self.a), {})

    def test_07_cada_cuenta_solo_toca_la_suya(self):
        self.vincular(chat=5001)
        self.vincular(self.c, CUIDA_A, chat=5002)
        self.assertTrue(self.store.telegram_estado(self.c)["vinculado"])
        self.store.telegram_desvincular(self.c)
        self.assertTrue(self.store.telegram_estado(self.a)["vinculado"])
        self.assertFalse(self.store.telegram_estado(self.b)["vinculado"])

    # --- solicitudes -------------------------------------------------------------------------------------
    def test_08_solicitud_idempotente_y_validaciones(self):
        self.vincular()
        r = self.pedir("evento-77")
        self.assertEqual((set(r), r["estado"], r["duplicado"], r["vinculado"]), ({"id", "estado", "duplicado", "vinculado"}, "pendiente", False, True))
        otra = self.pedir("evento-77")
        self.assertEqual((otra["id"], otra["duplicado"]), (r["id"], True))
        with self.assertRaises(Problem) as ctx:
            self.pedir("evento-77", titulo="Otro contenido")
        self.assertEqual(ctx.exception.status, 409)
        for mala in ("", "k" * 129):
            with self.assertRaises(Problem) as ctx:
                self.pedir(mala)
            self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(Problem) as ctx:
            self.pedir("e", estado="Rojo")
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(Problem) as ctx:
            self.pedir("e", usuario=uid())
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(Problem) as ctx:
            self.pedir("e", evento_id=uid())
        self.assertEqual(ctx.exception.status, 404)

    def test_09_usuario_sin_telegram_queda_sin_vinculo(self):
        r = self.pedir()
        self.assertEqual((r["estado"], r["vinculado"]), ("sin_vinculo", False))
        self.assertEqual(self.proc.procesar_cola(), {"enviadas": 0, "reintentos": 0, "permanentes": 0})
        self.assertEqual(self.falso.enviados, [])
        self.assertIn("no tiene Telegram", self.h.fila(r["id"])["last_error"])

    def test_10_envio_exitoso_con_formato_y_sin_reenvio(self):
        self.vincular()
        n = len(self.falso.enviados)
        r = self.pedir(titulo="Temp <b>alta</b> & riesgo")
        self.assertEqual(self.proc.procesar_cola()["enviadas"], 1)
        enviado = self.falso.enviados[n]
        self.assertEqual(enviado["chat_id"], "5001")
        for trozo in ("<b>Ecosistema IoT Infantil</b>", "Temp &lt;b&gt;alta&lt;/b&gt; &amp; riesgo", "Dispositivo: CUNA-1",
                      "Fecha: 04/03/2026", "Hora: 12:30", "Estado: Crítico"):
            self.assertIn(trozo, enviado["text"])
        self.assertEqual(self.h.fila(r["id"])["status"], "enviada")
        self.proc.procesar_cola()
        self.assertEqual(len(self.falso.enviados), n + 1)

    # --- reintentos --------------------------------------------------------------------------------------
    def test_11_error_temporal_se_recupera(self):
        self.vincular()
        r = self.pedir()
        self.falso.guion.append((503, {"ok": False}))
        self.proc.procesar_cola()
        self.assertEqual(self.h.fila(r["id"])["status"], "error_recuperable")
        self.assertEqual(self.proc.procesar_cola()["enviadas"], 0, "todavía no toca reintentar")
        self.h.vencer(r["id"])
        self.assertEqual(self.proc.procesar_cola()["enviadas"], 1)
        fila = self.h.fila(r["id"])
        self.assertEqual((fila["status"], fila["attempts"], fila["last_error"]), ("enviada", 2, None))

    def test_12_error_permanente_y_bloqueo_del_bot(self):
        self.vincular()
        r = self.pedir()
        self.falso.guion.append((403, {"ok": False, "description": "Forbidden: bot was blocked by the user"}))
        self.proc.procesar_cola()
        self.assertEqual(self.h.fila(r["id"])["status"], "error_permanente")
        self.assertTrue(self.store.telegram_estado(self.a)["bloqueado"])
        self.h.vencer(r["id"])
        n = len(self.falso.llamadas)
        self.proc.procesar_cola()
        self.assertEqual(len(self.falso.llamadas), n)

    def test_13_reintentos_limitados(self):
        self.vincular()
        r = self.pedir()
        for _ in range(notificaciones.MAX_INTENTOS + 3):
            self.falso.guion.append((500, {"ok": False}))
        for _ in range(notificaciones.MAX_INTENTOS + 3):
            self.proc.procesar_cola()
            self.h.vencer(r["id"])
        fila = self.h.fila(r["id"])
        self.assertEqual((fila["status"], fila["attempts"]), ("error_permanente", notificaciones.MAX_INTENTOS))
        self.assertIn("Reintentos agotados", fila["last_error"])

    def test_14_429_respeta_retry_after_y_el_token_no_se_guarda(self):
        self.vincular()
        r = self.pedir()
        self.falso.guion.append((429, {"ok": False, "parameters": {"retry_after": 600}, "description": "x " + TOKEN}))
        self.proc.procesar_cola()
        self.assertGreaterEqual(self.h.espera(r["id"]), 590)
        self.assertNotIn(TOKEN, str(self.h.fila(r["id"])))

    def test_15_telegram_caido_o_sin_red_es_recuperable(self):
        self.vincular()
        r = self.pedir()
        caido = Procesador(self.store, ClienteTelegram(TOKEN, "http://127.0.0.1:9", timeout=1), "p", self.reloj, salida=io.StringIO())
        self.assertEqual(caido.procesar_cola()["reintentos"], 1)
        self.assertEqual(self.h.fila(r["id"])["status"], "error_recuperable")

    # --- cola concurrente y reinicios ---------------------------------------------------------------------
    def test_16_reclamo_concurrente_no_duplica(self):
        self.vincular()
        ids = [self.pedir(f"c{i}")["id"] for i in range(18)]
        tomados, fallos = [], []

        def trabajar(nombre):
            try:
                tomados.extend(t["id"] for t in self.store.cola_reclamar(nombre, 18))
            except Exception as exc:
                fallos.append(exc)
        hilos = [threading.Thread(target=trabajar, args=(f"h{i}",)) for i in range(5)]
        [h.start() for h in hilos]
        [h.join() for h in hilos]
        self.assertEqual(fallos, [])
        self.assertEqual(sorted(tomados), sorted(ids))
        self.assertEqual(len(set(tomados)), len(tomados))

    def test_17_dos_procesadores_a_la_vez_no_repiten_mensajes(self):
        self.vincular()
        for i in range(12):
            self.pedir(f"d{i}")
        n = len(self.falso.enviados)
        otro = Procesador(self.store, self.cliente, "p9", self.reloj, salida=io.StringIO())
        hilos = [threading.Thread(target=p.procesar_cola, args=(12,)) for p in (self.proc, otro)]
        [h.start() for h in hilos]
        [h.join() for h in hilos]
        self.assertEqual(len(self.falso.enviados) - n, 12)

    def test_18_solicitud_abandonada_se_recupera_y_el_resultado_tardio_se_ignora(self):
        self.vincular()
        r = self.pedir()
        tomado = self.store.cola_reclamar("proceso-muerto")
        self.assertEqual(len(tomado), 1)
        self.assertEqual(set(tomado[0]) >= {"id", "member_id", "message", "attempts", "max_attempts", "chat_id", "token"}, True)
        self.assertEqual(self.proc.procesar_cola()["enviadas"], 0, "el arrendamiento sigue vigente")
        self.h.expirar(r["id"])
        self.assertEqual(self.proc.procesar_cola()["enviadas"], 1)
        self.assertEqual(self.h.fila(r["id"])["attempts"], 2)
        self.assertIsNone(self.store.cola_resultado(tomado[0], "permanente", "tarde"))
        self.assertEqual(self.h.fila(r["id"])["status"], "enviada")

    def test_19_interrupciones_repetidas_terminan_en_error_permanente(self):
        self.vincular()
        r = self.pedir()
        for i in range(notificaciones.MAX_INTENTOS + 1):
            self.store.cola_reclamar(f"muere{i}")
            self.h.expirar(r["id"])
        self.store.cola_reclamar("otro")
        self.assertEqual(self.h.fila(r["id"])["status"], "error_permanente")

    def test_20_reinicio_la_solicitud_pendiente_sobrevive(self):
        self.vincular()
        r = self.pedir()
        nuevo = self.h.reabrir()
        procesador = Procesador(nuevo, self.cliente, "p2", self.reloj, salida=io.StringIO())
        self.assertEqual(procesador.procesar_cola()["enviadas"], 1)
        self.assertEqual(self.h.fila(r["id"])["status"], "enviada")

    def test_21_offset_arrendamiento_y_prueba_de_envio(self):
        self.assertIsNone(self.store.telegram_offset())
        self.vincular()
        self.assertEqual(self.store.telegram_offset(), 102)
        enviados = len(self.falso.enviados)
        self.proc.procesar_actualizaciones(0)
        self.assertEqual(len(self.falso.enviados), enviados, "una actualización ya vista no se reprocesa")
        self.assertFalse(self.store.telegram_estado(self.a)["procesador_activo"])
        self.proc.ciclo(0)
        self.assertTrue(self.store.telegram_estado(self.a)["procesador_activo"])
        with self.assertRaises(ProcesadorOcupado):
            Procesador(self.store, self.cliente, "p2", self.reloj, salida=io.StringIO()).ciclo(0)
        self.store.telegram_procesador("proc-1", liberar=True)
        Procesador(self.store, self.cliente, "p2", self.reloj, salida=io.StringIO()).ciclo(0)
        with self.assertRaises(Problem) as ctx:
            self.store.telegram_probar(self.b)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.store.telegram_probar(self.a)["estado"], "pendiente")
        with self.assertRaises(Problem) as ctx:
            self.store.telegram_probar(self.a)
        self.assertEqual(ctx.exception.status, 429)
        self.proc.procesar_cola()
        self.assertIn("Notificación de prueba", self.falso.textos()[-1])
        self.assertEqual(self.store.telegram_estado(self.a)["ultima_prueba"], {"estado": "enviada", "error": None})

    # --- familias ----------------------------------------------------------------------------------------
    def test_22_aislamiento_entre_familias(self):
        self.vincular()
        self.pedir("comun")
        ajeno = self.store.solicitar_notificacion(ADMIN_B, "comun", titulo="Otra familia")
        propio = self.pedir("comun")
        self.assertNotEqual(ajeno["id"], propio["id"], "la clave de idempotencia es por familia")
        self.assertEqual(ajeno["estado"], "sin_vinculo")
        self.assertFalse(self.store.telegram_estado(self.b)["vinculado"])
        self.proc.procesar_cola()
        self.assertEqual(len([t for t in self.falso.textos() if "Temperatura alta" in t]), 1)


@unittest.skipUnless(PGTEST_URL, "Defina PGTEST_URL para probar SQLite y PostgreSQL con el mismo contrato")
class ContratoSupabase(ContratoComun, unittest.TestCase):
    Harness = HarnessSupabase


class ContratoSqlite(ContratoComun, unittest.TestCase):
    Harness = HarnessSqlite


@unittest.skipUnless(PGTEST_URL, "Defina PGTEST_URL para probar contra PostgreSQL local")
class SoloSupabase(unittest.TestCase):
    def setUp(self):
        self.h = HarnessSupabase()
        self.addCleanup(self.h.cerrar)
        self.store, self.a, self.b = self.h.store, self.h.sesiones[ADMIN_A], self.h.sesiones[ADMIN_B]
        self.falso = TelegramFalso()
        self.addCleanup(self.falso.cerrar)
        self.reloj = instant
        self.cliente = ClienteTelegram(TOKEN, self.falso.base, timeout=2)
        self.proc = Procesador(self.store, self.cliente, "proc-1", self.reloj, salida=io.StringIO())

    def vincular(self):
        self.falso.mensaje("/start " + self.store.telegram_codigo(self.a)["codigo"])
        self.proc.procesar_actualizaciones(0)

    def test_30_las_acciones_del_usuario_no_usan_la_clave_de_servicio(self):
        self.store.telegram_estado(self.a)
        self.store.telegram_codigo(self.a)
        self.store.telegram_desvincular(self.a)
        with self.assertRaises(Problem):
            self.store.telegram_probar(self.a)
        usadas = set(self.h.puente.registro)
        for funcion in ("cuna_telegram_status", "cuna_telegram_start", "cuna_telegram_stop", "cuna_telegram_test"):
            self.assertIn((funcion, "authenticated"), usadas)
            self.assertNotIn((funcion, "service_role"), usadas)
        self.vincular()
        self.store.solicitar_notificacion(ADMIN_A, "k", titulo="x")
        self.proc.ciclo(0)
        servicio = {f for f, rol in self.h.puente.registro if rol == "service_role"}
        self.assertTrue({"cuna_queue_claim", "cuna_queue_result", "cuna_notify", "cuna_telegram_link", "cuna_telegram_lease"} <= servicio)

    def test_31_un_usuario_autenticado_o_anonimo_no_puede_llamar_funciones_de_servicio(self):
        sesion = self.a
        llamadas = [("cuna_queue_claim", {"token": "x", "lim": 5}), ("cuna_notify", {"member": ADMIN_B, "key": "k", "msg": "m", "event": None}),
                    ("cuna_telegram_code", {"member": ADMIN_B}), ("cuna_telegram_link", {"code": "ABCDEFGH", "chat": "1", "tg_user": "1", "next_offset": None}),
                    ("cuna_telegram_lease", {"owner_id": "x", "release": False}), ("cuna_telegram_offset", {"new_value": 5}),
                    ("cuna_queue_result", {"job": uid(), "token": "t", "kind": "ok", "err": None, "retry_after": 0, "blocked": False})]
        for funcion, datos in llamadas:
            with self.assertRaises(Problem, msg=funcion) as ctx:
                self.store.request("/rest/v1/rpc/" + funcion, datos, token=self.store.access(sesion))
            self.assertEqual(ctx.exception.status, 403, funcion)
            with self.assertRaises(Problem, msg=funcion) as ctx:
                self.store.request("/rest/v1/rpc/" + funcion, datos)
            self.assertEqual(ctx.exception.status, 403, funcion)
        self.assertEqual(self.h.sql("select count(*) from notification_queue"), [("0",)])

    def test_32_un_usuario_no_puede_consultar_ni_vincular_a_otro(self):
        self.store.telegram_codigo(self.a)
        self.assertEqual(self.h.sql("select member_id from telegram_codes"), [(ADMIN_A,)])
        self.assertFalse(self.store.telegram_estado(self.b)["vinculado"])
        self.assertIsNone(self.store.telegram_estado(self.b)["codigo_expira"])
        sin_cuenta = HarnessSupabase.sesion(uid())
        with self.assertRaises(Problem) as ctx:
            self.store.telegram_estado(sin_cuenta)
        self.assertEqual(ctx.exception.status, 403)

    def test_33_las_tablas_no_se_leen_con_el_rol_del_usuario(self):
        self.vincular()
        self.store.solicitar_notificacion(ADMIN_A, "k", titulo="x")
        r = psql(self.h.url, f"set role authenticated; select set_config('request.jwt.claim.sub','{ADMIN_A}',false) \\g /dev/null\nselect count(*) from public.notification_queue;")
        self.assertIn("permission denied", r.stderr)
        r = psql(self.h.url, f"set role authenticated; select set_config('request.jwt.claim.sub','{ADMIN_A}',false) \\g /dev/null\nselect chat_id from public.telegram_links;")
        self.assertIn("permission denied", r.stderr)

    def test_34_la_aplicacion_ya_no_responde_501_con_supabase(self):
        servidor = Application(("127.0.0.1", 0), self.store)
        servidor.bot_username = "cuna_prueba_bot"
        servidor.session = lambda sid: self.a
        hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
        hilo.start()
        try:
            navegador = Browser("http://127.0.0.1:" + str(servidor.server_port))
            estado, datos = navegador.call("/api/telegram")
            self.assertEqual((estado, datos["vinculado"], datos["bot"]), (200, False, "cuna_prueba_bot"))
            estado, datos = navegador.call("/api/telegram/link", {})
            self.assertEqual(estado, 200)
            self.assertEqual(datos["enlace"], f"https://t.me/cuna_prueba_bot?start={datos['codigo']}")
            self.assertEqual(navegador.call("/api/telegram/test", {})[0], 409)
            self.falso.mensaje("/start " + datos["codigo"])
            self.proc.procesar_actualizaciones(0)
            self.assertTrue(navegador.call("/api/telegram")[1]["vinculado"])
            self.assertEqual(navegador.call("/api/telegram/test", {})[0], 200)
            self.assertEqual(navegador.call("/api/telegram/test", {})[0], 429)
            self.assertEqual(navegador.call("/api/telegram/unlink", {})[0], 200)
            self.assertFalse(navegador.call("/api/telegram")[1]["vinculado"])
            cuerpos = str([navegador.call("/api/telegram"), navegador.call("/api/telegram/link", {})])
            for secreto in (CLAVE_SECRETA, TOKEN, "service_role"):
                self.assertNotIn(secreto, cuerpos)
            servidor.session = Application.session.__get__(servidor)
            self.assertEqual(navegador.call("/api/telegram")[0], 401)
        finally:
            servidor.shutdown()
            servidor.server_close()

    def test_35_el_servidor_web_no_necesita_leer_el_token_de_telegram(self):
        fuente = (NEST / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("TELEGRAM_BOT_TOKEN", fuente)
        self.assertNotIn("service_role", (NEST / "web" / "app.js").read_text(encoding="utf-8"))

    def test_36_auditoria_en_supabase_envio_fallo_y_eventos_falsos(self):
        from datetime import datetime, timezone
        niño = self.store.change(self.a, "child", {"name": "Bebé"})["id"]
        secreto = self.store.change(self.a, "device", {"device_id": "CUNA-1", "name": "Sensor"})["secret"]
        self.store.change(self.a, "assign", {"device_id": "CUNA-1", "expected": None, "child_id": niño})
        ahora = datetime.now(timezone.utc).isoformat()
        self.store.record("CUNA-1", secreto, (uid(), ahora, "ALERTA_MANUAL", "Aviso", "warning"))
        evento = self.h.sql("select id from audit_log where event_type='ALERTA_MANUAL'")[0][0]
        for tipo in ("NOTIFICACION_ENVIADA", "NOTIFICACION_FALLIDA"):
            with self.assertRaises(Problem) as ctx:
                self.store.record("CUNA-1", secreto, (uid(), ahora, tipo, "falso", "info"))
            self.assertEqual(ctx.exception.status, 403, tipo)
        self.vincular()
        self.store.solicitar_notificacion(ADMIN_A, "ok", titulo="Aviso", dispositivo="CUNA-1", evento_id=evento)
        self.proc.procesar_cola()
        self.assertEqual(self.h.sql("select count(*) from audit_log where event_type='NOTIFICACION_ENVIADA' and device_id='CUNA-1'"), [("1",)])
        self.falso.guion.append((400, {"ok": False, "description": "chat not found"}))
        self.store.solicitar_notificacion(ADMIN_A, "mal", titulo="Aviso", evento_id=evento)
        self.proc.procesar_cola()
        self.assertEqual(self.h.sql("select count(*) from audit_log where event_type='NOTIFICACION_FALLIDA'"), [("1",)])
        with self.assertRaises(Problem) as ctx:
            self.store.solicitar_notificacion(ADMIN_B, "cruce", titulo="x", evento_id=evento)
        self.assertEqual(ctx.exception.status, 404)

    def test_37_supabase_caido_no_detiene_el_procesador(self):
        muerto = Cloud("http://127.0.0.1:9", CLAVE_PUBLICA, CLAVE_SECRETA)
        procesador = Procesador(muerto, self.cliente, "p", self.reloj, salida=io.StringIO())
        with self.assertRaises(Problem) as ctx:
            muerto.telegram_estado(self.a)
        self.assertEqual(ctx.exception.status, 503)
        parar = threading.Event()
        hilo = threading.Thread(target=procesador.ejecutar, args=(parar, 0), daemon=True)
        hilo.start()
        hilo.join(1.5)
        self.assertTrue(hilo.is_alive(), "el procesador sigue vivo y reintenta")
        parar.set()
        hilo.join(10)
        self.assertFalse(hilo.is_alive())
        self.assertIn("almacén de datos", procesador.salida.getvalue())

    def test_38_clave_de_servicio_rechazada_detiene_el_procesador_con_aviso(self):
        mala = self.h.nuevo_cloud(secreta="sb_secret_equivocada")
        procesador = Procesador(mala, self.cliente, "p", self.reloj, salida=io.StringIO())
        hilo = threading.Thread(target=procesador.ejecutar, args=(threading.Event(), 0), daemon=True)
        hilo.start()
        hilo.join(10)
        self.assertFalse(hilo.is_alive())
        self.assertTrue(procesador.almacen_rechazado)

    def correr_worker(self, *args, **entorno):
        base = {k: v for k, v in os.environ.items() if not k.startswith(("TELEGRAM_", "CUNA_", "SUPABASE_"))}
        base.update(PYTHONDONTWRITEBYTECODE="1", **entorno)
        return subprocess.run([sys.executable, str(NEST / "telegram_worker.py"), *args], env=base, capture_output=True,
                              text=True, timeout=60, cwd=str(NEST))

    def entorno_supabase(self, **extra):
        return dict(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_API_BASE=self.falso.base, SUPABASE_URL=self.h.puente.url,
                    SUPABASE_PUBLISHABLE_KEY=CLAVE_PUBLICA, SUPABASE_SECRET_KEY=CLAVE_SECRETA, **extra)

    def test_39_telegram_worker_con_supabase_es_un_proceso_independiente(self):
        self.vincular()
        r = self.store.solicitar_notificacion(ADMIN_A, "proceso", titulo="Desde el proceso")
        n = len(self.falso.enviados)
        resultado = self.correr_worker("--supabase", "--una-vez", **self.entorno_supabase())
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn("almacén Supabase", resultado.stderr)
        self.assertEqual(len(self.falso.enviados) - n, 1)
        self.assertEqual(self.h.fila(r["id"]) if hasattr(self.h, "fila") else None, {"status": "enviada", "attempts": 1, "last_error": None})
        self.assertNotIn(TOKEN + CLAVE_SECRETA, resultado.stdout)
        for secreto in (TOKEN, CLAVE_SECRETA):
            self.assertNotIn(secreto, resultado.stdout + resultado.stderr)

    def test_40_telegram_worker_elige_el_almacen_y_valida_la_configuracion(self):
        entorno = self.entorno_supabase()
        entorno.pop("SUPABASE_SECRET_KEY")
        r = self.correr_worker("--supabase", "--una-vez", **entorno)
        self.assertEqual(r.returncode, 2)
        self.assertIn("SUPABASE_SECRET_KEY", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        with tempfile.TemporaryDirectory() as carpeta:
            sqlite = self.correr_worker("--una-vez", TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_API_BASE=self.falso.base,
                                        CUNA_DB=str(Path(carpeta) / "x.db"), SUPABASE_URL=self.h.puente.url)
            self.assertEqual(sqlite.returncode, 0, sqlite.stderr)
            self.assertIn("almacén SQLite", sqlite.stderr)
        malo = self.correr_worker("--supabase", "--una-vez", **{**self.entorno_supabase(), "SUPABASE_SECRET_KEY": "sb_secret_equivocada"})
        self.assertEqual(malo.returncode, 5)
        self.assertNotIn("Traceback", malo.stderr)

    def test_41_los_resultados_de_supabase_y_sqlite_tienen_la_misma_forma(self):
        h2 = HarnessSqlite()
        self.addCleanup(h2.cerrar)
        formas = []
        for store, sesion, falso in ((self.store, self.a, self.falso), (h2.store, h2.sesiones[ADMIN_A], TelegramFalso())):
            proc = Procesador(store, ClienteTelegram(TOKEN, falso.base, timeout=2), "p", self.reloj, salida=io.StringIO())
            falso.mensaje("/start " + store.telegram_codigo(sesion)["codigo"])
            proc.procesar_actualizaciones(0)
            sol = store.solicitar_notificacion(ADMIN_A, "k", titulo="Forma", dispositivo="D", estado="Informativo")
            trabajos = store.cola_reclamar("t", 5)
            formas.append({"estado": sorted(store.telegram_estado(sesion)), "solicitud": sorted(sol), "tipos": [type(sol[k]).__name__ for k in sorted(sol)],
                           "trabajo": sorted(trabajos[0]), "texto": falso.enviados[-1]["text"] if falso.enviados else None,
                           "codigo": sorted(store.telegram_codigo(sesion)), "offset": store.telegram_offset(),
                           "vincular": store.telegram_vincular("ZZZZZZZZ", 1, 1)[0], "resultado": store.cola_resultado(trabajos[0])})
            falso.cerrar() if falso is not self.falso else None
        self.assertEqual(formas[0], formas[1])


if __name__ == "__main__":
    unittest.main()
