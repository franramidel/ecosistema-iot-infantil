-- ESTADO: CAMBIO AL MODELO COMPARTIDO PENDIENTE DE APROBACIÓN DE SEBASTIÁN. NO DESPLEGAR SIN SU AUTORIZACIÓN.
-- Modifica public.devices (columna uuid, dos claves únicas, trigger) y agrega audit_log.device_uuid con llaves foráneas.
-- No cambia devices.id ni ningún identificador existente. No es independiente: 003, 006 y 007 dependen de ella.
-- Probada solo en PostgreSQL local. No se ha ejecutado en Supabase real. Ver docs/INTEGRACION-pendientes.md.
-- Corrección UUID (sobre SCRUM-20). Ejecutar DESPUÉS de 001, 003 y 004.
-- devices.id sigue siendo el Device_ID operativo (TEMP-01). devices.uuid es el ID_Dispositivo del SRS.
-- audit_log.device_uuid enlaza cada registro con el UUID mediante una clave foránea real.
-- Es idempotente y corre en una sola transacción: si algo falla, no queda nada a medias.
-- No borra datos. Para completar el historial desactiva un instante el trigger que bloquea UPDATE
-- en audit_log, dentro de la misma transacción, y lo vuelve a activar antes de confirmar.
begin;

alter table public.devices add column if not exists uuid uuid not null default gen_random_uuid();

do $$ begin
 if not exists (select 1 from pg_constraint where conname='devices_uuid_key' and conrelid='public.devices'::regclass) then
  alter table public.devices add constraint devices_uuid_key unique (uuid);
 end if;
 if not exists (select 1 from pg_constraint where conname='devices_id_uuid_key' and conrelid='public.devices'::regclass) then
  alter table public.devices add constraint devices_id_uuid_key unique (id,uuid);
 end if;
end $$;

create or replace function cuna_private.keep_device_uuid() returns trigger language plpgsql as $$
begin raise exception 'El UUID del dispositivo no puede cambiar'; end; $$;
revoke all on function cuna_private.keep_device_uuid() from public, anon, authenticated;
drop trigger if exists devices_keep_uuid on public.devices;
create trigger devices_keep_uuid before update of uuid on public.devices for each row
 when (old.uuid is distinct from new.uuid) execute function cuna_private.keep_device_uuid();

alter table public.audit_log add column if not exists device_uuid uuid;

-- audit_clock ya corre antes de cada INSERT: ahora también completa device_uuid desde devices cuando
-- no viene dado. Si viene dado y no coincide con el dispositivo, la clave foránea compuesta lo rechaza.
-- Así los triggers y cuna_record existentes no necesitan cambios.
create or replace function cuna_private.audit_clock() returns trigger language plpgsql
 security definer set search_path='' as $$
begin
 new.event_date := (new.occurred_at at time zone 'America/Costa_Rica')::date;
 new.event_time := date_trunc('second', new.occurred_at at time zone 'America/Costa_Rica')::time;
 if new.device_uuid is null then
  select d.uuid into new.device_uuid from public.devices d where d.id=new.device_id and d.family_id=new.family_id;
 end if;
 return new;
end; $$;
revoke all on function cuna_private.audit_clock() from public, anon, authenticated;

alter table public.audit_log disable trigger audit_immutable;
update public.audit_log l set device_uuid=d.uuid
 from public.devices d where d.id=l.device_id and d.family_id=l.family_id and l.device_uuid is null;
alter table public.audit_log enable trigger audit_immutable;

alter table public.audit_log alter column device_uuid set not null;

do $$ begin
 if not exists (select 1 from pg_constraint where conname='audit_log_device_uuid_fkey' and conrelid='public.audit_log'::regclass) then
  alter table public.audit_log add constraint audit_log_device_uuid_fkey
   foreign key (device_uuid) references public.devices(uuid);
 end if;
 if not exists (select 1 from pg_constraint where conname='audit_log_device_id_uuid_fkey' and conrelid='public.audit_log'::regclass) then
  alter table public.audit_log add constraint audit_log_device_id_uuid_fkey
   foreign key (device_id,device_uuid) references public.devices(id,uuid);
 end if;
end $$;

commit;
