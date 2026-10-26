(() => {
  const $=selector=>document.querySelector(selector),DAY=86400000;
  const RANGE_DAYS={"24h":1,"7d":7,"30d":30,all:Infinity};
  const RANGE_NAME={"24h":"24 hours","7d":"7 days","30d":"30 days",all:"all time"};
  const RANGE_PREVIOUS={"24h":"previous 24 hours","7d":"previous 7 days","30d":"previous 30 days",all:"previous period"};
  const state={open:false,tab:"insights",source:"Codex",range:"30d",metric:"cost",snapshot:null,statsSnapshot:null,insights:null,version:0,loadToken:0,policy:null,monitors:[],lock:null,keepOpen:null,settingsLoaded:false,settingsBusy:false,frame:null,renderKey:"",settingsRenderKey:"",settingsStatus:"",settingsError:false};
  const esc=value=>String(value??"").replace(/[&<>"']/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const finite=value=>typeof value==="number"&&Number.isFinite(value)?value:null;
  const isRange=value=>Object.prototype.hasOwnProperty.call(RANGE_DAYS,value);
  const readPref=(key,fallback)=>{try{return localStorage.getItem(key)??fallback;}catch{return fallback;}};
  const writePref=(key,value)=>{try{localStorage.setItem(key,value);}catch{}};
  const reducedMotion=()=>window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches===true;
  const nfmt=new Intl.NumberFormat("en",{notation:"compact",maximumSignificantDigits:3});
  const usd=new Intl.NumberFormat("en-US",{style:"currency",currency:"USD",maximumFractionDigits:2});
  const number=value=>finite(value)==null?"":nfmt.format(value);
  const money=value=>finite(value)==null?"":usd.format(value);
  const localDate=date=>`${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,"0")}-${String(date.getDate()).padStart(2,"0")}`;
  const dayLabel=value=>{const date=new Date(`${value}T12:00:00`);return Number.isNaN(date.getTime())?String(value||""):new Intl.DateTimeFormat("en-US",{month:"short",day:"numeric"}).format(date);};
  const modelName=raw=>String(window.TokenTrackerFormatModel?.({model:raw})||raw||"");
  const providerKey=name=>/claude/i.test(String(name||""))?"claude":"codex";
  const modelKey=raw=>/^gpt-/i.test(String(raw||""))?"codex":"claude";
  const metricKey=()=>isCost()?"apiEquivalentCost":"tokens";
  const metricFormat=value=>isCost()?money(value):number(value);
  const availableProviders=()=>Array.isArray(state.insights?.providers)?state.insights.providers:[];
  const costAvailable=()=>availableProviders().some(p=>finite(p.totals?.apiEquivalentCost)!=null||(p.series||[]).some(row=>finite(row.apiEquivalentCost)!=null));
  const isCost=()=>state.metric==="cost"&&costAvailable();
  const setInsights=value=>{state.insights=value;state.version++;if(state.insights&&state.metric==="cost"&&!costAvailable())state.metric="tokens";syncSegments();};
  function smoothPath(points){if(points.length<2)return "";let d="M"+points[0][0].toFixed(1)+" "+points[0][1].toFixed(1);for(let i=0;i<points.length-1;i++){const a=points[Math.max(0,i-1)],b=points[i],c=points[i+1],next=points[Math.min(points.length-1,i+2)];d+=" C"+(b[0]+(c[0]-a[0])/6).toFixed(1)+" "+(b[1]+(c[1]-a[1])/6).toFixed(1)+" "+(c[0]-(next[0]-b[0])/6).toFixed(1)+" "+(c[1]-(next[1]-b[1])/6).toFixed(1)+" "+c[0].toFixed(1)+" "+c[1].toFixed(1);}return d;}
  function seriesChart(data){
    const rows=data.series,providers=data.providers.filter(p=>rows.some(row=>(finite(row[p.name])||0)>0));
    if(!rows.length||!providers.length)return "";
    const W=668,H=113,L=42,R=4,T=9,B=20,PW=W-L-R,PH=H-T-B,step=PW/Math.max(rows.length-1,1);
    const totals=rows.map(row=>providers.reduce((sum,p)=>sum+(finite(row[p.name])||0),0));
    const max=Math.max(...totals)*1.1||1;
    const xAt=index=>L+index*step,yAt=value=>T+PH-value/max*PH;
    let cumulative=rows.map(()=>0),paths="";
    for(const p of providers){
      const cls=providerKey(p.name),lower=cumulative,upper=rows.map((row,index)=>lower[index]+(finite(row[p.name])||0));
      cumulative=upper;
      const top=upper.map((value,index)=>[xAt(index),yAt(value)]),bottom=lower.map((value,index)=>[xAt(index),yAt(value)]);
      const edge=smoothPath(top),back=smoothPath([...bottom].reverse()).replace(/^M[^C]*/,"");
      paths+=`<path class="area-fill ${cls}" d="${edge} L${bottom.at(-1)[0].toFixed(1)} ${bottom.at(-1)[1].toFixed(1)}${back} Z"/><path class="area-glow ${cls}" d="${edge}"/><path class="area-edge ${cls}" d="${edge}"/>`;
    }
    const ticks=[1,.5,0].map(fraction=>{const y=yAt(max*fraction),label=fraction===0?(isCost()?"$0":"0"):metricFormat(max*fraction);return `<line class="dash-grid" x1="${L}" x2="${W-R}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}"/><text class="dash-axis-value" x="${L-8}" y="${(y+3).toFixed(1)}" text-anchor="end">${esc(label)}</text>`;}).join("");
    const labelIndexes=[...new Set([0,Math.floor((rows.length-1)/3),Math.floor(2*(rows.length-1)/3),rows.length-1])];
    const dates=labelIndexes.map(index=>{const anchor=index===0?"start":index===rows.length-1?"end":"middle";return `<text class="dash-date" x="${xAt(index).toFixed(1)}" y="${H-3}" text-anchor="${anchor}">${esc(dayLabel(rows[index].date))}</text>`;}).join("");
    const hover=rows.map((row,index)=>{const x=xAt(index),total=totals[index],tip=`<span class="tip-date">${esc(dayLabel(row.date))}</span><b class="tip-total">${esc(metricFormat(total))}</b>${providers.map(p=>`<span class="tip-row"><span><i class="dash-dot ${providerKey(p.name)}"></i>${esc(p.name)}</span><strong>${esc(metricFormat(finite(row[p.name])||0))}</strong></span>`).join("")}`;
      return `<g class="dash-point" tabindex="0" data-tip-html="${esc(tip)}"><line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${T}" y2="${T+PH}"/><circle cx="${x.toFixed(1)}" cy="${yAt(total).toFixed(1)}" r="2.5"/></g>`;}).join("");
    const defs='<defs><linearGradient id="dash-codex-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#6675ff" stop-opacity=".25"/><stop offset="1" stop-color="#6675ff" stop-opacity="0"/></linearGradient><linearGradient id="dash-claude-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#e98953" stop-opacity=".26"/><stop offset="1" stop-color="#e98953" stop-opacity="0"/></linearGradient><filter id="dash-soft-glow" x="-30%" y="-100%" width="160%" height="300%"><feGaussianBlur stdDeviation="2.8"/></filter></defs>';
    return `<svg class="dash-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Daily ${isCost()?"API-equivalent cost":"tokens"} by provider">${defs}${ticks}${paths}${hover}${dates}</svg>`;
  }
  function insightsData(){
    const fallback=state.insights?.fallback===true,key=metricKey(),range=state.range,allProviders=availableProviders();
    let dates=[...new Set(allProviders.flatMap(p=>(p.series||[]).map(row=>row.date).filter(Boolean)))].sort(),end=dates.at(-1);
    const days=RANGE_DAYS[range];
    if(Number.isFinite(days)&&end)dates=dates.filter(date=>(new Date(`${end}T12:00:00`)-new Date(`${date}T12:00:00`))/DAY<days);
    const inRange=new Set(dates);
    const sumOf=values=>{const present=values.filter(value=>finite(value)!=null);return present.length?present.reduce((sum,value)=>sum+value,0):null;};
    const scoped=p=>fallback&&range!=="30d"&&range!=="all"?{tokens:sumOf((p.series||[]).filter(row=>inRange.has(row.date)).map(row=>finite(row.tokens))),apiEquivalentCost:sumOf((p.series||[]).filter(row=>inRange.has(row.date)).map(row=>finite(row.apiEquivalentCost)))}:{tokens:finite(p.totals?.tokens),apiEquivalentCost:finite(p.totals?.apiEquivalentCost)};
    const providers=allProviders.map(p=>({name:p.name,scoped:scoped(p),raw:p}));
    const totals={};
    for(const k of ["tokens","apiEquivalentCost"])totals[k]=sumOf(providers.map(p=>p.scoped[k]));
    for(const k of ["sessions","subagentsActive","activeHours"])totals[k]=fallback?null:sumOf(allProviders.map(p=>finite(p.totals?.[k])));
    const cacheRows=allProviders.filter(p=>finite(p.totals?.cacheHitRate)!=null&&(!fallback||range==="30d"||range==="all"));
    const cacheWeight=cacheRows.reduce((sum,p)=>sum+(finite(p.totals?.tokens)||1),0);
    totals.cacheHitRate=cacheRows.length?cacheRows.reduce((sum,p)=>sum+p.totals.cacheHitRate*(finite(p.totals?.tokens)||1),0)/cacheWeight:null;
    const previous={};
    for(const k of ["tokens","apiEquivalentCost"]){const rows=!fallback&&(range==="24h"||range==="7d")?allProviders.filter(p=>finite(p.previous?.[k])!=null):[];previous[k]=rows.length&&rows.length===allProviders.length?rows.reduce((sum,p)=>sum+p.previous[k],0):null;}
    const series=dates.map(date=>{const point={date};for(const p of allProviders){const row=(p.series||[]).find(item=>item.date===date);point[p.name]=finite(row?.[key]);}return point;});
    const modelMap=new Map();
    if(!fallback||range==="30d"||range==="all")for(const p of allProviders)for(const row of p.models||[]){const raw=String(row?.name||"");if(!raw)continue;const name=modelName(raw)||raw,item=modelMap.get(name)||{name,raw,tokens:null,apiEquivalentCost:null};for(const k of ["tokens","apiEquivalentCost"]){const value=finite(row[k]);if(value!=null)item[k]=(item[k]??0)+value;}modelMap.set(name,item);}
    const models=[...modelMap.values()].filter(row=>row.tokens!=null||row.apiEquivalentCost!=null).sort((a,b)=>(b[key]||0)-(a[key]||0));
    const peaks=allProviders.map(p=>({name:p.name,fiveHour:finite(p.limitPeaks?.fiveHour),weekly:finite(p.limitPeaks?.weekly)})).filter(p=>p.fiveHour!=null||p.weekly!=null);
    return {fallback,providers,totals,previous,series,models,peaks};
  }
  const tokenHero=value=>{const [figure,unit]=value>=1e6?[(value/1e6).toFixed(1),"M tokens"]:value>=1e3?[(value/1e3).toFixed(1),"K tokens"]:[String(Math.round(value)),"tokens"];return `<strong class="dash-hero-number">${esc(figure)}</strong><span class="dash-hero-unit">${esc(unit)}</span>`;};
  function splitHTML(data){
    const rows=data.providers.map(p=>({name:p.name,value:finite(p.scoped[metricKey()])})).filter(row=>row.value>0),sum=rows.reduce((total,row)=>total+row.value,0);
    if(!sum)return "";
    return `<div class="dash-split" aria-hidden="true">${rows.map(row=>`<span class="${providerKey(row.name)}-fill" style="flex:${row.value}"></span>`).join("")}</div><div class="dash-split-legend">${rows.map(row=>`<span class="dash-legend-item"><i class="dash-dot ${providerKey(row.name)}"></i>${esc(row.name)} <b>${Math.round(100*row.value/sum)}%</b></span>`).join("")}</div>`;
  }
  function emptyHTML(){return `<section class="dash-empty-state"><span class="dash-eyebrow">${state.metric==="cost"?"API-equivalent cost":`Tokens over ${RANGE_NAME[state.range]}`}</span><p>No recorded usage in this range</p></section>`;}
  function insightsHTML(){
    const data=insightsData(),key=metricKey(),cost=isCost(),value=finite(data.totals[key]),prev=finite(data.previous[key]),delta=value!=null&&prev!=null&&prev!==0?(value-prev)/prev*100:null;
    if(!(finite(data.totals.tokens)>0)&&!(finite(data.totals.apiEquivalentCost)>0))return emptyHTML();
    const figure=value==null?`<span class="dash-empty-line">${cost?"Cost is not available for this source.":"Token totals are not available for this source."}</span>`:cost?`<span class="dash-hero-currency">$</span><strong class="dash-hero-number">${esc(money(value).replace(/^\$/,""))}</strong><span class="dash-hero-unit">estimated</span>`:tokenHero(value);
    const hero=`<section class="dash-hero"><div class="dash-hero-meta"><span class="dash-eyebrow">${cost?"API-equivalent cost":`Tokens over ${RANGE_NAME[state.range]}`}</span>${delta==null?"":`<span class="dash-delta${delta<0?" is-down":""}">${delta<0?"↓":"↑"} ${Math.abs(delta).toFixed(1)}% <span class="muted">vs ${RANGE_PREVIOUS[state.range]}</span></span>`}</div><div class="dash-hero-number-row">${figure}</div>${splitHTML(data)}</section>`;
    const chart=seriesChart(data),chartHTML=chart?`<section class="dash-chart-section"><div class="dash-section-head"><span class="dash-section-title">Usage over time</span><span class="dash-section-note">${data.fallback?"Last 31 days · ":""}Daily · ${cost?"API-equivalent cost":"tokens"}</span></div>${chart}</section>`:"";
    const stats=[["Tokens",data.totals.tokens,number],["Sessions",data.totals.sessions,number],["Subagents active",data.totals.subagentsActive,number],["Cache hit rate",data.totals.cacheHitRate,v=>`${Math.round(v)}%`],["Active hours",data.totals.activeHours,v=>`${number(v)}h`]].filter(([,v])=>finite(v)!=null);
    const statsHTML=stats.length?`<section class="dash-stats" style="grid-template-columns:repeat(${stats.length},minmax(0,1fr))">${stats.map(([label,v,format])=>`<div class="dash-stat"><span class="dash-stat-label">${label}</span><strong class="dash-stat-value">${esc(format(v))}</strong></div>`).join("")}</section>`:"";
    const models=data.models,tokenSum=models.reduce((sum,row)=>sum+(finite(row.tokens)||0),0);
    const modelHTML=models.length?`<div class="dash-model-block"><div class="dash-section-label"><span>Model breakdown</span><small>${RANGE_NAME[state.range]}</small></div><div class="dash-model-head"><span>Model</span><span>Tokens</span><span>Cost</span><span>Share</span></div>${models.map(row=>{const family=modelKey(row.raw),pct=tokenSum&&finite(row.tokens)!=null?100*row.tokens/tokenSum:null;return `<div class="dash-model-row"><span class="dash-model-name ${family}"><i></i>${esc(row.name)}</span><b>${esc(number(row.tokens))}</b><b>${esc(money(row.apiEquivalentCost))}</b><span class="dash-share">${pct==null?"":`<i><em class="${family}" style="width:${pct.toFixed(2)}%"></em></i><small>${Math.round(pct)}%</small>`}</span></div>`;}).join("")}</div>`:"";
    const limits=data.peaks.length?`<div class="dash-limits"><div class="dash-section-label"><span>Limit peaks</span><small>period max</small></div>${data.peaks.map(p=>`<div class="dash-limit-group ${providerKey(p.name)}-color"><div class="dash-limit-period">${esc(p.name)}</div>${[["5-hour",p.fiveHour],["Weekly",p.weekly]].filter(([,v])=>v!=null).map(([label,v])=>`<div class="dash-limit-row"><span>${label}</span><i class="dash-limit-track"><em style="width:${Math.max(0,Math.min(100,v)).toFixed(1)}%"></em></i><b>${Math.round(v)}%</b></div>`).join("")}</div>`).join("")}</div>`:"";
    const lower=modelHTML||limits?`<section class="dash-lower${modelHTML&&limits?"":" is-single"}">${modelHTML}${limits}</section>`:"";
    return hero+chartHTML+statsHTML+lower;
  }
  const toggleRow=(label,description,action,value)=>`<div class="dash-setting-row"><div class="dash-setting-copy"><span class="dash-setting-label">${label}</span><span class="dash-setting-description">${description}</span></div><div class="dash-setting-control"><button class="dash-toggle${value?" is-on":""}" role="switch" aria-checked="${Boolean(value)}" aria-label="${label}" data-dashboard-action="${action}"></button></div></div>`;
  const rowHTML=(label,description,control="")=>`<div class="dash-setting-row"><div class="dash-setting-copy"><span class="dash-setting-label">${label}</span><span class="dash-setting-description">${description}</span></div><div class="dash-setting-control">${control}</div></div>`;
  function settingsHTML(){
    const clock=readPref("branchglanceClockFormat","12")==="24"?"24":"12",policies=Array.isArray(state.policy?.providers)?state.policy.providers:[];
    const providers=policies.length?`<section class="dash-settings-group" aria-label="Provider permissions"><h2>Providers <span class="count">Policy controlled</span></h2>${policies.map(row=>rowHTML(esc(row.name),"Current provider policy",`<span class="dash-policy-flags">${["show","collect","send"].map(key=>`<span><small>${key}</small><b>${row[key]===true?"On":row[key]===false?"Off":"—"}</b></span>`).join("")}</span>`)).join("")}<p class="dash-setting-note">Show, collect and send flags are set in the policy file.${state.policy?.policyFile&&state.policy.policyFile!=="absent"?` <code>${esc(state.policy.policyFile)}</code>`:""}</p></section>`:"";
    // Reset actions stays isolated in its existing frame; the dashboard never receives a settings token.
    const reset=state.settingsLoaded?`<iframe class="reset-embed" title="Auto-greet, continue and Claude tracking settings" src="/reset_actions.html?embedded=1&amp;source=${encodeURIComponent(state.source)}"></iframe>`:`<p class="dash-status">${esc(state.settingsStatus||"Loading reset settings…")}</p>`;
    const notifications=`<section class="dash-settings-group"><h2>Notifications</h2>${rowHTML("Reset actions","Auto-greet and continue preferences")}${reset}${rowHTML("Claude tracking","Manage Claude Desktop collection",`<button class="dash-action" data-dashboard-action="claude-settings">Change Claude tracking…</button>`)}</section>`;
    const general=`<section class="dash-settings-group"><h2>General</h2>${toggleRow("Lock widget position","Keep the widget where it is","lock",state.lock?.locked)}${toggleRow("Keep widget open","Do not collapse automatically","keep-open",state.keepOpen?.keepOpen)}${rowHTML("Clock format","Use a 12 or 24-hour clock",`<button class="dash-picker" data-dashboard-action="clock">${clock}-hour <i class="chevron" aria-hidden="true"></i></button>`)}</section>`;
    const monitorOptions=state.monitors.map(row=>`<option value="${esc(row.id)}"${row.selected?" selected":""}>${esc(row.label||row.kind||"Monitor")}</option>`).join("");
    const display=`<section class="dash-settings-group"><h2>Display</h2>${state.monitors.length?rowHTML("Monitor","Choose where the widget opens",`<select class="dash-select" aria-label="Monitor" data-dashboard-action="monitor">${monitorOptions}</select>`):""}${rowHTML("Recenter widget","Return to the selected display",`<button class="dash-action" data-dashboard-action="recenter">Recenter</button>`)}</section>`;
    const status=state.settingsStatus?`<p class="dash-status${state.settingsError?" error":""}" role="status">${esc(state.settingsStatus)}</p>`:"";
    return `<div class="dash-settings-columns"><div class="dash-settings-column">${general}${providers}${notifications}</div><div class="dash-settings-column">${display}${status}</div></div>`;
  }
  function bindTooltips(){
    const root=$("#dashboard-view"),tip=$("#dash-tooltip");if(!root||!tip)return;
    const show=target=>{tip.innerHTML=target.getAttribute("data-tip-html")||"";tip.classList.add("is-visible");tip.setAttribute("aria-hidden","false");};
    root.addEventListener("pointerover",event=>{const target=event.target?.closest?.("[data-tip-html]");if(target)show(target);});
    root.addEventListener("focusin",event=>{const target=event.target?.closest?.("[data-tip-html]");if(target)show(target);});
    for(const eventName of ["pointerout","focusout"])root.addEventListener(eventName,event=>{if(event.target?.closest?.("[data-tip-html]")){tip.classList.remove("is-visible");tip.setAttribute("aria-hidden","true");}});
    root.addEventListener("pointermove",event=>{if(tip.classList.contains("is-visible")){tip.style.left=Math.min(event.clientX+12,window.innerWidth-170)+"px";tip.style.top=Math.min(event.clientY+12,window.innerHeight-100)+"px";}});
  }
  let tooltipBound=false;
  function bindTooltipsOnce(){if(tooltipBound)return;tooltipBound=true;bindTooltips();}
  function crossFade(change){
    const content=$("#dashboard-content");
    if(!content||reducedMotion()){change();return;}
    content.classList.add("is-switching");
    setTimeout(()=>{change();requestAnimationFrame(()=>content.classList.remove("is-switching"));},90);
  }
  function syncTabs(){
    const root=$("#dashboard-view");if(root)root.dataset.tab=state.tab;
    for(const key of ["insights","settings"]){const button=$("#dashboard-tab-"+key),panel=$("#dashboard-"+key);if(button){button.classList.toggle("is-active",key===state.tab);button.setAttribute("aria-selected",String(key===state.tab));}if(panel)panel.hidden=key!==state.tab;}
  }
  function syncSegments(){
    document.querySelectorAll("[data-dashboard-range]").forEach(button=>{const active=button.dataset.dashboardRange===state.range;button.classList.toggle("is-active",active);button.setAttribute("aria-pressed",String(active));});
    const unpriced=state.insights!=null&&!costAvailable();
    document.querySelectorAll("[data-dashboard-metric]").forEach(button=>{const metric=button.dataset.dashboardMetric,active=metric===state.metric,blocked=metric==="cost"&&unpriced;button.classList.toggle("is-active",active);button.classList.toggle("is-unavailable",blocked);button.setAttribute("aria-pressed",String(active));button.setAttribute("aria-disabled",String(blocked));button.title=blocked?"No published pricing for these models":"";});
  }
  function selectTab(name){
    if(name!=="insights"&&name!=="settings")return;
    if(name===state.tab)return;
    crossFade(()=>{state.tab=name;syncTabs();state.renderKey="";render();if(name==="settings"&&!state.settingsLoaded)void loadSettings();});
  }
  function setRange(range){
    if(!isRange(range)||range===state.range)return;
    crossFade(()=>{state.range=range;writePref("branchglanceInsightsRange",range);syncSegments();state.renderKey="";void loadInsights();render();});
  }
  function setMetric(metric){
    if(!["tokens","cost"].includes(metric)||metric===state.metric)return;
    if(metric==="cost"&&state.insights&&!costAvailable())return;
    state.metric=metric;writePref("branchglanceInsightsMetric",metric);syncSegments();state.renderKey="";render();
  }
  async function loadSettings(){
    if(state.settingsBusy)return;state.settingsBusy=true;state.settingsError=false;state.settingsStatus="Loading settings…";render();const api=window.pywebview?.api;
    try{const [policyResponse,identityResponse]=await Promise.all([fetch("/api/provider-policy",{cache:"no-store"}),fetch("/api/identity",{cache:"no-store"})]);if(policyResponse.ok)state.policy=await policyResponse.json();if(identityResponse.ok)state.identity=await identityResponse.json();const [monitors,lock,keep]=await Promise.all([api?.get_monitors?.()||[],api?.get_widget_lock?.()||null,api?.get_keep_open?.()||null]);state.monitors=Array.isArray(monitors)?monitors:[];state.lock=lock;state.keepOpen=keep;state.settingsStatus="";state.settingsLoaded=true;}
    catch(error){state.settingsError=true;state.settingsStatus=error?.message||"Settings could not be loaded.";}
    finally{state.settingsBusy=false;state.settingsRenderKey="";render();}
  }
  async function chooseMonitor(id){
    const api=window.pywebview?.api;if(typeof api?.select_monitor!=="function")return;
    try{const result=await api.select_monitor(id);state.monitors=Array.isArray(result)?result:state.monitors;state.settingsError=false;state.settingsStatus="";}
    catch(error){state.settingsError=true;state.settingsStatus=error?.message||"Monitor could not be selected.";}
    finally{state.settingsRenderKey="";render();}
  }
  async function toggleSetting(action){
    const api=window.pywebview?.api;
    try{if(action==="lock"&&typeof api?.set_widget_lock==="function")state.lock=await api.set_widget_lock(state.lock?.locked!==true,state.lock?.expanded===true,null,state.lock?.revision);if(action==="keep-open"&&typeof api?.set_keep_open==="function")state.keepOpen=await api.set_keep_open(state.keepOpen?.keepOpen!==true,state.keepOpen?.keepOpenRevision);state.settingsError=false;state.settingsStatus="";}
    catch{state.settingsError=true;state.settingsStatus="Setting could not be changed.";}
    finally{state.settingsRenderKey="";render();}
  }
  function setClockFormat(value){
    value=String(value);if(!["12","24"].includes(value))return false;
    writePref("branchglanceClockFormat",value);window.TokenTrackerSetClockFormat?.(value);state.settingsRenderKey="";render();return true;
  }
  function toggleClock(){setClockFormat(readPref("branchglanceClockFormat","12")==="12"?"24":"12");}
  function render(){
    if(!state.open)return;
    const insightsKey=[state.version,state.source,state.range,state.metric,state.tab].join("|");
    if(insightsKey!==state.renderKey){state.renderKey=insightsKey;const insights=$("#dashboard-insights");if(insights)insights.innerHTML=insightsHTML();}
    const settings=$("#dashboard-settings");
    if(settings&&state.tab==="settings"){
      const settingsKey=[state.source,state.policy?.generation,state.settingsLoaded,state.settingsStatus,state.settingsBusy,state.settingsError,state.lock?.revision,state.keepOpen?.keepOpenRevision,readPref("branchglanceClockFormat","12"),state.monitors.map(m=>`${m.id}:${m.label}:${m.selected}`).join("|")].join("|");
      if(settingsKey!==state.settingsRenderKey){state.settingsRenderKey=settingsKey;settings.innerHTML=settingsHTML();state.frame=$("#dashboard-settings iframe");}
    }
    bindTooltipsOnce();
  }
  function open({source}={}){
    state.open=true;state.tab="insights";if(typeof source==="string")state.source=source;
    state.range=readPref("branchglanceInsightsRange","30d");if(!isRange(state.range))state.range="30d";
    state.metric=readPref("branchglanceInsightsMetric","cost");if(!["tokens","cost"].includes(state.metric))state.metric="cost";
    const root=$("#dashboard-view");if(root){root.hidden=false;root.inert=false;root.setAttribute("aria-hidden","false");}
    syncTabs();syncSegments();render();void loadInsights();
  }
  function close(){
    state.open=false;const root=$("#dashboard-view");if(root){root.hidden=true;root.inert=true;root.setAttribute("aria-hidden","true");}
    const frame=$("#dashboard-settings iframe");if(frame)frame.src="about:blank";
    state.frame=null;state.settingsLoaded=false;state.settingsRenderKey="";
  }
  function normalizeStats(snapshot){
    const providers=Array.isArray(snapshot?.providers)?snapshot.providers:[];
    return {fallback:true,generatedAt:snapshot?.generatedAt||null,providers:providers.map(row=>{const cache=row?.cache||{};return {name:row?.name,totals:{tokens:finite(row?.tracked31Days),apiEquivalentCost:finite(row?.apiEquivalentCost??cache.apiEquivalentCost),sessions:finite(row?.sessions),subagentsActive:finite(row?.subagentsActive),activeHours:finite(row?.activeHours),cacheHitRate:finite(row?.cacheHitRate??cache.percent)},previous:null,series:(Array.isArray(row?.activity)?row.activity:[]).map(item=>({date:item.date,tokens:finite(item.value??item.tokens),apiEquivalentCost:finite(item.apiEquivalentCost)})).filter(item=>/^\d{4}-\d\d-\d\d$/.test(item.date||"")),models:(Array.isArray(row?.modelUsage)?row.modelUsage:[]).map(item=>({name:item?.model,tokens:finite(item?.tokens),apiEquivalentCost:finite(item?.apiEquivalentCost)})),limitPeaks:row?.limitPeaks||null};})};
  }
  async function loadInsights(){
    const token=++state.loadToken;
    try{
      const response=await fetch("/api/insights?range="+encodeURIComponent(state.range),{cache:"no-store"});
      if(response.ok){const value=await response.json();if(Array.isArray(value?.providers)){if(token!==state.loadToken)return state.insights;state.statsSnapshot=null;setInsights(value);render();return value;}}
      const fallback=await fetch("/api/stats?background=1",{cache:"no-store"});if(!fallback.ok)throw new Error("Stats unavailable");
      const stats=await fallback.json();if(token!==state.loadToken)return state.insights;
      state.statsSnapshot=stats;setInsights(normalizeStats(stats));
    }catch{
      if(token!==state.loadToken)return state.insights;
      setInsights(state.snapshot?.providers?normalizeStats(state.snapshot):{fallback:true,providers:[]});
    }
    render();return state.insights;
  }
  function setSnapshot(snapshot){
    state.snapshot=snapshot;
    if(state.open&&(!state.insights||state.insights.fallback)){state.statsSnapshot=snapshot;setInsights(normalizeStats(snapshot));render();}
  }
  function setSource(source){if(typeof source==="string"&&source!==state.source){state.source=source;state.renderKey="";render();}}
  document.addEventListener("click",event=>{
    const tab=event.target?.closest?.("[data-dashboard-tab]");if(tab){selectTab(tab.dataset.dashboardTab);return;}
    const range=event.target?.closest?.("[data-dashboard-range]");if(range){setRange(range.dataset.dashboardRange);return;}
    const metric=event.target?.closest?.("[data-dashboard-metric]");if(metric){setMetric(metric.dataset.dashboardMetric);return;}
    const action=event.target?.closest?.("[data-dashboard-action]");if(!action)return;
    const name=action.dataset.dashboardAction;
    if(name==="close")window.tokenTrackerCloseDashboard?.();
    else if(name==="lock"||name==="keep-open")void toggleSetting(name);
    else if(name==="clock")toggleClock();
    else if(name==="recenter")void window.tokenTrackerRecenter?.();
    else if(name==="claude-settings")void window.pywebview?.api?.open_reset_actions?.();
  });
  document.addEventListener("change",event=>{const target=event.target;if(target?.dataset?.dashboardAction==="monitor")void chooseMonitor(target.value);});
  window.addEventListener("message",event=>{if(event.origin===location.origin&&event.source===state.frame?.contentWindow&&event.data?.type==="dashboard-reset-cancel")window.tokenTrackerCloseDashboard?.();});
  window.addEventListener("load",()=>{state.frame=$("#dashboard-settings iframe");});
  window.TokenTrackerDashboard={open,close,setSource,setSnapshot,selectTab,render,loadSettings,state};
  if(window.__TOKEN_TRACKER_DASHBOARD_TEST__)window.__tokenTrackerDashboardTest={insightsData,seriesChart,insightsHTML,settingsHTML,normalizeStats,emptyHTML,setSnapshot,setSource,setRange,setMetric,setClockFormat,loadInsights,state};
})();
