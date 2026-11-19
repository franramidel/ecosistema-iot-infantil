# Verificación del formulario de exportación (opcional)

Prueba la interfaz con jsdom (un DOM simulado, no un navegador real) contra el servidor verdadero.
No es parte de la aplicación ni de `unittest`. Necesita Node 18 o superior.

```powershell
mkdir $env:TEMP\nido-ui; cd $env:TEMP\nido-ui; npm init -y; npm install jsdom@24
# Terminal 1, desde la carpeta Nest:
python tests\ui\servidor.py
# Terminal 2, desde la carpeta de jsdom:
node <ruta-a-Nest>\tests\ui\verificar_exportacion.js <ruta-a-Nest>
```

Debe terminar con `UI OK`. Usa el puerto 8765 y una base temporal propia.

## Tarjeta de Telegram

Con el mismo servidor, `node <ruta-a-Nest>\tests\ui\verificar_telegram.js <ruta-a-Nest>` prueba la tarjeta de Telegram. Reinicia `servidor.py` antes de cada verificación para partir de una base limpia. Debe terminar con `UI OK`.
