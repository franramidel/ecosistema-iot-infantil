# Análisis: Device_ID de texto frente al UUID del SRS

> **Estado: PENDIENTE DE APROBACIÓN DE SEBASTIÁN.** Este diseño modifica `devices`, una tabla compartida de su módulo. No se ha desplegado en ningún Supabase. No hay que presentarlo como seguro para desplegar hasta que él lo apruebe. Ver `INTEGRACION-pendientes.md`.

Estado: la opción C está implementada y probada. Quedan pendientes la aprobación del equipo y del profesor y la confirmación de lo que exige el SRS sobre la clave primaria. El diseño final está en `uuid-dispositivos.md`.

## 1. Por qué se conservó el identificador de texto

La Persona 1 ya había definido `devices.id` como texto, por ejemplo `TEMP-01`, y lo documentó en el README como Device_ID. Ese identificador lo escribe una persona al registrar el dispositivo, lo usa el simulador, lo usará la Raspberry Pi en su configuración, aparece en la URL de ingreso y en todas las pruebas del equipo. Además es la clave foránea de `assignments`, `events` y `device_keys`, y en Supabase de claves compuestas con `family_id`. Cambiarlo en SCRUM-20 habría roto trabajo ajeno sin que nadie lo hubiera aprobado.

## 2. Qué criterio se incumple literalmente

SCRUM-20, CA1: `ID_Dispositivo: UUID, clave foránea`. La implementación guarda un texto con clave foránea. La clave foránea y la integridad referencial sí se cumplen, pero el tipo no. SCRUM-23, CA3, hereda el problema, porque el Excel debe llevar las columnas de RF-DAT-01.

## 3. Consecuencias de migrar a UUID

Migrar `devices.id` a UUID obliga a cambiar la clave primaria y todas sus referencias. Afecta las tablas `assignments`, `events`, `audit_log` y `device_keys`, las claves compuestas de Supabase, las funciones `cuna_change` y `cuna_ingest`, el validador `device_id` en `common.py`, el cuerpo de `POST /api/events`, `simular.py`, la configuración de la Raspberry Pi, el README y todas las pruebas. Hay que convertir los datos existentes. La ventaja es el cumplimiento literal. Los costos son un cambio que rompe la compatibilidad, un identificador que ya no se puede leer y coordinación con todo el equipo.

## 4. Cómo cumplir el requisito sin romper lo actual

Opción C, propuesta. Se agrega una columna `devices.uuid` de tipo UUID, única, generada automáticamente. `devices.id` sigue siendo el texto con el que los dispositivos se identifican. `audit_log` agrega `device_uuid` como clave foránea hacia `devices.uuid`, que un trigger llena a partir de `device_id`. La API de auditoría y la exportación devuelven `device_uuid` como `ID_Dispositivo` y conservan el texto en una columna aparte con el nombre `Device_ID`. Los dispositivos siguen enviando lo mismo. Los cambios afectan una migración por motor, `auditoria.py` y las consultas. No tocan `events`, `assignments`, el simulador ni la Raspberry Pi.

## 5. Opciones y recomendación

| Opción | Cumple el SRS | Rompe a otros | Estado |
| --- | --- | --- | --- |
| A. Mantener texto, documentar la desviación | No, literalmente | No | Reemplazada por C |
| B. Migrar `devices.id` a UUID | Sí | Sí, a varios módulos | No implementada |
| C. Agregar `devices.uuid` y exponerlo como ID_Dispositivo | Sí, en la bitácora y en la exportación | No | Implementada, pendiente de aprobación |

Recomiendo C. Cumple el criterio donde se mide, que son la tabla de auditoría y el Excel, sin tocar el contrato de ingreso que otros ya usan. Conviene decidirlo antes de SCRUM-23, porque la exportación necesita el nombre y el tipo de columna definitivos. Antes de implementarla hay que preguntar al profesor si el SRS exige que sea la clave primaria o basta con que el dato exista como UUID con integridad referencial.
