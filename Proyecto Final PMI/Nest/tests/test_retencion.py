"""SCRUM-21 · RF-DAT-02. Retención permanente, respaldo y restauración (SQLite)."""
import hashlib
import re
from contextlib import closing
import sqlite3
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from apoyo import ADMIN, Base, Browser, volcado
from cuna.common import instant, uid
from cuna.local import Local
from cuna.retencion import RespaldoError, TABLAS, nombre_de_respaldo, respaldar, restaurar, verificar

NEST = Path(__file__).resolve().parent.parent


def huella(ruta):
    return hashlib.sha256(Path(ruta).read_bytes()).hexdigest()


class Retencion(Base):
    def sembrar(self, cantidad=5, nombre="OLD-1", desde="2026-11-10T00:00:00+00:00"):
        child = self.child("Luna")
        self.seed_device(child, nombre, started="2020-01-01T00:00:00.000000+00:00")
        inicio = instant(desde)
        for i in range(cantidad):
            at = instant(f"2026-11-10T{i // 60:02d}:{i % 60:02d}:00+00:00")
            self.db.ingest(nombre, self.keys[nombre], (uid(), at, "Temperatura", 20.0 + i % 5, "°C"))
        return child

    def test_ca1_los_registros_sobreviven_al_reinicio_y_la_auditoria_sigue(self):
        child = self.sembrar(12)
        antes = volcado(self.path)
        self.reiniciar()
        self.assertEqual(volcado(self.path), antes)
        self.assertEqual(len(self.state(child=child)["events"]), 12)
        self.assertEqual(len(self.audit("tipo=LECTURA")[1]["items"]), 12)
        nueva = self.reading("OLD-1")
        self.assertEqual(self.send(nueva), (200, {"child_id": child}))
        self.assertEqual(len(self.audit_rows(event_type="LECTURA")), 13)
        self.reiniciar()
        self.reiniciar()
        self.assertEqual(len(self.audit_rows(event_type="LECTURA")), 13)
        self.assertTrue(verificar(self.path)["ok"])

    def test_registros_antiguos_se_conservan_tras_nuevas_inserciones(self):
        child = self.sembrar(30)
        antes = volcado(self.path)
        for _ in range(15):
            self.assertEqual(self.send(self.reading("OLD-1"))[0], 200)
        self.change("assign", device_id="OLD-1", expected=self.state()["devices"][0]["assignment_id"], child_id=self.child("Sol"))
        despues = volcado(self.path)
        for tabla in ("events", "audit_log"):
            self.assertEqual(len(despues[tabla]), len(antes[tabla]) + (15 if tabla == "events" else 17))
            conservadas = set(despues[tabla])
            self.assertTrue(set(antes[tabla]) <= conservadas, tabla)
        antiguas = [f for f in despues["assignments"] if f[0] in {a[0] for a in antes["assignments"]}]
        self.assertEqual(len(antiguas), len(antes["assignments"]))

    def test_el_historico_no_se_puede_borrar_ni_modificar(self):
        self.sembrar(3)
        for sentencia in ("delete from events", "update events set value=0", "delete from audit_log",
                          "update audit_log set detail='x'", "delete from assignments"):
            with self.assertRaises(sqlite3.IntegrityError, msg=sentencia), self.db.connection(True) as db:
                db.execute(sentencia)
        for sentencia in ("delete from children", "delete from devices", "delete from families"):
            with self.assertRaises(sqlite3.IntegrityError, msg=sentencia), self.db.connection(True) as db:
                db.execute(sentencia)
        self.assertEqual(verificar(self.path)["conteos"]["events"], 3)

    def test_no_existen_procesos_ni_codigo_que_depure_la_telemetria(self):
        prohibido = [r"\bdelete\s+from\b", r"\bdrop\s+(table|schema)\b", r"\btruncate\b", r"threading\.Timer",
                     r"\bsched\b", r"apscheduler", r"\bschedule\b", r"os\.remove", r"rmtree", r"\bvacuum\b"]
        codigo = [p for p in [NEST / "app.py", NEST / "simular.py", NEST / "respaldo.py", *(NEST / "cuna").glob("*.py")]]
        for ruta in codigo:
            texto = ruta.read_text(encoding="utf-8")
            if ruta.name == "migraciones.py":
                # Reconstruye solo las tablas devices y audit_log copiando todas sus filas. Ver test_uuid.
                self.assertEqual(sorted(set(re.findall(r"drop table (\w+)", texto))), ["audit_log", "devices"])
                texto = re.sub(r"drop table \w+", "", texto)
            if ruta.name == "exportar.py":
                # Solo borra el archivo temporal que él mismo creó, nunca datos de la base.
                self.assertEqual(sorted(set(re.findall(r"os\.remove\(([\w.]+)\)", texto))), ["ruta", "self.ruta"])
                texto = re.sub(r"os\.remove\([\w.]+\)", "", texto)
            for patron in prohibido:
                self.assertIsNone(re.search(patron, texto, re.I), f"{ruta.name}: {patron}")
        for ruta in (NEST / "supabase").glob("00[0-9]_*.sql"):
            texto = re.sub(r"--[^\n]*", "", ruta.read_text(encoding="utf-8"))
            for patron in (r"\bdelete\s+from\b", r"\bdrop\s+(table|schema)\b", r"create\s+extension", r"cron\.schedule"):
                self.assertIsNone(re.search(patron, texto, re.I), f"{ruta.name}: {patron}")
            if ruta.name != "004_retencion.sql":
                self.assertIsNone(re.search(r"\btruncate\b", texto, re.I), ruta.name)
        sql = re.sub(r"--[^\n]*", "", (NEST / "supabase" / "004_retencion.sql").read_text(encoding="utf-8"))
        self.assertIsNone(re.search(r"(?<!before )(?<!no_)(?<!block_)\btruncate\b(?!')", sql, re.I))
        self.assertFalse([t for t in threading.enumerate() if isinstance(t, threading.Timer)])
        unlink = [r.name for r in (NEST / "cuna").glob("*.py") if "unlink(" in r.read_text(encoding="utf-8")]
        self.assertEqual(unlink, ["retencion.py"])

    def test_integridad_referencial_del_historico(self):
        a, b = self.child("Luna"), self.child("Sol")
        first = self.device(a)
        for _ in range(4):
            self.send(self.reading())
        self.change("assign", device_id="TEST-1", expected=first, child_id=b)
        self.send(self.reading())
        with self.db.connection() as db:
            self.assertEqual(db.execute("pragma foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("pragma integrity_check").fetchone()[0], "ok")
        informe = verificar(self.path)
        self.assertTrue(informe["ok"], informe["problemas"])

    def test_verificar_detecta_danos(self):
        self.sembrar(3)
        copia = Path(self.temp.name) / "danada.db"
        respaldar(self.path, copia)
        db = sqlite3.connect(copia)
        db.execute("pragma foreign_keys=off")
        db.execute("drop trigger keep_event")
        db.execute("drop trigger audit_reading")
        db.execute("insert into events values (?,?,?,?,?,?,?)",
                   (uid(), "OLD-1", uid(), "2026-11-11T00:00:00.000000+00:00", "Temperatura", 1.0, "°C"))
        db.commit()
        db.close()
        problemas = " ".join(verificar(copia)["problemas"])
        self.assertIn("keep_event", problemas)
        self.assertIn("relaciones rotas", problemas)
        self.assertIn("sin renglón de auditoría", problemas)

    def test_ca2_el_cliente_no_guarda_historicos_y_el_servidor_prohibe_cachearlos(self):
        for ruta in (NEST / "web").iterdir():
            texto = ruta.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"localStorage|sessionStorage|indexedDB|openDatabase|caches\.|serviceWorker|cookieStore|document\.cookie",
                                        texto), ruta.name)
        child = self.sembrar(3)
        for ruta in ("/", "/app.js", "/api/state?child=" + child, "/api/audit", "/api/report?child=" + child):
            status, _, headers = self.admin.call(ruta, raw=True)
            self.assertEqual(status, 200, ruta)
            self.assertEqual(headers["Cache-Control"], "no-store", ruta)

    def test_historial_completo_se_consulta_desde_el_servidor(self):
        child = self.sembrar(250)
        self.assertEqual(len(self.state(child=child)["events"]), 100)
        recuperadas, cursor = [], ""
        while True:
            pagina = self.audit("tipo=LECTURA&limit=100" + ("&after=" + cursor.replace("+", "%2B").replace("|", "%7C") if cursor else ""))[1]
            recuperadas += pagina["items"]
            cursor = pagina["next"]
            if not cursor:
                break
        self.assertEqual(len(recuperadas), 250)
        horas = [(i["event_date"], i["event_time"]) for i in recuperadas]
        self.assertEqual(horas, sorted(horas))
        csv = self.admin.call("/api/report?child=" + child)[1].strip().splitlines()
        self.assertEqual(len(csv), 251)

    def test_error_de_almacenamiento_no_pierde_ni_corrompe(self):
        child = self.sembrar(3)
        antes = volcado(self.path)
        with self.db.connection(True) as db:
            db.execute("create trigger disco_lleno before insert on events begin select raise(abort,'disco lleno'); end")
        lectura = self.reading("OLD-1")
        self.assertEqual(self.send(lectura)[0], 500)
        self.assertEqual(volcado(self.path), antes)
        with self.db.connection(True) as db:
            db.execute("drop trigger disco_lleno")
        self.assertEqual(self.send(lectura), (200, {"child_id": child}))
        self.assertEqual(self.send(lectura), (200, {"child_id": child}))
        self.assertEqual(len(self.audit_rows(source_event_id=lectura["event_id"])), 1)
        self.assertTrue(verificar(self.path)["ok"])

    def test_derecho_de_supresion_es_posible_con_el_procedimiento_manual(self):
        child = self.sembrar(4)
        otro = self.child("Sol")
        self.seed_device(otro, "OTRO-1", started="2020-01-01T00:00:00.000000+00:00")
        self.db.ingest("OTRO-1", self.keys["OTRO-1"], (uid(), instant("2026-11-10T00:00:00+00:00"), "Temperatura", 21.0, "°C"))
        db = sqlite3.connect(self.path)
        db.execute("pragma foreign_keys=on")
        for trigger in ("keep_event", "keep_history", "keep_assignments", "audit_keep_update", "audit_keep_delete"):
            db.execute(f"drop trigger {trigger}")
        for tabla in ("audit_log", "events", "assignments"):
            db.execute(f"delete from {tabla} where child_id=?", (child,))
        db.execute("delete from devices where id='OLD-1'")
        db.execute("delete from children where id=?", (child,))
        db.commit()
        db.close()
        Local(self.path)
        informe = verificar(self.path)
        self.assertTrue(informe["ok"], informe["problemas"])
        self.assertEqual(informe["conteos"]["events"], 1)
        with self.assertRaises(sqlite3.IntegrityError), self.db.connection(True) as conexion:
            conexion.execute("delete from events")


class Respaldo(Base):
    def sembrar(self, cantidad=20):
        child = self.child("Luna")
        self.device(child)
        for _ in range(cantidad):
            self.send(self.reading())
        return child

    def test_respaldo_en_caliente_es_consistente_mientras_se_escribe(self):
        self.sembrar(10)
        parar = threading.Event()
        errores, enviadas = [], []

        def escribir():
            while not parar.is_set():
                try:
                    self.db.ingest("TEST-1", self.keys["TEST-1"], (uid(), instant(), "Temperatura", 22.0, "°C"))
                    enviadas.append(1)
                except Exception as exc:
                    errores.append(exc)
                    return

        hilo = threading.Thread(target=escribir)
        hilo.start()
        try:
            while len(enviadas) < 5 and not errores:
                pass
            destinos = [Path(self.temp.name) / f"caliente{i}.sqlite3" for i in range(3)]
            informes = [respaldar(self.path, d) for d in destinos]
        finally:
            parar.set()
            hilo.join()
        self.assertEqual(errores, [])
        for destino, informe in zip(destinos, informes):
            self.assertTrue(informe["ok"], informe["problemas"])
            self.assertTrue(verificar(destino)["ok"])
            self.assertEqual(informe["conteos"]["events"], informe["conteos"]["audit_log"] - informe["conteos"]["assignments"])

    def test_restaurar_recupera_registros_relaciones_y_servicio(self):
        child = self.sembrar(25)
        original = volcado(self.path)
        respaldo = Path(self.temp.name) / "carpeta" / "copia.sqlite3"
        informe = respaldar(self.path, respaldo)
        self.assertEqual(informe["conteos"]["events"], 25)
        nuevo = Path(self.temp.name) / "recuperada.db"
        resultado = restaurar(respaldo, nuevo)
        self.assertIsNone(resultado["copia_previa"])
        self.assertEqual(volcado(nuevo), original)
        with closing(sqlite3.connect(nuevo)) as db:
            db.execute("pragma foreign_keys=on")
            self.assertEqual(db.execute("pragma foreign_key_check").fetchall(), [])
        self.path = nuevo
        self.db = Local(nuevo)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.arrancar()
        self.assertEqual(len(self.state(child=child)["events"]), 25)
        self.assertEqual(len(self.audit("tipo=LECTURA&limit=500")[1]["items"]), 25)
        self.assertEqual(self.send(self.reading())[0], 200)
        self.assertEqual(len(self.audit_rows(event_type="LECTURA")), 26)

    def test_restaurar_sobre_una_base_existente_conserva_la_anterior(self):
        self.sembrar(6)
        respaldo = Path(self.temp.name) / "r.sqlite3"
        respaldar(self.path, respaldo)
        original = volcado(self.path)
        for _ in range(4):
            self.send(self.reading())
        self.assertEqual(len(volcado(self.path)["events"]), 10)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        resultado = restaurar(respaldo, self.path)
        self.assertEqual(volcado(self.path), original)
        self.assertEqual(len(volcado(resultado["copia_previa"])["events"]), 10)
        self.arrancar()

    def test_respaldo_no_sobrescribe_y_falla_con_origen_inexistente(self):
        self.sembrar(2)
        destino = Path(self.temp.name) / "unico.sqlite3"
        respaldar(self.path, destino)
        firma = huella(destino)
        with self.assertRaises(RespaldoError):
            respaldar(self.path, destino)
        self.assertEqual(huella(destino), firma)
        with self.assertRaises(RespaldoError):
            respaldar(Path(self.temp.name) / "no-existe.db", Path(self.temp.name) / "otro.sqlite3")
        self.assertFalse((Path(self.temp.name) / "otro.sqlite3").exists())
        self.assertEqual(list(Path(self.temp.name).glob("*.parcial")), [])

    def test_restauracion_con_archivos_danados_no_toca_el_destino(self):
        self.sembrar(8)
        bueno = Path(self.temp.name) / "bueno.sqlite3"
        respaldar(self.path, bueno)
        contenido = bueno.read_bytes()
        casos = {
            "basura.sqlite3": b"esto no es una base de datos" * 100,
            "cortado.sqlite3": contenido[: len(contenido) // 2],
            "vacio.sqlite3": b"",
        }
        ajena = Path(self.temp.name) / "ajena.sqlite3"
        with closing(sqlite3.connect(ajena)) as db:
            db.execute("create table notas(x)")
            db.commit()
        casos["ajena.sqlite3"] = ajena.read_bytes()
        destino = Path(self.temp.name) / "destino.db"
        restaurar(bueno, destino)
        firma = huella(destino)
        for nombre, datos in casos.items():
            archivo = Path(self.temp.name) / ("malo-" + nombre)
            archivo.write_bytes(datos)
            with self.assertRaises(RespaldoError, msg=nombre):
                restaurar(archivo, destino)
            self.assertEqual(huella(destino), firma, nombre)
        with self.assertRaises(RespaldoError):
            restaurar(Path(self.temp.name) / "no-existe.sqlite3", destino)
        self.assertEqual([p.name for p in Path(self.temp.name).glob("*.restaurando")], [])

    def test_no_restaura_con_la_base_en_uso(self):
        self.sembrar(2)
        respaldo = Path(self.temp.name) / "uso.sqlite3"
        respaldar(self.path, respaldo)
        firma = huella(self.path)
        bloqueo = sqlite3.connect(self.path)
        bloqueo.execute("begin exclusive")
        try:
            with self.assertRaises(RespaldoError):
                restaurar(respaldo, self.path)
        finally:
            bloqueo.rollback()
            bloqueo.close()
        self.assertEqual(huella(self.path), firma)

    def test_nombres_de_respaldo_no_se_repiten_en_el_mismo_segundo_y_no_se_depuran(self):
        carpeta = Path(self.temp.name) / "respaldos"
        self.sembrar(1)
        primero = nombre_de_respaldo(carpeta)
        respaldar(self.path, primero)
        self.assertTrue(primero.name.startswith("nido-") and primero.suffix == ".sqlite3")
        for i in range(3):
            respaldar(self.path, carpeta / f"extra{i}.sqlite3")
        self.assertEqual(len(list(carpeta.glob("*.sqlite3"))), 4)

    def test_linea_de_comandos(self):
        self.sembrar(5)
        carpeta = Path(self.temp.name) / "cli"
        correr = lambda *a: subprocess.run([sys.executable, "respaldo.py", *a], cwd=NEST, capture_output=True, text=True, timeout=60)
        creado = correr("crear", "--db", str(self.path), "--carpeta", str(carpeta))
        self.assertEqual(creado.returncode, 0, creado.stderr)
        archivo = next(carpeta.glob("nido-*.sqlite3"))
        self.assertIn("events: 5", creado.stdout)
        self.assertEqual(correr("verificar", str(archivo)).returncode, 0)
        destino = Path(self.temp.name) / "desde-cli.db"
        self.assertNotEqual(correr("restaurar", str(archivo), "--db", str(destino)).returncode, 0)
        self.assertFalse(destino.exists())
        self.assertEqual(correr("restaurar", str(archivo), "--db", str(destino), "--confirmar").returncode, 0)
        self.assertEqual(volcado(destino)["events"], volcado(self.path)["events"])
        malo = Path(self.temp.name) / "malo.sqlite3"
        malo.write_bytes(b"x" * 50)
        self.assertEqual(correr("verificar", str(malo)).returncode, 1)
        fallo = correr("restaurar", str(malo), "--db", str(destino), "--confirmar")
        self.assertEqual(fallo.returncode, 1)
        self.assertIn("Error", fallo.stderr)


if __name__ == "__main__":
    unittest.main()
