"""Envía una medición por el mismo punto de entrada que utilizará la Raspberry Pi."""
import argparse
import json
import os
from getpass import getpass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cuna.common import instant, uid


def enviar_lectura(url, clave, evento):
    """Conservar evento (incluido su UUID) para cualquier reintento."""
    req = Request(url.rstrip("/") + "/api/events", data=json.dumps(evento).encode(),
                  headers={"Authorization":"Bearer "+clave, "Content-Type":"application/json"})
    with urlopen(req,timeout=15) as response:
        return json.load(response)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url",default="http://127.0.0.1:8000")
    p.add_argument("--device",required=True)
    p.add_argument("--valor",type=float,required=True)
    p.add_argument("--medida",default="Temperatura")
    p.add_argument("--unidad",default="°C")
    p.add_argument("--fecha",help="Instante de captura ISO 8601 con zona horaria")
    p.add_argument("--evento",help="UUID original al reintentar una lectura")
    args = p.parse_args()
    clave = os.getenv("CUNA_DEVICE_KEY") or getpass("Clave del dispositivo: ")
    evento = dict(event_id=args.evento or uid(),device_id=args.device,captured_at=instant(args.fecha),
                  metric=args.medida,value=args.valor,unit=args.unidad)
    print(json.dumps(evento,ensure_ascii=False))
    try:
        resultado = enviar_lectura(args.url,clave,evento)
    except HTTPError as exc:
        p.exit(1,json.load(exc)["error"]+"\n")
    except (URLError,TimeoutError):
        p.exit(1,"No se pudo contactar con el servidor. Conserva el UUID y la fecha para reintentar.\n")
    print("Lectura guardada en Child_ID",resultado["child_id"])


if __name__ == "__main__":
    main()

