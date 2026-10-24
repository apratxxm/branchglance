(() => {
  const $=selector=>document.querySelector(selector),DAY=86400000;
  const state={open:false,tab:"insights",source:"Codex",snapshot:null,limitHistory:null,policy:null,identity:null,monitors:[],settingsLoaded:false,settingsBusy:false,settingsToken:null,frame:null,renderKey:"",settingsStatus:"",settingsError:false};
  const esc=value=>String(value??"").replace(/[&<>"']/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const finite=value=>typeof value==="number"&&Number.isFinite(value)?value:null;
  const nfmt=new Intl.NumberFormat("en",{notation:"compact",maximumSignificantDigits:3});
  const number=value=>finite(value)==null?"—":nfmt.format(value);
  const money=value=>finite(value)==null?"—":new Intl.NumberFormat("en-US",{style:"currency",currency:"USD",maximumFractionDigits:2}).format(value);
  const localDate=date=>`${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,"0")}-${String(date.getDate()).padStart(2,"0")}`;
  const dayLabel=value=>{const date=new Date(`${value}T12:00:00`);return Number.isNaN(date.getTime())?String(value||""):new Intl.DateTimeFormat("en-US",{month:"short",day:"numeric"}).format(date);};
  const availableProviders=()=>Array.isArray(state.snapshot?.providers)?state.snapshot.providers:[];
  const provider=()=>availableProviders().find(item=>item?.name===state.source)||null;
  function dailySeries(value){
    const source=Array.isArray(value?.activity)?value.activity:[],byDate=new Map();
    for(const row of source){if(!row||typeof row.date!=="string"||!/^(\d{4}-\d\d-\d\d)$/.test(row.date))continue;byDate.set(row.date,{value:Math.max(0,finite(row.value)||0),models:row.models||row.modelUsage||null});}
    const today=new Date();today.setHours(12,0,0,0);
    return Array.from({length:31},(_,index)=>{const date=new Date(today.getTime()-(30-index)*DAY),key=localDate(date);return {date:key,value:byDate.get(key)?.value||0,models:byDate.get(key)?.models||null};});
  }
  function streaks(activity){
    const active=new Set(activity.filter(row=>row.value>0).map(row=>row.date));let run=0,cursor=new Date();
    for(;;){const key=localDate(cursor);if(!active.has(key))break;run++;cursor=new Date(cursor.getTime()-DAY);}
    const dates=[...active].sort();let longest=0,best=0,previous=null;
    for(const value of dates){const current=new Date(`${value}T12:00:00`);best=previous&&current-previous===DAY?best+1:1;longest=Math.max(longest,best);previous=current;}
    return {current:run,longest};
  }
  function sparkline(rows){
    const width=320,height=54,pad=4,max=Math.max(1,...rows.map(row=>row.value)),step=(width-pad*2)/Math.max(1,rows.length-1);
    const points=rows.map((row,index)=>`${(pad+index*step).toFixed(1)},${(height-pad-row.value/max*(height-pad*2)).toFixed(1)}`);
    const first=points[0]||`${pad},${height-pad}`,last=points.at(-1)||`${width-pad},${height-pad}`;
    return `<svg class="dash-sparkline" viewBox="0 0 ${width} ${height}" role="img" aria-label="31-day usage sparkline"><defs><linearGradient id="dash-spark-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="var(--source-accent)" stop-opacity=".34"/><stop offset="1" stop-color="var(--source-accent)" stop-opacity="0"/></linearGradient></defs><path class="dash-spark-area" d="M${first} L${points.slice(1).join(" L")} L${width-pad} ${height-pad} L${pad} ${height-pad} Z"/><polyline class="dash-spark-line" points="${points.join(" ")}"/><circle class="dash-spark-end" cx="${last.split(",")[0]}" cy="${last.split(",")[1]}" r="3"/></svg>`;
  }
  function modelEntries(models){
    if(!models||typeof models!=="object")return [];
    const entries=Array.isArray(models)?models.map(item=>[item?.model,item?.tokens]):Object.entries(models);
    return entries.filter(([name,value])=>typeof name==="string"&&finite(value)!=null&&value>0).sort((a,b)=>b[1]-a[1]);
  }
  function dailyChart(rows){
    const width=640,height=174,left=44,right=8,top=12,bottom=34,plotW=width-left-right,plotH=height-top-bottom,max=Math.max(1,...rows.map(row=>row.value)),step=plotW/rows.length,barW=Math.max(5,step*.58),ticks=[0,.5,1];
    const grid=ticks.map(fraction=>{const y=top+plotH*(1-fraction),label=fraction===0?"0":number(max*fraction);return `<line class="grid-line" x1="${left}" x2="${width-right}" y1="${y}" y2="${y}"/><text class="dash-y-label" x="${left-8}" y="${y+3}" text-anchor="end">${esc(label)}</text>`;}).join("");
    const bars=rows.map((row,index)=>{const center=left+index*step+step/2,x=center-barW/2,total=row.value,barH=total?Math.max(1,total/max*plotH):0,y=top+plotH-barH,entries=modelEntries(row.models),split=entries.map(([name,amount])=>`${name}: ${number(amount)}`).join("\n"),tip=`${dayLabel(row.date)} | ${number(total)} tokens${split?`\n${split}`:""}`;
      let stack="";
      if(total>0&&entries.length){let offset=0,modelTotal=entries.reduce((sum,entry)=>sum+entry[1],0)||1;stack=entries.map(([name,amount],part)=>{const partHeight=barH*amount/modelTotal,rectY=top+plotH-offset-partHeight;offset+=partHeight;return `<rect class="usage-bar usage-stack mix-${part%5}" tabindex="0" x="${x.toFixed(2)}" y="${rectY.toFixed(2)}" width="${barW.toFixed(2)}" height="${Math.max(1,partHeight).toFixed(2)}" rx="4"><title>${esc(tip)}</title></rect>`;}).join("");}
      const bar=total>0&&!entries.length?`<rect class="usage-bar" tabindex="0" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barW.toFixed(2)}" height="${barH.toFixed(2)}" rx="4"><title>${esc(tip)}</title></rect>`:total===0?`<rect class="usage-bar is-zero" tabindex="0" x="${x.toFixed(2)}" y="${top+plotH-2}" width="${barW.toFixed(2)}" height="2" rx="2"><title>${esc(tip)}</title></rect>`:"";
      return `<g class="dash-bar-group" data-tip="${esc(tip)}"><line class="dash-crosshair" x1="${center.toFixed(2)}" x2="${center.toFixed(2)}" y1="${top}" y2="${top+plotH}"/>${stack||bar}</g>`;
    }).join("");
    const labels=rows.map((row,index)=>index%7===0?`<text class="axis-label" x="${left+index*step+step/2}" y="${height-8}" text-anchor="middle">${esc(dayLabel(row.date))}</text>`:"").join("");
    return `<svg class="dash-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Daily tokens over the last 31 days"><title>Daily token usage over the last 31 days</title><defs><linearGradient id="dash-bars-gradient" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="var(--source-accent)"/><stop offset="1" stop-color="var(--source-glow-primary)"/></linearGradient></defs>${grid}${bars}${labels}</svg>`;
  }
  function heatmap(rows){
    const max=Math.max(1,...rows.map(row=>row.value)),today=localDate(new Date());
    return `<div class="dash-heat-legend"><span>Less</span><i data-level="0"></i><i data-level="1"></i><i data-level="2"></i><i data-level="3"></i><i data-level="4"></i><span>More</span></div><div class="dash-heatmap" role="grid" aria-label="31-day activity heatmap">${rows.slice(-35).map(row=>{const fraction=row.value/max,level=row.value===0?0:fraction<.2?1:fraction<.4?2:fraction<.7?3:4,title=`${dayLabel(row.date)} | ${number(row.value)} tokens`;return `<span class="dash-heat-cell${row.date===today?" is-today":""}" role="gridcell" data-level="${level}" title="${esc(title)}" aria-label="${esc(title)}"></span>`;}).join("")}</div><div class="dash-heat-days" aria-hidden="true"><span>Last 31 days</span><span>Today outlined</span></div>`;
  }
  function modelMix(value){
    const models=Array.isArray(value?.modelUsage)?value.modelUsage.filter(row=>row&&typeof row.model==="string"&&finite(row.tokens)!=null&&row.tokens>0).slice(0,6):[];
    if(!models.length)return `<p class="dash-inline-empty">Model-level usage is not available for this source.</p>`;
    const total=models.reduce((sum,row)=>sum+row.tokens,0)||1;
    return `<div class="dash-model-stack" role="img" aria-label="Model mix over the last 31 days">${models.map((row,index)=>`<i class="mix-${index%5}" style="width:${(row.tokens*100/total).toFixed(2)}%" title="${esc(row.model)}: ${Math.round(row.tokens*100/total)}%"></i>`).join("")}</div><div class="dash-model-legend">${models.map((row,index)=>`<div class="dash-model-key"><i class="mix-${index%5}"></i><span class="dash-model-name" title="${esc(row.model)}">${esc(row.model)}</span><b>${Math.round(row.tokens*100/total)}%</b></div>`).join("")}</div>`;
  }
  function limitHistoryHTML(){
    const events=Array.isArray(state.limitHistory?.events)?state.limitHistory.events.filter(row=>!row.provider||row.provider===state.source).slice(-4).reverse():[];
    if(!events.length)return `<div class="dash-empty">No reset events recorded for ${esc(state.source)}.</div>`;
    return `<div class="dash-limits">${events.map(row=>{const type=String(row.event||row.type||"Reset event").replaceAll("_"," "),date=row.at||row.observedAt||row.timestamp||row.createdAt;return `<div class="dash-limit"><span class="dash-limit-label">${esc(type)}</span><span class="dash-limit-value">${esc(date?new Date(date).toLocaleString():"Recorded")}</span><span class="dash-limit-note">${esc(row.detail||row.reason||row.window||"")}</span></div>`;}).join("")}</div>`;
  }
  function insightsHTML(){
    const current=provider(),rows=dailySeries(current),streak=streaks(rows),cache=current?.cache||{},tokens=finite(current?.tracked31Days),lifetime=finite(current?.lifetimeTokens),cost=finite(cache.apiEquivalentCost),savings=finite(cache.cacheSavings),cacheRate=finite(cache.percent);
    const supporting=`<div class="dash-hero-support"><article class="dash-card dash-support-card"><span>Lifetime tokens</span><strong>${lifetime==null?"?":number(lifetime)}</strong></article><article class="dash-card dash-support-card"><span>Current streak</span><strong>${number(current?.currentStreakDays??streak.current)}<small> days</small></strong></article><article class="dash-card dash-support-card"><span>Longest streak</span><strong>${number(current?.longestStreakDays??streak.longest)}<small> days</small></strong></article></div>`;
    const sourceNote=current?.activityNote||"Daily usage from recorded provider activity.";
    return `<div class="dash-hero"><article class="dash-card dash-hero-main"><div class="dash-hero-heading"><span class="dash-kpi-label">TOKENS OVER 31 DAYS</span><span class="dash-hero-source">${esc(state.source)} <i></i></span></div><div class="dash-hero-number">${tokens==null?"?":number(tokens)}<small> tokens</small></div>${sparkline(rows)}</article>${supporting}</div><div class="dash-secondary-stats"><article class="dash-card dash-secondary-stat"><span>API-equivalent cost</span><strong>${money(cost)}</strong><small>Estimated model pricing</small></article><article class="dash-card dash-secondary-stat"><span>Cache savings</span><strong>${money(savings)}</strong><small>Estimated cache-read savings</small></article><article class="dash-card dash-secondary-stat"><span>Cache hit rate</span><strong>${cacheRate==null?"?":`${Math.round(cacheRate)}%`}</strong><small>Input served from cache</small></article></div><div class="dash-layout"><article class="dash-card dash-section dash-daily"><div class="dash-section-head"><h2 class="dash-section-title">Daily usage</h2><span class="dash-section-meta">31 days</span></div>${dailyChart(rows)}<p class="dash-section-meta dash-source-note">${esc(sourceNote)}</p></article><article class="dash-card dash-section dash-activity"><div class="dash-section-head"><h2 class="dash-section-title">Activity</h2><span class="dash-section-meta">${esc(state.source)}</span></div>${heatmap(rows)}<div class="dash-section-head dash-model-head"><h2 class="dash-section-title">Model mix</h2><span class="dash-section-meta">31-day share</span></div>${modelMix(current)}<div class="dash-section-head dash-limit-head"><h2 class="dash-section-title">Limit history</h2><span class="dash-section-meta">Recent events</span></div>${limitHistoryHTML()}</article></div>`;
  }
  function policyHTML(){
    const policy=state.policy,rows=Array.isArray(policy?.providers)?policy.providers:[];
    const list=rows.length?rows.map(row=>`<div class="dash-provider"><span class="dash-provider-name">${esc(row.name)}</span>${["show","collect","send"].map(key=>`<span class="dash-flag${row[key]===true?" is-on":""}"><i></i><b>${key}</b><small>${row[key]===true?"On":row[key]===false?"Off":"—"}</small></span>`).join("")}</div>`).join(""):`<div class="dash-empty">Provider policy is unavailable.</div>`;
    const path=typeof policy?.policyFile==="string"&&policy.policyFile!=="absent"?`<span class="dash-policy-path">${esc(policy.policyFile)}</span>`:"";
    const warning=Array.isArray(policy?.warnings)?policy.warnings.filter(value=>typeof value==="string"&&value.trim()).map(value=>esc(value)).join(" · "):"";
    return `<article class="dash-card dash-section"><div class="dash-section-head"><h2 class="dash-section-title">Provider permissions</h2><span class="dash-readonly">Read only</span></div><div class="dash-provider-list">${list}</div><p class="dash-policy-note">Show, collect and send flags are set in the provider policy file.${path}${warning?`<span class="dash-policy-path">${warning}</span>`:""}</p></article>`;
  }
  function monitorHTML(){
    if(!state.monitors.length)return `<div class="dash-empty">Monitor information is unavailable.</div>`;
    return `<div class="dash-monitor-list">${state.monitors.map(monitor=>`<div class="dash-monitor"><div class="dash-monitor-main"><span class="dash-monitor-label">${esc(monitor.label||"Monitor")}${monitor.selected?" · current":""}</span><span class="dash-monitor-detail">${esc(monitor.kind||monitor.index?`${monitor.kind||`Display ${monitor.index}`} · ${monitor.workArea?.width||"?"} × ${monitor.workArea?.height||"?"}`:"Detected display")}</span></div><div class="dash-monitor-edit"><input class="dash-monitor-input" maxlength="32" aria-label="Name ${esc(monitor.label||"monitor")}" value="${esc(monitor.label||"")}" ${monitor.stableId?"":"disabled"}><button class="dash-mini-button" data-dashboard-action="save-monitor" data-monitor-id="${esc(monitor.id)}" ${monitor.stableId?"":"disabled"}>Save</button></div></div>`).join("")}</div>`;
  }
  function settingsHTML(){
    const startup=typeof state.identity?.startAtLogin==="boolean"?state.identity.startAtLogin?"Enabled":"Disabled":"Status not available from this widget";
    const reset=state.settingsToken?`<iframe class="dash-reset-frame" title="Auto-greet, continue and Claude hook settings" src="/reset_actions.html?embedded=1&amp;source=${encodeURIComponent(state.source)}#t=${encodeURIComponent(state.settingsToken)}"></iframe>`:`<div class="dash-empty">${esc(state.settingsStatus||"Loading settings…")}</div>`;
    return `<div class="dash-settings"><div class="dash-settings-stack">${policyHTML()}<article class="dash-card dash-section"><div class="dash-section-head"><h2 class="dash-section-title">Monitors</h2><span class="dash-section-meta">Names saved by display identity</span></div>${monitorHTML()}<p class="dash-status${state.settingsError?" is-error":""}" role="status">${esc(state.settingsStatus)}</p><div class="dash-actions"><button class="dash-mini-button" data-dashboard-action="refresh-settings" ${state.settingsBusy?"disabled":""}>Refresh</button><button class="dash-mini-button" data-dashboard-action="recenter">Recenter widget</button></div></article><article class="dash-card dash-section"><div class="dash-section-head"><h2 class="dash-section-title">Start at login</h2><span class="dash-readonly">Read only</span></div><p class="dash-settings-note">${esc(startup)}. This widget does not expose a startup-setting control.</p></article></div><div class="dash-settings-stack"><article class="dash-card dash-section"><div class="dash-section-head"><h2 class="dash-section-title">Auto-greet, continue and Claude Code</h2><span class="dash-section-meta">Existing reset settings</span></div>${reset}</article></div></div>`;
  }
  function bindTooltips(){
    const root=$("#dashboard-view"),tip=$("#dash-tooltip");if(!root||!tip)return;
    for(const eventName of ["pointerover","focusin"]){root.addEventListener(eventName,event=>{const target=event.target?.closest?.("[data-tip]");if(!target)return;tip.textContent=target.getAttribute("data-tip");tip.classList.add("is-visible");tip.setAttribute("aria-hidden","false");});}
    for(const eventName of ["pointerout","focusout"]){root.addEventListener(eventName,event=>{if(event.target?.closest?.("[data-tip]")){tip.classList.remove("is-visible");tip.setAttribute("aria-hidden","true");}});}
    root.addEventListener("pointermove",event=>{if(tip.classList.contains("is-visible")){tip.style.left=Math.min(event.clientX+12,window.innerWidth-230)+"px";tip.style.top=Math.min(event.clientY+12,window.innerHeight-58)+"px";}});
  }
  function selectTab(name){
    if(name!=="insights"&&name!=="settings")return;
    state.tab=name;const title=$("#dashboard-title");if(title)title.textContent=name==="insights"?"Insights":"Settings";
    for(const key of ["insights","settings"]){const button=$("#dashboard-tab-"+key),panel=$("#dashboard-"+key);if(button){button.classList.toggle("is-active",key===name);button.setAttribute("aria-selected",String(key===name));}if(panel)panel.hidden=key!==name;}
    render();if(name==="settings"&&!state.settingsLoaded)void loadSettings();
  }
  async function loadSettings(){
    if(state.settingsBusy)return;state.settingsBusy=true;state.settingsError=false;state.settingsStatus="Loading settings…";render();
    const api=window.pywebview?.api;
    try{
      const [policyResponse,identityResponse]=await Promise.all([fetch("/api/provider-policy",{cache:"no-store"}),fetch("/api/identity",{cache:"no-store"})]);
      if(policyResponse.ok)state.policy=await policyResponse.json();else throw new Error("Provider policy is unavailable.");
      if(identityResponse.ok)state.identity=await identityResponse.json();
      if(typeof api?.get_monitors==="function"){const result=await api.get_monitors();state.monitors=Array.isArray(result)?result:[];}else state.monitors=[];
      if(typeof api?.get_dashboard_settings_token==="function")state.settingsToken=await api.get_dashboard_settings_token();
      if(!state.settingsToken)state.settingsStatus="Reset settings are unavailable in this window.";
      else state.settingsStatus="";
      state.settingsLoaded=true;
    }catch(error){state.settingsError=true;state.settingsStatus=error?.message||"Settings could not be loaded.";}
    finally{state.settingsBusy=false;render();}
  }
  async function saveMonitor(button){
    const id=button.dataset.monitorId,row=button.closest(".dash-monitor"),input=row?.querySelector(".dash-monitor-input"),api=window.pywebview?.api;
    if(!id||!input||typeof api?.set_monitor_name!=="function")return;
    button.disabled=true;state.settingsStatus="Saving monitor name…";state.settingsError=false;render();
    try{const result=await api.set_monitor_name(id,input.value);state.monitors=Array.isArray(result)?result:[];state.settingsStatus="Monitor name saved.";}
    catch(error){state.settingsError=true;state.settingsStatus=error?.message||"Monitor name could not be saved.";}
    finally{render();}
  }
  function render(){
    if(!state.open)return;
    const current=provider(),key=[state.source,state.tab,state.snapshot?.generatedAt,state.snapshot?.structureStamp,state.limitHistory?.events?.length].join("|");
    if(key!==state.renderKey){state.renderKey=key;const insights=$("#dashboard-insights");if(insights)insights.innerHTML=insightsHTML();}
    const settings=$("#dashboard-settings");
    if(settings&&state.tab==="settings"){
      const settingsKey=[state.source,state.policy?.generation,state.identity?.stateRoot,state.settingsToken,state.settingsStatus,state.settingsBusy,state.settingsError,state.monitors.map(m=>`${m.id}:${m.label}`).join("|")].join("|");
      if(settingsKey!==state.settingsRenderKey){state.settingsRenderKey=settingsKey;settings.innerHTML=settingsHTML();state.frame=$("#dashboard-settings iframe");}
    }
    const root=$("#dashboard-view");if(root){root.dataset.provider=current?.name||state.source;bindTooltipsOnce();}
  }
  let tooltipBound=false;
  function bindTooltipsOnce(){if(tooltipBound)return;tooltipBound=true;bindTooltips();}
  async function loadLimitHistory(){
    try{const response=await fetch("/api/limit-reset",{cache:"no-store"});if(response.ok){state.limitHistory=await response.json();state.renderKey="";render();}}
    catch{state.limitHistory=null;}
  }
  async function open({source}={}){
    state.open=true;if(typeof source==="string")state.source=source;
    const root=$("#dashboard-view");if(root){root.hidden=false;root.inert=false;root.setAttribute("aria-hidden","false");}
    render();void loadLimitHistory();
  }
  function close(){
    state.open=false;const root=$("#dashboard-view");if(root){root.hidden=true;root.inert=true;root.setAttribute("aria-hidden","true");}
    const frame=$("#dashboard-settings iframe");if(frame)frame.src="about:blank";
    if(state.settingsToken){void window.pywebview?.api?.release_dashboard_settings_token?.(state.settingsToken);state.settingsToken=null;}
    state.frame=null;state.settingsLoaded=false;state.settingsRenderKey="";
  }
  function setSnapshot(snapshot){state.snapshot=snapshot;if(state.open)render();}
  function setSource(source){if(typeof source==="string"&&source!==state.source){state.source=source;state.renderKey="";render();}}
  document.addEventListener("click",event=>{
    const tab=event.target?.closest?.("[data-dashboard-tab]");if(tab){selectTab(tab.dataset.dashboardTab);return;}
    const action=event.target?.closest?.("[data-dashboard-action]");if(!action)return;
    if(action.dataset.dashboardAction==="close")window.tokenTrackerCloseDashboard?.();
    else if(action.dataset.dashboardAction==="save-monitor")void saveMonitor(action);
    else if(action.dataset.dashboardAction==="refresh-settings"){state.settingsLoaded=false;void loadSettings();}
    else if(action.dataset.dashboardAction==="recenter")void window.tokenTrackerRecenter?.();
  });
  window.addEventListener("message",event=>{if(event.origin===location.origin&&event.source===state.frame?.contentWindow&&event.data?.type==="dashboard-reset-cancel")window.tokenTrackerCloseDashboard?.();});
  window.addEventListener("load",()=>{state.frame=$("#dashboard-settings iframe");});
  window.TokenTrackerDashboard={open,close,setSource,setSnapshot,selectTab,render,loadSettings,state};
  if(window.__TOKEN_TRACKER_DASHBOARD_TEST__)window.__tokenTrackerDashboardTest={dailySeries,streaks,dailyChart,heatmap,modelMix,insightsHTML,policyHTML,state,setSnapshot,setSource,selectTab};
})();
