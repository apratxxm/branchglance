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
  const availableProviders=()=>Array.isArray(state.insights?.providers)?state.insights.providers:[];
  // Cost is available only when the 31-day or range totals carry a published price; it never comes from a per-day series alone.
  const costAvailable=()=>insightsData(false).totals.apiEquivalentCost!=null;
  const setInsights=value=>{state.insights=value;state.version++;if(state.metric==="cost"&&(!costAvailable()||(!state.metricExplicit&&insightsData(false).mostlyUnpriced)))state.metric="tokens";syncSegments();};
  const pt=p=>p[0].toFixed(1)+" "+p[1].toFixed(1);
  // Monotone cubic (Fritsch-Carlson): each segment stays between its two end values, so the line never overshoots peaks or dips below the floor.
  function curvePath(points,reverse=false){
    const n=points.length;if(n<2)return "";
    const dx=[],slope=[],m=new Array(n);
    for(let i=0;i<n-1;i++){dx[i]=points[i+1][0]-points[i][0];slope[i]=dx[i]?(points[i+1][1]-points[i][1])/dx[i]:0;}
    m[0]=slope[0];m[n-1]=slope[n-2];
    for(let i=1;i<n-1;i++)m[i]=slope[i-1]*slope[i]<=0?0:(slope[i-1]+slope[i])/2;
    for(let i=0;i<n-1;i++){
      if(slope[i]===0){m[i]=0;m[i+1]=0;continue;}
      const a=m[i]/slope[i],b=m[i+1]/slope[i];
      if(a<0||b<0){m[i]=0;m[i+1]=0;continue;}
      const sq=a*a+b*b;if(sq>9){const t=3/Math.sqrt(sq);m[i]=t*a*slope[i];m[i+1]=t*b*slope[i];}
    }
    const control=i=>{const h=dx[i]/3;return [[points[i][0]+h,points[i][1]+m[i]*h],[points[i+1][0]-h,points[i+1][1]-m[i+1]*h]];};
    if(!reverse){let d="M"+pt(points[0]);for(let i=0;i<n-1;i++){const [c1,c2]=control(i);d+=" C"+pt(c1)+" "+pt(c2)+" "+pt(points[i+1]);}return d;}
    let d="M"+pt(points[n-1]);for(let i=n-2;i>=0;i--){const [c1,c2]=control(i);d+=" C"+pt(c2)+" "+pt(c1)+" "+pt(points[i]);}return d;
  }
  // Round scale top for a chart axis: the smallest 1, 2, 4, 6, 8 or 10 times a power of ten at or above the value.
  function niceCeil(value){if(!(value>0))return 0;const power=10**Math.floor(Math.log10(value));for(const step of [1,2,4,6,8,10])if(step*power>=value*(1-1e-9))return step*power;return value;}
  function insightsData(requestCost=state.metric==="cost"){
    const fallback=state.insights?.fallback===true,range=state.range,allProviders=availableProviders();
    let dates=[...new Set(allProviders.flatMap(p=>(p.series||[]).map(row=>row.date).filter(Boolean)))].sort(),end=dates.at(-1);
    const days=RANGE_DAYS[range];
    if(Number.isFinite(days)&&end)dates=dates.filter(date=>(new Date(`${end}T12:00:00`)-new Date(`${date}T12:00:00`))/DAY<days);
    const inRange=new Set(dates);
    const sumOf=values=>{const present=values.filter(value=>finite(value)!=null);return present.length?present.reduce((sum,value)=>sum+value,0):null;};
    const rowsIn=p=>(p.series||[]).filter(row=>inRange.has(row.date));
    const windowed=fallback&&range!=="30d"&&range!=="all";
    const providers=allProviders.map(p=>({name:p.name,scoped:windowed?{tokens:sumOf(rowsIn(p).map(row=>finite(row.tokens))),apiEquivalentCost:sumOf(rowsIn(p).map(row=>finite(row.apiEquivalentCost)))}:{tokens:finite(p.totals?.tokens),apiEquivalentCost:finite(p.totals?.apiEquivalentCost)}}));
    const totals={};
    for(const k of ["tokens","apiEquivalentCost"])totals[k]=sumOf(providers.map(p=>p.scoped[k]));
    for(const k of ["sessions","subagentsActive","activeHours"])totals[k]=fallback?null:sumOf(allProviders.map(p=>finite(p.totals?.[k])));
    const cacheRows=allProviders.filter(p=>finite(p.totals?.cacheHitRate)!=null&&(!fallback||range==="30d"||range==="all"));
    const cacheWeight=cacheRows.reduce((sum,p)=>sum+(finite(p.totals?.tokens)||1),0);
    totals.cacheHitRate=cacheRows.length?cacheRows.reduce((sum,p)=>sum+p.totals.cacheHitRate*(finite(p.totals?.tokens)||1),0)/cacheWeight:null;
    const previous={};
    for(const k of ["tokens","apiEquivalentCost"]){const rows=!fallback&&(range==="24h"||range==="7d")?allProviders.filter(p=>finite(p.previous?.[k])!=null):[];previous[k]=rows.length&&rows.length===allProviders.length?rows.reduce((sum,p)=>sum+p.previous[k],0):null;}
    const seriesFor=key=>dates.map(date=>{const point={date};for(const p of allProviders){const row=(p.series||[]).find(item=>item.date===date);point[p.name]=finite(row?.[key]);}return point;});
    const seriesByKey={tokens:seriesFor("tokens"),apiEquivalentCost:seriesFor("apiEquivalentCost")};
    const positive=rows=>rows.some(row=>allProviders.some(p=>(finite(row[p.name])||0)>0));
    const costHero=requestCost&&totals.apiEquivalentCost!=null;
    const chartKey=costHero&&positive(seriesByKey.apiEquivalentCost)?"apiEquivalentCost":"tokens";
    const heroKey=costHero?"apiEquivalentCost":totals.tokens!=null?"tokens":totals.apiEquivalentCost!=null?"apiEquivalentCost":null;
    const modelMap=new Map();
    if(!windowed)for(const p of allProviders)for(const row of p.models||[]){const raw=String(row?.name||"");if(!raw)continue;const name=modelName(raw)||raw,item=modelMap.get(name)||{name,raw,tokens:null,apiEquivalentCost:null};for(const k of ["tokens","apiEquivalentCost"]){const value=finite(row[k]);if(value!=null)item[k]=(item[k]??0)+value;}modelMap.set(name,item);}
    const sortKey=heroKey||"tokens";
    const models=[...modelMap.values()].filter(row=>row.tokens!=null||row.apiEquivalentCost!=null).sort((a,b)=>(b[sortKey]||0)-(a[sortKey]||0));
    const costPartial=allProviders.some(p=>p.totals?.costPartial===true);
    const unpricedModels=[...new Set(allProviders.flatMap(p=>Array.isArray(p.totals?.unpricedModels)?p.totals.unpricedModels:[]).map(name=>modelName(String(name))||String(name)).filter(Boolean))];
    const peaks=allProviders.map(p=>({name:p.name,fiveHour:finite(p.limitPeaks?.fiveHour),weekly:finite(p.limitPeaks?.weekly)})).filter(p=>p.fiveHour!=null||p.weekly!=null);
    const unpricedTokens=models.filter(row=>row.apiEquivalentCost==null&&row.tokens!=null).reduce((sum,row)=>sum+row.tokens,0);
    const tokenDenominator=finite(totals.tokens)??models.reduce((sum,row)=>sum+(finite(row.tokens)||0),0);
    const mostlyUnpriced=tokenDenominator>0&&unpricedTokens>tokenDenominator/2;
    return {fallback,providers,totals,previous,seriesByKey,series:seriesByKey[chartKey],chartKey,heroKey,costHero,costPartial,unpricedModels,mostlyUnpriced,models,peaks,dates};
  }
  const joinNames=names=>names.length<=2?names.join(" and "):`${names.slice(0,-1).join(", ")} and ${names.at(-1)}`;
  const tokenHero=value=>{const [figure,unit]=value>=1e9?[(value/1e9).toFixed(2),"B tokens"]:value>=1e6?[(value/1e6).toFixed(1),"M tokens"]:value>=1e3?[(value/1e3).toFixed(1),"K tokens"]:[String(Math.round(value)),"tokens"];return `<strong class="dash-hero-number">${esc(figure)}</strong><span class="dash-hero-unit">${esc(unit)}</span>`;};
  function seriesChart(data){
    const key=data.chartKey,rows=data.seriesByKey[key],cost=key==="apiEquivalentCost",format=value=>cost?money(value):number(value);
    const providers=data.providers.filter(p=>rows.some(row=>(finite(row[p.name])||0)>0));
    if(!rows.length||!providers.length)return "";
    const W=668,H=113,L=42,R=4,T=9,B=20,PW=W-L-R,PH=H-T-B,step=PW/Math.max(rows.length-1,1);
    const totals=rows.map(row=>providers.reduce((sum,p)=>sum+(finite(row[p.name])||0),0));
    const max=niceCeil(Math.max(...totals)*1.1)||1;
    const xAt=index=>L+index*step,yAt=value=>T+PH-value/max*PH;
    let cumulative=rows.map(()=>0),paths="";
    for(const p of providers){
      const cls=providerKey(p.name),lower=cumulative,upper=rows.map((row,index)=>lower[index]+(finite(row[p.name])||0));
      cumulative=upper;
      const top=upper.map((value,index)=>[xAt(index),yAt(value)]),bottom=lower.map((value,index)=>[xAt(index),yAt(value)]);
      const edge=curvePath(top),back=curvePath(bottom,true).replace(/^M[^C]*/,"");
      paths+=`<path class="area-fill ${cls}" d="${edge} L${pt(bottom.at(-1))} ${back} Z"/><path class="area-glow ${cls}" d="${edge}"/><path class="area-edge ${cls}" d="${edge}"/>`;
    }
    const ticks=[1,.5,0].map(fraction=>{const y=yAt(max*fraction),label=fraction===0?(cost?"$0":"0"):format(max*fraction);return `<line class="dash-grid" x1="${L}" x2="${W-R}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}"/><text class="dash-axis-value" x="${L-8}" y="${(y+3).toFixed(1)}" text-anchor="end">${esc(label)}</text>`;}).join("");
    const labelIndexes=[...new Set([0,rows.length-1])];
    const dates=labelIndexes.map(index=>{const anchor=index===0?"start":index===rows.length-1?"end":"middle";return `<text class="dash-date" x="${xAt(index).toFixed(1)}" y="${H-3}" text-anchor="${anchor}">${esc(dayLabel(rows[index].date))}</text>`;}).join("");
    const hover=rows.map((row,index)=>{const x=xAt(index),total=totals[index],tip=`<span class="tip-date">${esc(dayLabel(row.date))}</span><b class="tip-total">${esc(format(total))}</b>${providers.map(p=>`<span class="tip-row"><span><i class="dash-dot ${providerKey(p.name)}"></i>${esc(p.name)}</span><strong>${esc(format(finite(row[p.name])||0))}</strong></span>`).join("")}`;
      return `<g class="dash-point" tabindex="0" data-tip-html="${esc(tip)}"><line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${T}" y2="${T+PH}"/><circle cx="${x.toFixed(1)}" cy="${yAt(total).toFixed(1)}" r="2.5"/></g>`;}).join("");
    return `<svg class="dash-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Daily ${cost?"API-equivalent cost":"tokens"} by provider">${ticks}${paths}${hover}${dates}</svg>`;
  }
  function splitHTML(data){
    const key=data.heroKey;if(!key)return "";
    const rows=data.providers.map(p=>({name:p.name,value:finite(p.scoped[key])})).filter(row=>row.value>0),sum=rows.reduce((total,row)=>total+row.value,0);
    if(!sum)return "";
    return `<div class="dash-split" aria-hidden="true">${rows.map(row=>`<span class="${providerKey(row.name)}-fill" style="flex:${row.value}"></span>`).join("")}</div><div class="dash-split-legend">${rows.map(row=>`<span class="dash-legend-item"><i class="dash-dot ${providerKey(row.name)}"></i>${esc(row.name)} <b>${Math.round(100*row.value/sum)}%</b></span>`).join("")}</div>`;
  }
  function emptyHTML(){return `<section class="dash-empty-state"><span class="dash-eyebrow">Usage</span><p>No recorded usage in this range</p></section>`;}
  function insightsHTML(){
    const data=insightsData(),key=data.heroKey;
    if(key==null||!(finite(data.totals.tokens)>0||finite(data.totals.apiEquivalentCost)>0))return emptyHTML();
    const cost=key==="apiEquivalentCost",value=data.totals[key],prev=finite(data.previous[key]),delta=prev!=null&&prev!==0?(value-prev)/prev*100:null;
    const figure=cost?`<span class="dash-hero-currency">$</span><strong class="dash-hero-number">${esc(money(value).replace(/^\$/,""))}</strong><span class="dash-hero-unit">estimated</span>`:tokenHero(value);
    const chart=state.range==="24h"?"":seriesChart(data),chartHTML=chart?`<section class="dash-chart-section"><div class="dash-section-head"><span class="dash-section-title">Usage over time</span><span class="dash-section-note">${data.fallback?"Last 31 days · ":""}Daily · ${data.chartKey==="apiEquivalentCost"?"API-equivalent cost estimate":"tokens"}</span></div>${chart}</section>`:"";
    // A zero stat next to non-zero tokens means the source did not report that figure, so it is hidden rather than shown as 0.
    const tokensPresent=finite(data.totals.tokens)>0;
    const missing=v=>finite(v)==null||(v===0&&tokensPresent);
    const stats=[[data.totals.sessions,v=>`${number(v)} sessions`],[data.totals.subagentsActive,v=>`${number(v)} subagents`],[data.totals.cacheHitRate,v=>`${Math.round(v)}% cache hits`],[data.totals.activeHours,v=>`${number(v)}h active`]].filter(([v])=>!missing(v));
    const statsHTML=stats.length?`<p class="dash-summary">${stats.map(([v,format])=>esc(format(v))).join(" · ")}</p>`:"";
    const hero=`<section class="dash-hero"><div class="dash-hero-meta"><span class="dash-eyebrow">${cost?"API-equivalent cost estimate":`Tokens over ${RANGE_NAME[state.range]}`}</span>${delta==null?"":`<span class="dash-delta${delta<0?" is-down":""}">${delta<0?"↓":"↑"} ${Math.abs(delta).toFixed(1)}% <span class="muted">vs ${RANGE_PREVIOUS[state.range]}</span></span>`}</div><div class="dash-hero-number-row">${figure}</div>${cost&&data.costPartial&&data.unpricedModels.length?`<p class="dash-cost-note">Excludes ${esc(joinNames(data.unpricedModels))} (no published price)</p>`:""}${splitHTML(data)}${statsHTML}</section>`;
    const models=data.models;
    const modelHTML=models.length?`<div class="dash-model-block"><div class="dash-section-label"><span>Models</span>${models.some(row=>finite(row.apiEquivalentCost)!=null)?'<small>Costs estimated</small>':''}</div>${models.slice(0,5).map(row=>{const family=modelKey(row.raw);return `<div class="dash-model-row"><span class="dash-model-name ${family}"><i></i>${esc(row.name)}</span><b>${esc(number(row.tokens))}</b><b title="Estimated API-equivalent cost">${esc(money(row.apiEquivalentCost))}</b></div>`;}).join("")}${models.length>5?`<div class="dash-model-more">+${models.length-5} more</div>`:""}</div>`:"";
    const limits=data.peaks.length?`<div class="dash-limits"><div class="dash-section-label"><span>Limit peaks</span></div>${data.peaks.map(p=>`<div class="dash-limit-group ${providerKey(p.name)}-color"><div class="dash-limit-period">${esc(p.name)}</div>${[["5-hour",p.fiveHour],["Weekly",p.weekly]].filter(([,v])=>v!=null).map(([label,v])=>`<div class="dash-limit-row"><span>${label}</span><i class="dash-limit-track"><em style="width:${Math.max(0,Math.min(100,v)).toFixed(1)}%"></em></i><b>${Math.round(v)}%</b></div>`).join("")}</div>`).join("")}</div>`:"";
    const lower=modelHTML||limits?`<section class="dash-lower${modelHTML&&limits?"":" is-single"}">${modelHTML}${limits}</section>`:"";
    return hero+lower+chartHTML;
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
    const version=typeof state.identity?.version==="string"&&typeof state.identity?.build==="string"?`<p class="dash-setting-note">Version ${esc(state.identity.version)}+${esc(state.identity.build)}</p>`:"";
    return `<div class="dash-settings-columns"><div class="dash-settings-column">${general}${providers}${notifications}</div><div class="dash-settings-column">${display}${status}${version}</div></div>`;
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
    state.metricExplicit=true;state.metric=metric;writePref("branchglanceInsightsMetric",metric);syncSegments();state.renderKey="";render();
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
      const settingsKey=[state.source,state.policy?.generation,state.identity?.version,state.identity?.build,state.settingsLoaded,state.settingsStatus,state.settingsBusy,state.settingsError,state.lock?.revision,state.keepOpen?.keepOpenRevision,readPref("branchglanceClockFormat","12"),state.monitors.map(m=>`${m.id}:${m.label}:${m.selected}`).join("|")].join("|");
      if(settingsKey!==state.settingsRenderKey){state.settingsRenderKey=settingsKey;settings.innerHTML=settingsHTML();state.frame=$("#dashboard-settings iframe");}
    }
    bindTooltipsOnce();
  }
  function open({source}={}){
    state.open=true;state.tab="insights";if(typeof source==="string")state.source=source;
    state.range=readPref("branchglanceInsightsRange","30d");if(!isRange(state.range))state.range="30d";
    state.metricExplicit=readPref("branchglanceInsightsMetric",null)!==null;state.metric=readPref("branchglanceInsightsMetric","cost");if(!["tokens","cost"].includes(state.metric))state.metric="cost";
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
  if(window.__TOKEN_TRACKER_DASHBOARD_TEST__)window.__tokenTrackerDashboardTest={insightsData,seriesChart,curvePath,insightsHTML,settingsHTML,normalizeStats,emptyHTML,setSnapshot,setSource,setRange,setMetric,setClockFormat,loadInsights,state};
})();
