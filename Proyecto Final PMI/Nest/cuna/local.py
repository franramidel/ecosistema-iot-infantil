"""Demo con SQLite. Cada mutación se confirma en una sola transacción."""
import hashlib
import hmac
import os
import sqlite3
from contextlib import contextmanager

from .common import Problem, device_secret, fingerprint, instant, uid

SCHEMA = """
create table if not exists families(id text primary key, version integer not null default 0);
create table if not exists members(
 id text primary key, family_id text not null references families, email text unique not null,
 password_hash text not null, role text not null check(role in ('admin','caregiver')),
 can_operate integer not null);
create table if not exists children(id text primary key, family_id text not null references families, name text not null);
create table if not exists devices(
 id text primary key, family_id text not null references families, name text not null, secret_hash text not null);
create table if not exists assignments(
 id text primary key, device_id text not null references devices, child_id text not null references children,
 started_at text not null, ended_at text, enabled integer not null default 0,
 check(ended_at is null or ended_at >= started_at));
create unique index if not exists one_active on assignments(device_id) where ended_at is null;
create index if not exists period on assignments(device_id,started_at);
create table if not exists events(
 id text primary key, device_id text not null references devices, child_id text not null references children,
 captured_at text not null, metric text not null, value real not null, unit text not null);
create index if not exists timeline on events(child_id,captured_at,id);
create trigger if not exists keep_event before update on events begin select raise(abort,'Evento inmutable'); end;
create trigger if not exists keep_history before delete on events begin select raise(abort,'Evento inmutable'); end;
"""


def password_hash(password, salt=None):
    salt = salt or os.urandom(16).hex()
    derived = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return salt + ":" + derived.hex()


class Local:
    sync_mode = "sse"

    def __init__(self, path):
        self.path = str(path)
        self.dummy = password_hash(uid())
        with self.connection() as db:
            db.executescript(SCHEMA)

    def sync_config(self, session):
        return {"mode": self.sync_mode}

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("pragma foreign_keys=on")
            db.execute("begin immediate" if write else "begin")
            yield db
            db.commit()
        finally:
            db.close()

    def bootstrap(self, email, password):
        with self.connection(True) as db:
            if db.execute("select 1 from members limit 1").fetchone():
                return False
            family = uid()
            db.execute("insert into families(id) values (?)", (family,))
            db.execute("insert into members values (?,?,?,?,?,1)",
                       (uid(), family, email, password_hash(password), "admin"))
            return True

    def login(self, email, password):
        with self.connection() as db:
            row = db.execute("select id,password_hash from members where email=?", (email,)).fetchone()
        digest = row["password_hash"] if row else self.dummy
        valid = hmac.compare_digest(password_hash(password, digest.split(":")[0]), digest)
        if not row or not valid:
            raise Problem("Correo o contraseña incorrectos.", 401)
        return {"id": row["id"]}

    @staticmethod
    def actor(db, session):
        row = db.execute("select id,email,family_id,role,can_operate from members where id=?",
                         (session["id"],)).fetchone()
        if not row:
            raise Problem("Vuelve a iniciar sesión.", 401)
        return dict(row)

    @staticmethod
    def child(db, family, child):
        if not db.execute("select 1 from children where family_id=? and id=?", (family, child)).fetchone():
            raise Problem("Perfil no disponible.", 404)

    def version(self, session):
        with self.connection() as db:
            family = self.actor(db, session)["family_id"]
            return db.execute("select version from families where id=?", (family,)).fetchone()[0]

    def state(self, session, selected):
        with self.connection() as db:
            me = self.actor(db, session)
            family = me["family_id"]
            children = [dict(r) for r in db.execute("select id,name from children where family_id=? order by name,id", (family,))]
            selected = selected or (children[0]["id"] if children else None)
            if selected:
                self.child(db, family, selected)
            events = [dict(r) for r in db.execute("select * from events where child_id=? order by captured_at desc,id desc limit 100", (selected,))]
            devices = [dict(r) for r in db.execute("""
                select d.id,d.name,a.child_id,a.id as assignment_id,coalesce(a.enabled,0) as enabled
                from devices d left join assignments a on a.device_id=d.id and a.ended_at is null
                where d.family_id=? order by d.id""", (family,))]
            members = []
            if me["role"] == "admin":
                members = [dict(r) for r in db.execute("select id,email,role,can_operate from members where family_id=? order by email", (family,))]
            return dict(me=me, children=children, selected=selected, devices=devices, events=events, members=members)

    def export_page(self, session, child, after):
        with self.connection() as db:
            me = self.actor(db, session)
            if me["role"] != "admin":
                raise Problem("Se requiere el rol de administrador.", 403)
            self.child(db, me["family_id"], child)
            return [dict(r) for r in db.execute("select * from events where child_id=? and id>? order by id limit 500", (child, after or ""))]

    def change(self, session, action, p):
        with self.connection(True) as db:
            me = self.actor(db, session)
            family = me["family_id"]
            if action == "operate":
                if not me["can_operate"]:
                    raise Problem("No tienes permiso para operar dispositivos.", 403)
            elif me["role"] != "admin":
                raise Problem("Se requiere el rol de administrador.", 403)
            result = {}
            try:
                if action == "member":
                    db.execute("insert into members values (?,?,?,?,?,?)",
                               (uid(), family, p["email"], password_hash(p["password"]), p["role"], p["can_operate"]))
                elif action == "permission":
                    changed = db.execute("update members set can_operate=? where id=? and family_id=? and role='caregiver'",
                                         (p["enabled"], p["member_id"], family))
                    if not changed.rowcount:
                        raise Problem("Cuidador no disponible.", 404)
                elif action == "child":
                    result["id"] = uid()
                    db.execute("insert into children values (?,?,?)", (result["id"], family, p["name"]))
                elif action == "rename":
                    self.child(db, family, p["child_id"])
                    db.execute("update children set name=? where id=?", (p["name"], p["child_id"]))
                elif action == "device":
                    secret = device_secret()
                    db.execute("insert into devices values (?,?,?,?)", (p["device_id"], family, p["name"], fingerprint(secret)))
                    result["secret"] = secret
                else:
                    self.update_device(db, family, action, p)
            except sqlite3.IntegrityError as exc:
                raise Problem("El correo o Device_ID ya está registrado.", 409) from exc
            db.execute("update families set version=version+1 where id=?", (family,))
            return result

    def update_device(self, db, family, action, p):
        device = p["device_id"]
        if not db.execute("select 1 from devices where family_id=? and id=?", (family, device)).fetchone():
            raise Problem("Dispositivo no disponible.", 404)
        current = db.execute("select * from assignments where device_id=? and ended_at is null", (device,)).fetchone()
        if (current["id"] if current else None) != p["expected"]:
            raise Problem("La asignación cambió. Actualiza el panel.", 409)
        if action == "operate":
            if not current:
                raise Problem("Asigna primero el dispositivo.")
            db.execute("update assignments set enabled=? where id=?", (p["enabled"], current["id"]))
            return
        target = p["child_id"]
        if target:
            self.child(db, family, target)
        if (current["child_id"] if current else None) == target:
            return
        now = instant()
        if current:
            db.execute("update assignments set ended_at=? where id=?", (now, current["id"]))
        if target:
            db.execute("insert into assignments(id,device_id,child_id,started_at) values (?,?,?,?)", (uid(), device, target, now))

    def ingest(self, device, secret, observation):
        event_id, at, metric, value, unit = observation
        with self.connection(True) as db:
            row = db.execute("select family_id,secret_hash from devices where id=?", (device,)).fetchone()
            if not row or not hmac.compare_digest(row["secret_hash"], fingerprint(secret)):
                raise Problem("Credencial de dispositivo inválida.", 403)
            prior = db.execute("select * from events where id=?", (event_id,)).fetchone()
            if prior:
                if tuple(prior[k] for k in ("device_id","captured_at","metric","value","unit")) != (device, at, metric, value, unit):
                    raise Problem("El identificador del evento ya tiene otro contenido.", 409)
                return {"child_id": prior["child_id"]}
            assignment = db.execute("""select child_id from assignments where device_id=? and started_at<=?
                and (ended_at is null or ? < ended_at)""", (device, at, at)).fetchone()
            if not assignment:
                raise Problem("El dispositivo estaba sin asignar en esa fecha.", 409)
            child = assignment["child_id"]
            db.execute("insert into events values (?,?,?,?,?,?,?)", (event_id, device, child, at, metric, value, unit))
            db.execute("update families set version=version+1 where id=?", (row["family_id"],))
            return {"child_id": child}

