"use strict";
const $ = id => document.getElementById(id);
let state, chosen = null, tab = "profile", syncResource, syncPromise, syncEpoch = 0, generation = 0, onSave;
let noticeTimer;
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const roleName = role => role === "admin" ? "Padre / administrador" : "Cuidador";
const fmt = number => new Intl.NumberFormat("es-CR", {maximumFractionDigits: 3}).format(number);
const date = value => new Date(value).toLocaleString("es-CR");
const admin = () => state?.me.role === "admin";

function notice(msg, error=false) {
  clearTimeout(noticeTimer);
  $("notice").textContent = msg;
  $("notice").className = error ? "error" : "";
  $("notice").hidden = false;
  noticeTimer = setTimeout(() => $("notice").hidden = true, 6000);
}
async function api(path, body) {
  let res;
  try {
    res = await fetch(path, body === undefined ? {} : {
      method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)
    });
  } catch { throw Error("No se pudo conectar con el servidor."); }
  const data = await res.json();
  if (!res.ok) {
    const error = Error(data.error || "La operación falló.");
    error.status = res.status;
    if (res.status === 401 && path !== "/api/login") showLogin();
    throw error;
  }
  return data;
}
function showLogin() {
  generation++;
  stopSync();
  state = null; chosen = null; tab = "profile";
  if ($("modal").open) $("modal").close();
  $("account").hidden = $("dashboard").hidden = true;
  $("login").hidden = false;
}
async function refresh() {
  const marker = ++generation;
  const data = await api("/api/state" + (chosen ? "?child="+encodeURIComponent(chosen) : ""));
  if (marker !== generation) return;
  state = data; chosen = data.selected;
  $("login").hidden = true; $("dashboard").hidden = $("account").hidden = false;
  render();
  ensureSync();
}
function reportSyncError(error) {
  if (error.status !== 401) notice(error.message,true);
}
function stopSync() {
  syncEpoch++;
  syncResource?.close();
  syncResource = syncPromise = null;
}
function ensureSync() {
  if (syncResource || syncPromise) return;
  const epoch = syncEpoch;
  syncPromise = api("/api/sync").then(config => {
    if (epoch !== syncEpoch || !state) return;
    if (config.mode === "sse") {
      const source = new EventSource("/api/stream");
      const update = () => refresh().catch(reportSyncError);
      source.onmessage = update;
      source.addEventListener("revisar",update);
      syncResource = {close:() => source.close()};
      return;
    }
    if (config.mode !== "realtime" || !config.url || !config.family_id || !config.access_token) {
      throw Error("La configuración de sincronización de Supabase no es válida.");
    }
    syncResource = createRealtimeSync(epoch);
    syncResource.connect(config);
  }).catch(error => {
    if (epoch === syncEpoch && state) reportSyncError(error);
  }).finally(() => {
    if (epoch === syncEpoch) syncPromise = null;
  });
}
function createRealtimeSync(epoch) {
  let socket, stopped = false, retryTimer, heartbeatTimer, tokenTimer, joinTimer, refreshTimer;
  let retries = 0, nextRef = 0, joinRef, reportedFailure = false;
  const isCurrent = () => !stopped && epoch === syncEpoch && !!state;
  const clearConnectionTimers = () => {
    clearInterval(heartbeatTimer);
    clearTimeout(tokenTimer);
    clearTimeout(joinTimer);
    clearTimeout(refreshTimer);
  };
  const scheduleStateRefresh = () => {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => refresh().catch(reportSyncError),100);
  };
  const scheduleReconnect = (error, currentSocket) => {
    if (!isCurrent() || retryTimer || (currentSocket && socket !== currentSocket)) return;
    clearConnectionTimers();
    const previous = socket;
    socket = null;
    if (previous && previous.readyState < WebSocket.CLOSING) previous.close();
    if (error && !reportedFailure) {
      notice(error.message || "Se perdió la conexión de sincronización.",true);
      reportedFailure = true;
    }
    retries++;
    const delay = Math.min(30000,1000 * (2 ** Math.min(retries - 1,5))) + Math.random() * 500;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      connect();
    },delay);
  };
  const scheduleTokenRefresh = (config, currentSocket) => {
    clearTimeout(tokenTimer);
    const delay = Math.max(1000,config.expires_at * 1000 - Date.now() - 60000);
    tokenTimer = setTimeout(async () => {
      try {
        const latest = await api("/api/sync");
        if (!isCurrent() || socket !== currentSocket || currentSocket.readyState !== WebSocket.OPEN) return;
        if (latest.mode !== "realtime" || latest.family_id !== config.family_id) {
          throw Error("La familia de la sesión cambió. Vuelve a iniciar sesión.");
        }
        currentSocket.send(JSON.stringify({
          topic:"realtime:families", event:"access_token",
          payload:{access_token:latest.access_token}, ref:String(++nextRef), join_ref:joinRef
        }));
        scheduleTokenRefresh(latest,currentSocket);
      } catch (error) {
        scheduleReconnect(error,currentSocket);
      }
    },delay);
  };
  const connect = async initialConfig => {
    if (!isCurrent()) return;
    try {
      const config = initialConfig || await api("/api/sync");
      if (!isCurrent()) return;
      if (config.mode !== "realtime" || !config.url || !config.family_id || !config.access_token) {
        throw Error("La configuración de sincronización de Supabase no es válida.");
      }
      const currentSocket = new WebSocket(config.url);
      socket = currentSocket;
      currentSocket.onopen = () => {
        if (socket !== currentSocket) return;
        joinRef = String(++nextRef);
        currentSocket.send(JSON.stringify({
          topic:"realtime:families", event:"phx_join", ref:joinRef, join_ref:joinRef,
          payload:{
            config:{
              broadcast:{ack:false,self:false}, presence:{key:""},
              postgres_changes:[{
                event:"UPDATE",schema:"public",table:"families",
                filter:"id=eq."+config.family_id
              }]
            },
            access_token:config.access_token
          }
        }));
        heartbeatTimer = setInterval(() => {
          if (currentSocket.readyState === WebSocket.OPEN) {
            currentSocket.send(JSON.stringify({
              topic:"phoenix",event:"heartbeat",payload:{},ref:String(++nextRef)
            }));
          }
        },25000);
        joinTimer = setTimeout(() => scheduleReconnect(
          Error("Supabase Realtime no confirmó la suscripción."),currentSocket),10000);
      };
      currentSocket.onmessage = event => {
        if (socket !== currentSocket) return;
        let message;
        try { message = JSON.parse(event.data); }
        catch { scheduleReconnect(Error("Respuesta inválida de Supabase Realtime."),currentSocket); return; }
        if (message.event === "phx_reply" && String(message.ref) === joinRef) {
          if (message.payload?.status !== "ok") {
            scheduleReconnect(Error("Supabase rechazó la suscripción de la familia."),currentSocket);
            return;
          }
          clearTimeout(joinTimer);
          retries = 0; reportedFailure = false;
          scheduleTokenRefresh(config,currentSocket);
          scheduleStateRefresh();
        } else if (message.event === "postgres_changes") {
          scheduleStateRefresh();
        } else if (message.event === "phx_error" || message.event === "phx_close") {
          scheduleReconnect(Error("Supabase cerró la suscripción de la familia."),currentSocket);
        }
      };
      currentSocket.onerror = () => scheduleReconnect(
        Error("No se pudo conectar con Supabase Realtime."),currentSocket);
      currentSocket.onclose = () => scheduleReconnect(
        Error("La conexión con Supabase Realtime se cerró."),currentSocket);
    } catch (error) {
      scheduleReconnect(error);
    }
  };
  return {
    connect,
    close() {
      stopped = true;
      clearConnectionTimers();
      clearTimeout(retryTimer);
      const current = socket;
      socket = null;
      if (current && current.readyState < WebSocket.CLOSING) current.close();
    }
  };
}
document.addEventListener("visibilitychange",() => {
  if (!document.hidden && state) refresh().catch(reportSyncError);
});
$("signin").onsubmit = async e => {
  e.preventDefault();
  const btn = e.target.querySelector("button");
  btn.disabled = true;
  try {
    await api("/api/login",Object.fromEntries(new FormData(e.target)));
    e.target.reset(); await refresh();
  } catch(err) { notice(err.message,true); }
  finally { btn.disabled = false; }
};
$("logout").onclick = async () => {
  try { await api("/api/logout",{}); showLogin(); }
  catch(err) { notice(err.message,true); }
};
$("selector").onchange = async e => {
  chosen = e.target.value;
  // Quitar de inmediato los datos del perfil anterior durante el cambio.
  $("child-panel").hidden = true;
  try { await refresh(); } catch(err) { notice(err.message,true); }
};
document.querySelectorAll("[data-tab]").forEach(b => b.onclick = () => {
  tab = b.dataset.tab;
  render();
});
function setTabs() {
  if (!admin() && tab === "people") tab = "profile";
  for (const id of ["profile","hardware","people"]) $(id).hidden = tab !== id;
  document.querySelectorAll("[data-tab]").forEach(b => {
    b.classList.toggle("active",b.dataset.tab === tab);
    if (b.dataset.tab === tab) b.setAttribute("aria-current","page"); else b.removeAttribute("aria-current");
  });
}
function options(items, selected) {
  return items.map(item => '<option value="'+esc(item.id)+'"'+(item.id === selected ? " selected" : "")+'>'+esc(item.name)+'</option>').join("");
}
function button(label, action, id) {
  return '<button class="secondary" data-do="'+action+'" data-id="'+esc(id)+'">'+esc(label)+'</button>';
}
function deviceRow(d, inventory) {
  const child = state.children.find(c => c.id === d.child_id);
  const controls = [];
  if (child && state.me.can_operate) controls.push(button(d.enabled ? "Desactivar" : "Activar","operate",d.id));
  if (inventory && admin()) controls.push(button(child ? "Reasignar" : "Asignar","assign",d.id));
  return '<div class="row"><div><h3>'+esc(d.name)+'</h3><small>Device_ID · '+esc(d.id)+'</small>'+
    (inventory ? '<small>'+esc(child?.name || "Sin asignar")+'</small>' : "")+
    '</div><div class="row-actions">'+(child ? '<span class="status">'+(d.enabled ? "Activo" : "Inactivo")+'</span>' : "")+controls.join("")+'</div></div>';
}
function empty(msg) { return '<p class="empty">'+esc(msg)+'</p>'; }
function render() {
  setTabs();
  $("identity").textContent = state.me.email + " · " + roleName(state.me.role);
  for (const id of ["add-child","rename","people-tab","add-device","report"]) $(id).hidden = !admin();
  const child = state.children.find(c => c.id === state.selected);
  $("selector-box").hidden = !child;
  $("selector").innerHTML = options(state.children,chosen);
  $("child-title").textContent = child?.name || "Ficha del bebé";
  $("child-ref").textContent = "Child_ID · " + (chosen || "");
  $("empty-profile").hidden = !!child; $("child-panel").hidden = !child;
  $("empty-help").textContent = admin() ? "Crea el primer perfil para asignarle dispositivos y registrar lecturas." : "El administrador puede crear el primer perfil de bebé.";
  $("report").href = "/api/report?child=" + encodeURIComponent(chosen || "");
  $("inventory").innerHTML = state.devices.map(d=>deviceRow(d,true)).join("") || empty("Todavía no hay dispositivos registrados.");
  const assigned = state.devices.filter(d=>d.child_id === chosen && chosen);
  $("child-devices").innerHTML = assigned.map(d=>deviceRow(d,false)).join("") || empty("Este bebé aún no tiene dispositivos asignados.");
  $("members").innerHTML = state.members.map(m=>'<div class="row"><div><h3>'+esc(m.email)+'</h3><small>'+roleName(m.role)+'</small></div><div class="row-actions"><span class="status">'+
    (m.can_operate ? "Puede operar" : "Solo lectura")+'</span>'+(m.role === "caregiver" ? button("Editar permiso","permission",m.id) : "")+'</div></div>').join("");
  const groups = [...new Set(state.events.map(e=>JSON.stringify([e.metric,e.unit])))];
  const old = $("metric").value;
  $("metric").innerHTML = options(groups.map(id=>({id,name:JSON.parse(id).join(" · ")})),old);
  $("metric").hidden = !groups.length;
  const rows = state.events;
  $("history").innerHTML = rows.length ? '<div class="scroll"><table><thead><tr><th>Fecha de captura</th><th>Device_ID</th><th>Medida</th><th>Lectura</th></tr></thead><tbody>'+
    rows.map(e=>'<tr><td>'+esc(date(e.captured_at))+'</td><td>'+esc(e.device_id)+'</td><td>'+esc(e.metric)+'</td><td>'+fmt(e.value)+' '+esc(e.unit)+'</td></tr>').join("")+'</tbody></table></div>' : empty("Las lecturas aparecerán aquí cuando el dispositivo envíe datos.");
  draw();
}
function draw() {
  if (!state || tab !== "profile") return;
  const events = state.events.filter(e=>JSON.stringify([e.metric,e.unit]) === $("metric").value).reverse();
  $("graph").hidden = !events.length;
  const last = events.at(-1);
  $("latest").innerHTML = last ? '<span class="metric-value">'+fmt(last.value)+'</span> '+esc(last.unit) : empty("Sin lecturas para este perfil.");
  $("graph-info").textContent = !last ? "" : events.length === 1
    ? "1 lectura · "+date(last.captured_at)
    : events.length+" lecturas · "+date(events[0].captured_at)+" – "+date(last.captured_at);
  if (!last) return;
  const canvas = $("graph"), w = canvas.clientWidth, h = 130, ratio = window.devicePixelRatio || 1;
  canvas.width=w*ratio;canvas.height=h*ratio;
  const ctx=canvas.getContext("2d");ctx.scale(ratio,ratio);
  const values=events.map(e=>e.value), low=Math.min(...values), high=Math.max(...values);
  const times=events.map(e=>Date.parse(e.captured_at)), span=times.at(-1)-times[0];
  ctx.strokeStyle="#dce6de";ctx.lineWidth=1;
  for (const y of [12,65,118]) {ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke();}
  const points=events.map((e,i)=>[6+(span ? (times[i]-times[0])/span : .5)*(w-12),high===low ? 65 : 118-(e.value-low)/(high-low)*106]);
  ctx.strokeStyle="#287660";ctx.lineWidth=2.5;ctx.beginPath();
  points.forEach(([x,y],i)=>i ? ctx.lineTo(x,y) : ctx.moveTo(x,y));ctx.stroke();
  ctx.fillStyle="#287660";
  for (const [x,y] of points) {ctx.beginPath();ctx.arc(x,y,3,0,Math.PI*2);ctx.fill();}
  canvas.setAttribute("aria-label",last.metric+"; mínimo "+fmt(low)+", máximo "+fmt(high)+" "+last.unit);
}
$("metric").onchange = draw;
window.addEventListener("resize",draw);
function modal(title, html, save, label="Guardar") {
  $("modal-title").textContent=title; $("fields").innerHTML=html; $("form-error").hidden=true;
  $("confirm").textContent=label; onSave=save;
  if (!$("modal").open) $("modal").showModal();
}
$("close").onclick=()=>$("modal").close();
$("modal").addEventListener("close",()=>{ $("fields").replaceChildren(); onSave=null; });
$("editor").onsubmit=async e=>{
  e.preventDefault(); const btn=$("confirm"); btn.disabled=true;
  try {
    const values=Object.fromEntries(new FormData(e.target));
    const keep=await onSave(values);
    if (!keep && $("modal").open) $("modal").close();
    await refresh();
  } catch(err) {
    $("form-error").textContent=err.message; $("form-error").hidden=false;
    if (!$("modal").open) notice(err.message,true);
  } finally {btn.disabled=false;}
};
const input=(name,label,value="",type="text",attrs="")=>'<label>'+esc(label)+'<input name="'+name+'" type="'+type+'" value="'+esc(value)+'" required '+attrs+'></label>';
const change=(action,data)=>api("/api/change/"+action,data);
$("add-child").onclick=()=>modal("Nuevo perfil",input("name","Nombre del bebé","","text",'maxlength="80"'),async data=>{
  chosen=(await change("child",data)).id; notice("Perfil creado.");
});
$("rename").onclick=()=>{
  const child=state.children.find(c=>c.id===chosen);
  modal("Editar perfil",input("name","Nombre del bebé",child.name,"text",'maxlength="80"'),async data=>{
    await change("rename",{...data,child_id:child.id}); notice("Nombre actualizado.");
  });
};
$("add-device").onclick=()=>modal("Registrar dispositivo",input("device_id","Device_ID","","text",'maxlength="64"')+input("name","Nombre del dispositivo","","text",'maxlength="80"'),async data=>{
  const result=await change("device",data);
  modal("Clave del dispositivo",'<p class="hint">Copia esta clave ahora para configurar el simulador o la Raspberry Pi. Solo se muestra una vez.</p><label>Device_ID<input readonly value="'+esc(data.device_id)+'"></label><label>Clave privada<textarea class="secret" readonly>'+esc(result.secret)+'</textarea></label>',async()=>{}, "Listo");
  return true;
});
$("add-member").onclick=()=>{
  modal("Crear cuenta familiar",input("email","Correo electrónico","","email",'autocomplete="off"')+
    input("password","Contraseña inicial","","password",'minlength="10" autocomplete="new-password"')+
    '<label>Rol<select id="new-role" name="role"><option value="caregiver">Cuidador</option><option value="admin">Padre / administrador</option></select></label>'+
    '<label id="operate-option" class="check"><input type="checkbox" name="operate"> Permitir operar dispositivos</label>',async d=>{
      await change("member",{email:d.email,password:d.password,role:d.role,can_operate:d.operate==="on"});
      notice("Cuenta vinculada a la familia.");
    },"Crear cuenta");
  $("new-role").onchange=e=>$("operate-option").hidden=e.target.value==="admin";
};
document.addEventListener("click",async e=>{
  const b=e.target.closest("[data-do]"); if (!b) return;
  const {do:action,id}=b.dataset;
  const d=state.devices.find(row=>row.id===id);
  if (action==="assign") {
    const origin=state.children.find(c=>c.id===d.child_id);
    const choices=state.children.filter(c=>c.id!==d.child_id);
    if (origin) choices.push({id:"",name:"Dejar sin asignar"});
    if (!choices.length) {notice("Primero crea un perfil de bebé.");return;}
    modal("Asignación de "+d.id,'<label>Destino<select name="child_id">'+options(choices,null)+'</select></label>'+
      (origin ? '<p class="hint">Los datos previos quedan archivados en la ficha de '+esc(origin.name)+'. Las lecturas capturadas a partir del traslado se guardarán en el perfil de destino.</p>' : ""),async v=>{
        await change("assign",{device_id:id,expected:d.assignment_id,child_id:v.child_id||null}); notice("Asignación actualizada.");
      },"Confirmar asignación");
  } else if(action==="operate") {
    try {b.disabled=true;await change("operate",{device_id:id,expected:d.assignment_id,enabled:!d.enabled});await refresh();}
    catch(err){notice(err.message,true);await refresh().catch(()=>{});}finally{b.disabled=false;}
  } else if(action==="permission") {
    const m=state.members.find(row=>row.id===id);
    modal("Permiso del cuidador",'<p>'+esc(m.email)+'</p><label class="check"><input type="checkbox" name="operate" '+(m.can_operate?"checked":"")+'> Permitir operar dispositivos</label>',async v=>{
      await change("permission",{member_id:id,enabled:v.operate==="on"});notice("Permiso actualizado.");
    });
  }
});
refresh().catch(e=>{showLogin();if(e.status!==401)notice(e.message,true);});

