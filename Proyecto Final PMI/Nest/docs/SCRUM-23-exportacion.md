# SCRUM-23: exportación del historial a Excel (RF-DAT-04)

## Qué hace

`GET /api/export/xlsx` genera y descarga un archivo `.xlsx` con el registro unificado de eventos. La interfaz tiene un formulario con bebé, dispositivo, fecha inicial y fecha final. Solo lo ven los administradores. El endpoint CSV `/api/report` sigue igual.

| Criterio | Cómo se cumple |
| --- | --- |
| CA1. Un botón descarga un `.xlsx` | Botón Descargar Excel en la ficha del bebé. Usa `fetch`, arma un blob y lo descarga con el nombre que da el servidor |
| CA2. Respeta filtros de bebé, dispositivo y rango | Cuatro filtros combinables. Se validan igual que `/api/audit` porque usan la misma función `consulta` |
| CA3. Trae las columnas de RF-DAT-01 | Fecha, Hora, Tipo_de_Evento, Descripción_Detalle, ID_Bebé e ID_Dispositivo, en ese orden y con ese texto |

## El libro

Tiene tres hojas.

- **Resumen**: filtros usados, hora de generación en Costa Rica, totales, el criterio de fechas y el corte de la consulta. No lleva el nombre del bebé, solo su UUID.
- **Auditoría**: todos los eventos menos las lecturas. Después de las seis columnas del SRS trae `Device_ID` y `Severidad`.
- **Telemetría**: las lecturas. Después de las seis columnas trae `Device_ID`, `Medida`, `Valor` y `Unidad`.

Una lectura aparece solo en Telemetría, aunque `audit_log` guarde un renglón `LECTURA` por cada una. Así ningún hecho se exporta dos veces. Se separaron las hojas porque la telemetría necesita valor y unidad como número y texto, y la auditoría necesita severidad. Mezclarlas obligaría a dejar columnas vacías.

`ID_Bebé` e `ID_Dispositivo` son UUID. El identificador operativo, como `TEMP-01`, va en la columna extra `Device_ID` y nunca sustituye al UUID.

Fecha y Hora son valores de Excel con formato `yyyy-mm-dd` y `hh:mm:ss`, no texto. Hay filas fijas, filtro automático y anchos de columna.

## Fechas

Las fechas filtran por la fecha local de Costa Rica, que es UTC-6 todo el año. Ambos extremos están incluidos. Un evento a las 00:00:00 hora local cuenta en el día que empieza. Uno a las 23:59:59 cuenta en el día que termina. Una prueba usa lecturas a las 05:59:59 y a las 06:00:00 UTC para comprobarlo.

## Seguridad

- Autenticación: sin sesión responde 401.
- Autorización: solo administradores. Un cuidador recibe 403 y la interfaz oculta el formulario. Las dos barreras existen porque ocultar un botón no protege.
- Familia: el bebé y el dispositivo del filtro se buscan dentro de la familia de quien consulta. Uno ajeno responde 404, igual que uno inexistente, para no revelar si existe.
- SQL: todos los valores van como parámetros. Los nombres de columna salen de una lista fija.
- Fórmulas: un texto que empieza por `=`, `+`, `-`, `@`, tabulador o salto de línea, incluso tras espacios, se guarda con una comilla delante y como texto. Se eliminan los caracteres de control que Excel no admite. Una prueba revisa todas las celdas del libro y exige que ninguna sea fórmula. La comilla se ve en la celda. Es el precio de la defensa recomendada por OWASP.
- Datos personales: el archivo no lleva el nombre del bebé, solo UUID.
- Archivo temporal: se crea con `mkstemp`, con permisos solo para el dueño, y se borra siempre al terminar, también si hay error. Una prueba lo comprueba en éxito, en error de validación y en fallo a mitad de la generación.
- Errores: los fallos de validación ocurren antes de crear el archivo y responden JSON con 400, 403 o 404. Un error inesperado responde 500 sin detalles. La respuesta usa `Cache-Control: no-store`.

## Memoria y volumen

`openpyxl` trabaja en modo `write_only`. Las filas se piden de 500 en 500 con un cursor por instante y orden de inserción. Una prueba compara la memoria con 2 000 y con 24 000 lecturas, y exige que el pico no crezca de forma proporcional. Cada hoja admite hasta 1 048 575 filas. Si se superan, responde 413 y pide acotar el rango.

## Consistencia con eventos nuevos

Al empezar, `audit_corte` fija el último registro existente. Todas las páginas piden `seq <= corte`. Un evento que llega durante la exportación queda fuera, aunque su fecha caiga en medio del rango, y no duplica ni quita filas. Una prueba inserta tres lecturas entre páginas y comprueba que el archivo no cambia. En PostgreSQL el valor `seq` se asigna antes de confirmar, así que una transacción lenta podría confirmar después del corte con un `seq` menor. Eso solo la deja fuera del archivo y se corrige exportando de nuevo.

## SQLite y Supabase

Las dos rutas comparten `cuna/exportar.py`. El almacén ofrece `audit_corte` y `audit_page`.

- SQLite hace un join entre `audit_log` y `events` por `source_event_id`.
- Supabase usa la vista `audit_export` de `supabase/006_exportacion.sql`. Es `security_invoker`, así que aplica la RLS de `audit_log` y solo los administradores de la familia ven filas.
- PostgREST limita las respuestas con `max-rows`. En modo exportación, `Cloud.audit_page` devuelve siempre el cursor de la última fila y el exportador sigue hasta recibir una página vacía. Un tope menor que el lote no corta el archivo. Una prueba con un PostgREST simulado de tope 100 y 730 filas produce un libro idéntico al de SQLite.

## Nombre del archivo

`nido-reporte-bebe-<8 caracteres>-disp-<id>-<desde>_<hasta>-<AAAAMMDD-HHMMSS>.xlsx`. Las partes de filtro aparecen solo si se usaron. Es ASCII, no contiene datos personales y no se repite.

## Ejemplo

Entrada: `GET /api/export/xlsx?child=ad163f7c-855f-4e22-b843-4f0231950524&device=TEMP-01&desde=2026-03-01&hasta=2026-03-10`

Salida: archivo `nido-reporte-bebe-ad163f7c-disp-TEMP-01-2026-03-01_2026-03-10-20261118-213705.xlsx` con tipo `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`. En Telemetría:

| Fecha | Hora | Tipo_de_Evento | Descripción_Detalle | ID_Bebé | ID_Dispositivo | Device_ID | Medida | Valor | Unidad |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-03-05 | 06:00:00 | LECTURA | Temperatura: 22.0 °C | ad163f7c-... | 3b0c9a52-... | TEMP-01 | Temperatura | 22 | °C |

Errores: `desde=ayer` responde 400 con `{"error":"desde usa el formato AAAA-MM-DD."}`. Un dispositivo de otra familia responde 404 con `{"error":"Dispositivo no disponible."}`.

## Prueba manual

1. Inicia el servidor y entra como administrador.
2. En la ficha de un bebé con lecturas, baja a Exportar a Excel.
3. Elige bebé, dispositivo y un rango. Pulsa Descargar Excel. Debe verse el aviso de generación y luego el archivo.
4. Ábrelo en Excel. Revisa las tres hojas, que Fecha y Hora se alineen como valores y que ID_Dispositivo sea un UUID.
5. Pon una fecha inicial posterior a la final. Debe aparecer el error sin descargar nada.
6. Entra como cuidador. El formulario no debe aparecer. Pega `/api/export/xlsx` en la barra de direcciones y debe responder 403.
7. Con una lectura cuyo detalle empiece por `=`, verifica que Excel muestra texto y no calcula.

## Funciones principales

- `exportar.generar`: valida con `audit_corte`, crea el archivo temporal, escribe las tres hojas y lo devuelve. Si algo falla, lo borra.
- `exportar.paginas`: recorre una hoja por lotes hasta que no hay cursor siguiente.
- `exportar.texto_seguro`: quita caracteres ilegales y neutraliza fórmulas.
- `exportar.celda`: crea celdas con formato de fecha y hora o fuerza tipo texto.
- `auditoria.resolver_filtros`: comprueba que bebé y dispositivo son de la familia y acepta Device_ID o UUID.
- `auditoria.pagina`: una consulta ordenada, con filtros, corte y la hoja pedida.
- `Handler.send_file` en `app.py`: envía el archivo por bloques con los encabezados de seguridad.
