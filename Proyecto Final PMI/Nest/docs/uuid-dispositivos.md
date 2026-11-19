# UUID de dispositivos: diseño de doble identificador

> **Estado: PENDIENTE DE APROBACIÓN DE SEBASTIÁN.** Este diseño modifica `devices`, una tabla compartida de su módulo. No se ha desplegado en ningún Supabase. No hay que presentarlo como seguro para desplegar hasta que él lo apruebe. Ver `INTEGRACION-pendientes.md`.

Corrige la desviación que quedó documentada en SCRUM-20. Implementa la opción C del análisis en `analisis-device-id-uuid.md`.

## Qué se decidió

Cada dispositivo tiene dos identificadores con funciones distintas.

| Identificador | Dónde | Quién lo usa | Cambia |
| --- | --- | --- | --- |
| `devices.id`, por ejemplo `TEMP-01` | texto, clave primaria | Simulador, Raspberry Pi, URL de ingreso, pantallas, claves foráneas de otros módulos | Nunca. Lo escribe una persona al registrar el dispositivo |
| `devices.uuid` | UUID v4, único | Auditoría y Excel como `ID_Dispositivo` | Nunca. Lo asigna la base una sola vez |

`audit_log.device_uuid` es una clave foránea real hacia `devices.uuid`. Además hay una clave compuesta `(device_id, device_uuid)` hacia `devices(id, uuid)`. Con ella un registro no puede mezclar el texto de un dispositivo con el UUID de otro.

## Qué no se tocó

`assignments`, `events` y `device_keys` siguen apuntando a `devices.id`. El contrato de `POST /api/events` y de `POST /api/audit/events` no cambió. El simulador y la configuración de la Raspberry Pi siguen igual. Quien ya usaba `TEMP-01` no nota nada.

## Aprobación pendiente

No tuve el SRS completo. El criterio de SCRUM-20 dice `ID_Dispositivo: UUID, clave foránea` y el de SCRUM-23 exige que el Excel lleve ese UUID. La solución cumple ambos donde se miden, que son la auditoría y el archivo. Si el SRS exige que la clave primaria de `devices` sea el UUID, la solución de doble identificador necesita aprobación del equipo y del profesor, porque no cumple esa lectura literal. Hay que preguntarlo antes de la defensa.

## SQLite

`devices` y `audit_log` se reconstruyen dentro de una sola transacción, porque SQLite no puede agregar una columna única y no nula con valor distinto por fila. El módulo `cuna/migraciones.py` hace la reconstrucción.

- UUID por defecto: una expresión SQL con `randomblob` que produce un UUID v4. Una restricción `check` exige el formato.
- `unique(uuid)` y `unique(id, uuid)`.
- Trigger `devices_keep_uuid`: impide cambiar el UUID con `update`.
- `audit_log` se copia con su `rowid` original, así el orden histórico queda igual.
- Los triggers de auditoría se vuelven a crear y ahora guardan `device_uuid`.

## PostgreSQL y Supabase

`supabase/005_device_uuid.sql` usa el tipo `uuid` real. Es idempotente y corre en una transacción. Agrega `devices.uuid` con `gen_random_uuid()`, completa `audit_log.device_uuid` para el historial, lo marca `not null` y crea las claves foráneas. Para completar el historial desactiva el trigger `audit_immutable` dentro de la misma transacción y lo reactiva antes de confirmar. Si algo falla, la transacción entera se revierte.

`cuna_private.audit_clock()` se redefine para llenar `device_uuid` desde `devices` cuando no viene dado. Por eso `cuna_record` y los triggers de 003 no necesitaron cambios. Si alguien da un UUID que no corresponde al dispositivo, la clave compuesta lo rechaza.

## Cómo se verificó

Las pruebas están en `tests/test_uuid.py` para SQLite y en `tests/postgres/escenario_005.sql` y `ejecutar.sh` para PostgreSQL 16 local. Cubren los doce casos pedidos: dispositivo existente con UUID asignado, dispositivo nuevo con UUID válido, persistencia tras reiniciar, unicidad, integridad de la clave foránea, asociación correcta en la auditoría, historial conservado, identificadores tipo `TEMP-01`, triggers, migración desde una base antigua, migración repetida sin duplicar y fallo a mitad de la migración con reversión.

## Ejemplo

Entrada: un dispositivo registrado como `TEMP-01` que ya tiene 3 lecturas.

Salida de `GET /api/audit?device=TEMP-01`, que acepta también el UUID:

```json
{"items":[{"id":"...","occurred_at":"2026-03-05T12:00:00.000000+00:00","event_date":"2026-03-05","event_time":"06:00:00",
 "event_type":"LECTURA","detail":"Temperatura: 22.0 °C","child_id":"ad163f7c-...","device_id":"TEMP-01",
 "device_uuid":"3b0c9a52-6f1e-4c0a-9d5e-8c7a1f2b4d10","severity":"info"}],"next":null}
```

## Preguntas posibles del profesor

**¿Por qué no cambiar la clave primaria a UUID?** Obliga a migrar `assignments`, `events`, `device_keys`, las funciones de Supabase, el simulador y la Raspberry Pi. Rompe el trabajo de otros y el contrato de ingreso, sin ganar nada que el UUID adicional no dé.

**¿Cómo se garantiza que el UUID no cambia?** Una restricción `unique`, un trigger que bloquea `update` y una prueba que reinicia el servidor y compara.

**¿Qué pasa si la migración se corta?** Todo ocurre en una transacción. Hay pruebas que inyectan un fallo antes de confirmar y verifican que la base queda byte a byte igual, y otra con un registro huérfano.

**¿Se pierde historial?** No. Se compara el conteo de filas antes de borrar la tabla vieja, se conserva el orden por `rowid` y las pruebas verifican que cada fila de auditoría conserva su contenido.

**¿Por qué una clave compuesta además de la simple?** La simple garantiza que el UUID existe. La compuesta garantiza que ese UUID es el del mismo dispositivo que el texto.
