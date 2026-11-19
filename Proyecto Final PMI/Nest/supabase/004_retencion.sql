-- ESTADO: NO APROBADA PARA DESPLEGAR. NO EJECUTAR EN EL SUPABASE COMPARTIDO SIN AUTORIZACIÓN DEL EQUIPO.
-- Pone triggers sobre tablas de otro módulo: events, audit_log y assignments. Impide vaciar esas tablas e impide borrar
-- filas de assignments. El código actual no las borra, pero un módulo futuro sí podría intentarlo.
-- Debe revisarla Sebastián, dueño de assignments. Ver docs/INTEGRACION-pendientes.md.
-- SCRUM-21 · RF-DAT-02. Retención permanente en Supabase.
-- Ejecutar después de 001 y 003. 001 y 003 ya bloquean UPDATE y DELETE sobre events y audit_log.
-- Esta migración cierra dos huecos: TRUNCATE, que no activa los triggers de fila, y el borrado de
-- asignaciones, que dejaría sin explicación a las lecturas ya guardadas.
begin;

create function cuna_private.block_truncate() returns trigger language plpgsql as $$
begin raise exception 'Los registros históricos no se pueden vaciar'; end; $$;
revoke all on function cuna_private.block_truncate() from public, anon, authenticated;

create trigger no_truncate before truncate on public.events for each statement execute function cuna_private.block_truncate();
create trigger no_truncate before truncate on public.audit_log for each statement execute function cuna_private.block_truncate();
create trigger no_truncate before truncate on public.assignments for each statement execute function cuna_private.block_truncate();
create trigger keep_assignments before delete on public.assignments for each row execute function cuna_private.keep_history();

commit;
