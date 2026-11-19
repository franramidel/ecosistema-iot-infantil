"""Adaptador de Supabase Auth y PostgREST. Sólo el alta de usuarios usa la clave secreta."""
import json
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from . import notificaciones, panel
from .auditoria import codificar
from .common import Problem, device_secret

UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


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
            if code in ("P0001", "PT409", "PT404", "PT429"):
                raise Problem(err["message"], {"PT409": 409, "PT404": 404, "PT429": 429}.get(code, 400))
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

    def record(self, device, secret, observation):
        event_id, at, kind, detail, level = observation
        return self.request("/rest/v1/rpc/cuna_record", {
            "device":device, "secret":secret, "event_id":event_id, "at_time":at,
            "kind":kind, "detail_text":detail, "level":level
        })

    def audit_filtros(self, session, filtros):
        """Solo administradores. Comprueba bebé y dispositivo (Device_ID o UUID) y devuelve los filtros con el Device_ID."""
        if self.actor(session)["role"] != "admin":
            raise Problem("Se requiere el rol de administrador.", 403)
        if "child" in filtros and not self.rows(session, "children", select="id", id="eq."+filtros["child"]):
            raise Problem("Perfil no disponible.", 404)
        if "device" in filtros:
            columna = "uuid" if UUID.fullmatch(filtros["device"]) else "id"
            fila = self.rows(session, "devices", select="id", **{columna: "eq."+filtros["device"]})
            if not fila:
                raise Problem("Dispositivo no disponible.", 404)
            filtros = {**filtros, "device": fila[0]["id"]}
        return filtros

    def audit_corte(self, session, filtros):
        """Último seq visible para el administrador. Fija el alcance de una exportación."""
        self.audit_filtros(session, filtros)
        fila = self.rows(session, "audit_log", select="seq", order="seq.desc", limit="1")
        return fila[0]["seq"] if fila else 0

    def audit_page(self, session, filtros, after, limit):
        """Mismo contrato que Local.audit_page. La RLS ya limita las filas a administradores de la familia.

        Con `hoja` (solo la exportación) lee la vista audit_export. En ese modo `next` apunta siempre a la
        última fila recibida y la lectura termina con una página vacía. Así un tope de filas de PostgREST
        (max-rows) menor que el lote pedido no corta la exportación en silencio.
        """
        filtros = self.audit_filtros(session, filtros)
        exportando = "hoja" in filtros
        columnas = "id,seq,occurred_at,event_date,event_time,event_type,detail,child_id,device_id,device_uuid,severity"
        query = {"select": columnas + (",metric,value,unit" if exportando else ""),
                 "order": "occurred_at.asc,seq.asc", "limit": str(limit + 1)}
        for column, key in (("child_id","child"), ("device_id","device"), ("event_type","tipo")):
            if key in filtros:
                query[column] = "eq." + filtros[key]
        bounds = [f"event_date.{op}.{filtros[key]}" for op, key in (("gte","desde"), ("lte","hasta")) if key in filtros]
        if "corte" in filtros:
            bounds.append(f"seq.lte.{int(filtros['corte'])}")
        if bounds:
            query["and"] = "(" + ",".join(bounds) + ")"
        if exportando:
            query["hoja"] = "eq." + filtros["hoja"]
        if after:
            at, seq = after
            query["or"] = f'(occurred_at.gt."{at}",and(occurred_at.eq."{at}",seq.gt.{seq}))'
        rows = self.rows(session, "audit_export" if exportando else "audit_log", **query)
        continua = len(rows) > limit or (exportando and rows)
        next_token = codificar(rows[min(limit, len(rows)) - 1]) if continua else None
        return {"items": [{k: v for k, v in r.items() if k != "seq"} for r in rows[:limit]], "next": next_token}

    # Sprint 2 (SCRUM-54, 35, 51, 57, 56). Mismo contrato que Local. Cada operación es una función de
    # supabase/009_panel_sprint2.sql que toma actor y familia del JWT. El parámetro `ahora` solo existe por compatibilidad.
    def rpc_panel(self, session, funcion, **argumentos):
        return self.request("/rest/v1/rpc/" + funcion, argumentos, self.access(session))

    def history(self, session, child, filtros, after, limite):
        inicio, fin = panel.rango_utc(filtros.get("desde"), filtros.get("hasta"))
        filas = self.rpc_panel(session, "cuna_history", child=child, from_at=inicio, to_at=fin,
                               metric_name=filtros.get("metric"), after_at=after[0] if after else None,
                               after_id=after[1] if after else None, lim=limite + 1)
        return panel.pagina_historial(filas, limite)

    def sleep_state(self, session, child, ahora=None):
        return self.rpc_panel(session, "cuna_sleep_state", child=child)

    def sleep_action(self, session, child, accion, ahora=None):
        funcion = {"start": "cuna_sleep_start", "snapshot": "cuna_sleep_snapshot", "disarm": "cuna_sleep_disarm"}.get(accion)
        if not funcion:
            raise Problem("Operación desconocida.", 404)
        return self.rpc_panel(session, funcion, child=child)

    def feeding_log(self, session, child, event_id, duracion, volumen, unidad, ahora=None):
        return self.rpc_panel(session, "cuna_log_feeding", child=child, event_id=event_id, duration_s=duracion,
                              volume=volumen, unit=unidad)

    def settings(self, session, child):
        return self.rpc_panel(session, "cuna_get_settings", child=child)

    def settings_save(self, session, child, velocidad, audio, volumen, ahora=None):
        return self.rpc_panel(session, "cuna_set_settings", child=child, led=velocidad, audio_name=audio, vol=volumen)

    def modules(self, session, child):
        return self.rpc_panel(session, "cuna_get_modules", child=child)

    def modules_save(self, session, child, modulo, encendido, modo, ahora=None):
        return self.rpc_panel(session, "cuna_set_module", child=child, module_name=modulo, on_off=encendido, mode_name=modo)

    # Telegram (SCRUM-24). Mismo contrato que Local. Las acciones del usuario llaman funciones que usan
    # auth.uid() con el token de la sesión. Solo el procesador y solicitar_notificacion usan la clave de
    # servicio, y esa clave vive en el servidor o en telegram_worker.py, nunca en el navegador.
    def umbrales(self, session, child):
        return self.request("/rest/v1/rpc/cuna_get_thresholds", {"child":child}, self.access(session))

    def umbrales_guardar(self, session, child, minimo, maximo, ahora=None):
        return self.request("/rest/v1/rpc/cuna_set_thresholds",
                            {"child":child, "tmin":minimo, "tmax":maximo}, self.access(session))

    def umbrales_dispositivo(self, device, secret, ahora=None):
        return self.request("/rest/v1/rpc/cuna_device_thresholds", {"device":device, "secret":secret})

    # El parámetro `ahora` existe por compatibilidad con Local: aquí el reloj es el de la base de datos.

    def telegram_estado(self, session, ahora=None):
        return self.request("/rest/v1/rpc/cuna_telegram_status", {}, self.access(session))

    def telegram_codigo(self, session, ahora=None):
        return self.request("/rest/v1/rpc/cuna_telegram_start", {}, self.access(session))

    def telegram_desvincular(self, session, ahora=None):
        self.request("/rest/v1/rpc/cuna_telegram_stop", {}, self.access(session))
        return {}

    def telegram_probar(self, session, ahora=None):
        return self.request("/rest/v1/rpc/cuna_telegram_test", {}, self.access(session))

    def solicitar_notificacion(self, usuario_id, clave_idempotencia, *, titulo, dispositivo=None, ocurrido=None,
                               estado="Advertencia", evento_id=None, ahora=None):
        """Contrato para SCRUM-25, igual que en Local. Necesita la clave de servicio, así que solo la usa código del servidor."""
        if not isinstance(clave_idempotencia, str) or not 1 <= len(clave_idempotencia) <= 128 or not clave_idempotencia.isprintable():
            raise Problem("La clave de idempotencia debe tener entre 1 y 128 caracteres.")
        if evento_id is not None and not UUID.fullmatch(str(evento_id)):
            raise Problem("El evento no existe en la familia del destinatario.", 404)
        if not UUID.fullmatch(str(usuario_id)):
            raise Problem("El usuario destinatario no existe.", 404)
        mensaje = notificaciones.formatear_alerta(titulo, dispositivo, ocurrido, estado)
        return self.request("/rest/v1/rpc/cuna_notify", {"member": usuario_id, "key": clave_idempotencia, "msg": mensaje,
                                                         "event": evento_id}, admin=True)

    def telegram_vincular(self, texto, chat_id, user_id, siguiente_offset=None, ahora=None):
        codigo = notificaciones.normalizar_codigo(texto)
        estado = self.request("/rest/v1/rpc/cuna_telegram_link", {
            "code": codigo or "", "chat": str(chat_id), "tg_user": str(user_id), "next_offset": siguiente_offset}, admin=True)
        return estado, None

    def telegram_offset(self, nuevo=None):
        return self.request("/rest/v1/rpc/cuna_telegram_offset", {"new_value": nuevo}, admin=True)

    def telegram_procesador(self, dueno, ahora=None, liberar=False):
        return self.request("/rest/v1/rpc/cuna_telegram_lease", {"owner_id": dueno, "release": liberar}, admin=True)

    def cola_reclamar(self, token, limite=10, ahora=None):
        return self.request("/rest/v1/rpc/cuna_queue_claim", {"token": token, "lim": limite}, admin=True)

    def cola_resultado(self, trabajo, tipo=None, error=None, reintentar_en=None, bloqueado=False, ahora=None):
        return self.request("/rest/v1/rpc/cuna_queue_result", {
            "job": trabajo["id"], "token": trabajo["token"], "kind": "ok" if tipo is None else tipo,
            "err": error, "retry_after": int(reintentar_en or 0), "blocked": bool(bloqueado)}, admin=True)
