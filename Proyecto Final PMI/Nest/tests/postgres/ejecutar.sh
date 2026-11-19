#!/usr/bin/env bash
# Aplica 001, 003, 004, 005, 006, 007, 008 y 009 sobre bases PostgreSQL vacías, corre los escenarios y la verificación de retención.
# Uso: PGURL=postgresql://postgres@localhost:5432/prueba [PGURL_VACIA=postgresql://.../otra] tests/postgres/ejecutar.sh
# Las bases deben estar vacías y ser desechables.
# ADVERTENCIA: nunca apuntes esto al Supabase compartido del equipo ni a producción. Inserta datos falsos.
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${PGURL:?Define PGURL con la base de pruebas}"
for u in "$PGURL" "${PGURL_VACIA:-}"; do
  case "$u" in *supabase.co*|*supabase.com*|*pooler*) echo "ABORTADO: la URL parece de Supabase. Este script es solo para PostgreSQL local desechable."; exit 1;; esac
done
export PGOPTIONS="${PGOPTIONS:-} -c cuna.entorno_desechable=si"
ejecutar() { echo "== $1"; psql "$2" -v ON_ERROR_STOP=1 -q -f "$1" 2>&1 | grep -E "NOTICE|ERROR" || true; }
verificar() {
  resultado=$(psql "$1" -v ON_ERROR_STOP=1 -At -F '|' -f supabase/verificar_retencion.sql)
  echo "$resultado"
  if echo "$resultado" | grep -q '|f$' || [ "$(echo "$resultado" | wc -l)" -ne 10 ]; then echo "ERROR: la verificación falló"; exit 1; fi
}

# A. Base con datos anteriores a 005 (escenarios 003 y 004): la migración 005 debe asignar UUID sin perder nada.
for f in tests/postgres/stub_supabase.sql supabase/001_usuarios.sql supabase/003_auditoria.sql supabase/004_retencion.sql \
         tests/postgres/escenario_003.sql tests/postgres/escenario_004.sql; do ejecutar "$f" "$PGURL"; done
antes=$(psql "$PGURL" -At -c "select (select count(*) from public.audit_log)||'/'||(select count(*) from public.events)||'/'||(select count(*) from public.devices)")
echo "== 005 con un fallo inyectado: debe revertirse por completo"
sed 's/^commit;/select 1\/0;\ncommit;/' supabase/005_device_uuid.sql > /tmp/005_con_fallo.sql
if psql "$PGURL" -v ON_ERROR_STOP=1 -q -f /tmp/005_con_fallo.sql >/dev/null 2>&1; then echo "ERROR: el fallo inyectado no abortó"; exit 1; fi
sin_columna=$(psql "$PGURL" -At -c "select count(*) from information_schema.columns where table_name in ('devices','audit_log') and column_name in ('uuid','device_uuid')")
[ "$sin_columna" = "0" ] || { echo "ERROR: la migración fallida dejó columnas a medias"; exit 1; }
echo "OK: el fallo dejó la base intacta"
ejecutar supabase/005_device_uuid.sql "$PGURL"
uuids1=$(psql "$PGURL" -At -c "select string_agg(id||'='||uuid, ',' order by id) from public.devices")
ejecutar supabase/005_device_uuid.sql "$PGURL"
uuids2=$(psql "$PGURL" -At -c "select string_agg(id||'='||uuid, ',' order by id) from public.devices")
[ "$uuids1" = "$uuids2" ] || { echo "ERROR: repetir 005 cambió los UUID"; exit 1; }
despues=$(psql "$PGURL" -At -c "select (select count(*) from public.audit_log)||'/'||(select count(*) from public.events)||'/'||(select count(*) from public.devices)")
[ "$antes" = "$despues" ] || { echo "ERROR: conteos distintos $antes -> $despues"; exit 1; }
echo "OK: historial conservado ($antes) y UUID estables al repetir 005"
ejecutar tests/postgres/escenario_005.sql "$PGURL"
ejecutar supabase/006_exportacion.sql "$PGURL"
ejecutar supabase/006_exportacion.sql "$PGURL"
ejecutar tests/postgres/escenario_006.sql "$PGURL"
ejecutar supabase/007_telegram.sql "$PGURL"
ejecutar supabase/007_telegram.sql "$PGURL"
ejecutar tests/postgres/escenario_007.sql "$PGURL"
ejecutar supabase/008_alertas_temperatura.sql "$PGURL"
ejecutar supabase/008_alertas_temperatura.sql "$PGURL"
ejecutar tests/postgres/escenario_008.sql "$PGURL"
ejecutar supabase/009_panel_sprint2.sql "$PGURL"
ejecutar supabase/009_panel_sprint2.sql "$PGURL"
echo "== supabase/verificar_retencion.sql (base con datos)"; verificar "$PGURL"

# B. Esquema nuevo en una base vacía, aplicando las migraciones en orden.
if [ -n "${PGURL_VACIA:-}" ]; then
  for f in tests/postgres/stub_supabase.sql supabase/001_usuarios.sql supabase/003_auditoria.sql supabase/004_retencion.sql \
           supabase/005_device_uuid.sql tests/postgres/escenario_005.sql supabase/006_exportacion.sql tests/postgres/escenario_006.sql supabase/007_telegram.sql tests/postgres/escenario_007.sql supabase/008_alertas_temperatura.sql tests/postgres/escenario_008.sql supabase/009_panel_sprint2.sql; do ejecutar "$f" "$PGURL_VACIA"; done
  echo "== supabase/verificar_retencion.sql (base vacía)"; verificar "$PGURL_VACIA"
fi
