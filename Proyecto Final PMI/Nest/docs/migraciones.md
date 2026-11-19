# Migraciones

> **Estado: ninguna migración de Supabase (003 a 008) está aprobada para desplegar.** Solo se ejecutaron en PostgreSQL local. No se ejecutaron en el Supabase compartido ni en uno de pruebas. Las que tocan tablas de otro módulo esperan aprobación. Ver `INTEGRACION-pendientes.md`.

## SQLite

No hay un archivo por migración. `Local.__init__` llama a `migraciones.migrar(ruta, esquema_base)` cada vez que arranca. La función es idempotente y usa la versión guardada en `pragma user_version`.

| Versión | Contenido |
| --- | --- |
| 0 | Cualquier base anterior: la original del equipo o la de SCRUM-20 y SCRUM-21, que se entregaron sin `user_version`. La migración reconoce qué falta mirando la estructura |
| 3 | UUID de dispositivos y clave foránea en `audit_log` |
| 4 | Tablas de Telegram: vínculos, códigos, intentos, estado, arrendamiento y cola |
| 4 (sin cambio de número) | `thresholds` y `alert_state` (alertas de temperatura). Se crean con `create ... if not exists` en cada arranque |

Pasos de `migrar`, todos en una transacción `begin immediate` con claves foráneas apagadas:

1. Crea `devices` con UUID si no existe. Si existe sin `uuid`, la reconstruye y comprueba el conteo de filas.
2. Aplica el esquema base con `create ... if not exists`.
3. Crea el trigger que protege el UUID.
4. Crea `audit_log` o, si falta `device_uuid`, la reconstruye copiando `rowid` y verificando el conteo. Un registro huérfano rompe `not null` y revierte todo.
5. Recrea índices y triggers de auditoría.
6. Rellena el historial pendiente con `source_event_id` para no duplicar.
7. Corre `pragma foreign_key_check`. Si hay filas rotas, aborta.
8. Crea las tablas de Telegram con `create ... if not exists`.
9. Fija `user_version = 4` y confirma.

Cualquier excepción hace `rollback` y se informa como `MigracionError`. La función recibe un gancho `antes_de_confirmar` que solo usan las pruebas para simular un fallo.

**Antes de migrar una base real**, hazle un respaldo con `python respaldo.py crear`.

## PostgreSQL y Supabase

Se ejecutan a mano, en orden, en el editor SQL. Cada archivo es una transacción.

| Archivo | Contenido | Idempotente |
| --- | --- | --- |
| `001_usuarios.sql` | Base del equipo | No, es de la Persona 1 |
| `002_primer_administrador.sql` | Primer administrador | Del equipo |
| `003_auditoria.sql` | `audit_log`, triggers, RLS, `cuna_record` | No |
| `004_retencion.sql` | Bloqueo de `TRUNCATE` y borrado de asignaciones | No |
| `005_device_uuid.sql` | UUID de dispositivos y claves foráneas | Sí |
| `006_exportacion.sql` | Vista `audit_export` | Sí |
| `007_telegram.sql` | Tablas, RLS y funciones de la cola de Telegram. Doce funciones: ocho para el procesador y SCRUM-25, que exigen `service_role`, y cuatro del usuario, que usan `auth.uid()` | Sí |
| `008_alertas_temperatura.sql` | Tabla `thresholds`, estado `cuna_private.alert_state`, trigger `alert_temperature` sobre `events` y RPC `cuna_get_thresholds`, `cuna_set_thresholds` y `cuna_device_thresholds`. Crea la alerta, la auditoría y los avisos en la misma transacción que la lectura | Sí |
| `verificar_retencion.sql` | Diez comprobaciones de solo lectura, todas deben dar `true` | Sí |

Ejecuta primero en un proyecto de pruebas. Haz respaldo antes de producción. No las ejecutes en el Supabase compartido sin autorización del equipo: 004 y 005 cambian tablas de otro módulo, y 003, 006 y 007 dependen de ellas.

## Cómo se probó

`tests/postgres/ejecutar.sh` aplica 001, 003, 004 en una base nueva, corre los escenarios 003 y 004 que dejan datos, inyecta un fallo en 005 y comprueba que no queda nada a medias, aplica 005 dos veces y compara UUID y conteos, y corre 006, 007 y 008 dos veces y los escenarios 005, 006, 007 y 008. Con `PGURL_VACIA` repite la secuencia completa sobre una base vacía.

Es PostgreSQL 16 local con un sustituto mínimo de los roles de Supabase. No es Supabase. Los escenarios y el sustituto se niegan a correr si la sesión no declara `cuna.entorno_desechable=si`, que solo ponen `ejecutar.sh`, `respaldo_restauracion.sh` y las pruebas automáticas. Los scripts también rechazan URL de Supabase.
