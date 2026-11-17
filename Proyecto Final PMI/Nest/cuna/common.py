"""Tipos y validación compartidos por la demo, Supabase y el servidor."""
import hashlib
import math
import re
import secrets
import uuid
from datetime import datetime, timezone


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def uid():
    return str(uuid.uuid4())


def instant(value=None):
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError()
        return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        raise Problem("Usa una fecha ISO 8601 con zona horaria.")


def field(data, key, limit=80):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Problem("Revisa el campo " + key + ".")
    return value.strip()


def email_address(data):
    email = field(data, "email", 254).lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise Problem("Escribe un correo válido.")
    return email


def member_input(data):
    email = email_address(data)
    password = data.get("password")
    if not isinstance(password, str) or not 10 <= len(password) or len(password.encode()) > 72:
        raise Problem("Usa al menos 10 caracteres y hasta 72 bytes de contraseña.")
    role = data.get("role")
    operate = data.get("can_operate", False)
    if role not in ("admin", "caregiver") or not isinstance(operate, bool):
        raise Problem("Revisa el rol y los permisos.")
    return email, password, role, role == "admin" or operate


def device_id(data):
    value = field(data, "device_id", 64)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise Problem("Device_ID admite letras, números, puntos, guiones y guion bajo.")
    return value


def reading(data):
    try:
        event_id = str(uuid.UUID(data["event_id"]))
        value = data["value"]
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        raise Problem("Se requiere un event_id UUID y un valor numérico finito.")
    at = instant(field(data, "captured_at"))
    if at > instant():
        raise Problem("La captura está en el futuro. Revisa el reloj del dispositivo.")
    return event_id, at, field(data, "metric", 40), float(value), field(data, "unit", 16)


def device_secret():
    return secrets.token_urlsafe(32)


def fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()

