#!/usr/bin/env bash
# SIMULA el respaldo y la restauración de Supabase con pg_dump y pg_restore sobre PostgreSQL local.
# No prueba los respaldos diarios ni PITR de Supabase, que dependen del plan y se operan en su panel.
# Uso: PGURL_ORIGEN=... PGURL_DESTINO=... tests/postgres/respaldo_restauracion.sh
# Ambas bases deben ser desechables. La de destino debe estar vacía.
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${PGURL_ORIGEN:?}" "${PGURL_DESTINO:?}"
# ADVERTENCIA: solo PostgreSQL local desechable. Nunca el Supabase compartido ni producción.
for u in "$PGURL_ORIGEN" "$PGURL_DESTINO"; do
  case "$u" in *supabase.co*|*supabase.com*|*pooler*) echo "ABORTADO: la URL parece de Supabase."; exit 1;; esac
done
export PGOPTIONS="${PGOPTIONS:-} -c cuna.entorno_desechable=si"
carpeta=$(mktemp -d)
trap 'rm -rf "$carpeta"' EXIT
falla() { echo "FALLA: $1"; exit 1; }
ok() { echo "OK: $1"; }

PGURL="$PGURL_ORIGEN" bash tests/postgres/ejecutar.sh >/dev/null || falla "no se pudo preparar el origen"
tablas="families members children devices assignments events audit_log"
huella() {
  for t in $tablas; do psql "$1" -At -c "select '$t', count(*), md5(coalesce(string_agg(x::text, '' order by x::text),'')) from public.$t x"; done
}

pg_dump --format=custom --no-owner "$PGURL_ORIGEN" > "$carpeta/respaldo.dump"
[ -s "$carpeta/respaldo.dump" ] || falla "el respaldo salió vacío"
ok "respaldo creado"

pg_restore --list "$carpeta/respaldo.dump" >/dev/null || falla "el respaldo no se puede leer"
pg_restore --no-owner --exit-on-error --dbname "$PGURL_DESTINO" "$carpeta/respaldo.dump" || falla "la restauración falló"
ok "restauración completada en una base nueva"

[ "$(huella "$PGURL_ORIGEN")" = "$(huella "$PGURL_DESTINO")" ] || falla "el contenido restaurado difiere del original"
ok "las siete tablas tienen las mismas filas y el mismo contenido"

resultado=$(psql "$PGURL_DESTINO" -v ON_ERROR_STOP=1 -At -F '|' -f supabase/verificar_retencion.sql)
if echo "$resultado" | grep -q '|f$' || [ "$(echo "$resultado" | wc -l)" -ne 10 ]; then echo "$resultado"; falla "la verificación de retención falló en lo restaurado"; fi
ok "verificar_retencion.sql pasa en lo restaurado, con triggers, permisos y relaciones"
[ "$(psql "$PGURL_ORIGEN" -At -c "select string_agg(id||uuid::text,',' order by id) from public.devices")" = "$(psql "$PGURL_DESTINO" -At -c "select string_agg(id||uuid::text,',' order by id) from public.devices")" ] || falla "los UUID de dispositivos cambiaron al restaurar"
ok "los UUID de los dispositivos son idénticos tras restaurar"
[ "$(psql "$PGURL_DESTINO" -At -c "select count(*) from public.audit_export")" = "$(psql "$PGURL_ORIGEN" -At -c "select count(*) from public.audit_export")" ] || falla "la vista audit_export difiere tras restaurar"
ok "la vista de exportación funciona tras restaurar"

salida=$(psql "$PGURL_DESTINO" -At -c "delete from public.events" 2>&1 || true)
echo "$salida" | grep -q "Evento inmutable" || falla "DELETE no quedó bloqueado tras restaurar"
salida=$(psql "$PGURL_DESTINO" -At -c "truncate public.events" 2>&1 || true)
echo "$salida" | grep -q "no se pueden vaciar" || falla "TRUNCATE no quedó bloqueado tras restaurar"
ok "la inmutabilidad sobrevive a la restauración"
[ "$(psql "$PGURL_DESTINO" -At -c "select count(*) from pg_policies where schemaname='public'")" -ge 6 ] || falla "faltan políticas RLS"
ok "las políticas RLS se restauraron"

if pg_restore --no-owner --exit-on-error --dbname "$PGURL_DESTINO" "$carpeta/respaldo.dump" >/dev/null 2>&1; then
  falla "restaurar sobre una base con datos debía fallar"
fi
[ "$(huella "$PGURL_ORIGEN")" = "$(huella "$PGURL_DESTINO")" ] || falla "un intento fallido alteró el destino"
ok "restaurar sobre una base con datos falla y no la altera"

head -c 200 "$carpeta/respaldo.dump" > "$carpeta/cortado.dump"
if pg_restore --list "$carpeta/cortado.dump" >/dev/null 2>&1; then falla "un respaldo cortado debía rechazarse"; fi
ok "un respaldo dañado se rechaza"
echo "Simulación local completada. No se probó ninguna función de respaldo de Supabase."
