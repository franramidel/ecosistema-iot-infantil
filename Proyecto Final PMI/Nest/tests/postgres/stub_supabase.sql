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
-- Lo mínimo de Supabase que necesitan las migraciones: roles, auth.users, auth.uid() y la publicación.
do $$ begin
 if not exists (select 1 from pg_roles where rolname='anon') then create role anon nologin; end if;
 if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated nologin; end if;
 if not exists (select 1 from pg_roles where rolname='service_role') then create role service_role nologin bypassrls; end if;
end $$;
create schema auth;
create table auth.users(id uuid primary key default gen_random_uuid(), email text, raw_app_meta_data jsonb default '{}');
create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
grant usage on schema auth to authenticated, anon;
create publication supabase_realtime;
grant usage on schema public to anon, authenticated, service_role;
