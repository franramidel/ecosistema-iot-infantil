-- ADVERTENCIA: SOLO PARA UNA BASE POSTGRESQL LOCAL Y DESECHABLE.
-- Este archivo inserta usuarios y datos falsos y no debe correr nunca sobre el Supabase compartido del equipo
-- ni sobre producción. Se niega a correr si la sesión no declara que es un entorno desechable
-- (tests/postgres/ejecutar.sh y las pruebas automáticas lo declaran por sí mismos).
\set ON_ERROR_STOP 1
do $$ begin
 if coalesce(current_setting('cuna.entorno_desechable', true), '') <> 'si' then
  raise exception 'ABORTADO: este script es solo para una base local desechable, nunca para Supabase compartido o producción.';
 end if;
end $$;
-- Prueba de la migración 003 contra un PostgreSQL real. Ver tests/postgres/ejecutar.sh.
-- Simula los roles anon y authenticated de Supabase. No usar contra un proyecto de producción.
create or replace function pg_temp.expect(ok boolean, label text) returns void language plpgsql as $$ begin if not ok then raise exception 'FALLA: %', label; end if; raise notice 'OK: %', label; end $$;

insert into public.families default values returning id as fam1 \gset
insert into public.families default values returning id as fam2 \gset
insert into auth.users(id,email) values ('11111111-1111-1111-1111-111111111111','padre@example.test'),('22222222-2222-2222-2222-222222222222','cuidador@example.test'),('33333333-3333-3333-3333-333333333333','otra@example.test');
insert into public.members values ('11111111-1111-1111-1111-111111111111',:'fam1','padre@example.test','admin',true),
 ('22222222-2222-2222-2222-222222222222',:'fam1','cuidador@example.test','caregiver',true),
 ('33333333-3333-3333-3333-333333333333',:'fam2','otra@example.test','admin',true);
grant select on all tables in schema public to postgres;

set role authenticated; set request.jwt.claim.sub = '11111111-1111-1111-1111-111111111111';
select (public.cuna_change('child','{"name":"Luna"}'::jsonb)->>'id') as luna \gset
select (public.cuna_change('child','{"name":"Sol"}'::jsonb)->>'id') as sol \gset
select public.cuna_change('device','{"device_id":"TEST-1","name":"S","secret":"k1"}'::jsonb);
select public.cuna_change('assign', jsonb_build_object('device_id','TEST-1','expected',null,'child_id',:'luna'));
select a.id as asg from public.assignments a where device_id='TEST-1' and ended_at is null \gset
reset role;

-- telemetría y evento propio, como dispositivo (anon)
select clock_timestamp() as t0 \gset
set role anon;
select public.cuna_ingest('TEST-1','k1','aaaaaaaa-0000-4000-8000-000000000001',:'t0','Temperatura',22.4,'°C');
select public.cuna_record('TEST-1','k1','bbbbbbbb-0000-4000-8000-000000000001',:'t0','ALERTA_MANUAL','Prueba','warning') as r1 \gset
select public.cuna_record('TEST-1','k1','bbbbbbbb-0000-4000-8000-000000000001',:'t0','ALERTA_MANUAL','Prueba','warning') as r2 \gset
reset role;
select pg_temp.expect(:'r1' = :'r2', 'reintento idempotente devuelve lo mismo');

set role authenticated; set request.jwt.claim.sub = '11111111-1111-1111-1111-111111111111';
select pg_temp.expect((select count(*) from public.audit_log where event_type='LECTURA' and detail='Temperatura: 22.4 °C' and child_id=:'luna'::uuid and device_id='TEST-1')=1, 'lectura auditada con bebé y dispositivo');
select pg_temp.expect((select count(*) from public.audit_log where event_type='ASIGNACION')=1, 'asignación auditada');
select pg_temp.expect((select count(*) from public.audit_log where event_type='ALERTA_MANUAL')=1, 'evento propio guardado una sola vez');
select pg_temp.expect((select child_id from public.audit_log where event_type='ALERTA_MANUAL')=:'luna'::uuid, 'bebé resuelto por asignación');
-- activar y trasladar
select public.cuna_change('operate', jsonb_build_object('device_id','TEST-1','expected',:'asg','enabled',true));
select public.cuna_change('assign', jsonb_build_object('device_id','TEST-1','expected',:'asg','child_id',:'sol'));
select pg_temp.expect((select string_agg(event_type,',' order by occurred_at,seq) from public.audit_log where child_id=:'luna'::uuid)='ASIGNACION,LECTURA,ALERTA_MANUAL,ACTIVACION,DESASIGNACION', 'orden de eventos de Luna');
select pg_temp.expect((select string_agg(event_type,',' order by occurred_at,seq) from public.audit_log where child_id=:'sol'::uuid)='ASIGNACION', 'traslado abre auditoría de Sol');
reset role;

-- horario de Costa Rica
insert into public.audit_log(family_id,occurred_at,event_type,detail,child_id,device_id)
 select family_id,'2026-11-13 05:59:59.9+00','X','a',child_id,device_id from public.audit_log limit 1;
insert into public.audit_log(family_id,occurred_at,event_type,detail,child_id,device_id)
 select family_id,'2026-11-13 06:00:00+00','X','b',child_id,device_id from public.audit_log limit 1;
select pg_temp.expect((select event_date||' '||event_time from public.audit_log where detail='a')='2026-11-12 23:59:59', 'fecha y hora locales antes de medianoche');
select pg_temp.expect((select event_date||' '||event_time from public.audit_log where detail='b')='2026-11-13 00:00:00', 'fecha y hora locales en medianoche');

-- RLS
set role authenticated; set request.jwt.claim.sub = '22222222-2222-2222-2222-222222222222';
select pg_temp.expect((select count(*) from public.audit_log)=0, 'cuidador no lee auditoría');
set request.jwt.claim.sub = '33333333-3333-3333-3333-333333333333';
select pg_temp.expect((select count(*) from public.audit_log)=0, 'otra familia no ve eventos ajenos');
reset role;

-- errores controlados y restricciones
select set_config('app.t0',:'t0',false);
do $$ begin
 begin perform public.cuna_record('TEST-1','mala','cccccccc-0000-4000-8000-000000000001',now(),'X','d'); raise exception 'no falló'; exception when sqlstate '42501' then raise notice 'OK: clave inválida -> 42501'; end;
 begin perform public.cuna_record('TEST-1','k1','cccccccc-0000-4000-8000-000000000002',now(),'LECTURA','d'); raise exception 'no falló'; exception when sqlstate '42501' then raise notice 'OK: tipo reservado rechazado'; end;
 begin perform public.cuna_record('TEST-1','k1','bbbbbbbb-0000-4000-8000-000000000001',current_setting('app.t0')::timestamptz,'ALERTA_MANUAL','OTRO','warning'); raise exception 'no falló'; exception when sqlstate 'PT409' then raise notice 'OK: contenido distinto -> PT409'; end;
 begin perform public.cuna_record('TEST-1','k1','cccccccc-0000-4000-8000-000000000003',now() + interval '1 day','X','d'); raise exception 'no falló'; exception when sqlstate 'P0001' then raise notice 'OK: futuro rechazado'; end;
 begin perform public.cuna_record('TEST-1','k1','cccccccc-0000-4000-8000-000000000004',now(),'X','d','grave'); raise exception 'no falló'; exception when sqlstate 'P0001' then raise notice 'OK: severidad inválida'; end;
 begin update public.audit_log set detail='x'; raise exception 'no falló'; exception when raise_exception then if sqlerrm <> 'Evento inmutable' then raise; end if; raise notice 'OK: update bloqueado'; end;
 begin delete from public.audit_log; raise exception 'no falló'; exception when raise_exception then if sqlerrm <> 'Evento inmutable' then raise; end if; raise notice 'OK: delete bloqueado'; end;
 begin insert into public.audit_log(family_id,occurred_at,event_type,detail,child_id,device_id) values (gen_random_uuid(),now(),'X','d',gen_random_uuid(),'TEST-1'); raise exception 'no falló'; exception when foreign_key_violation then raise notice 'OK: clave foránea exigida'; end;
end $$;
-- Sol sin dispositivo asignado de vuelta: retirar y enviar evento
set role authenticated; set request.jwt.claim.sub = '11111111-1111-1111-1111-111111111111';
select a.id as asg2 from public.assignments a where device_id='TEST-1' and ended_at is null \gset
select public.cuna_change('assign', jsonb_build_object('device_id','TEST-1','expected',:'asg2','child_id',null));
reset role;
do $$ begin
 begin perform public.cuna_record('TEST-1','k1','dddddddd-0000-4000-8000-000000000001',now(),'X','d'); raise exception 'no falló'; exception when sqlstate 'PT409' then raise notice 'OK: dispositivo sin asignar -> PT409'; end;
end $$;

-- el historial se rellena una sola vez
select count(*) as antes from public.audit_log \gset
alter table public.audit_log disable trigger audit_immutable;
delete from public.audit_log where event_type in ('LECTURA','ASIGNACION','DESASIGNACION');
alter table public.audit_log enable trigger audit_immutable;
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

select pg_temp.expect((select count(*) from public.audit_log)=:antes, 'relleno recupera lecturas y asignaciones');
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

select pg_temp.expect((select count(*) from public.audit_log)=:antes, 'repetir el relleno no duplica');
