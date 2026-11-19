"""Utilidades compartidas por las pruebas de auditoría, exportación y Telegram.

Levanta un servidor real contra una base SQLite temporal, igual que test_historias.py,
pero con ayudantes para sembrar datos históricos sin pasar por la API.
"""
import http.cookiejar
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPCookieProcessor, Request

from app import Application
from cuna.common import device_secret, fingerprint, instant, uid
from cuna.local import Local, password_hash

ADMIN = ("padre@example.test", "Demo_prueba01")


def volcado(ruta):
    """Contenido completo de las tablas del sistema, para comparar dos bases fila por fila."""
    from cuna.retencion import TABLAS
    db = sqlite3.connect(ruta)
    try:
        return {t: [tuple(r) for r in db.execute(f"select * from {t} order by 1")] for t in TABLAS}
    finally:
        db.close()


class Browser:
    def __init__(self, url):
        self.url = url
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path, data=None, headers=None, raw=False):
        req = Request(self.url + path, data=None if data is None else json.dumps(data).encode(),
                      headers={"Content-Type": "application/json", **(headers or {})})
        try:
            res = self.opener.open(req, timeout=8)
        except HTTPError as err:
            res = err
        with res:
            contents = res.read()
            if raw:
                return res.status, contents, res.headers
            kind = res.headers["Content-Type"]
            return res.status, json.loads(contents) if "application/json" in kind else contents.decode("utf-8-sig")


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "pruebas.db"
        self.db = Local(self.path)
        self.db.bootstrap(*ADMIN)
        self.keys = {}
        self.arrancar()

    def arrancar(self):
        self.server = Application(("127.0.0.1", 0), self.db)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.admin = self.login(*ADMIN)

    def reiniciar(self):
        """Detiene el servidor y lo vuelve a levantar sobre el mismo archivo, como un reinicio real."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.db = Local(self.path)
        self.arrancar()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def login(self, email, password):
        client = Browser(self.url)
        self.assertEqual(client.call("/api/login", dict(email=email, password=password))[0], 200)
        return client

    def change(self, action, client=None, **data):
        status, result = (client or self.admin).call("/api/change/" + action, data)
        self.assertEqual(status, 200, result)
        return result

    def child(self, name):
        return self.change("child", name=name)["id"]

    def device(self, child, name="TEST-1"):
        """Registra y asigna un dispositivo por la API. Devuelve el id de la asignación."""
        self.keys[name] = self.change("device", device_id=name, name="Sensor")["secret"]
        self.change("assign", expected=None, child_id=child, device_id=name)
        return next(d for d in self.state()["devices"] if d["id"] == name)["assignment_id"]

    def state(self, client=None, child=None):
        code, data = (client or self.admin).call("/api/state" + ("?child=" + child if child else ""))
        self.assertEqual(code, 200, data)
        return data

    def family_of_admin(self):
        with self.db.connection() as db:
            return db.execute("select family_id from members where email=?", (ADMIN[0],)).fetchone()[0]

    def seed_device(self, child, name, started="2026-01-01T00:00:00.000000+00:00", family=None):
        """Crea un dispositivo con una asignación antigua, para probar fechas del pasado."""
        key = device_secret()
        with self.db.connection(True) as db:
            fam = family or self.family_of_admin()
            db.execute("insert into devices(id,family_id,name,secret_hash) values (?,?,?,?)", (name, fam, "Sensor", fingerprint(key)))
            db.execute("insert into assignments(id,device_id,child_id,started_at) values (?,?,?,?)",
                       (uid(), name, child, started))
        self.keys[name] = key
        return key

    def second_family_admin(self):
        """Segunda familia con su administrador, su bebé y su dispositivo."""
        family, child = uid(), uid()
        with self.db.connection(True) as db:
            db.execute("insert into families(id) values (?)", (family,))
            db.execute("insert into members values (?,?,?,?,?,1)",
                       (uid(), family, "otra.familia@example.test", password_hash("Demo_segura02"), "admin"))
            db.execute("insert into children values (?,?,?)", (child, family, "Ajeno"))
        key = self.seed_device(child, "AJENO-1", family=family)
        return self.login("otra.familia@example.test", "Demo_segura02"), child, key

    def caregiver(self, allowed=False):
        self.change("member", email="cuidador@example.test", password="Demo_segura02", role="caregiver", can_operate=allowed)
        return self.login("cuidador@example.test", "Demo_segura02")

    def reading(self, device="TEST-1", at=None, value=23.0):
        return dict(event_id=uid(), device_id=device, captured_at=at or instant(), metric="Temperatura", value=value, unit="°C")

    def send(self, event, device=None, key=None):
        name = device or event["device_id"]
        return Browser(self.url).call("/api/events", event, {"Authorization": "Bearer " + (key or self.keys[name])})

    def audit_event(self, device="TEST-1", tipo="ALERTA_MANUAL", detail="Prueba", **extra):
        return dict(event_id=uid(), device_id=device, event_type=tipo, detail=detail, occurred_at=instant(), **extra)

    def send_audit(self, event, key=None):
        return Browser(self.url).call("/api/audit/events", event,
                                      {"Authorization": "Bearer " + (key or self.keys[event["device_id"]])})

    def audit(self, query="", client=None):
        return (client or self.admin).call("/api/audit" + ("?" + query if query else ""))

    def audit_rows(self, **where):
        with self.db.connection() as db:
            clause = " and ".join(k + "=?" for k in where) or "1"
            return [dict(r) for r in db.execute(
                f"select * from audit_log where {clause} order by occurred_at,rowid", tuple(where.values()))]
