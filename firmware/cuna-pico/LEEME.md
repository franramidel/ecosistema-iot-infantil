# Firmware Pico W (cuna)

Lee un DHT22 (GP15), enciende un LED (GP14) y envía Temperatura y Humedad al servidor o a Supabase.

## Puesta en marcha
1. En la app: registra el dispositivo (Device_ID) y copia su clave. Asígnalo a un bebé.
2. Copia `secrets.example.py` como `secrets.py` en la Pico y llénalo (WiFi, `DEVICE_ID`, `DEVICE_KEY`, `MODO`, `SERVIDOR_URL`). `secrets.py` no se sube a git.
3. En `main.py` cambia `ENVIAR = False` a `True`.
4. Copia `main.py` y `secrets.py` a la Pico (Thonny). Para que arranque solo, guárdalo como `main.py`.
5. En la PC: `python app.py` (accesible por la red local) y `python telegram_worker.py --espera 1` con `TELEGRAM_BOT_TOKEN`.

## Comportamiento
- Lee cada 2 s. Envía si la temperatura cambia ≥ 0,5 °C, de inmediato al cruzar un umbral y cada 30 s como latido.
- Hasta 20 eventos esperan en memoria y se reintentan en orden. Descarta con 409 (sin asignar) o 403 (clave mala).
- Hora por NTP, resincronizada cada hora. Los umbrales se refrescan cada 60 s.

Pruebas sin hardware: `python3 -m unittest discover -s tests -p "test_firmware.py"` desde `Nest`.
