import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def capturar(carpeta="fotos", timeout=20):
    """Toma una foto de la cámara y devuelve la ruta del archivo."""
    url = os.environ.get("CAMARA_URL")
    if not url:
        raise RuntimeError("Falta la variable de entorno CAMARA_URL")
    Path(carpeta).mkdir(exist_ok=True)
    momento = datetime.now(timezone.utc)
    ruta = Path(carpeta) / ("captura-" + momento.strftime("%Y%m%d-%H%M%S") + ".jpg")
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-rtsp_transport", "tcp",
         "-i", url, "-frames:v", "1", "-update", "1", str(ruta)],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not ruta.exists():
        raise RuntimeError("ffmpeg falló: " + r.stderr.replace(url, "<camara>").strip()[-300:])
    return ruta


if __name__ == "__main__":
    print("Foto guardada en:", capturar())