"""Encola una alerta de prueba sin usar el navegador (SCRUM-24). Sirve para comprobar CA2.

    python solicitar_alerta.py --correo padre@example.test            SQLite, busca al usuario por correo
    python solicitar_alerta.py --usuario <uuid>                       SQLite o Supabase, por id de usuario
    python solicitar_alerta.py --supabase --usuario <uuid>            Supabase

Llama a solicitar_notificacion, la misma función que usará SCRUM-25. No envía nada por sí mismo: la solicitud
queda en la cola y la envía telegram_worker.py. Con --supabase necesita la clave de servicio en el entorno.

Con --supabase escribe una fila de prueba en la base del proyecto configurado. Úsalo solo en un proyecto de pruebas,
nunca en el Supabase compartido del equipo sin autorización. Por eso pide confirmación explícita antes de escribir:
escribe la frase exacta cuando el script la pida, o pasa --confirmo-escribir-en-supabase si no hay terminal.
"""
import argparse
import secrets
import sys
from urllib.parse import urlsplit

from app import settings
from cuna.common import Problem
from cuna.local import Local
from cuna.telegram import ConfigError
from telegram_worker import crear_almacen


FRASE = "ESCRIBIR EN SUPABASE"


def confirmado(args, cfg):
    """Pide confirmación antes de escribir en Supabase. Muestra solo el servidor, nunca claves ni datos de personas."""
    servidor = urlsplit(cfg.get("SUPABASE_URL") or "").hostname or "(sin SUPABASE_URL)"
    print(f"Esto escribirá una fila de prueba en la cola de notificaciones del proyecto Supabase en {servidor}.\n"
          "Úsalo solo en un proyecto de pruebas, no en el Supabase compartido sin autorización del equipo.")
    if args.confirmo_escribir_en_supabase:
        return True
    if not sys.stdin.isatty():
        print("No hay terminal para confirmar. Repite con --confirmo-escribir-en-supabase si es un proyecto de pruebas.")
        return False
    try:
        respuesta = input(f"Para continuar escribe {FRASE}: ").strip()
    except EOFError:
        print("No se recibió confirmación. Repite con --confirmo-escribir-en-supabase si es un proyecto de pruebas.")
        return False
    return respuesta == FRASE


def main(argv=None):
    parser = argparse.ArgumentParser(description="Encola una alerta de prueba")
    parser.add_argument("--supabase", action="store_true")
    parser.add_argument("--usuario", help="Id del usuario destinatario")
    parser.add_argument("--correo", help="Correo del destinatario (solo SQLite)")
    parser.add_argument("--titulo", default="Alerta de prueba sin navegador")
    parser.add_argument("--confirmo-escribir-en-supabase", action="store_true",
                        help="Confirma sin preguntar que se puede escribir una fila de prueba en Supabase")
    args = parser.parse_args(argv)
    if bool(args.usuario) == bool(args.correo):
        parser.error("Indica --usuario o --correo, pero no ambos.")
    try:
        store, nombre = crear_almacen(args.supabase, settings())
    except ConfigError as exc:
        parser.exit(2, f"Configuración: {exc}\n")
    usuario = args.usuario
    if args.supabase and not confirmado(args, settings()):
        parser.exit(4, "Cancelado. No se escribió nada en Supabase.\n")
    if args.correo:
        if args.supabase:
            parser.error("--correo solo funciona con SQLite. Con Supabase usa --usuario.")
        with store.connection() as db:
            fila = db.execute("select id from members where email=?", (args.correo.strip().lower(),)).fetchone()
        if not fila:
            parser.exit(2, "No hay un usuario con ese correo.\n")
        usuario = fila["id"]
    try:
        resultado = store.solicitar_notificacion(usuario, "manual-" + secrets.token_hex(6), titulo=args.titulo, estado="Informativo")
    except Problem as exc:
        parser.exit(2, f"No se pudo encolar ({exc.status}): {exc}\n")
    print(resultado)
    if not resultado["vinculado"]:
        parser.exit(3, "El usuario no tiene Telegram vinculado: la solicitud quedó en sin_vinculo y no se enviará.\n")


if __name__ == "__main__":
    main()
