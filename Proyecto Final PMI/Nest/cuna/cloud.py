"""Adaptador de Supabase Auth y PostgREST. Sólo el alta de usuarios usa la clave secreta."""
import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .common import Problem, device_secret


class Cloud:
    sync_mode = "realtime"

    def __init__(self, url, public_key, secret_key):
        self.url = url.rstrip("/")
        parts = urlsplit(self.url)
        if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")):
            raise ValueError("Supabase requiere HTTPS o un servidor local.")
        self.public_key = public_key
        self.secret_key = secret_key
        self.realtime_scheme = "wss" if parts.scheme == "https" else "ws"
        self.realtime_origin = self.realtime_scheme + "://" + parts.netloc
        self.realtime_url = urlunsplit((
            self.realtime_scheme,
            parts.netloc,
            parts.path.rstrip("/") + "/realtime/v1/websocket",
            urlencode({"apikey": public_key, "vsn": "1.0.0"}),
            "",
        ))

    def request(self, path, data=None, token=None, admin=False):
        key = self.secret_key if admin else self.public_key
        headers = {"apikey": key, "Content-Type": "application/json"}
        if token or (admin and not key.startswith("sb_secret_")):
            headers["Authorization"] = "Bearer " + (token or key)
        req = Request(self.url + path, headers=headers,
                      data=None if data is None else json.dumps(data).encode())
        try:
            with urlopen(req, timeout=12) as res:
                body = res.read()
                return json.loads(body) if body else {}
        except HTTPError as exc:
            try:
                err = json.loads(exc.read())
            except ValueError:
                err = {}
            code = err.get("code")
            if code in ("P0001", "PT409"):
                raise Problem(err["message"], 409 if code == "PT409" else 400)
            if code in ("23505","email_exists","user_already_exists") or exc.code == 422:
                raise Problem("Revisa los datos. El correo o Device_ID puede estar registrado.", 409)
            if exc.code == 401:
                raise Problem("La sesión o las credenciales no son válidas.", 401)
            if exc.code == 403 or code == "42501":
                raise Problem("Permiso insuficiente.", 403)
            if exc.code == 429:
                raise Problem("Demasiados intentos. Prueba más tarde.", 429)
            if exc.code >= 500 or code in ("PGRST202", "PGRST205"):
                raise Problem("Supabase no pudo completar la operación. Revisa la migración y la configuración.", 502)
            raise Problem("Supabase rechazó los datos enviados.", 400)
        except (URLError, TimeoutError, OSError):
            raise Problem("No hay conexión con Supabase.", 503)

    def login(self, email, password):
        try:
            data = self.request("/auth/v1/token?grant_type=password", {"email": email, "password": password})
        except Problem as exc:
            if exc.status in (400, 401, 403):
                raise Problem("Correo o contraseña incorrectos.", 401)
            raise
        ctx = {"id": data["user"]["id"], "lock": threading.Lock()}
        self.save_token(ctx, data)
        self.actor(ctx)
        return ctx

    @staticmethod
    def save_token(ctx, data):
        ctx.update(access=data["access_token"], refresh=data["refresh_token"], expires=time.time()+data["expires_in"])

    def access(self, ctx):
        with ctx["lock"]:
            if time.time() >= ctx["expires"] - 60:
                data = self.request("/auth/v1/token?grant_type=refresh_token", {"refresh_token":ctx["refresh"]})
                self.save_token(ctx, data)
            return ctx["access"]

    def rows(self, session, table, **query):
        return self.request("/rest/v1/" + table + "?" + urlencode(query), token=self.access(session))

    def actor(self, session):
        rows = self.rows(session, "members", id="eq." + session["id"], select="id,family_id,role")
        if not rows:
            raise Problem("Esta cuenta aún no pertenece a una familia.", 403)
        return rows[0]

    def state(self, session, selected):
        return self.request("/rest/v1/rpc/cuna_state", {"selected_child":selected}, self.access(session))

    def version(self, session):
        rows = self.rows(session, "families", select="version")
        if not rows:
            raise Problem("Cuenta sin acceso.", 403)
        return rows[0]["version"]

    def sync_config(self, session):
        actor = self.actor(session)
        return {
            "mode": self.sync_mode,
            "url": self.realtime_url,
            "family_id": actor["family_id"],
            "access_token": self.access(session),
            "expires_at": int(session["expires"]),
        }

    def change(self, session, action, p):
        if action == "member":
            actor = self.actor(session)
            if actor["role"] != "admin":
                raise Problem("Se requiere el rol de administrador.", 403)
            self.request("/auth/v1/admin/users", {
                "email": p["email"], "password": p["password"], "email_confirm": True,
                "app_metadata": {"cuna_creator": actor["id"], "cuna_role": p["role"], "cuna_operate": p["can_operate"]}
            }, admin=True)
            return {}
        if action == "device":
            p = dict(p, secret=device_secret())
        result = self.request("/rest/v1/rpc/cuna_change", {"action":action, "p":p}, self.access(session))
        if action == "device":
            result["secret"] = p["secret"]
        return result

    def export_page(self, session, child, after):
        if self.actor(session)["role"] != "admin":
            raise Problem("Se requiere el rol de administrador.", 403)
        if not self.rows(session,"children",select="id",id="eq."+child):
            raise Problem("Perfil no disponible.", 404)
        page = {"id":"gt."+after} if after else {}
        return self.rows(session, "events", child_id="eq."+child, select="id,child_id,device_id,captured_at,metric,value,unit",
                         order="id", limit="500", **page)

    def ingest(self, device, secret, observation):
        event_id, at, metric, value, unit = observation
        return self.request("/rest/v1/rpc/cuna_ingest", {
            "device":device, "secret":secret, "event_id":event_id, "at_time":at,
            "metric_name":metric, "measurement":value, "measurement_unit":unit
        })

