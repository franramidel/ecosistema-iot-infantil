"""UUID persistente de dispositivos (corrección sobre SCRUM-20) y migración de bases antiguas."""
import re
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from cuna import migraciones
from cuna.common import device_secret, fingerprint, uid
from cuna.local import SCHEMA, Local
from apoyo import Base

ANTIGUO = Path(__file__).parent / "fixtures" / "esquema_scrum21.sql"
UUID_V4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
T0 = "2026-01-01T00:00:00.000000+00:00"


def base_antigua(ruta, dispositivos=("TEMP-01", "HUM-02"), lecturas=3):
    """Crea una base con el esquema de la entrega anterior: sin UUID y con auditoría por texto."""
    familia, bebe = uid(), uid()
    with closing(sqlite3.connect(ruta)) as db:
        db.executescript(ANTIGUO.read_text(encoding="utf-8"))
        db.execute("insert into families(id) values (?)", (familia,))
        db.execute("insert into children values (?,?,?)", (bebe, familia, "Luna"))
        for nombre in dispositivos:
            db.execute("insert into devices values (?,?,?,?)", (nombre, familia, "Sensor", fingerprint(device_secret())))
            db.execute("insert into assignments(id,device_id,child_id,started_at) values (?,?,?,?)", (uid(), nombre, bebe, T0))
            for i in range(lecturas):
                db.execute("insert into events values (?,?,?,?,?,?,?)",
                           (uid(), nombre, bebe, f"2026-02-0{i + 1}T12:00:00.000000+00:00", "Temperatura", 20.0 + i, "°C"))
        db.commit()
    return familia, bebe


def auditoria_sin_uuid(ruta):
    with closing(sqlite3.connect(ruta)) as db:
        return [tuple(r) for r in db.execute(
            "select rowid,id,family_id,occurred_at,event_date,event_time,event_type,detail,child_id,device_id,"
            "severity,source_event_id from audit_log order by rowid")]


class MigracionSqlite(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ruta = Path(self.temp.name) / "antigua.db"
        base_antigua(self.ruta)

    def tearDown(self):
        self.temp.cleanup()

    def uuids(self):
        with closing(sqlite3.connect(self.ruta)) as db:
            return dict(db.execute("select id,uuid from devices"))

    def test_la_base_antigua_no_tiene_uuid(self):
        with closing(sqlite3.connect(self.ruta)) as db:
            self.assertNotIn("uuid", [r[1] for r in db.execute("pragma table_info(devices)")])

    def test_dispositivos_existentes_reciben_uuid_valido_y_unico(self):
        Local(self.ruta)
        valores = self.uuids()
        self.assertEqual(set(valores), {"TEMP-01", "HUM-02"})
        for valor in valores.values():
            self.assertRegex(valor, UUID_V4)
        self.assertEqual(len(set(valores.values())), 2)
        self.assertNotIn(set(valores), set(valores.values()))

    def test_el_historial_se_conserva_con_el_mismo_orden_y_se_asocia_al_uuid(self):
        antes = auditoria_sin_uuid(self.ruta)
        self.assertEqual(len(antes), 2 * (3 + 1))
        Local(self.ruta)
        self.assertEqual(auditoria_sin_uuid(self.ruta), antes)
        with closing(sqlite3.connect(self.ruta)) as db:
            malos = db.execute("""select count(*) from audit_log l join devices d on d.id=l.device_id
                where l.device_uuid<>d.uuid""").fetchone()[0]
            self.assertEqual(malos, 0)
            self.assertEqual(db.execute("select count(*) from events").fetchone()[0], 6)
            self.assertEqual(db.execute("pragma foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("pragma user_version").fetchone()[0], migraciones.VERSION)

    def test_migrar_otra_vez_no_cambia_uuid_ni_duplica_filas(self):
        Local(self.ruta)
        primeros, historial = self.uuids(), auditoria_sin_uuid(self.ruta)
        for _ in range(3):
            Local(self.ruta)
        self.assertEqual(self.uuids(), primeros)
        self.assertEqual(auditoria_sin_uuid(self.ruta), historial)

    def test_los_uuid_persisten_tras_reiniciar(self):
        Local(self.ruta)
        antes = self.uuids()
        shutil.copy(self.ruta, self.ruta.with_name("copia.db"))
        Local(self.ruta)
        self.assertEqual(self.uuids(), antes)

    def test_los_triggers_de_auditoria_y_de_inmutabilidad_quedan_activos(self):
        Local(self.ruta)
        with closing(sqlite3.connect(self.ruta)) as db:
            db.execute("pragma foreign_keys=on")
            nombres = {r[0] for r in db.execute("select name from sqlite_master where type='trigger'")}
            for t in (*migraciones.auditoria.NOMBRES_TRIGGERS, "keep_event", "keep_history", "keep_assignments",
                      "devices_keep_uuid"):
                self.assertIn(t, nombres)
            antes = db.execute("select count(*) from audit_log").fetchone()[0]
            db.execute("insert into events values (?,?,?,?,?,?,?)",
                       (uid(), "TEMP-01", db.execute("select id from children").fetchone()[0],
                        "2026-03-01T12:00:00.000000+00:00", "Temperatura", 22.0, "°C"))
            nuevo = db.execute("select device_uuid from audit_log order by rowid desc limit 1").fetchone()[0]
            self.assertEqual(nuevo, db.execute("select uuid from devices where id='TEMP-01'").fetchone()[0])
            self.assertEqual(db.execute("select count(*) from audit_log").fetchone()[0], antes + 1)
            for sentencia in ("update audit_log set detail='x'", "delete from audit_log",
                              "update devices set uuid='00000000-0000-4000-8000-000000000000'"):
                with self.assertRaises(sqlite3.IntegrityError, msg=sentencia):
                    db.execute(sentencia)

    def test_un_fallo_durante_la_migracion_deja_la_base_intacta(self):
        antes = self.ruta.read_bytes()

        def fallar(db):
            db.execute("select 1/0 from nada_que_no_existe")

        with self.assertRaises(migraciones.MigracionError):
            migraciones.migrar(self.ruta, SCHEMA, antes_de_confirmar=fallar)
        with closing(sqlite3.connect(self.ruta)) as db:
            self.assertNotIn("uuid", [r[1] for r in db.execute("pragma table_info(devices)")])
            self.assertNotIn("device_uuid", [r[1] for r in db.execute("pragma table_info(audit_log)")])
            self.assertEqual(db.execute("pragma user_version").fetchone()[0], 0)
            self.assertEqual(db.execute("select count(*) from audit_log").fetchone()[0], 8)
        self.assertEqual(self.ruta.read_bytes()[:16], antes[:16])
        Local(self.ruta)  # después del fallo, la migración sí puede completarse
        self.assertEqual(len(self.uuids()), 2)

    def test_una_auditoria_huerfana_aborta_sin_cambios(self):
        with closing(sqlite3.connect(self.ruta)) as db:
            db.execute("pragma foreign_keys=off")
            db.execute("""insert into audit_log(family_id,occurred_at,event_type,detail,child_id,device_id)
                select family_id,'2026-01-01T00:00:00.000000+00:00','X','huérfano',id,'NO-EXISTE' from children""")
            db.commit()
        with self.assertRaises(migraciones.MigracionError):
            Local(self.ruta)
        with closing(sqlite3.connect(self.ruta)) as db:
            self.assertNotIn("uuid", [r[1] for r in db.execute("pragma table_info(devices)")])
            self.assertEqual(db.execute("select count(*) from audit_log").fetchone()[0], 9)

    def test_el_respaldo_anterior_se_verifica_con_aviso_y_no_con_error(self):
        from cuna.retencion import verificar
        resultado = verificar(self.ruta)
        self.assertTrue(resultado["ok"], resultado)
        self.assertTrue(resultado["avisos"])
        Local(self.ruta)
        resultado = verificar(self.ruta)
        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual(resultado["avisos"], [])


class UuidSqlite(Base):
    def uuid_de(self, device):
        with self.db.connection() as db:
            return db.execute("select uuid from devices where id=?", (device,)).fetchone()[0]

    def test_un_dispositivo_nuevo_recibe_uuid_v4_y_conserva_su_id_operativo(self):
        bebe = self.child("Luna")
        self.device(bebe, "TEMP-01")
        valor = self.uuid_de("TEMP-01")
        self.assertRegex(valor, UUID_V4)
        self.assertNotEqual(valor, "TEMP-01")
        self.assertEqual([d["id"] for d in self.state()["devices"]], ["TEMP-01"])

    def test_el_uuid_sobrevive_al_reinicio_del_servidor(self):
        bebe = self.child("Luna")
        self.device(bebe, "TEMP-01")
        antes = self.uuid_de("TEMP-01")
        self.reiniciar()
        self.assertEqual(self.uuid_de("TEMP-01"), antes)

    def test_dos_dispositivos_nunca_comparten_uuid(self):
        bebe = self.child("Luna")
        self.device(bebe, "A-1")
        self.device(bebe, "A-2")
        with self.assertRaises(sqlite3.IntegrityError), self.db.connection(True) as db:
            db.execute("update devices set secret_hash=secret_hash where id='A-1'")
            db.execute("insert into devices(id,family_id,name,secret_hash,uuid) select 'A-3',family_id,name,secret_hash,uuid "
                       "from devices where id='A-1'")

    def test_el_uuid_debe_tener_formato_valido(self):
        fam = self.family_of_admin()
        for malo in ("TEMP-01", "", "ZZZZZZZZ-0000-4000-8000-000000000000", "123"):
            with self.assertRaises(sqlite3.IntegrityError, msg=malo), self.db.connection(True) as db:
                db.execute("insert into devices(id,family_id,name,secret_hash,uuid) values ('X',?,?,?,?)",
                           (fam, "n", "h", malo))

    def test_la_clave_foranea_de_auditoria_se_cumple(self):
        bebe = self.child("Luna")
        self.device(bebe, "TEMP-01")
        self.device(bebe, "TEMP-02")
        fam, uuid_1, uuid_2 = self.family_of_admin(), self.uuid_de("TEMP-01"), self.uuid_de("TEMP-02")
        insertar = ("insert into audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,device_uuid) "
                    "values (?,?,?,?,?,?,?)")
        with self.db.connection(True) as db:  # control positivo
            db.execute(insertar, (fam, T0, "PRUEBA", "ok", bebe, "TEMP-01", uuid_1))
        causas = {
            "uuid inexistente": ("TEMP-01", "00000000-0000-4000-8000-000000000000"),
            "uuid de otro dispositivo": ("TEMP-01", uuid_2),
            "dispositivo inexistente": ("NO-EXISTE", uuid_1),
        }
        for causa, (dispositivo, valor) in causas.items():
            with self.assertRaises(sqlite3.IntegrityError, msg=causa), self.db.connection(True) as db:
                db.execute(insertar, (fam, T0, "PRUEBA", "mal", bebe, dispositivo, valor))
        with self.assertRaises(sqlite3.IntegrityError), self.db.connection(True) as db:
            db.execute("delete from devices where id='TEMP-01'")

    def test_lectura_y_evento_propio_quedan_asociados_al_uuid_correcto(self):
        bebe = self.child("Luna")
        self.device(bebe, "TEMP-01")
        self.device(bebe, "TEMP-02")
        self.assertEqual(self.send(self.reading("TEMP-01"))[0], 200)
        self.assertEqual(self.send_audit(self.audit_event("TEMP-02"))[0], 200)
        filas = self.audit_rows(child_id=bebe)
        por_dispositivo = {f["device_id"]: f["device_uuid"] for f in filas}
        self.assertEqual(por_dispositivo, {"TEMP-01": self.uuid_de("TEMP-01"), "TEMP-02": self.uuid_de("TEMP-02")})
        por_uuid = self.admin.call("/api/audit?device=" + self.uuid_de("TEMP-02"))[1]["items"]
        por_id = self.admin.call("/api/audit?device=TEMP-02")[1]["items"]
        self.assertEqual(por_uuid, por_id)
        self.assertEqual({f["device_id"] for f in por_uuid}, {"TEMP-02"})
        self.assertEqual({f["device_uuid"] for f in por_uuid}, {self.uuid_de("TEMP-02")})

    def test_filtrar_por_un_dispositivo_inexistente_o_ajeno_da_404(self):
        bebe = self.child("Luna")
        self.device(bebe, "TEMP-01")
        otro, _, _ = self.second_family_admin()
        ajeno = self.uuid_de("AJENO-1")
        for valor in ("NO-EXISTE", "00000000-0000-4000-8000-000000000000", ajeno, "AJENO-1"):
            self.assertEqual(self.admin.call("/api/audit?device=" + valor)[0], 404, valor)


if __name__ == "__main__":
    unittest.main()
