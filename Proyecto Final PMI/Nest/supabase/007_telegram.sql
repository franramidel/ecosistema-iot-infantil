-- ESTADO: NO APROBADA PARA DESPLEGAR. NO EJECUTAR EN EL SUPABASE COMPARTIDO SIN AUTORIZACIÓN DEL EQUIPO.
-- Tablas y funciones propias de SCRUM-24. No modifica members ni families. Pero referencia public.audit_log (migración 003)
-- y agrega un trigger sobre audit_log, así que no es independiente de las migraciones que esperan aprobación.
-- Las funciones de procesador solo se permiten a service_role. Eso se probó en PostgreSQL local, no en Supabase real.
-- SCRUM-24 · RF-NOT-01. Vinculación con Telegram y cola persistente de notificaciones para Supabase.
-- Ejecutar DESPUÉS de 005. No modifica ninguna tabla ni función anterior y no toca la retención (SCRUM-21):
-- nada se borra, los códigos y vínculos se marcan. Repetirlo no cambia nada.
-- Las tablas no tienen política de escritura: solo las funciones de abajo las modifican. Las del procesador y
-- el contrato de SCRUM-25 exigen service_role. Las del usuario (status, start, stop, test) usan auth.uid(). Un usuario autenticado solo lee su propio estado de vínculo, sin chat_id.
begin;

create table if not exists public.telegram_links (
 member_id uuid primary key references public.members,
 family_id uuid not null references public.families,
 chat_id text unique,
 telegram_user_id text,
 linked_at timestamptz, unlinked_at timestamptz, blocked_at timestamptz
);
create table if not exists public.telegram_codes (
 code_hash text primary key,
 member_id uuid not null references public.members,
 family_id uuid not null references public.families,
 created_at timestamptz not null, expires_at timestamptz not null, used_at timestamptz,
 check(expires_at > created_at)
);
create unique index if not exists telegram_one_open_code on public.telegram_codes(member_id) where used_at is null;
create table if not exists public.telegram_attempts (
 chat_id text primary key, failures integer not null check(failures >= 0), window_start timestamptz not null
);
create table if not exists public.telegram_state (key text primary key, value text not null);
create table if not exists public.telegram_worker (
 id integer primary key check(id = 1), owner text not null, expires_at timestamptz not null
);
create table if not exists public.notification_queue (
 id uuid primary key default gen_random_uuid(),
 family_id uuid not null references public.families,
 member_id uuid not null references public.members,
 event_id uuid references public.audit_log(id),
 idempotency_key text not null check(length(idempotency_key) between 1 and 128),
 message text not null check(length(message) between 1 and 4096),
 status text not null check(status in ('pendiente','procesando','enviada','error_recuperable','error_permanente','sin_vinculo')),
 attempts integer not null default 0 check(attempts >= 0),
 max_attempts integer not null default 5 check(max_attempts between 1 and 20),
 next_attempt_at timestamptz not null, locked_until timestamptz, lock_token text,
 last_attempt_at timestamptz, last_error text check(length(last_error) <= 300),
 created_at timestamptz not null default now(), sent_at timestamptz,
 unique(family_id, idempotency_key)
);
create index if not exists queue_due on public.notification_queue(status, next_attempt_at);
create index if not exists queue_member on public.notification_queue(member_id, created_at);

-- Seguridad: RLS activa en todo. Sin políticas, solo service_role (que la omite) escribe o lee.
alter table public.telegram_links enable row level security;
alter table public.telegram_codes enable row level security;
alter table public.telegram_attempts enable row level security;
alter table public.telegram_state enable row level security;
alter table public.telegram_worker enable row level security;
alter table public.notification_queue enable row level security;
revoke all on table public.telegram_links, public.telegram_codes, public.telegram_attempts, public.telegram_state,
 public.telegram_worker, public.notification_queue from public, anon, authenticated;
grant select (member_id, family_id, linked_at, unlinked_at, blocked_at) on public.telegram_links to authenticated;
drop policy if exists own_link on public.telegram_links;
create policy own_link on public.telegram_links for select to authenticated using (member_id = (select auth.uid()));

-- Los eventos NOTIFICACION_* solo los puede crear la función que cierra una solicitud. Ni un dispositivo
-- ni un usuario los falsifican, aunque cuna_record (003/005) no los conozca.
create or replace function cuna_private.guard_notification_events() returns trigger language plpgsql as $$
begin
 if new.event_type in ('NOTIFICACION_ENVIADA','NOTIFICACION_FALLIDA') and coalesce(current_setting('cuna.notificacion', true),'') <> '1'
  then raise sqlstate '42501' using message='Ese tipo de evento lo registra el sistema.'; end if;
 return new;
end; $$;
drop trigger if exists guard_notification_events on public.audit_log;
create trigger guard_notification_events before insert on public.audit_log
 for each row execute function cuna_private.guard_notification_events();

-- Código de vinculación: 8 caracteres de un alfabeto sin ambigüedades. Cada byte aleatorio se reduce
-- módulo 32, y 256 es múltiplo de 32, así que no hay sesgo. Solo se guarda su hash.
create or replace function public.cuna_telegram_code(member uuid) returns jsonb
language plpgsql security definer set search_path='' as $$
declare alfabeto constant text := 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'; hex text; codigo text := ''; fam uuid; expira timestamptz := now() + interval '10 minutes';
begin
 select family_id into fam from public.members where id = member;
 if fam is null then raise exception 'El usuario no existe.'; end if;
 hex := replace(gen_random_uuid()::text, '-', '');
 for i in 0..7 loop
  codigo := codigo || substr(alfabeto, (('x' || substr(hex, i*2+1, 2))::bit(8)::int % 32) + 1, 1);
 end loop;
 update public.telegram_codes set used_at = now() where member_id = member and used_at is null;
 insert into public.telegram_codes values (encode(sha256(convert_to(codigo,'UTF8')),'hex'), member, fam, now(), expira, null);
 insert into public.telegram_links(member_id, family_id) values (member, fam) on conflict (member_id) do nothing;
 return jsonb_build_object('codigo', codigo, 'expira', expira);
end; $$;

-- Completa la vinculación con el chat real que escribió al bot. Devuelve el estado como texto.
drop function if exists public.cuna_telegram_link(text,text,text);
create or replace function public.cuna_telegram_link(code text, chat text, tg_user text, next_offset bigint default null) returns text
language plpgsql security definer set search_path='' as $$
declare reg public.telegram_codes; intento public.telegram_attempts; existe boolean; norm text := upper(regexp_replace(coalesce(code,''), '[^A-Za-z0-9]', '', 'g'));
begin
 -- El offset de getUpdates se guarda en la misma transacción que el resultado, como en SQLite.
 if next_offset is not null then
  insert into public.telegram_state values ('offset', next_offset::text) on conflict (key) do update set value = excluded.value;
 end if;
 select * into intento from public.telegram_attempts where chat_id = chat;
 if found and intento.failures >= 5 and intento.window_start + interval '15 minutes' > now() then return 'bloqueado'; end if;
 select * into reg from public.telegram_codes where code_hash = encode(sha256(convert_to(norm,'UTF8')),'hex') and length(norm) = 8 for update;
 existe := found; -- FOUND cambia con el INSERT de abajo, por eso se copia aquí
 if not existe or reg.used_at is not null or reg.expires_at <= now() then
  insert into public.telegram_attempts values (chat, 1, now())
   on conflict (chat_id) do update set
    failures = case when public.telegram_attempts.window_start + interval '15 minutes' <= now() then 1 else public.telegram_attempts.failures + 1 end,
    window_start = case when public.telegram_attempts.window_start + interval '15 minutes' <= now() then now() else public.telegram_attempts.window_start end;
  return case when existe and reg.used_at is null then 'expirado' else 'invalido' end;
 end if;
 if exists (select 1 from public.telegram_links where chat_id = chat and member_id <> reg.member_id) then return 'chat_en_uso'; end if;
 insert into public.telegram_links(member_id, family_id, chat_id, telegram_user_id, linked_at) values (reg.member_id, reg.family_id, chat, tg_user, now())
  on conflict (member_id) do update set chat_id = excluded.chat_id, telegram_user_id = excluded.telegram_user_id,
   linked_at = excluded.linked_at, unlinked_at = null, blocked_at = null;
 update public.telegram_codes set used_at = now() where code_hash = reg.code_hash;
 update public.telegram_attempts set failures = 0 where chat_id = chat;
 update public.families set version = version + 1 where id = reg.family_id;
 return 'vinculado';
end; $$;

create or replace function public.cuna_telegram_unlink(member uuid) returns void
language plpgsql security definer set search_path='' as $$
begin
 update public.telegram_links set chat_id = null, telegram_user_id = null, unlinked_at = now(), blocked_at = null
  where member_id = member and chat_id is not null;
 update public.telegram_codes set used_at = now() where member_id = member and used_at is null;
end; $$;

-- Contrato para SCRUM-25: guarda la solicitud y responde sin esperar a Telegram. Idempotente por familia.
create or replace function public.cuna_notify(member uuid, key text, msg text, event uuid default null) returns jsonb
language plpgsql security definer set search_path='' as $$
declare fam uuid; prev public.notification_queue; linked boolean; new_id uuid; inicial text;
begin
 select family_id into fam from public.members where id = member;
 if fam is null then raise sqlstate 'PT404' using message='El usuario destinatario no existe.'; end if;
 if event is not null and not exists (select 1 from public.audit_log where id = event and family_id = fam)
  then raise sqlstate 'PT404' using message='El evento no existe en la familia del destinatario.'; end if;
 select * into prev from public.notification_queue where family_id = fam and idempotency_key = key;
 if found then
  if (prev.member_id, prev.message, prev.event_id) is distinct from (member, msg, event)
   then raise sqlstate 'PT409' using message='La clave de idempotencia ya se usó con otro contenido.'; end if;
  return jsonb_build_object('id', prev.id, 'estado', prev.status, 'duplicado', true, 'vinculado', prev.status <> 'sin_vinculo');
 end if;
 linked := exists (select 1 from public.telegram_links where member_id = member and chat_id is not null);
 inicial := case when linked then 'pendiente' else 'sin_vinculo' end;
 insert into public.notification_queue(family_id, member_id, event_id, idempotency_key, message, status, next_attempt_at, last_error)
  values (fam, member, event, key, msg, inicial, now(), case when linked then null else 'El usuario no tiene Telegram vinculado.' end)
  returning id into new_id;
 return jsonb_build_object('id', new_id, 'estado', inicial, 'duplicado', false, 'vinculado', linked);
end; $$;

-- Reclama solicitudes listas de forma atómica. for update skip locked evita que dos procesadores tomen la misma.
create or replace function public.cuna_queue_claim(token text, lim integer default 10) returns jsonb
language plpgsql security definer set search_path='' as $$
declare r public.notification_queue; salida jsonb := '[]'::jsonb; chat text;
begin
 update public.notification_queue set status = 'error_permanente', lock_token = null, locked_until = null,
  last_error = 'Reintentos agotados tras interrupciones del procesador.'
  where status = 'procesando' and locked_until <= now() and attempts >= max_attempts;
 for r in select * from public.notification_queue
   where (status in ('pendiente','error_recuperable') and next_attempt_at <= now()) or (status = 'procesando' and locked_until <= now())
   order by next_attempt_at, created_at limit lim for update skip locked loop
  update public.notification_queue set status = 'procesando', attempts = attempts + 1, lock_token = token,
   locked_until = now() + interval '2 minutes', last_attempt_at = now() where id = r.id;
  select l.chat_id into chat from public.telegram_links l where l.member_id = r.member_id;
  salida := salida || jsonb_build_object('id', r.id, 'family_id', r.family_id, 'event_id', r.event_id, 'member_id', r.member_id, 'message', r.message,
   'attempts', r.attempts + 1, 'max_attempts', r.max_attempts, 'chat_id', chat, 'token', token);
 end loop;
 return salida;
end; $$;

-- Cierra un intento. kind: 'ok', 'temporal' o 'permanente'. Solo vale si el procesador conserva el arrendamiento.
drop function if exists public.cuna_queue_result(uuid,text,text,text,integer);
create or replace function public.cuna_queue_result(job uuid, token text, kind text, err text default null, retry_after integer default 0, blocked boolean default false) returns text
language plpgsql security definer set search_path='' as $$
declare r public.notification_queue; final text; limpio text := left(regexp_replace(coalesce(err,''), '\d{5,}:[A-Za-z0-9_-]{20,}', '***', 'g'), 300);
begin
 if kind not in ('ok','temporal','permanente') then raise exception 'Resultado inválido.'; end if;
 select * into r from public.notification_queue where id = job and lock_token = token and status = 'procesando' for update;
 if not found then return null; end if;
 if kind = 'ok' then
  final := 'enviada';
  update public.notification_queue set status = final, sent_at = now(), last_error = null, lock_token = null, locked_until = null where id = job;
 elsif kind = 'temporal' and r.attempts < r.max_attempts then
  final := 'error_recuperable';
  update public.notification_queue set status = final, last_error = limpio, lock_token = null, locked_until = null,
   next_attempt_at = now() + make_interval(secs => greatest(least(30 * power(4, greatest(r.attempts - 1, 0)), 1800), least(coalesce(retry_after, 0), 3600)))
   where id = job;
 else
  final := 'error_permanente';
  update public.notification_queue set status = final, last_error = case when kind = 'temporal' then left('Reintentos agotados: ' || limpio, 300) else limpio end,
   lock_token = null, locked_until = null where id = job;
 end if;
 if blocked then
  update public.telegram_links set blocked_at = now() where member_id = r.member_id and chat_id is not null;
 end if;
 if final <> 'error_recuperable' and r.event_id is not null then
  perform set_config('cuna.notificacion', '1', true);
  insert into public.audit_log(family_id, occurred_at, event_type, detail, child_id, device_id, device_uuid, severity, source_event_id)
   select family_id, now(), case when final = 'enviada' then 'NOTIFICACION_ENVIADA' else 'NOTIFICACION_FALLIDA' end,
    case when final = 'enviada' then 'Alerta enviada por Telegram' else 'Alerta por Telegram no entregada' end,
    child_id, device_id, device_uuid, case when final = 'enviada' then 'info' else 'warning' end,
    'notif:' || r.id || ':' || final
   from public.audit_log where id = r.event_id on conflict do nothing;
 end if;
 return final;
end; $$;

-- Estado del procesador y offset, solo para service_role (los usa telegram_worker.py).
create or replace function public.cuna_telegram_lease(owner_id text, release boolean default false) returns boolean
language plpgsql security definer set search_path='' as $$
declare fila public.telegram_worker;
begin
 select * into fila from public.telegram_worker where id = 1 for update;
 if release then
  update public.telegram_worker set expires_at = now() where id = 1 and owner = owner_id;
  return true;
 end if;
 if found and fila.owner <> owner_id and fila.expires_at > now() then return false; end if;
 insert into public.telegram_worker values (1, owner_id, now() + interval '90 seconds')
  on conflict (id) do update set owner = excluded.owner, expires_at = excluded.expires_at;
 return true;
end; $$;

create or replace function public.cuna_telegram_offset(new_value bigint default null) returns bigint
language plpgsql security definer set search_path='' as $$
begin
 if new_value is not null then
  insert into public.telegram_state values ('offset', new_value::text) on conflict (key) do update set value = excluded.value;
  return new_value;
 end if;
 return (select value::bigint from public.telegram_state where key = 'offset');
end; $$;

-- Funciones para el usuario autenticado. Usan auth.uid(): nadie actúa sobre otra cuenta y el servidor web
-- no necesita la clave de servicio para ninguna acción del usuario.
create or replace function public.cuna_telegram_status() returns jsonb
language plpgsql security definer set search_path='' as $$
declare yo uuid := auth.uid(); l public.telegram_links; c timestamptz; u record;
begin
 if yo is null or not exists (select 1 from public.members where id = yo) then raise sqlstate '42501' using message='Cuenta sin acceso.'; end if;
 select * into l from public.telegram_links where member_id = yo;
 select expires_at into c from public.telegram_codes where member_id = yo and used_at is null and expires_at > now();
 select status, last_error into u from public.notification_queue where member_id = yo and idempotency_key like 'prueba:%' order by created_at desc limit 1;
 return jsonb_build_object(
  'vinculado', coalesce(l.chat_id is not null, false),
  'vinculado_desde', case when l.chat_id is not null then l.linked_at end,
  'bloqueado', coalesce(l.chat_id is not null and l.blocked_at is not null, false),
  'codigo_expira', c,
  'procesador_activo', exists (select 1 from public.telegram_worker where expires_at > now()),
  'ultima_prueba', case when u.status is not null then jsonb_build_object('estado', u.status, 'error', u.last_error) end);
end; $$;

create or replace function public.cuna_telegram_start() returns jsonb
language plpgsql security definer set search_path='' as $$
begin
 if auth.uid() is null or not exists (select 1 from public.members where id = auth.uid()) then raise sqlstate '42501' using message='Cuenta sin acceso.'; end if;
 return public.cuna_telegram_code(auth.uid());
end; $$;

create or replace function public.cuna_telegram_stop() returns jsonb
language plpgsql security definer set search_path='' as $$
declare fam uuid;
begin
 select family_id into fam from public.members where id = auth.uid();
 if fam is null then raise sqlstate '42501' using message='Cuenta sin acceso.'; end if;
 perform public.cuna_telegram_unlink(auth.uid());
 update public.families set version = version + 1 where id = fam;
 return '{}'::jsonb;
end; $$;

create or replace function public.cuna_telegram_test() returns jsonb
language plpgsql security definer set search_path='' as $$
declare yo uuid := auth.uid(); ahora timestamp := now() at time zone 'America/Costa_Rica';
begin
 if yo is null or not exists (select 1 from public.members where id = yo) then raise sqlstate '42501' using message='Cuenta sin acceso.'; end if;
 if not exists (select 1 from public.telegram_links where member_id = yo and chat_id is not null)
  then raise sqlstate 'PT409' using message='Vincula tu cuenta de Telegram antes de enviar una prueba.'; end if;
 if exists (select 1 from public.notification_queue where member_id = yo and idempotency_key like 'prueba:%' and created_at > now() - interval '15 seconds')
  then raise sqlstate 'PT429' using message='Espera unos segundos antes de enviar otra prueba.'; end if;
 return public.cuna_notify(yo, 'prueba:' || gen_random_uuid()::text,
  E'<b>Ecosistema IoT Infantil</b>\n\nAlerta: Notificación de prueba\nFecha: ' || to_char(ahora, 'DD/MM/YYYY') ||
  E'\nHora: ' || to_char(ahora, 'HH24:MI') || E'\nEstado: Informativo', null);
end; $$;

revoke all on function public.cuna_telegram_code(uuid), public.cuna_telegram_link(text,text,text,bigint), public.cuna_telegram_unlink(uuid),
 public.cuna_notify(uuid,text,text,uuid), public.cuna_queue_claim(text,integer), public.cuna_queue_result(uuid,text,text,text,integer,boolean),
 public.cuna_telegram_lease(text,boolean), public.cuna_telegram_offset(bigint),
 public.cuna_telegram_status(), public.cuna_telegram_start(), public.cuna_telegram_stop(), public.cuna_telegram_test()
 from public, anon, authenticated;
grant execute on function public.cuna_telegram_code(uuid), public.cuna_telegram_link(text,text,text,bigint), public.cuna_telegram_unlink(uuid),
 public.cuna_notify(uuid,text,text,uuid), public.cuna_queue_claim(text,integer), public.cuna_queue_result(uuid,text,text,text,integer,boolean),
 public.cuna_telegram_lease(text,boolean), public.cuna_telegram_offset(bigint) to service_role;
grant execute on function public.cuna_telegram_status(), public.cuna_telegram_start(), public.cuna_telegram_stop(), public.cuna_telegram_test() to authenticated;
commit;
