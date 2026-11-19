# Dependencias

| Paquete | Versión | Para qué | Dónde |
| --- | --- | --- | --- |
| Python | 3.10 o superior. Probado con 3.13 | Todo el sistema | |
| `openpyxl` | `>=3.1,<4`. Probado con 3.1.5 | Exportación a Excel | `requirements.txt` |
| `et-xmlfile` | La instala `openpyxl` | Escritura XML por flujo | Indirecta |

Por qué `openpyxl` 3.1: trae el modo `write_only`, necesario para exportar sin cargar todo en memoria, formatos de celda y filtros. Se limita a menos de 4 porque una versión mayor podría cambiar esa API.

La aplicación sigue funcionando sin `openpyxl`. Solo `/api/export/xlsx` responde 503 con un mensaje que pide instalarla.

Solo para pruebas opcionales de la interfaz: Node 18 o superior y `jsdom`. No son dependencias del sistema.

PostgreSQL 16 local y `psql` son necesarios solo para probar las migraciones de Supabase.

Instalación en Windows:

```powershell
python -m pip install -r requirements.txt
```

Telegram (SCRUM-24) no agrega dependencias. Usa `urllib` de la biblioteca estándar. El procesador necesita Internet hacia `api.telegram.org` por HTTPS, puerto 443.
