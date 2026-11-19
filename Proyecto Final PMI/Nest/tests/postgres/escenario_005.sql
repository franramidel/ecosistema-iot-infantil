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
-- Prueba de la migración 005 (UUID de dispositivos) contra un PostgreSQL real. Es autónoma:
-- crea su propia familia y usa identificadores propios, así que corre igual sobre una base vacía
-- o sobre una que ya tiene datos de los escenarios 003 y 004. No usar contra producción.
create or replace function pg_temp.expect(ok boolean, label text) returns void language plpgsql as $$ begin if not ok then raise exception 'FALLA: %', label; end if; raise notice 'OK: %', label; end $$;

insert into public.families default values returning id as famu \gset
insert into auth.users(id,email) values ('44444444-4444-4444-4444-444444444444','uuid.padre@example.test');
insert into public.members values ('44444444-4444-4444-4444-444444444444',:'famu','uuid.padre@example.test','admin',true);

set role authenticated; set request.jwt.claim.sub = '44444444-4444-4444-4444-444444444444';
select (public.cuna_change('child','{"name":"Nube"}'::jsonb)->>'id') as nube \gset
select public.cuna_change('device','{"device_id":"TEMP-01","name":"S","secret":"ku1"}'::jsonb);
select public.cuna_change('device','{"device_id":"TEMP-02","name":"S","secret":"ku2"}'::jsonb);
select public.cuna_change('assign', jsonb_build_object('device_id','TEMP-01','expected',null,'child_id',:'nube'));
reset role;

select clock_timestamp() as t0 \gset
set role anon;
select public.cuna_ingest('TEMP-01','ku1','aaaaaaaa-0000-4000-8000-0000000000a1',:'t0','Temperatura',21.5,'°C');
select public.cuna_record('TEMP-01','ku1','bbbbbbbb-0000-4000-8000-0000000000a1',:'t0','ALERTA_MANUAL','Prueba','warning');
reset role;

select uuid as u1 from public.devices where id='TEMP-01' \gset
select uuid as u2 from public.devices where id='TEMP-02' \gset
select pg_temp.expect(:'u1' <> :'u2' and :'u1' <> 'TEMP-01', 'dispositivo nuevo con UUID propio y distinto del ID operativo');
select pg_temp.expect((select id from public.devices where uuid=:'u1'::uuid)='TEMP-01', 'el ID operativo TEMP-01 se conserva');
select pg_temp.expect((select count(*) from public.audit_log where device_id='TEMP-01')=3
  and (select count(*) from public.audit_log where device_id='TEMP-01' and device_uuid=:'u1'::uuid)=3,
  'asignación, lectura y evento propio quedan con el UUID del dispositivo');

set role authenticated; set request.jwt.claim.sub = '44444444-4444-4444-4444-444444444444';
select a.id as asg from public.assignments a where device_id='TEMP-01' and ended_at is null \gset
select public.cuna_change('operate', jsonb_build_object('device_id','TEMP-01','expected',:'asg','enabled',true));
select pg_temp.expect((select count(*) from public.audit_log where event_type='ACTIVACION' and device_uuid=:'u1'::uuid)=1, 'activación auditada con UUID');
select pg_temp.expect((select count(*) from public.audit_log where device_uuid=:'u1'::uuid)=4, 'el administrador lee auditoría por UUID');
reset role;

select set_config('app.u1',:'u1',false), set_config('app.famu',:'famu',false), set_config('app.nube',:'nube',false);
do $$ declare u uuid := current_setting('app.u1')::uuid; f uuid := current_setting('app.famu')::uuid; c uuid := current_setting('app.nube')::uuid; begin
 -- control positivo: una fila coherente entra
 insert into public.audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,device_uuid) values (f,now(),'X','ok',c,'TEMP-01',u);
 raise notice 'OK: fila coherente aceptada';
 begin insert into public.audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,device_uuid) values (f,now(),'X','mal',c,'TEMP-01',gen_random_uuid());
  raise exception 'no falló';
 exception when foreign_key_violation then raise notice 'OK: UUID de otro dispositivo o inexistente rechazado'; end;
end $$;

do $$ begin
 begin insert into public.devices(id,family_id,name,uuid) select 'TEMP-03',family_id,'S',uuid from public.devices where id='TEMP-01';
  raise exception 'no falló';
 exception when unique_violation then raise notice 'OK: UUID repetido rechazado'; end;
 begin update public.devices set uuid=gen_random_uuid() where id='TEMP-02'; raise exception 'no falló';
 exception when raise_exception then if sqlerrm <> 'El UUID del dispositivo no puede cambiar' then raise; end if; raise notice 'OK: el UUID no se puede cambiar'; end;
 begin delete from public.devices where id='TEMP-01'; raise exception 'no falló';
 exception when foreign_key_violation then raise notice 'OK: un dispositivo con historial no se puede borrar'; end;
 begin update public.audit_log set detail='x' where device_id='TEMP-01'; raise exception 'no falló';
 exception when raise_exception then if sqlerrm <> 'Evento inmutable' then raise; end if; raise notice 'OK: auditoría sigue inmutable'; end;
 begin truncate public.audit_log cascade; raise exception 'no falló';
 exception when raise_exception then if sqlerrm <> 'Los registros históricos no se pueden vaciar' then raise; end if; raise notice 'OK: TRUNCATE sigue bloqueado'; end;
end $$;
select pg_temp.expect((select count(*) from pg_trigger where tgname in ('audit_immutable','devices_keep_uuid') and tgenabled <> 'D')=2, 'triggers de inmutabilidad activos');
select pg_temp.expect((select data_type from information_schema.columns where table_name='devices' and column_name='uuid')='uuid'
 and (select data_type from information_schema.columns where table_name='audit_log' and column_name='device_uuid')='uuid', 'columnas con tipo UUID real');
