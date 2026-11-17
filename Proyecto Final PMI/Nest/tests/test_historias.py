import concurrent.futures
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
from cuna.common import instant, uid
from cuna.local import Local, password_hash


class Browser:
    def __init__(self, url):
        self.url = url
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self,path,data=None,headers=None):
        req = Request(self.url+path,data=None if data is None else json.dumps(data).encode(),
                      headers={"Content-Type":"application/json",**(headers or {})})
        try:
            res = self.opener.open(req,timeout=6)
        except HTTPError as err:
            res = err
        with res:
            contents = res.read()
            return res.status, json.loads(contents) if "application/json" in res.headers["Content-Type"] else contents.decode("utf-8-sig")


class Historias(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Local(Path(self.temp.name)/"pruebas.db")
        self.db.bootstrap("padre@example.test","Demo_prueba01")
        self.server = Application(("127.0.0.1",0),self.db)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:"+str(self.server.server_port)
        self.admin = self.login("padre@example.test","Demo_prueba01")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def login(self,email,pw):
        client = Browser(self.url)
        self.assertEqual(client.call("/api/login",dict(email=email,password=pw))[0],200)
        return client

    def change(self,action,client=None,**data):
        status,result = (client or self.admin).call("/api/change/"+action,data)
        self.assertEqual(status,200,result)
        return result

    def child(self,name):
        return self.change("child",name=name)["id"]

    def device(self,child):
        self.key=self.change("device",device_id="TEST-1",name="Sensor")["secret"]
        self.change("assign",expected=None,child_id=child,device_id="TEST-1")
        return self.state()["devices"][0]["assignment_id"]

    def state(self,client=None,child=None):
        code,data = (client or self.admin).call("/api/state"+("?child="+child if child else ""))
        self.assertEqual(code,200,data)
        return data

    def event(self, at=None, value=23):
        return dict(event_id=uid(),device_id="TEST-1",captured_at=at or instant(),metric="Temperatura",value=value,unit="°C")

    def send(self,event,key=None):
        return Browser(self.url).call("/api/events",event,{"Authorization":"Bearer "+(key or self.key)})

    def member(self,role="caregiver",allowed=False):
        self.change("member",email="otra@example.test",password="Demo_segura02",role=role,can_operate=allowed)
        return self.login("otra@example.test","Demo_segura02")

    def test_scrum15_credenciales_hash_y_salida(self):
        guest = Browser(self.url)
        self.assertEqual(guest.call("/api/state")[0],401)
        errors=[guest.call("/api/login",dict(email=e,password="incorrecta")) for e in ("nadie@example.test","padre@example.test")]
        self.assertEqual(errors[0],errors[1])
        self.assertEqual(errors[0][0],401)
        with self.db.connection() as db:
            digest = db.execute("select password_hash from members").fetchone()[0]
            self.assertNotIn("Demo_prueba01",digest)
            self.assertEqual(password_hash("Demo_prueba01",digest.split(":")[0]),digest)
        self.admin.call("/api/logout",{})
        self.assertEqual(self.admin.call("/api/state")[0],401)

    def test_scrum16_roles_y_revocacion_en_sesion(self):
        child=self.child("Luna")
        assignment=self.device(child)
        carer=self.member()
        operate=dict(device_id="TEST-1",expected=assignment,enabled=True)
        for action,data in [("operate",operate),("assign",dict(device_id="TEST-1",expected=assignment,child_id=None)),
                            ("member",dict(email="nuevo@example.test",password="Demo_prueba01",role="admin")),
                            ("child",dict(name="Otro"))]:
            self.assertEqual(carer.call("/api/change/"+action,data)[0],403)
        self.assertEqual(carer.call("/api/report?child="+child)[0],403)
        who=self.state(carer)["me"]["id"]
        self.change("permission",member_id=who,enabled=True)
        self.change("operate",carer,**operate)
        self.assertTrue(self.state()["devices"][0]["enabled"])
        self.change("permission",member_id=who,enabled=False)
        self.assertEqual(carer.call("/api/change/operate",operate)[0],403)

    def test_scrum17_notificacion_a_segunda_sesion(self):
        child=self.child("Luna")
        assignment=self.device(child)
        other=self.member(role="admin")
        with other.opener.open(self.url+"/api/stream",timeout=8) as stream:
            first=stream.readline()
            stream.readline()
            self.change("operate",device_id="TEST-1",expected=assignment,enabled=True)
            while True:
                changed=stream.readline()
                if changed.startswith(b"data:"): break
                self.assertTrue(changed)
            self.assertNotEqual(first,changed)
        self.assertTrue(self.state(other)["devices"][0]["enabled"])

    def test_scrum18_19_traslado_captura_tardia_e_inmutabilidad(self):
        a,b=self.child("Luna"),self.child("Sol")
        first=self.device(a)
        early=self.event()
        self.assertEqual(self.send(early),(200,{"child_id":a}))
        delayed=self.event()
        self.change("assign",device_id="TEST-1",expected=first,child_id=b)
        self.assertEqual(self.send(delayed),(200,{"child_id":a}))
        self.assertEqual(self.send(delayed),(200,{"child_id":a}))
        self.assertEqual(self.send(dict(delayed,value=998))[0],409)
        self.assertEqual(self.send(self.event(),"invalida")[0],403)
        with self.db.connection() as db:
            boundary=db.execute("select ended_at from assignments where id=?",(first,)).fetchone()[0]
        latest=self.event(boundary)
        self.assertEqual(self.send(latest),(200,{"child_id":b}))
        self.assertEqual({r["id"] for r in self.state(child=a)["events"]},{early["event_id"],delayed["event_id"]})
        self.assertEqual([r["id"] for r in self.state(child=b)["events"]],[latest["event_id"]])
        exported=self.admin.call("/api/report?child="+a)[1]
        self.assertIn(delayed["event_id"],exported)
        self.assertNotIn(latest["event_id"],exported)
        with self.assertRaises(sqlite3.IntegrityError), self.db.connection(True) as db:
            db.execute("update events set child_id=? where id=?",(b,early["event_id"]))
        active=self.state()["devices"][0]
        self.change("assign",device_id="TEST-1",expected=active["assignment_id"],child_id=None)
        self.assertEqual(self.send(self.event())[0],409)
        self.assertEqual(len(self.state(child=b)["events"]),1)

    def test_scrum19_dos_traslados_concurrentes(self):
        a,b,c=[self.child(n) for n in ("A","B","C")]
        old=self.device(a)
        other=self.member(role="admin")
        def move(args):
            client,target=args
            return client.call("/api/change/assign",dict(device_id="TEST-1",expected=old,child_id=target))[0]
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(move,[(self.admin,b),(other,c)])),[200,409])
        with self.db.connection() as db:
            self.assertEqual(db.execute("select count(*) from assignments where ended_at is null").fetchone()[0],1)

    def test_aislamiento_entre_familias(self):
        other_family,other_child=uid(),uid()
        with self.db.connection(True) as db:
            db.execute("insert into families(id) values(?)",(other_family,))
            db.execute("insert into children values(?,?,?)",(other_child,other_family,"Privado"))
        child=self.child("Visible")
        assignment=self.device(child)
        self.assertEqual(len(self.state()["children"]),1)
        self.assertEqual(self.admin.call("/api/state?child="+other_child)[0],404)
        self.assertEqual(self.admin.call("/api/report?child="+other_child)[0],404)
        self.assertEqual(self.admin.call("/api/change/assign",dict(device_id="TEST-1",expected=assignment,child_id=other_child))[0],404)
        self.assertEqual(self.state()["devices"][0]["child_id"],child)


if __name__=="__main__":
    unittest.main()

