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
-- Prueba de la migración 007 (Telegram) contra un PostgreSQL real. Requiere haber corrido antes
-- escenario_005.sql y escenario_006.sql: usa a 44444444 (admin), 55555555 (cuidador) y 66666666 (admin de otra familia).
-- Simula los roles de Supabase. No usar contra producción.
create or replace function pg_temp.expect(ok boolean, label text) returns void language plpgsql as $$ begin if not ok then raise exception 'FALLA: %', label; end if; raise notice 'OK: %', label; end $$;

select family_id as famu from public.members where id='44444444-4444-4444-4444-444444444444' \gset
select id as evento from public.audit_log where family_id=:'famu' limit 1 \gset
select set_config('t.evento', :'evento', false);

set role service_role;
select (public.cuna_telegram_code('44444444-4444-4444-4444-444444444444')->>'codigo') as c1 \gset
select pg_temp.expect(length(:'c1')=8 and :'c1' ~ '^[A-HJ-NP-Z2-9]{8}$', 'el código tiene 8 caracteres del alfabeto sin ambigüedades');
reset role;
select pg_temp.expect((select count(*) from public.telegram_codes where code_hash like '%'||:'c1'||'%')=0, 'solo se guarda el hash del código');
set role service_role;
select (public.cuna_telegram_code('44444444-4444-4444-4444-444444444444')->>'codigo') as c2 \gset
select pg_temp.expect(public.cuna_telegram_link(:'c1','9001','9001')='invalido', 'un código nuevo anula el anterior');
select pg_temp.expect(public.cuna_telegram_link(lower(:'c2'),'9001','9001')='vinculado', 'el código válido vincula (sin distinguir mayúsculas)');
select pg_temp.expect(public.cuna_telegram_link(:'c2','9002','9002')='invalido', 'el código es de un solo uso');
reset role;
select pg_temp.expect((select chat_id from public.telegram_links where member_id='44444444-4444-4444-4444-444444444444')='9001', 'el chat queda guardado');
set role service_role;

select (public.cuna_telegram_code('66666666-6666-6666-6666-666666666666')->>'codigo') as c3 \gset
select pg_temp.expect(public.cuna_telegram_link(:'c3','9001','9001')='chat_en_uso', 'un chat ya vinculado no se puede reclamar desde otra cuenta');
reset role;
select pg_temp.expect((select chat_id is null from public.telegram_links where member_id='66666666-6666-6666-6666-666666666666'), 'la otra cuenta sigue sin vínculo');
set role service_role;
reset role;
update public.telegram_codes set expires_at=now()-interval '1 second', created_at=now()-interval '11 minutes' where member_id='66666666-6666-6666-6666-666666666666' and used_at is null;
set role service_role;
select pg_temp.expect(public.cuna_telegram_link(:'c3','9003','9003')='expirado', 'un código vencido no vincula');
do $$ begin for i in 1..5 loop perform public.cuna_telegram_link('ABCDEFGH','9004','9004'); end loop; end $$;
select (public.cuna_telegram_code('66666666-6666-6666-6666-666666666666')->>'codigo') as c4 \gset
select pg_temp.expect(public.cuna_telegram_link(:'c4','9004','9004')='bloqueado', 'tras 5 fallos el chat queda bloqueado temporalmente');

-- Cola: idempotencia, aislamiento y reclamo.
select (public.cuna_notify('44444444-4444-4444-4444-444444444444','k-1','Mensaje 1',:'evento')->>'id') as n1 \gset
select pg_temp.expect((public.cuna_notify('44444444-4444-4444-4444-444444444444','k-1','Mensaje 1',:'evento')->>'duplicado')::boolean, 'la misma clave y contenido devuelve la solicitud existente');
reset role;
select pg_temp.expect((select count(*) from public.notification_queue where idempotency_key='k-1')=1, 'no se duplicó la fila');
set role service_role;
do $$ begin
 begin perform public.cuna_notify('44444444-4444-4444-4444-444444444444','k-1','Otro texto',current_setting('t.evento')::uuid); raise exception 'no falló';
 exception when sqlstate 'PT409' then raise notice 'OK: misma clave con otro contenido se rechaza'; end;
 begin perform public.cuna_notify('66666666-6666-6666-6666-666666666666','k-2','x',current_setting('t.evento')::uuid); raise exception 'no falló';
 exception when sqlstate 'PT404' then raise notice 'OK: no se puede usar el evento de otra familia'; end;
end $$;
select pg_temp.expect((public.cuna_notify('66666666-6666-6666-6666-666666666666','k-3','Sin Telegram')->>'estado')='sin_vinculo', 'quien no tiene Telegram queda en sin_vinculo');

select public.cuna_queue_claim('trabajador-a', 10) as lote \gset
select pg_temp.expect(jsonb_array_length(:'lote'::jsonb)=1 and (:'lote'::jsonb->0->>'chat_id')='9001', 'se reclama una solicitud con su chat');
select pg_temp.expect(jsonb_array_length(public.cuna_queue_claim('trabajador-b', 10))=0, 'otro procesador no la toma mientras el arrendamiento vive');
select pg_temp.expect(public.cuna_queue_result(:'n1','token-viejo','ok') is null, 'un token ajeno no cierra la solicitud');
select pg_temp.expect(public.cuna_queue_result(:'n1','trabajador-a','ok')='enviada', 'el dueño del arrendamiento cierra la solicitud');
reset role;
select pg_temp.expect((select count(*) from public.audit_log where event_type='NOTIFICACION_ENVIADA' and family_id=:'famu')=1, 'queda constancia en la auditoría');
set role service_role;

-- Reintentos limitados y recuperación de abandonadas.
select (public.cuna_notify('44444444-4444-4444-4444-444444444444','k-4','Mensaje 4')->>'id') as n4 \gset
reset role;
do $$ declare i int; r text; begin
 for i in 1..5 loop
  perform public.cuna_queue_claim('t'||i, 5);
  r := public.cuna_queue_result((select id from public.notification_queue where idempotency_key='k-4'),'t'||i,'temporal','HTTP 500 con token 123456:'||repeat('a',30));
  update public.notification_queue set next_attempt_at=now() where idempotency_key='k-4' and status='error_recuperable';
 end loop;
 if r <> 'error_permanente' then raise exception 'FALLA: no pasó a permanente (%)', r; end if;
 raise notice 'OK: tras agotar los intentos el error es permanente';
end $$;
set role service_role;
reset role;
select pg_temp.expect((select attempts=5 and last_error not like '%123456:%' from public.notification_queue where idempotency_key='k-4'), 'se hicieron 5 intentos y el token se limpió del error');
set role service_role;
select (public.cuna_notify('44444444-4444-4444-4444-444444444444','k-5','Mensaje 5')->>'id') as n5 \gset
select public.cuna_queue_claim('muere', 5) as perdido \gset
reset role;
update public.notification_queue set locked_until=now()-interval '1 second' where idempotency_key='k-5';
set role service_role;
select pg_temp.expect(jsonb_array_length(public.cuna_queue_claim('nuevo', 5))=1, 'una solicitud abandonada se recupera al vencer el arrendamiento');
select pg_temp.expect(public.cuna_queue_result(:'n5','muere','ok') is null and public.cuna_queue_result(:'n5','nuevo','ok')='enviada', 'el resultado tardío del proceso viejo no cuenta');

-- Seguridad. Nadie falsifica eventos de notificación ni lee la cola desde la API de tablas.
reset role;
do $$ begin
 begin
  insert into public.audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,device_uuid,severity)
   select family_id,now(),'NOTIFICACION_ENVIADA','falso',child_id,device_id,device_uuid,'info' from public.audit_log limit 1;
  raise exception 'no falló';
 exception when sqlstate '42501' then raise notice 'OK: no se pueden falsificar eventos NOTIFICACION_ENVIADA'; end;
end $$;
set role authenticated; set request.jwt.claim.sub = '44444444-4444-4444-4444-444444444444';
select pg_temp.expect((select count(*) from public.telegram_links)=1, 'un usuario ve solo su propio estado de vínculo');
do $$ begin
 begin perform chat_id from public.telegram_links; raise exception 'no falló';
 exception when insufficient_privilege then raise notice 'OK: chat_id no se puede leer desde la API de tablas'; end;
 begin perform count(*) from public.notification_queue; raise exception 'no falló';
 exception when insufficient_privilege then raise notice 'OK: la cola no se lee desde la API de tablas'; end;
 begin perform public.cuna_notify('44444444-4444-4444-4444-444444444444','x','y'); raise exception 'no falló';
 exception when insufficient_privilege then raise notice 'OK: un usuario no puede encolar ni reclamar directamente'; end;
end $$;
reset role;
select pg_temp.expect((select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public'
  and c.relname in ('telegram_links','telegram_codes','telegram_attempts','telegram_state','telegram_worker','notification_queue') and c.relrowsecurity)=6, 'las seis tablas tienen RLS activa');

-- Funciones añadidas para el almacén de Supabase: procesador (arrendamiento, offset, bloqueo) y usuario (auth.uid()).
set role service_role;
select pg_temp.expect(public.cuna_telegram_link('ZZZZZZZZ','9100','9100')='invalido', 'un código desconocido es inválido, no expirado');
select pg_temp.expect(public.cuna_telegram_lease('proc-a')=true, 'el primer procesador toma el arrendamiento');
select pg_temp.expect(public.cuna_telegram_lease('proc-b')=false, 'un segundo procesador no lo toma');
select pg_temp.expect(public.cuna_telegram_lease('proc-a')=true, 'el dueño lo renueva');
select pg_temp.expect(public.cuna_telegram_lease('proc-a', true)=true and public.cuna_telegram_lease('proc-b')=true, 'al liberarlo otro procesador puede tomarlo');
select pg_temp.expect(public.cuna_telegram_offset(41)=41 and public.cuna_telegram_offset()=41, 'el offset se guarda y se lee');
select pg_temp.expect(public.cuna_telegram_link('ZZZZZZZZ','9100','9100',55)='invalido' and public.cuna_telegram_offset()=55, 'el offset se guarda junto con el resultado de la vinculación');
select (public.cuna_notify('44444444-4444-4444-4444-444444444444','k-bloqueo','Mensaje de bloqueo')->>'id') as nb \gset
select public.cuna_queue_claim('t-bloqueo', 10) as lote2 \gset
select pg_temp.expect(public.cuna_queue_result(:'nb','t-bloqueo','permanente','Forbidden',0,true)='error_permanente', 'un 403 cierra la solicitud como permanente');
reset role;
select pg_temp.expect((select blocked_at is not null from public.telegram_links where member_id='44444444-4444-4444-4444-444444444444'), 'el vínculo queda marcado como bloqueado');
select pg_temp.expect((select count(*) from public.notification_queue where status='procesando')<=1, 'no quedan solicitudes colgadas por la prueba de bloqueo');

set role authenticated; set request.jwt.claim.sub = '44444444-4444-4444-4444-444444444444';
select pg_temp.expect((public.cuna_telegram_status()->>'vinculado')::boolean and (public.cuna_telegram_status()->>'bloqueado')::boolean, 'status devuelve el vínculo propio y el bloqueo');
select pg_temp.expect((public.cuna_telegram_status()->>'procesador_activo')::boolean, 'status informa que el procesador está activo');
select pg_temp.expect(length(public.cuna_telegram_start()->>'codigo')=8, 'start genera un código para el propio usuario');
select pg_temp.expect((public.cuna_telegram_status()->>'codigo_expira') is not null, 'status informa el vencimiento del código');
select public.cuna_telegram_stop();
select pg_temp.expect(not (public.cuna_telegram_status()->>'vinculado')::boolean, 'stop desvincula al propio usuario');
do $$ begin
 begin perform public.cuna_telegram_test(); raise exception 'no falló';
 exception when sqlstate 'PT409' then raise notice 'OK: test sin vínculo da 409'; end;
end $$;
set request.jwt.claim.sub = '66666666-6666-6666-6666-666666666666';
select pg_temp.expect(not (public.cuna_telegram_status()->>'vinculado')::boolean, 'otro usuario no ve el vínculo ajeno');
reset role;
-- Vincular de nuevo al usuario 44... y probar el límite de una prueba cada 15 segundos.
set role service_role;
select (public.cuna_telegram_code('44444444-4444-4444-4444-444444444444')->>'codigo') as c9 \gset
select pg_temp.expect(public.cuna_telegram_link(:'c9','9200','9200')='vinculado', 'el usuario se vincula de nuevo');
reset role;
set role authenticated; set request.jwt.claim.sub = '44444444-4444-4444-4444-444444444444';
select pg_temp.expect((public.cuna_telegram_test()->>'estado')='pendiente', 'test encola una prueba propia');
do $$ begin
 begin perform public.cuna_telegram_test(); raise exception 'no falló';
 exception when sqlstate 'PT429' then raise notice 'OK: una segunda prueba inmediata da 429'; end;
end $$;
reset role;
set role anon;
do $$ begin
 begin perform public.cuna_telegram_status(); raise exception 'no falló';
 exception when insufficient_privilege then raise notice 'OK: anon no puede consultar el estado'; end;
end $$;
reset role;
