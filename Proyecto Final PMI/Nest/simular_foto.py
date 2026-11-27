"""Sube una foto por el mismo punto de entrada que usará la cámara del kit de seguridad (POST /api/photos).

    python simular_foto.py --device KIT-01                 genera una imagen PNG de prueba
    python simular_foto.py --device KIT-01 --archivo a.jpg  sube un JPEG o PNG real

La clave del dispositivo se lee de CUNA_DEVICE_KEY o se pide por terminal. Para reintentar tras un fallo de red usa
--evento con el mismo UUID: el servidor no duplica la foto.
"""
import argparse
import os
import struct
import zlib
from getpass import getpass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cuna.common import instant, uid


def png_de_prueba(ancho=320, alto=240, semilla=0):
    """PNG degradado, sin dependencias. Cada foto sale con un color distinto para distinguirlas en la galería."""
    filas = b"".join(b"\x00" + b"".join(bytes(((x * 255 // ancho + semilla * 37) % 256, y * 255 // alto, (semilla * 91) % 256)) for x in range(ancho))
                     for y in range(alto))
    def trozo(tipo, datos):
        cuerpo = tipo + datos
        return struct.pack(">I", len(datos)) + cuerpo + struct.pack(">I", zlib.crc32(cuerpo))
    return (b"\x89PNG\r\n\x1a\n" + trozo(b"IHDR", struct.pack(">IIBBBBB", ancho, alto, 8, 2, 0, 0, 0))
            + trozo(b"IDAT", zlib.compress(filas)) + trozo(b"IEND", b""))


def enviar_foto(url, clave, device, datos, tipo, evento=None, capturada=None):
    cab = {"Authorization": "Bearer " + clave, "Content-Type": tipo, "X-Device-Id": device,
           "X-Event-Id": evento or uid(), "X-Captured-At": capturada or instant()}
    with urlopen(Request(url.rstrip("/") + "/api/photos", data=datos, headers=cab), timeout=20) as r:
        import json
        return json.load(r)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--device", required=True)
    p.add_argument("--archivo")
    p.add_argument("--evento")
    p.add_argument("--fecha", help="Instante de captura ISO 8601 con zona horaria")
    args = p.parse_args()
    clave = os.getenv("CUNA_DEVICE_KEY") or getpass("Clave del dispositivo: ")
    if args.archivo:
        datos = open(args.archivo, "rb").read()
        tipo = "image/png" if datos.startswith(b"\x89PNG") else "image/jpeg"
    else:
        datos, tipo = png_de_prueba(semilla=int.from_bytes(os.urandom(1), "big")), "image/png"
    try:
        print(enviar_foto(args.url, clave, args.device, datos, tipo, args.evento, instant(args.fecha) if args.fecha else None))
    except HTTPError as exc:
        import json
        p.exit(1, json.load(exc).get("error", "Error") + "\n")
    except (URLError, TimeoutError):
        p.exit(1, "No se pudo contactar con el servidor. Conserva --evento y --fecha para reintentar.\n")


if __name__ == "__main__":
    main()
