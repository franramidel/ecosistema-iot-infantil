"""Respaldo, verificación y restauración de la base SQLite de la demo.

    python respaldo.py crear
    python respaldo.py verificar datos/respaldos/nido-20261118-210000.sqlite3
    python respaldo.py restaurar datos/respaldos/nido-20261118-210000.sqlite3 --confirmar

Para Supabase usa docs/SCRUM-21-retencion.md. Detén el servidor antes de restaurar.
"""
import argparse
from pathlib import Path

from app import ROOT, settings
from cuna.retencion import RespaldoError, nombre_de_respaldo, respaldar, restaurar, verificar


def mostrar(informe):
    for tabla, cantidad in informe["conteos"].items():
        print(f"  {tabla}: {cantidad}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("accion", choices=("crear", "verificar", "restaurar"))
    parser.add_argument("archivo", nargs="?", help="Respaldo a verificar o restaurar")
    parser.add_argument("--db", help="Base SQLite. Por defecto CUNA_DB o datos/demo.sqlite3")
    parser.add_argument("--carpeta", help="Carpeta de respaldos. Por defecto datos/respaldos")
    parser.add_argument("--confirmar", action="store_true", help="Necesario para restaurar")
    args = parser.parse_args()
    cfg = settings()
    base = Path(args.db or cfg.get("CUNA_DB", ROOT / "datos" / "demo.sqlite3"))
    try:
        if args.accion == "crear":
            destino = nombre_de_respaldo(args.carpeta or ROOT / "datos" / "respaldos")
            informe = respaldar(base, destino)
            print(f"Respaldo creado y verificado: {destino}")
            mostrar(informe)
        else:
            if not args.archivo:
                parser.error("Indica el archivo de respaldo.")
            if args.accion == "verificar":
                informe = verificar(args.archivo)
                print("Respaldo válido." if informe["ok"] else "Respaldo con problemas: " + "; ".join(informe["problemas"]))
                for aviso in informe.get("avisos", []):
                    print("Aviso: " + aviso)
                mostrar(informe)
                parser.exit(0 if informe["ok"] else 1)
            if not args.confirmar:
                parser.error("Restaurar reemplaza la base actual. Agrega --confirmar.")
            resultado = restaurar(args.archivo, base)
            print(f"Base restaurada en {base}")
            if resultado["copia_previa"]:
                print(f"La base anterior se conservó en {resultado['copia_previa']}")
            mostrar(resultado["informe"])
    except RespaldoError as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
