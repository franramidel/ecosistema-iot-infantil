"""Procesador de notificaciones de Telegram (SCRUM-24). Se ejecuta en una terminal aparte del servidor.

    python telegram_worker.py                 usa la base SQLite local (CUNA_DB)
    python telegram_worker.py --supabase      usa el proyecto de Supabase configurado en .env
    python telegram_worker.py --una-vez       un solo ciclo y termina, útil para pruebas manuales

Debe usar el mismo almacén que el servidor: si el servidor corre con --supabase, el procesador también.
Necesita TELEGRAM_BOT_TOKEN (entorno o .env). Con --supabase necesita además SUPABASE_URL,
SUPABASE_PUBLISHABLE_KEY y SUPABASE_SECRET_KEY. La clave secreta solo la lee este proceso y el servidor.
"""
import argparse
import sys
import threading
from pathlib import Path

from app import ROOT, settings
from cuna.cloud import Cloud
from cuna.common import Problem
from cuna.local import Local
from cuna.procesador import Procesador, ProcesadorOcupado
from cuna.telegram import API, ClienteTelegram, ConfigError, ErrorTelegram, token_desde


def crear_almacen(supabase, cfg):
    """Mismo criterio que app.py: --supabase elige Cloud, si no SQLite. Lanza ConfigError si falta algo."""
    if supabase:
        faltan = [k for k in ("SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY", "SUPABASE_SECRET_KEY") if not cfg.get(k)]
        if faltan:
            raise ConfigError("Faltan variables de Supabase: " + ", ".join(faltan) + ".")
        try:
            return Cloud(cfg["SUPABASE_URL"], cfg["SUPABASE_PUBLISHABLE_KEY"], cfg["SUPABASE_SECRET_KEY"]), "Supabase"
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
    return Local(Path(cfg.get("CUNA_DB", ROOT / "datos" / "demo.sqlite3"))), "SQLite"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Procesador de notificaciones de Telegram")
    parser.add_argument("--supabase", action="store_true", help="Usar el proyecto de Supabase configurado en .env")
    parser.add_argument("--una-vez", action="store_true", help="Un solo ciclo, sin esperar mensajes del bot")
    parser.add_argument("--espera", type=int, default=5, help="Segundos de long polling por ciclo (1 a 50)")
    args = parser.parse_args(argv)
    cfg = settings()
    try:
        cliente = ClienteTelegram(token_desde(cfg), cfg.get("TELEGRAM_API_BASE") or API)
        store, nombre = crear_almacen(args.supabase, cfg)
        yo = cliente.identidad()
    except ConfigError as exc:
        parser.exit(2, f"Configuración: {exc}\n")
    except ErrorTelegram as exc:
        parser.exit(2, f"No se pudo validar el bot con Telegram: {exc}\n")
    procesador = Procesador(store, cliente)
    print(f"Procesador iniciado para @{yo.get('username', '?')} con almacén {nombre}. Ctrl+C para detener.", file=sys.stderr, flush=True)
    parar = threading.Event()
    try:
        if args.una_vez:
            print(procesador.ciclo(0), flush=True)
        else:
            procesador.ejecutar(parar, max(1, min(args.espera, 50)))
    except ProcesadorOcupado as exc:
        parser.exit(3, f"{exc}\n")
    except Problem as exc:
        parser.exit(5, f"El almacén de datos respondió con error ({exc.status}): {exc}\n")
    except KeyboardInterrupt:
        parar.set()
    finally:
        try:
            store.telegram_procesador(procesador.dueno, liberar=True)
        except Problem:
            pass
    if procesador.token_invalido:
        parser.exit(4, "Telegram rechazó el token. Corrige TELEGRAM_BOT_TOKEN y vuelve a iniciar.\n")
    if procesador.almacen_rechazado:
        parser.exit(5, "El almacén rechazó las credenciales. Revisa SUPABASE_SECRET_KEY y que sea la clave de servicio.\n")


if __name__ == "__main__":
    main()
