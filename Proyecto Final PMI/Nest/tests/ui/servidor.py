"""Servidor de prueba para tests/ui/verificar_exportacion.js. Usa una base temporal que borra al empezar."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import Application
from cuna.local import Local

ruta = Path(tempfile.gettempdir()) / "nido-ui-prueba.db"
for sufijo in ("", "-wal", "-shm", "-journal"):
    Path(str(ruta) + sufijo).unlink(missing_ok=True)
db = Local(ruta)
db.bootstrap("padre@example.test", "Demo_prueba01")
servidor = Application(("127.0.0.1", 8765), db)
servidor.bot_username = "cuna_prueba_bot"
print("listo", flush=True)
servidor.serve_forever()
