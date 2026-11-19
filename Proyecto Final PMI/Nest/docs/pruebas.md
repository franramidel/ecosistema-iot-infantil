# Pruebas automatizadas y verificaciones

Fecha de la última ejecución: 18 de noviembre de 2026. Entorno: Linux, Python 3.13, SQLite 3.53.1, PostgreSQL 16.15 local, openpyxl 3.1.5, Node 22 con jsdom 24, LibreOffice.

## Cómo ejecutarlas

```powershell
python -m unittest discover -s tests -v
```

| Módulo | Pruebas | Qué cubre |
| --- | --- | --- |
| `test_historias.py` | 6 | Las originales del equipo. Sin cambios |
| `test_auditoria.py` | 14 | SCRUM-20: triggers, endpoints, idempotencia, concurrencia, traslados, cursor |
| `test_cloud_auditoria.py` | 4 | Forma de las consultas de `Cloud` con un doble de pruebas |
| `test_retencion.py` | 18 | SCRUM-21: inmutabilidad, respaldo, restauración, verificación, sin código que borre datos |
| `test_uuid.py` | 16 | UUID de dispositivos y migración desde una base antigua |
| `test_exportar.py` | 31 | SCRUM-23: filtros, seguridad, contenido del Excel, volumen y SQLite frente a Supabase |
| `test_telegram.py` | 52 | SCRUM-24: configuración, vinculación, envío, reintentos, cola, concurrencia, integración y endpoints, con un Telegram simulado |
| `test_telegram_nube.py` | 56 | SCRUM-24 con SQLite y Supabase: 22 casos de contrato ejecutados contra los dos almacenes, 12 propios de Supabase. Los de Supabase se omiten sin `PGTEST_URL` |
| Total | 197 | Con `PGTEST_URL` pasan 197. Sin él pasan 163 y se omiten 34 |

Se agregaron 47 pruebas a las 42 de la entrega anterior en SCRUM-20 a SCRUM-23, y 108 más en SCRUM-24. No se borró ninguna. Dos pruebas anteriores se adaptaron por el cambio de esquema y por la regla de que el código no borra datos: la de claves foráneas de la auditoría, que ahora tiene control positivo y una causa de fallo por caso, y la de migración, que usa el esquema anterior guardado en `tests/fixtures/esquema_scrum21.sql`.

## Los veinte escenarios de exportación

| Escenario | Prueba |
| --- | --- |
| Exportación vacía | `test_exportacion_vacia_es_un_libro_valido_con_encabezados` |
| Un registro | `test_un_solo_registro` |
| Muchos registros | `test_muchos_registros_abarcan_varias_paginas_sin_perder_ni_repetir` |
| Filtro por bebé | `test_filtro_por_bebe` |
| Filtro por dispositivo | `test_filtro_por_dispositivo_con_id_o_con_uuid` |
| Fecha inicial y final | `test_fecha_inicial_y_final_son_inclusivas`, `test_medianoche_local_pertenece_al_dia_que_empieza` |
| Todos los filtros | `test_todos_los_filtros_a_la_vez` |
| Fechas inválidas y rango invertido | `test_fechas_invalidas_y_rango_invertido`, `test_identificadores_mal_formados_o_inexistentes` |
| Acceso no autorizado | `test_sin_sesion_o_sin_permiso_no_hay_archivo` |
| Registros de otra familia | `test_otra_familia_no_se_ve_ni_manipulando_parametros` |
| Archivo `.xlsx` válido | `test_archivo_xlsx_valido_con_tipo_nombre_y_cabeceras_de_seguridad` |
| Encabezados del SRS | `test_encabezados_del_srs_en_ambas_hojas` |
| UUID correctos | `test_id_bebe_e_id_dispositivo_son_uuid_y_el_id_operativo_va_aparte` |
| Acentos y caracteres especiales | `test_acentos_y_caracteres_especiales` |
| Fórmulas maliciosas | `test_formulas_maliciosas_se_guardan_como_texto` |
| Volumen grande | `test_volumen_grande_no_consume_memoria_proporcional` |
| SQLite frente a Supabase | `test_nube_y_sqlite_exportan_lo_mismo_aunque_postgrest_recorte_las_paginas` |
| API anterior | `test_la_api_anterior_sigue_funcionando`, `test_la_api_publica_no_acepta_los_parametros_internos` |

Además: fechas con tipo y formato de Excel, un solo registro por hecho, resumen sin datos personales, archivos temporales borrados, limpieza si falla a mitad y eventos que llegan durante la exportación. El contenido del Excel se lee de vuelta con `openpyxl`. No se confía en la extensión.

## Telegram

Un servidor HTTP falso imita `api.telegram.org`. Ninguna prueba sale a Internet ni usa un token real. El token de las pruebas se arma en tiempo de ejecución y no sirve en Telegram.

Distinción de lo verificado:

| Nivel | Qué se hizo | Estado |
| --- | --- | --- |
| Simulación | 52 pruebas de `test_telegram.py` contra el servidor falso | Pasan |
| Integración local | Servidor web, procesador y SQLite reales, migración 3 a 4, Excel de SCRUM-23 con los nuevos eventos | Pasan |
| PostgreSQL local | `007_telegram.sql` dos veces y `escenario_007.sql` con 51 comprobaciones, en base con datos y base vacía | Pasan |
| Interfaz | `tests/ui/verificar_telegram.js` con jsdom, 14 comprobaciones | Pasan |
| Contrato SQLite y Supabase simulado | 22 casos idénticos contra `Local` y contra `Cloud` sobre PostgreSQL 16 local, con el puente `tests/puente_postgrest.py` | Pasan |
| Telegram real | No se hizo. Guía en `integracion-windows.md`, sección 8 | Pendiente |
| Supabase real | No se hizo. El puente no es PostgREST ni Supabase Auth | Pendiente |

### Cómo correr las pruebas de Supabase simulado

Necesitan PostgreSQL con permiso para crear bases y roles, y `psql` en el PATH. Cada prueba crea y borra una base propia a partir de una plantilla con las migraciones 001 a 007.

```powershell
$env:PGTEST_URL = "postgresql://postgres:<tu clave>@localhost:5432/postgres"
python -m unittest discover -s tests -v
```

La clave de PostgreSQL es la de tu instalación local. No se escribe en ningún archivo. Sin `PGTEST_URL` esas pruebas se omiten y las demás corren normal.

## Migraciones y PostgreSQL

`tests/postgres/ejecutar.sh` con dos bases de pruebas: una con datos de los escenarios 003 y 004 y otra vacía. Resultado: 001, 003, 004, 005, 006 y 007 aplicadas en orden, un fallo inyectado en 005 que revierte todo, 005 repetida sin cambiar UUID ni conteos, los escenarios 005, 006 y 007 sin fallas y la verificación con diez de diez filas en verdadero en ambas bases.

`tests/postgres/respaldo_restauracion.sh`: respaldo con `pg_dump`, restauración con `pg_restore` en una base nueva, mismo contenido en las siete tablas, mismos UUID de dispositivos, vista de exportación funcionando, inmutabilidad y políticas RLS conservadas.

## Verificaciones adicionales

- Interfaz: `tests/ui` con jsdom contra el servidor real. Catorce comprobaciones, todas correctas: el formulario aparece al administrador y no al cuidador, la petición lleva los cuatro filtros, el rango invertido no hace petición, el botón se bloquea durante la generación, se descarga un `.xlsx` con nombre descriptivo y los errores del servidor se muestran. jsdom no es un navegador real.
- Archivo: un `.xlsx` generado se abrió con LibreOffice. Las tres hojas cargan, las fechas y horas se muestran con el formato `yyyy-mm-dd` y `hh:mm:ss`, y un texto que empieza por `=` se muestra como texto.

## Lo que no se pudo verificar

- Supabase real. Las migraciones se probaron en PostgreSQL 16 local con un sustituto mínimo de los roles de Supabase. `Cloud.audit_page`, `Cloud.audit_corte` y el límite `max-rows` real solo se probaron contra un PostgREST simulado. Falta ejecutar 005 y 006 en un proyecto de pruebas de Supabase.
- Windows. Todo se ejecutó en Linux.
- Excel de Microsoft. Se usó LibreOffice y `openpyxl`.
- Un navegador real. La interfaz se probó con jsdom.
- Concurrencia real sobre PostgreSQL. El corte por `seq` se probó con SQLite y con el doble de pruebas.

## Ruido conocido

Si una exportación falla a mitad de la escritura, `openpyxl` puede imprimir en la consola un aviso `Exception ignored ... LxmlSyntaxError` al liberar su escritor interno. El archivo temporal ya se borró y la respuesta de error es correcta. No afecta el resultado. La prueba `test_un_error_a_mitad_de_la_exportacion_limpia_y_se_propaga` lo provoca. Los mensajes `disco lleno` que aparecen al correr la suite también son esperados: las pruebas simulan ese fallo a propósito.

## Preguntas posibles del profesor

**¿Cómo sabe que el Excel es correcto y no solo que existe?** Las pruebas lo leen de vuelta con `openpyxl`, comprueban tipos de celda, formatos, encabezados, valores y que ninguna celda sea fórmula. Además se abrió con LibreOffice.

**¿Por qué usó un PostgREST simulado?** Para probar la lógica de paginación con un tope de filas sin depender de un proyecto de Supabase. Se declara como simulación y queda pendiente la prueba real.

**¿Qué distingue un error del entorno de uno de la implementación?** Antes de cambiar código se revisa si el fallo reproduce en una base limpia. Dos fallos de esta entrega fueron de pruebas mías, no del código: un parseo de filtros en el doble de PostgREST y una prueba que esperaba un tipo `float` donde Excel guarda enteros.
