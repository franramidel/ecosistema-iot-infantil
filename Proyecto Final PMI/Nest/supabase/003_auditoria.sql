-- ESTADO: NO APROBADA PARA DESPLEGAR. NO EJECUTAR EN EL SUPABASE COMPARTIDO SIN AUTORIZACIÓN DEL EQUIPO.
-- Parte propia (SCRUM-20): tabla audit_log, su RLS y la función cuna_record.
-- Parte que toca tablas de otro módulo: triggers sobre public.events y public.assignments y la carga inicial
-- desde esas tablas. Debe revisarla Sebastián, dueño de ese modelo, y Francisco si sus alertas pasan por aquí.
-- El estado de audit_log tras la migración 005 depende del cambio a public.devices. Ver docs/INTEGRACION-pendientes.md.
-- SCRUM-20 · RF-DAT-01. Registro unificado de eventos para Supabase.
-- Ejecutar una vez DESPUÉS de 001_usuarios.sql. No modifica ninguna función ni tabla de 001.
-- Equivale a cuna/auditoria.py en la demo SQLite: los triggers crean el renglón de auditoría
-- en la misma transacción que la lectura o el cambio de asignación.
begin;

create table public.audit_log (
 id uuid primary key default gen_random_uuid(),
 seq bigint generated always as identity,
 family_id uuid not null,
 occurred_at timestamptz not null,
 event_date date not null,
 event_time time not null,
 event_type varchar(50) not null check(length(event_type) >= 1),
 detail text not null check(length(trim(detail)) between 1 and 1000),
 child_id uuid not null,
 device_id text not null,
 severity text not null default 'info' check(severity in ('info','warning','critical')),
 source_event_id text unique,
 foreign key (device_id,family_id) references public.devices(id,family_id),
 foreign key (child_id,family_id) references public.children(id,family_id)
);
create index audit_family_time on public.audit_log(family_id,occurred_at,seq);
create index audit_child_time on public.audit_log(child_id,occurred_at,seq);

-- Fecha y hora locales de Costa Rica. Se calculan en un trigger porque la conversión de zona
-- no es inmutable y no puede ser una columna generada.
create function cuna_private.audit_clock() returns trigger language plpgsql as $$
begin
 new.event_date := (new.occurred_at at time zone 'America/Costa_Rica')::date;
 new.event_time := date_trunc('second', new.occurred_at at time zone 'America/Costa_Rica')::time;
 return new;
end; $$;
create trigger audit_clock before insert on public.audit_log for each row execute function cuna_private.audit_clock();
create trigger audit_immutable before update or delete on public.audit_log for each row execute function cuna_private.keep_history();

create function cuna_private.audit_reading() returns trigger language plpgsql security definer set search_path='' as $$
begin
 insert into public.audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,source_event_id)
 values(new.family_id,new.captured_at,'LECTURA',new.metric||': '||new.value||' '||new.unit,new.child_id,new.device_id,new.id::text);
 return new;
end; $$;
create trigger audit_reading after insert on public.events for each row execute function cuna_private.audit_reading();

create function cuna_private.audit_assignment() returns trigger language plpgsql security definer set search_path='' as $$
begin
 if tg_op = 'INSERT' then
  insert into public.audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,source_event_id)
  values(new.family_id,new.started_at,'ASIGNACION','Dispositivo asignado al bebé',new.child_id,new.device_id,new.id::text||':inicio');
  return new;
 end if;
 if old.ended_at is null and new.ended_at is not null then
  insert into public.audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,source_event_id)
  values(new.family_id,new.ended_at,'DESASIGNACION','Dispositivo retirado del bebé',new.child_id,new.device_id,new.id::text||':fin');
 end if;
 if old.enabled is distinct from new.enabled then
  insert into public.audit_log(family_id,occurred_at,event_type,detail,child_id,device_id)
  values(new.family_id,clock_timestamp(),case when new.enabled then 'ACTIVACION' else 'DESACTIVACION' end,
   case when new.enabled then 'Sensor activado' else 'Sensor desactivado' end,new.child_id,new.device_id);
 end if;
 return new;
end; $$;
create trigger audit_assignment_insert after insert on public.assignments for each row execute function cuna_private.audit_assignment();
create trigger audit_assignment_update after update of ended_at, enabled on public.assignments for each row execute function cuna_private.audit_assignment();
revoke all on function cuna_private.audit_clock(), cuna_private.audit_reading(), cuna_private.audit_assignment() from public, anon, authenticated;

-- Historial anterior a esta migración. source_event_id evita duplicados si se repite el script.
insert into public.audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,source_event_id)
 select family_id,captured_at,'LECTURA',metric||': '||value||' '||unit,child_id,device_id,id::text from public.events
 on conflict (source_event_id) do nothing;
insert into public.audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,source_event_id)
 select family_id,started_at,'ASIGNACION','Dispositivo asignado al bebé',child_id,device_id,id::text||':inicio' from public.assignments
 on conflict (source_event_id) do nothing;
insert into public.audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,source_event_id)
 select family_id,ended_at,'DESASIGNACION','Dispositivo retirado del bebé',child_id,device_id,id::text||':fin' from public.assignments
 where ended_at is not null
 on conflict (source_event_id) do nothing;

-- Sólo los administradores de la familia leen la auditoría. Nadie escribe por la API de tablas.
alter table public.audit_log enable row level security;
revoke all on table public.audit_log from public, anon, authenticated;
grant select on table public.audit_log to authenticated;
create policy admin_reads_audit on public.audit_log for select to authenticated
 using (family_id=(select cuna_private.family()) and (select cuna_private.is_admin()));

-- Evento propio de un dispositivo (POST /api/audit/events). El bebé sale de la asignación vigente
-- en occurred_at y nunca de los datos que envía el dispositivo.
create function public.cuna_record(device text,secret text,event_id uuid,at_time timestamptz,
 kind text,detail_text text,level text default 'info') returns jsonb
language plpgsql security definer set search_path='' as $$
declare family uuid; kid uuid; prior public.audit_log; new_id uuid;
begin
 select d.family_id into family from public.devices d join cuna_private.device_keys k on k.device_id=d.id
  where d.id=device and k.digest=encode(sha256(convert_to(secret,'UTF8')),'hex') for update of d;
 if family is null then raise sqlstate '42501' using message='Credencial de dispositivo inválida.'; end if;
 if kind !~ '^[A-Z][A-Z0-9_]{0,49}$' then raise exception 'Tipo de evento inválido.'; end if;
 if kind = any(array['LECTURA','ASIGNACION','DESASIGNACION','ACTIVACION','DESACTIVACION'])
  then raise sqlstate '42501' using message='Ese tipo de evento lo registra el sistema.'; end if;
 if level not in ('info','warning','critical') then raise exception 'Severidad inválida.'; end if;
 if at_time > clock_timestamp() then raise exception 'El evento está en el futuro.'; end if;
 select * into prior from public.audit_log where source_event_id=event_id::text;
 if found then
  if (prior.device_id,prior.occurred_at,prior.event_type,prior.detail,prior.severity)
    is distinct from (device,at_time,kind,detail_text,level)
   then raise sqlstate 'PT409' using message='El identificador del evento ya tiene otro contenido.'; end if;
  return jsonb_build_object('child_id',prior.child_id,'id',prior.id);
 end if;
 select child_id into kid from public.assignments where device_id=device and started_at<=at_time
  and (ended_at is null or at_time<ended_at);
 if kid is null then raise sqlstate 'PT409' using message='El dispositivo estaba sin asignar en esa fecha.'; end if;
 insert into public.audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,severity,source_event_id)
  values(family,at_time,kind,detail_text,kid,device,level,event_id::text) returning id into new_id;
 update public.families set version=version+1 where id=family;
 return jsonb_build_object('child_id',kid,'id',new_id);
end; $$;
revoke all on function public.cuna_record(text,text,uuid,timestamptz,text,text,text) from public,anon,authenticated;
grant execute on function public.cuna_record(text,text,uuid,timestamptz,text,text,text) to anon,authenticated;
commit;
