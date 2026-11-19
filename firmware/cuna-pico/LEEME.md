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
- Búfer sin conexión: los eventos pendientes se guardan en `cola.jsonl` en la flash de la Pico, hasta 100, y sobreviven a un reinicio. Se reenvían en orden con su `event_id` y su `captured_at` originales; el servidor ignora los repetidos. Si se llena, se pierde primero el más viejo. El archivo se borra cuando la cola queda vacía. Descarta con 409 (sin asignar) o 403 (clave mala).
- Hora por NTP, resincronizada cada hora. Los umbrales se refrescan cada 60 s.

Pruebas sin hardware: `python3 -m unittest discover -s tests -p "test_firmware*.py"` desde `Nest`.

## Búfer sin conexión: límites
- Con red, la cola se escribe a la flash solo cuando cambia, no en cada lectura. Si se corta la energía con red, pueden perderse las lecturas aún no guardadas (unos 2 s).
- Si la placa reinicia sin red, no puede captar eventos nuevos hasta sincronizar la hora por NTP, porque cada evento lleva su hora real.
- Líneas dañadas o cortadas del archivo se ignoran. Un evento que el servidor rechaza para siempre se descarta para no bloquear la cola.
- Las pruebas (`test_firmware_cola.py`) usan sustitutos de `machine`, `dht`, `network` y `ntptime`. No prueban la flash ni el Wi-Fi reales.
