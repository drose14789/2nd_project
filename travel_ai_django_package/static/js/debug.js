async function dbgFetchJSON(url){
  const res = await fetch(url);
  if (!res.ok) return null;
  return await res.json();
}
function dbgBadge(ok){
  return ok ? '<span class="badge text-bg-success">OK</span>' : '<span class="badge text-bg-danger">MISSING</span>';
}
function dbgFmtTs(ms){
  try{ return new Date(ms).toLocaleString(); }catch(e){ return String(ms); }
}
function dbgRenderStatus(status){
  const envEl = document.getElementById("dbgEnv");
  const rtEl = document.getElementById("dbgRuntime");
  if (!status) return;
  const env = status.env || {};
  const rows = [
    ["Geoapify_Places_API", env.Geoapify_Places_API],
    ["openrouteservice_API", env.openrouteservice_API],
    ["Amadeus_Hotel_Search_API", env.Amadeus_Hotel_Search_API],
    ["Amadeus_Hotel_Search_API_SECRET", env.Amadeus_Hotel_Search_API_SECRET],
    ["Google_Places_API_KEY", env.Google_Places_API_KEY],
    ["Unsplash_Access_Key", env.Unsplash_Access_Key],
  ];
  envEl.innerHTML = rows.map(([k,v]) => {
    return `<div class="d-flex justify-content-between"><span>${k}</span>${dbgBadge(!!v)}</div>`;
  }).join("");
  const rt = status.runtime || {};
  rtEl.innerHTML = [
    `<div>debug: <b>${rt.debug ? "true" : "false"}</b></div>`,
    `<div>plan_id: <b>${rt.plan_id ? rt.plan_id : "(없음)"}</b></div>`
  ].join("");
}
function dbgRenderLogs(data){
  const pre = document.getElementById("dbgLogs");
  if (!pre) return;
  if (!data || !data.events) { pre.textContent = "(로그 없음)"; return; }
  const lines = data.events.slice().reverse().map(evt => {
    if (evt.kind === "request"){
      return `[${dbgFmtTs(evt.ts)}] ${evt.method} ${evt.path} -> ${evt.status} (${evt.dur_ms}ms)`;
    }
    if (evt.kind === "error"){
      return `[${dbgFmtTs(evt.ts)}] ERROR at ${evt.where}: ${evt.error_type}: ${evt.error}`;
    }
    return `[${dbgFmtTs(evt.ts)}] ${evt.kind}: ${JSON.stringify(evt)}`;
  });
  pre.textContent = lines.join("\n");
}
async function dbgRefresh(){
  const status = await dbgFetchJSON("/api/debug/status");
  dbgRenderStatus(status);
  const logs = await dbgFetchJSON("/api/debug/logs?limit=80");
  dbgRenderLogs(logs);
}
function dbgInit(){
  const panel = document.getElementById("debugPanel");
  if (!panel) return;
  const toggleBtn = document.getElementById("dbgToggleBtn");
  const refreshBtn = document.getElementById("dbgRefreshBtn");
  const body = document.getElementById("dbgBody");
  if (toggleBtn && body){
    toggleBtn.addEventListener("click", () => {
      const hidden = body.classList.toggle("d-none");
      toggleBtn.textContent = hidden ? "펼치기" : "접기";
    });
  }
  if (refreshBtn) refreshBtn.addEventListener("click", dbgRefresh);
  dbgRefresh();
  setInterval(dbgRefresh, 5000);
}
document.addEventListener("DOMContentLoaded", dbgInit);
