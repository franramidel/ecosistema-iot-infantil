# Alertas de temperatura y umbrales (RF-CUN-01, RF-CUN-02, RF-NOT-02)

## Regla
- Rango por defecto 19 a 22 °C, configurable por bebé (solo administrador).
- Alerta solo si `valor > máximo` o `valor < mínimo`. Los límites exactos no alertan.
- Se avisa **solo al cambiar de estado** (normal → calor/frío, calor ↔ frío). Seguir fuera de rango no repite; volver a normal no avisa.
- La humedad se guarda pero no alerta.
- Una lectura atrasada (anterior al último cambio de estado) no deshace el estado.

## Flujo
1. El dispositivo envía `Temperatura` en `°C` (`POST /api/events` o RPC `cuna_ingest`).
2. En la misma transacción se evalúa el estado (`cuna/alertas.py` o trigger `alert_temperature`).
3. Si cambió a calor/frío: fila en la auditoría (`ALERTA_TEMPERATURA_ALTA` o `_BAJA`, severidad crítica) y un aviso en la cola por cada miembro (clave `temp:{evento}:{miembro}`, idempotente).
4. El worker de Telegram (SCRUM-24) envía la cola. El mensaje lleva temperatura, límite, dispositivo y estado Crítico; **no** lleva el nombre del bebé.
5. Si el aviso no se puede encolar, la lectura tampoco se guarda (atomicidad).

## Latencia (< 2 s)
Corre el worker con `--espera 1`: `python telegram_worker.py --espera 1`. Con el valor por defecto (5 s) un aviso puede tardar más de 2 s.

## Firmware (`firmware/cuna-pico`)
- Pico W + DHT22 en GP15, LED en GP14.
- Copia `secrets.example.py` a `secrets.py` en la Pico y llénalo. Pon `ENVIAR = True` en `main.py`.
- Envía cada 30 s, al cambiar ≥ 0,5 °C y de inmediato al cruzar un umbral (los umbrales se refrescan cada 60 s). Guarda hasta 20 eventos en memoria y reintenta.
- Si el dispositivo no está asignado (409) o la clave es incorrecta (403), descarta el evento.

## Pruebas
- `tests/test_alertas.py` (32), `tests/test_firmware.py` (17), `tests/test_alertas_nube.py` (16, mitad contra PostgreSQL con `PGTEST_URL`).
- `tests/postgres/escenario_008.sql` (43 comprobaciones).

## Pendiente / límites
- 008 **no está aprobada** para el Supabase compartido; solo se ejecutó en PostgreSQL local.
- Los tipos de auditoría de alerta no están en `RESERVADOS`: un dispositivo podría falsificar esas filas de auditoría (no generan aviso).
- Prueba de punta a punta con la Pico real: pendiente (requiere hardware).
