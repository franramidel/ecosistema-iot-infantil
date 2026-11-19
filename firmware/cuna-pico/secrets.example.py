# Copia este archivo como secrets.py en la placa (Thonny: Guardar como -> Raspberry Pi Pico).
# secrets.py NO se sube a GitHub: está en .gitignore. Nunca pongas aquí claves de producción en el repositorio.

WIFI_SSID = "nombre-de-la-red"
WIFI_PASSWORD = "clave-del-wifi"

# Identificador y clave del dispositivo: se crean en la app (Dispositivos -> agregar). La clave se muestra una sola vez.
DEVICE_ID = "CUNA-1"
DEVICE_KEY = "clave-del-dispositivo"

# MODO "servidor": el dispositivo habla con app.py (misma red Wi-Fi que el computador que lo ejecuta).
MODO = "servidor"
SERVIDOR_URL = "http://192.168.1.50:8000"   # IP del computador con app.py; usa ipconfig para verla

# MODO "supabase": el dispositivo habla con Supabase (solo con las migraciones 003-008 aprobadas y desplegadas).
# MODO = "supabase"
# SUPABASE_URL = "https://xxxxxxxx.supabase.co"
# SUPABASE_KEY = "clave-publica-anon"   # la pública (anon), nunca la secreta (service_role)
