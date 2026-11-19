# Integración en Windows con PowerShell

Todos los comandos son para PowerShell. El parche acumulativo es `SCRUM-20-21-23-24-Ismael-FINAL.patch` e incluye SCRUM-20, SCRUM-21, la corrección del UUID de dispositivos, SCRUM-23 y SCRUM-24. No necesitas aplicar los parches anteriores. Se creó sobre el commit `1d22bfe886505a60070d6bdcac82630ead73af17` de la rama `main`. Si ya aplicaste un parche anterior, no apliques este encima: vuelve primero a una rama limpia como se explica en la sección 11.

Lo ejecutado para verificar el parche fue en Linux. En Windows no se ha corrido todavía.

## 1. Clonar y crear la rama

```powershell
cd $HOME\Documents
git clone https://github.com/franramidel/ecosistema-iot-infantil.git
cd ecosistema-iot-infantil
git switch -c feature/ismael-auditoria-exportacion-telegram
```

Si `main` avanzó y el parche ya no calza, parte del commit base. Es solo para esa situación:

```powershell
git switch -c feature/ismael-auditoria-exportacion-telegram 1d22bfe
```

## 2. Aplicar el parche

Primero se comprueba sin cambiar nada. Si no imprime errores, se aplica.

```powershell
$parche = "$HOME\Downloads\SCRUM-20-21-23-24-Ismael-FINAL.patch"
Get-FileHash $parche -Algorithm SHA256
git apply --check $parche
git apply --whitespace=nowarn $parche
git status --short
```

Compara el hash con el que se entregó junto al parche. Si `git apply --check` dice que no calza por saltos de línea, repite con `--ignore-whitespace`. Debe aparecer solo lo que está en la lista de archivos del parche. Si ves cambios que no reconoces, detente y revisa con `git diff`.

## 3. Instalar dependencias

Solo `openpyxl`, y solo lo necesita la exportación a Excel. Telegram usa la biblioteca estándar.

```powershell
cd "Proyecto Final PMI\Nest"
$env:PYTHONDONTWRITEBYTECODE = "1"
python -m pip install -r requirements.txt
python -c "import openpyxl; print(openpyxl.__version__)"
```

Debe imprimir una versión 3.1 o posterior. `PYTHONDONTWRITEBYTECODE` evita que Python modifique los `.pyc` que están subidos al repositorio. Repite esa línea en cada terminal nueva. Si `python` no existe, usa `py -3`.

## 4. Configurar SQLite y migrar

La demo crea y migra la base sola al arrancar. Usa un archivo propio para no modificar el `demo.sqlite3` del repositorio:

```powershell
$env:CUNA_DB = "$PWD\datos\mi-demo.sqlite3"
```

Si ya tenías una base con datos de la entrega anterior, haz una copia antes de migrar y verifícala:

```powershell
python respaldo.py crear
python respaldo.py verificar datos\respaldos\nido-AAAAMMDD-HHMMSS.sqlite3
```

Una base anterior da un aviso de que no tiene UUID. No es un error. Al iniciar `python app.py` la migración asigna un UUID a cada dispositivo sin cambiar su `Device_ID` y sin tocar el historial. Si la migración falla, la base queda como estaba.

Para Supabase, en el editor SQL de un proyecto de pruebas ejecuta en este orden `supabase/001_usuarios.sql`, `002_primer_administrador.sql` con tu correo, `003_auditoria.sql`, `004_retencion.sql`, `005_device_uuid.sql`, `006_exportacion.sql` y `007_telegram.sql`. Luego ejecuta `supabase/verificar_retencion.sql`. Las diez filas deben tener `ok` en verdadero. No lo hagas en un proyecto con datos reales sin un respaldo antes.

## 5. Ejecutar todas las pruebas

```powershell
python -m unittest discover -s tests -v
```

Deben pasar todas. Sin PostgreSQL se omiten 34 pruebas que ejercitan Supabase simulado. Para correrlas define `PGTEST_URL`, como explica `pruebas.md`. Las pruebas de PostgreSQL de `tests\postgres` y la verificación de la interfaz de `tests\ui` son opcionales y se explican en `pruebas.md`.

## 6. Iniciar el servidor y comprobar la persistencia

```powershell
python app.py
```

En el primer arranque pedirá un correo y una contraseña de al menos 10 caracteres. Abre http://127.0.0.1:8000, inicia sesión, crea el perfil Luna y en Dispositivos registra `TEMP-01`. Asígnalo a Luna y copia su clave. En otra terminal, desde la misma carpeta:

```powershell
$env:CUNA_DEVICE_KEY = "<clave copiada>"
python simular.py --device TEMP-01 --valor 22.4
```

Visita http://127.0.0.1:8000/api/audit?tipo=LECTURA con la sesión abierta. Debes ver la lectura con `device_id` y `device_uuid`. Detén el servidor con Ctrl+C, vuelve a iniciarlo, entra otra vez y repite la visita. La lectura y el UUID siguen igual.

## 7. Probar la exportación a Excel

1. En la ficha de Luna, baja a Exportar a Excel.
2. Elige un bebé, un dispositivo y fechas. Pulsa Descargar Excel.
3. El archivo se guarda como `nido-reporte-...xlsx`. Ábrelo con Excel.

Para inspeccionarlo sin Excel:

```powershell
python -c "from openpyxl import load_workbook; import sys; wb = load_workbook(sys.argv[1]); [print(ws.title, ws.max_row - 1, 'filas') for ws in wb]; print([c.value for c in wb['Telemetría'][1]])" "$HOME\Downloads\nido-reporte-XXXX.xlsx"
```

Debe mostrar las hojas Resumen, Auditoría y Telemetría, y la fila de encabezados con `Fecha`, `Hora`, `Tipo_de_Evento`, `Descripción_Detalle`, `ID_Bebé` e `ID_Dispositivo` al principio. Prueba también un rango de fechas invertido y entra como cuidador para comprobar que el formulario no aparece.

## 8. Probar las alertas por Telegram con un bot real

Las pruebas automáticas usan un Telegram simulado y no necesitan un bot. Esta sección es para verificar con uno real. Solo tú puedes hacerla. Nunca me envíes el token ni lo pegues en un chat, una captura, un commit o un documento.

El objetivo es comprobar tres cosas: que puedes vincular tu Telegram, que recibes una notificación y que sigues recibiendo alertas con la página cerrada mientras el procesador sigue corriendo.

### 8.1 Crear el bot con BotFather

1. En Telegram busca @BotFather y ábrelo.
2. Envía `/newbot`.
3. Elige un nombre y un usuario que termine en `bot`.
4. BotFather responde con un token. Es una contraseña. Si se filtra, envía `/revoke` a BotFather y crea otro.

### 8.2 Guardar el token sin mostrarlo

Opción A, solo para esta terminal. `Read-Host -AsSecureString` no muestra lo que escribes:

```powershell
$seguro = Read-Host "Token del bot" -AsSecureString
$env:TELEGRAM_BOT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($seguro))
$env:TELEGRAM_BOT_USERNAME = "nombre_de_tu_bot"
```

Opción B, en un archivo `.env` junto a `app.py`:

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env` está en `.gitignore`. Comprueba con `git status --short` que no aparezca. Nunca pongas valores reales en `.env.example`. Cada terminal nueva necesita la opción A otra vez, y la opción B no.

### 8.3 Elegir el almacén

El servidor y el procesador deben usar el mismo.

**Con SQLite.** Define `$env:CUNA_DB` igual en las dos terminales, como en la sección 4. No necesitas nada más.

**Con Supabase.** Usa un proyecto de pruebas, no uno con datos reales.

1. Ejecuta en el editor SQL, en orden, `001`, `002` con tu correo, `003`, `004`, `005`, `006` y `007_telegram.sql`. Luego `verificar_retencion.sql`: las diez filas deben dar `ok` en verdadero.
2. En `.env` completa `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` y `SUPABASE_SECRET_KEY`. La clave secreta o de servicio es tan sensible como el token. No la subas ni la compartas.
3. Crea tu cuenta en Authentication y confirma el correo.
4. Inicia con `--supabase` en las dos terminales.

### 8.4 Iniciar el servidor y el procesador

Son dos procesos. Abre dos terminales en la carpeta `Nest`.

```powershell
# Terminal 1, SQLite
python app.py
# Terminal 2, SQLite
python telegram_worker.py

# Terminal 1, Supabase
python app.py --supabase
# Terminal 2, Supabase
python telegram_worker.py --supabase
```

El procesador valida el token con Telegram y escribe `Procesador iniciado para @tu_bot con almacén SQLite` o `con almacén Supabase`. Si falta el token, termina con un mensaje claro y código 2. Si Telegram rechaza el token, termina con código 4. Si Supabase rechaza la clave de servicio, termina con código 5. Un segundo procesador sobre el mismo almacén se rechaza con código 3.

### 8.5 Vincular tu cuenta

1. En http://127.0.0.1:8000 busca la tarjeta Alertas por Telegram y pulsa Vincular Telegram.
2. Pulsa Abrir el bot en Telegram y luego Iniciar. Si no abre, escribe a mano el código que muestra la página.
3. El bot responde que la cuenta quedó vinculada. Pulsa Actualizar estado. Debe decir Telegram vinculado y El procesador de alertas está activo.

Si el bot responde que el código no es válido o expiró, genera otro. El código dura 10 minutos y sirve una vez.

### 8.6 Recibir una notificación

Pulsa Enviar prueba. El mensaje llega en unos segundos con el título Notificación de prueba. Si no llega, mira la terminal del procesador: cada intento deja una línea con el estado, sin el contenido del mensaje ni el token.

### 8.7 Comprobar que no depende de la página

Esta es la prueba que cierra el criterio CA2.

1. Con el procesador corriendo, cierra el navegador por completo. Deja el servidor y el procesador abiertos.
2. Genera una solicitud sin usar el navegador, en una tercera terminal de la carpeta `Nest`. Con SQLite:

```powershell
python solicitar_alerta.py --correo tu-correo@example.com
```

   Con Supabase, usa el id de tu usuario, que aparece en Authentication, Users:

```powershell
python solicitar_alerta.py --supabase --usuario <id del usuario>
```

   Con `--supabase` el script escribe una fila de prueba en la cola del proyecto. Antes muestra el servidor, sin claves, y pide que escribas `ESCRIBIR EN SUPABASE`. Úsalo solo en un proyecto de pruebas, nunca en el Supabase compartido del equipo sin autorización. Si no hay terminal para confirmar, agrega `--confirmo-escribir-en-supabase`.

   Imprime el estado `pendiente`. Esta orden llama a `solicitar_notificacion`, la misma función que usará SCRUM-25, y no envía nada por sí misma: la solicitud queda en la cola y la envía el procesador. Si el usuario no tiene Telegram vinculado, termina con código 3 y lo dice.
3. Debe llegar el mensaje a Telegram sin que el navegador esté abierto.
4. Detén el procesador con Ctrl+C, pide otra solicitud y confirma que no llega. Vuelve a iniciarlo: la solicitud pendiente llega en unos segundos. Eso comprueba que no se pierde.
5. Reinicia también el servidor web durante la prueba. Las solicitudes pendientes no se pierden.

Para ver el estado de la cola con SQLite:

```powershell
python -c "import sqlite3,os; d=sqlite3.connect(os.environ['CUNA_DB']); print(*d.execute('select status,attempts from notification_queue order by created_at desc limit 5'), sep=chr(10))"
```

### 8.8 Qué anotar

Al terminar, anota qué pasó en cada punto: vinculación, mensaje de prueba, mensaje con la página cerrada, mensaje tras reiniciar el procesador y el almacén que usaste. Con eso SCRUM-24 se puede dar por validada con Telegram real. Si usaste Supabase, también queda validada con un proyecto real. Si algo falla, copia solo la línea del procesador, sin el token.

## 9. Respaldo y restauración

```powershell
python respaldo.py crear
python respaldo.py verificar datos\respaldos\nido-AAAAMMDD-HHMMSS.sqlite3
```

Para restaurar, detén el servidor primero:

```powershell
python respaldo.py restaurar datos\respaldos\nido-AAAAMMDD-HHMMSS.sqlite3 --confirmar
```

La base anterior queda junto a la restaurada con el sufijo `.antes-de-restaurar`.

## 10. Guardar el trabajo y preparar el pull request

No uses `git add -A`: tomaría los `.pyc` y las bases de datos. Usa esta orden:

```powershell
cd ..\..
git add -- . ":(exclude)*.pyc" ":(exclude)*.sqlite3" ":(exclude)*__pycache__*"
git status --short
git commit -m "SCRUM-20, 21, 23 y 24: auditoría, retención, UUID, exportación a Excel y alertas por Telegram"
```

Revisa con `git status --short` que no aparezca ninguna base de datos ni carpeta `datos\respaldos`. Para preparar el pull request, sube la rama solo cuando el equipo lo autorice:

```powershell
git log --oneline main..HEAD
git diff --stat main...HEAD
```

Con esa salida redacta la descripción del pull request: lista las historias, las migraciones nuevas de Supabase que alguien debe ejecutar, la dependencia `openpyxl` y que Telegram necesita un token propio de cada entorno, que nunca va al repositorio. No hagas `git push` ni merge sin autorización.

## 11. Revertir de forma segura

Si todavía no hiciste commit, el parche se puede deshacer exactamente. Primero se comprueba:

```powershell
git apply -R --check $parche
git apply -R $parche
```

Si ya hiciste commit, no reescribas historia. Crea un commit que lo deshace:

```powershell
git revert HEAD
```

Si solo quieres abandonar la rama, cambia de rama sin tocar `main`:

```powershell
git switch main
git branch -m feature/ismael-auditoria-exportacion-telegram descartada-auditoria
```

Nunca uses `git reset --hard` ni `git clean` en esta carpeta sin revisar antes `git status`, porque pueden borrar trabajo tuyo o de tus compañeros.
