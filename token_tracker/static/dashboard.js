(() => {
  const $=selector=>document.querySelector(selector),DAY=86400000;
  const RANGE_DAYS={"24h":1,"7d":7,"30d":30,all:31};
  const RANGE_NAME={"24h":"24 hours","7d":"7 days","30d":"30 days",all:"31 days"};
  const RANGE_PREVIOUS={"24h":"previous 24 hours","7d":"previous 7 days","30d":"previous 30 days",all:"previous period"};
  const state={open:false,tab:"insights",source:"Codex",range:"30d",metric:"cost",snapshot:null,statsSnapshot:null,insights:null,version:0,loadToken:0,policy:null,monitors:[],lock:null,keepOpen:null,settingsLoaded:false,settingsBusy:false,frame:null,renderKey:"",settingsRenderKey:"",settingsStatus:"",settingsError:false,month:null,monthError:false,monthToken:0,resultRange:null,resultValue:null,loading:false,loadError:false,refreshTimer:null,fadeTimer:null,fadeVersion:0};
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
  function niceCeil(value){if(!(value>0))return 0;const power=10**Math.floor(Math.log10(value));for(const step of [1,1.2,1.5,2,3,4,5,6,8,10])if(step*power>=value*(1-1e-9))return step*power;return value;}
  function insightsData(requestCost=state.metric==="cost"){
    const fallback=state.insights?.fallback===true,range=state.resultValue===state.insights?(state.resultRange||state.range):(isRange(state.insights?.range)?state.insights.range:state.range),allProviders=availableProviders();
    let dates=[...new Set(allProviders.flatMap(p=>(p.series||[]).map(row=>row.date).filter(Boolean)))].sort(),end=dates.at(-1);
    const days=RANGE_DAYS[range];
    // 24h comes back as hourly points whose date is the local hour start (an ISO datetime). They are used as they are.
    const hourly=range==="24h"&&dates.length>1&&dates.every(date=>String(date).includes("T"));
    if(!hourly&&Number.isFinite(days)&&end)dates=dates.filter(date=>(new Date(`${end}T12:00:00`)-new Date(`${date}T12:00:00`))/DAY<days);
    if(!hourly&&dates.length>1){const filled=[],cursor=new Date(dates[0]+"T12:00:00"),last=dates.at(-1);while(localDate(cursor)<=last&&filled.length<31){filled.push(localDate(cursor));cursor.setDate(cursor.getDate()+1);}dates=filled;}
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
    for(const k of ["tokens","apiEquivalentCost"]){const rows=!fallback&&(range==="24h"||range==="7d")?allProviders.filter(p=>finite(p.previous?.[k])!=null&&finite(p.totals?.[k])!=null&&(k!=="apiEquivalentCost"||(p.totals?.costPartial!==true&&p.previous?.costPartial!==true&&!(p.totals?.unpricedModels?.length)&&!(p.previous?.unpricedModels?.length)))):[];previous[k]=rows.length&&rows.length===allProviders.length?rows.reduce((sum,p)=>sum+p.previous[k],0):null;}
    // Stats fallback: a day with no recorded row is zero usage, not missing provider data. Live /api/insights gaps stay null.
    const seriesFor=key=>dates.map(date=>{const point={date};for(const p of allProviders){const row=(p.series||[]).find(item=>item.date===date);point[p.name]=row?finite(row[key]):(fallback?0:null);point[p.name+"__partial"]=row?.costPartial===true;}return point;});
    const seriesByKey={tokens:seriesFor("tokens"),apiEquivalentCost:seriesFor("apiEquivalentCost")};
    const positive=rows=>rows.some(row=>allProviders.some(p=>(finite(row[p.name])||0)>0));
    const costHero=requestCost&&totals.apiEquivalentCost!=null;
    const chartKey=costHero&&seriesByKey.apiEquivalentCost.some(row=>allProviders.some(p=>finite(row[p.name])!=null))?"apiEquivalentCost":"tokens";
    const heroKey=costHero?"apiEquivalentCost":totals.tokens!=null?"tokens":totals.apiEquivalentCost!=null?"apiEquivalentCost":null;
    const modelMap=new Map();
    if(!windowed)for(const p of allProviders)for(const row of p.models||[]){const raw=String(row?.name||"");if(!raw)continue;const label=modelName(raw)||raw,longContext=/\s*·\s*long context$/i.test(label),name=label.replace(/\s*·\s*long context$/i,""),item=modelMap.get(name)||{name,raw,tokens:null,apiEquivalentCost:null,longContext:false};item.longContext=item.longContext||longContext;for(const k of ["tokens","apiEquivalentCost"]){const value=finite(row[k]);if(value!=null)item[k]=(item[k]??0)+value;}modelMap.set(name,item);}
    const sortKey=heroKey||"tokens";
    const models=[...modelMap.values()].filter(row=>row.tokens!=null||row.apiEquivalentCost!=null).sort((a,b)=>(b[sortKey]||0)-(a[sortKey]||0));
    const costPartial=allProviders.some(p=>p.totals?.costPartial===true||finite(p.totals?.apiEquivalentCost)==null);
    const unpricedModels=[...new Set(allProviders.flatMap(p=>Array.isArray(p.totals?.unpricedModels)?p.totals.unpricedModels:[]).map(name=>modelName(String(name))||String(name)).filter(Boolean))];
    const peaks=allProviders.map(p=>({name:p.name,fiveHour:finite(p.limitPeaks?.fiveHour),weekly:finite(p.limitPeaks?.weekly)})).filter(p=>p.fiveHour!=null||p.weekly!=null);
    const unpricedTokens=models.filter(row=>row.apiEquivalentCost==null&&row.tokens!=null).reduce((sum,row)=>sum+row.tokens,0);
    const tokenDenominator=finite(totals.tokens)??models.reduce((sum,row)=>sum+(finite(row.tokens)||0),0);
    const mostlyUnpriced=tokenDenominator>0&&unpricedTokens>tokenDenominator/2;
    return {fallback,range,hourly,providers,totals,previous,seriesByKey,series:seriesByKey[chartKey],chartKey,heroKey,costHero,costPartial,unpricedModels,mostlyUnpriced,models,peaks,dates};
  }
  const joinNames=names=>names.length<=2?names.join(" and "):`${names.slice(0,-1).join(", ")} and ${names.at(-1)}`;
  const tokenHero=value=>{const [figure,unit]=value>=1e9?[(value/1e9).toFixed(2),"B tokens"]:value>=1e6?[(value/1e6).toFixed(1),"M tokens"]:value>=1e3?[(value/1e3).toFixed(1),"K tokens"]:[String(Math.round(value)),"tokens"];return `<strong class="dash-hero-number">${esc(figure)}</strong><span class="dash-hero-unit">${esc(unit)}</span>`;};
  // Provider series hidden from the chart by clicking its legend name; the split and the Models list are unaffected.
  const hiddenProviders=new Set();
  // At least one provider stays on; the last one cannot be hidden.
  function toggleProvider(name){
    if(!name)return;
    if(hiddenProviders.has(name))hiddenProviders.delete(name);
    else if(availableProviders().some(p=>p.name!==name&&!hiddenProviders.has(p.name)))hiddenProviders.add(name);
    else return;
    state.renderKey="";render();
  }
  // Chart style: "stacked" (default) or "lines". Chosen with the small control in the chart header and remembered in localStorage.
  const chartStyle=()=>readPref("branchglanceChartStyle","stacked")==="lines"?"lines":"stacked";
  function setChartStyle(value){
    if(value!=="stacked"&&value!=="lines")return;
    if(value===chartStyle())return;
    dismissTooltip();writePref("branchglanceChartStyle",value);state.renderKey="";render();
  }
  const chartStyleHTML=()=>{const current=chartStyle();return `<div class="dash-chart-style" role="group" aria-label="Chart style">${[["lines","Lines"],["stacked","Stacked"]].map(([key,label])=>`<button type="button" data-dashboard-chart-style="${key}" aria-pressed="${current===key}">${label}</button>`).join("")}</div>`;};
  // Explicit provider filter chips beside the chart heading: a dot, the name and a checked state.
  const chipsHTML=data=>{
    if(data.providers.length<2)return "";
    return `<div class="dash-chips" role="group" aria-label="Providers shown in the chart">${data.providers.map(p=>{const on=!hiddenProviders.has(p.name);return `<button type="button" class="dash-chip ${providerKey(p.name)}-color" data-dashboard-provider="${esc(p.name)}" aria-pressed="${on}" title="${on?"Hide":"Show"} ${esc(p.name)} in the chart"><i class="dash-dot ${providerKey(p.name)}" aria-hidden="true"></i>${esc(p.name)}<span class="dash-chip-check" aria-hidden="true">${on?"\u2713":""}</span></button>`;}).join("")}</div>`;
  };
  function seriesChart(data){
    const key=data.chartKey,cost=key==="apiEquivalentCost",format=value=>cost?money(value):number(value);
    // The chart starts at the first day with usage (never fewer than two points when the range has them).
    const allRows=data.seriesByKey[key],firstUsed=allRows.findIndex(row=>data.providers.some(p=>(finite(row[p.name])||0)>0));
    const rows=firstUsed>0&&!data.hourly?allRows.slice(Math.min(firstUsed,Math.max(allRows.length-2,0))):allRows;
    const candidates=cost?data.providers.filter(p=>rows.some(row=>finite(row[p.name])!=null)):data.providers;
    const excluded=cost?data.providers.filter(p=>!candidates.includes(p)):[];
    if(!rows.length||!candidates.length||!rows.some(row=>candidates.some(p=>finite(row[p.name])!=null)))return "";
    const providers=candidates.filter(p=>!hiddenProviders.has(p.name));
    const lines=chartStyle()==="lines";
    if(!providers.length)return `<p class="dash-cost-note">Every provider is hidden. Click a provider in the split to show it.</p>`;
    const W=668,H=100,L=0,R=0,T=1,B=1,PW=W-L-R,PH=H-T-B,step=PW/Math.max(rows.length-1,1);
    const complete=row=>providers.every(p=>finite(row[p.name])!=null);
    const totals=rows.map(row=>complete(row)?providers.reduce((sum,p)=>sum+row[p.name],0):null);
    const peak=lines?Math.max(0,...rows.flatMap(row=>providers.map(p=>finite(row[p.name])??0))):Math.max(0,...totals.filter(v=>v!=null));
    const max=niceCeil(peak*1.1)||1;
    const xAt=index=>rows.length===1?W/2:L+index*step,yAt=value=>T+PH-value/max*PH;
    let paths="";
    if(lines){
      for(const p of providers){
        const cls=providerKey(p.name);let run=[];
        const flush=()=>{if(run.length>1)paths+=`<path class="area-glow ${cls}" d="${curvePath(run)}"/><path class="area-edge ${cls}" d="${curvePath(run)}"/>`;else if(run.length===1)paths+=`<circle class="area-edge ${cls}" cx="${run[0][0]}" cy="${run[0][1]}" r="2.5"/>`;run=[];};
        rows.forEach((row,i)=>{const value=finite(row[p.name]);if(value!=null)run.push([xAt(i),yAt(value)]);else flush();});flush();
      }
    }else for(let n=0;n<providers.length;n++){
      const p=providers[n],cls=providerKey(p.name);let segment=[];
      const flush=()=>{
        if(!segment.length)return;
        const top=segment.map(i=>[xAt(i),yAt(providers.slice(0,n+1).reduce((sum,q)=>sum+rows[i][q.name],0))]);
        const bottom=segment.map(i=>[xAt(i),yAt(providers.slice(0,n).reduce((sum,q)=>sum+rows[i][q.name],0))]);
        if(segment.length===1){paths+=`<circle class="area-edge ${cls}" cx="${top[0][0]}" cy="${top[0][1]}" r="2.5"/>`;}
        else{const edge=curvePath(top),back=curvePath(bottom,true).replace(/^M[^C]*/,"");paths+=`<path class="area-fill ${cls}" d="${edge} L${pt(bottom.at(-1))} ${back} Z"/><path class="area-glow ${cls}" d="${edge}"/><path class="area-edge ${cls}" d="${edge}"/>`;}
        segment=[];
      };
      rows.forEach((row,i)=>{if(complete(row))segment.push(i);else flush();});flush();
    }
    const ticks=[1,.5,0].map(fraction=>`<line class="dash-grid" x1="${L}" x2="${W-R}" y1="${yAt(max*fraction).toFixed(1)}" y2="${yAt(max*fraction).toFixed(1)}"/>`).join("");
    const axisFormat=value=>cost&&max>=10?"$"+Math.round(value).toLocaleString("en-US"):format(value);
    const axis=[1,.5,0].map(fraction=>`<span class="dash-axis-value" style="top:${(yAt(max*fraction)/H*100).toFixed(2)}%">${esc(fraction===0?(cost?"$0":"0"):axisFormat(max*fraction))}</span>`).join("");
    // Date ticks: the first and last day, plus two evenly spaced days between them once the range is long enough to read them.
    // Hourly (24h): a tick at every sixth local hour and "now" at the end. Daily ranges: first, last and two evenly spaced days.
    const hourOf=value=>Number(String(value).slice(11,13));
    let tickLabels;
    if(data.hourly){
      const last=rows.length-1,marks=rows.map((row,i)=>i).filter(i=>i<last-2&&hourOf(rows[i].date)%6===0);
      tickLabels=[...marks.map(i=>[i,hourOf(rows[i].date)+":00"]),[last,"now"]];
    }else{
      const idx=rows.length>=8?[0,Math.round((rows.length-1)/3),Math.round((rows.length-1)*2/3),rows.length-1]:rows.length>1?[0,rows.length-1]:[0];
      tickLabels=idx.map(i=>[i,dayLabel(rows[i].date)]);
    }
    const dates=`<div class="dash-dates">${tickLabels.map(([i,label],n)=>`<span class="dash-date${n===0&&i===0?" is-first":i===rows.length-1&&n>0?" is-last":""}" style="left:${(rows.length>1?i/(rows.length-1)*100:0).toFixed(2)}%">${esc(label)}</span>`).join("")}</div>`;
    const pointLabel=date=>data.hourly?new Intl.DateTimeFormat("en-US",{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}).format(new Date(date)):dayLabel(date);
    const hover=rows.map((row,index)=>{
      const x=xAt(index),total=totals[index],partial=cost&&(excluded.length>0||providers.some(p=>row[p.name+"__partial"])),unknown=total==null;
      const totalLabel=unknown?"Total unavailable":(partial?"Recorded subtotal: ":"Total: ")+format(total);
      const parts=providers.map(p=>`${p.name}: ${finite(row[p.name])==null?"unavailable":format(row[p.name])+(cost&&row[p.name+"__partial"]?" (partial estimate)":"")}`);
      const excludedLabel=excluded.length?`Excludes ${joinNames(excluded.map(p=>p.name))}: no recorded price`:"";
      const accessible=`${row.date}. ${totalLabel}. ${parts.join(". ")}${unknown?". Missing provider data":""}${excludedLabel?". "+excludedLabel:""}`;
      const tip=`<span class="tip-date">${esc(pointLabel(row.date))}</span><b class="tip-total">${esc(unknown?"—":(partial?"Recorded subtotal · ":"")+format(total))}</b>${providers.map(p=>`<span class="tip-row"><span><i class="dash-dot ${providerKey(p.name)}"></i>${esc(p.name)}</span><strong>${esc(finite(row[p.name])==null?"—":format(row[p.name])+(cost&&row[p.name+"__partial"]?" (partial)":""))}</strong></span>`).join("")}${unknown?'<span>Missing provider data</span>':""}${excludedLabel?`<span>${esc(excludedLabel)}</span>`:""}`;
      const left=index===0?L:(xAt(index-1)+x)/2,right=index===rows.length-1?W-R:(x+xAt(index+1))/2;
      return `<g class="dash-point" tabindex="0" role="group" aria-label="${esc(accessible)}" data-tip-html="${esc(tip)}"><rect class="dash-hit" x="${left.toFixed(1)}" y="${T}" width="${(right-left).toFixed(1)}" height="${PH}"/><line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${T}" y2="${T+PH}"/>${total==null?"":`<circle cx="${x.toFixed(1)}" cy="${yAt(total).toFixed(1)}" r="2.5"/>`}</g>`;
    }).join("");
    return `${excluded.length?`<p class="dash-cost-note">Daily estimate excludes ${esc(joinNames(excluded.map(p=>p.name)))} (no recorded price)</p>`:""}<div class="dash-plot"><div class="dash-axis" aria-hidden="true">${axis}</div><svg class="dash-chart${lines?" is-lines":""}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="group" aria-label="Daily ${cost?"API-equivalent cost":"tokens"} by provider">${ticks}${paths}${hover}</svg></div>${dates}`;
  }
  // The split bar sits under the figure it divides; the legend is a four-column table (provider, value, share, plan multiple) so every column aligns.
  function splitParts(data){
    const key=data.heroKey;if(!key)return null;
    const all=data.providers.map(p=>({name:p.name,value:finite(p.scoped[key])}));
    const sum=all.reduce((total,row)=>total+(row.value>0?row.value:0),0);
    if(!sum)return null;
    const bar=data.providers.length>1?`<div class="dash-split" aria-hidden="true">${all.filter(row=>row.value>0).map(row=>`<span class="${providerKey(row.name)}-fill" style="flex:${row.value}"></span>`).join("")}</div>`:"";
    const head=`<span class="dash-legend-head"></span><span class="dash-legend-head dash-legend-head-value">${key==="apiEquivalentCost"?"Cost":"Tokens"}</span><span class="dash-legend-head">Share</span><span class="dash-legend-head dash-plan-head" title="Last 30 days (calendar, incl. today) API-equivalent cost divided by the monthly plan price (default $20). It does not change with the range.">30d / plan</span>`;
    let unpriced=false;
    const items=all.map(row=>{
      const cls=`${providerKey(row.name)}-color`;
      const value=row.value==null?`<b aria-label="Unavailable">—</b><span class="dash-legend-share"></span>`:`<b>${esc(key==="apiEquivalentCost"?money(row.value):number(row.value))}</b><span class="dash-legend-share">${Math.round(100*row.value/sum)}%</span>`;
      const plan=planCellsHTML(row.name);
      if(plan.includes("some unpriced"))unpriced=true;
      return `<span class="dash-legend-item ${cls}"><span class="dash-legend-name">${esc(row.name)}</span>${value}${plan}</span>`;
    }).join("");
    return {bar,legend:`<div class="dash-split-legend">${head}${items}</div>${unpriced?'<p class="dash-plan-footnote">≥ excludes unpriced usage</p>':""}`};
  }
  // One provider has no split to compare against, so its plan multiple gets one line of its own.
  function singlePlanHTML(data){const p=data.providers[0];if(!p)return "";return `<div class="dash-plan-single ${providerKey(p.name)}-color"><span class="dash-legend-name">${esc(p.name)}</span>${planCellsHTML(p.name)}</div>`;}
  function emptyHTML(){return `<section class="dash-empty-state"><span class="dash-eyebrow">Usage</span><p>No recorded usage in this range</p></section>`;}
  // Status text for the Insights status line; background refreshes update only this element.
  function statusText(){
    if(!state.insights)return state.loading?"Loading usage…":state.loadError?"Usage could not be loaded.":"";
    const data=insightsData(false);
    const fallbackLabel=data.fallback?(state.insightsUnavailable?"Insights unavailable · Daily fallback":"Daily fallback"):"";
    return [state.loading?"Updating…":state.loadError?"Update failed":"",fallbackLabel].filter(Boolean).join(" · ");
  }
  // Failures and fallbacks are notices shown above the content; a plain freshness stamp sits at the end.
  const isNotice=()=>Boolean(state.loadError||state.insightsUnavailable||(state.insights&&insightsData(false).fallback));
  function updateStatusLine(){
    const text=statusText(),panel=$("#dashboard-insights");
    if(!panel)return;
    let el=panel.querySelector(".dash-status-line");
    if(!el){if(!text)return;panel.insertAdjacentHTML("beforeend",'<p class="dash-status-line" role="status"></p>');el=panel.querySelector(".dash-status-line");if(!el)return;}
    if(!text){el.remove();return;}
    el.textContent=text;el.classList.toggle("error",!state.insights&&state.loadError===true);el.classList.toggle("is-notice",isNotice());
  }
  function insightsHTML(){
    if(!state.insights)return `<p class="dash-status-line${state.loadError?" error":""}" role="status">${state.loading?"Loading usage…":state.loadError?"Usage could not be loaded.":""}</p>`;
    const data=insightsData(),key=data.heroKey;
    const stamp=state.insights.generatedAt?new Date(state.insights.generatedAt):null;
    const status=statusText();
    const statusHTML=status?`<p class="dash-status-line${isNotice()?" is-notice":""}" role="status">${esc(status)}</p>`:"";
    if(key==null)return statusHTML+(data.providers.length?'<section class="dash-empty-state"><span class="dash-eyebrow">Usage</span><p>No recorded measurement available</p></section>':emptyHTML());
    const cost=key==="apiEquivalentCost",value=data.totals[key],prev=finite(data.previous[key]),delta=prev!=null&&prev!==0?(value-prev)/prev*100:null;
    const figure=cost?`<span class="dash-hero-currency">$</span><strong class="dash-hero-number">${esc(money(value).replace(/^\$/,""))}</strong><span class="dash-hero-unit">estimated</span>`:tokenHero(value);
    const chart=data.range==="24h"&&!data.fallback&&!data.hourly?"":seriesChart(data),chartHTML=chart?`<section class="dash-chart-section"><div class="dash-section-head"><span class="dash-section-title">Usage over time</span><span class="dash-section-note">${data.hourly?"Hourly":"Daily"} · ${data.chartKey==="apiEquivalentCost"?"API-equivalent cost estimate":"tokens"}</span>${chipsHTML(data)}${chartStyleHTML()}</div>${chart}</section>`:"";
    // A zero stat next to non-zero tokens means the source did not report that figure, so it is hidden rather than shown as 0.
    const tokensPresent=finite(data.totals.tokens)>0;
    const missing=v=>finite(v)==null||(v===0&&tokensPresent);
    const stats=[[data.totals.sessions,v=>number(v),"Sessions"],[data.totals.subagentsActive,v=>number(v),"Subagents"],[data.totals.cacheHitRate,v=>`${Math.round(v)}%`,"Cache hits"],[data.totals.activeHours,v=>`${number(v)}h`,"Active"]].filter(([v])=>!missing(v));
    const statsHTML=stats.length?`<dl class="dash-summary">${stats.map(([v,format,label])=>`<div><dt>${label}</dt><dd>${esc(format(v))}</dd></div>`).join("")}</dl>`:"";
    const coverage=data.fallback?(data.range==="30d"||data.range==="all"?"31-day totals":data.dates.length?`Daily coverage: ${dayLabel(data.dates[0])}${data.dates.length>1?" – "+dayLabel(data.dates.at(-1)):""}${data.range==="24h"?" (latest recorded date)":" (recorded daily values)"}`: "31-day totals"):RANGE_NAME[data.range];
    const hero=`<section class="dash-hero"><div class="dash-hero-meta"><span class="dash-eyebrow">${data.providers.length===1?`${esc(data.providers[0].name)} · `:""}${cost?`API-equivalent cost estimate · ${coverage}`:`Tokens over ${coverage}`}</span>${delta==null?"":`<span class="dash-delta${delta<0?" is-down":""}">${delta<0?"↓":"↑"} ${Math.abs(delta).toFixed(1)}% <span class="muted">vs ${RANGE_PREVIOUS[data.range]}</span></span>`}</div><div class="dash-hero-number-row">${figure}</div>${data.providers.length>1?(split=>split?`${split.bar?`<div class="dash-hero-bar">${split.bar}</div>`:""}<div class="dash-hero-providers">${split.legend}</div>`:"")(splitParts(data)):singlePlanHTML(data)}</section>`;
    const models=data.models;
    // One row per model (long-context variants are merged into it). Seven rows fit beside the chart; beyond that the top six show and the rest sum into one Other row.
    const modelRows=(()=>{if(models.length<=7)return models.map(row=>({...row,family:modelKey(row.raw)}));const top=models.slice(0,6).map(row=>({...row,family:modelKey(row.raw)})),rest=models.slice(6);const sumOf=key=>{const present=rest.map(row=>finite(row[key])).filter(value=>value!=null);return present.length?present.reduce((total,value)=>total+value,0):null;};return [...top,{name:`Other · ${rest.length} models`,tokens:sumOf("tokens"),apiEquivalentCost:sumOf("apiEquivalentCost"),family:"other",other:true}];})();
    // Bars follow the active metric on a square-root scale relative to the largest row, so small models stay visible.
    // In Cost view a model with no published price gets an empty bar, not a zero-length one that reads as "free".
    const barKey=data.heroKey==="apiEquivalentCost"?"apiEquivalentCost":"tokens";
    const barMax=Math.max(0,...modelRows.map(row=>finite(row[barKey])||0));
    const modelHTML=models.length?`<div class="dash-model-block"><div class="dash-section-label dash-model-head"><span>Models</span><small aria-hidden="true"></small><small>Tokens</small><small title="API-equivalent estimate">Est. cost</small></div>${modelRows.map(row=>{const value=finite(row[barKey]),width=barMax>0&&value!=null&&value>0?Math.max(2,Math.min(100,100*Math.sqrt(value/barMax))):0,empty=barKey==="apiEquivalentCost"&&value==null;return `<div class="dash-model-row"><span class="dash-model-name ${row.family}"><i></i>${esc(row.name)}</span>${row.longContext?'<small class="dash-model-tag" title="Includes long-context usage">long ctx</small>':'<small class="dash-model-tag is-empty" aria-hidden="true"></small>'}<i class="dash-model-bar${empty?" is-empty":""}" aria-hidden="true">${empty?"":`<em class="${row.family}" style="width:${width.toFixed(1)}%"></em>`}</i><b>${esc(number(row.tokens))}</b><b title="Estimated API-equivalent cost">${finite(row.apiEquivalentCost)==null?'<span class="dash-none" role="img" aria-label="No published price">—</span>':esc(money(row.apiEquivalentCost))}</b></div>`;}).join("")}</div>`:"";
    const peaks=[...data.peaks].sort((a,b)=>(providerKey(a.name)==="codex"?0:1)-(providerKey(b.name)==="codex"?0:1));
    const peakCell=value=>value==null?'<span aria-label="Not recorded">—</span>':`${Math.round(value)}%`;
    const allMaxed=peaks.length>0&&peaks.every(p=>[p.fiveHour,p.weekly].every(value=>value!=null&&value>=99.5));
    const limits=allMaxed?`<div class="dash-limits is-collapsed"><div class="dash-section-label"><span>Highest usage</span><small title="Highest recorded quota usage in this period, not current allowance">Every window reached 100%</small></div></div>`:peaks.length?`<div class="dash-limits"><table class="dash-peaks-table"><caption>Highest recorded quota usage in this period, not current allowance</caption><thead><tr><th scope="col" title="Recorded in this period, not the current allowance">Highest usage</th><th scope="col">5h</th><th scope="col">Week</th></tr></thead><tbody>${peaks.map(p=>`<tr><th scope="row" class="${providerKey(p.name)}-color">${esc(p.name)}</th><td>${peakCell(p.fiveHour)}</td><td>${peakCell(p.weekly)}</td></tr>`).join("")}</tbody></table></div>`:"";
    const activity=statsHTML?`<div class="dash-activity"><div class="dash-section-label"><span>Activity</span></div>${statsHTML}</div>`:"";
    const side=limits||activity?`<div class="dash-side">${limits}${activity}</div>`:"";
    // Models and the side column share one row; the chart runs the full width under both. Without a chart (24h has no hourly series) the row stretches to the panel body.
    const lower=modelHTML||side?`<section class="dash-lower${modelHTML&&side?"":" is-single"}${chartHTML?"":" is-day"}">${modelHTML?`<div class="dash-lower-main">${modelHTML}</div>`:""}${side}${chartHTML}</section>`:chartHTML;
    return hero+lower+statusHTML;
  }
  // Plan multiple: 30-calendar-day (incl. today) API-equivalent cost over the plan price, per provider. It reads the 30-day window whatever range is selected, so the multiple means the same on every tab.
  const PLAN_PROVIDERS=["Claude","Codex"];
  const planPrice=name=>{const raw=Number(readPref(`branchglancePlanPrice.${name}`,""));return Number.isFinite(raw)&&raw>0?raw:20;};
  // A lower bound (unpriced models present) that rounds to zero reads "0.0", never "<0.1", which would claim an upper bound.
  const planMultiple=(value,lowerBound)=>value<.1?(lowerBound?"0.0":"<0.1"):value>=10?Math.round(value).toLocaleString("en-US"):value.toFixed(1);
  function rollingRows(){
    const sum=values=>{const present=values.map(finite).filter(value=>value!==null);return present.length?present.reduce((total,value)=>total+value,0):null;};
    return state.month.providers.map(p=>{
      const series=Array.isArray(p.series)?p.series:[];
      const unpriced=p.totals?.costPartial===true||(Array.isArray(p.totals?.unpricedModels)&&p.totals.unpricedModels.length>0)
        ||(Array.isArray(p.models)&&p.models.some(model=>model?.unpriced===true))
        ||series.some(row=>row.costPartial===true||(finite(row.tokens)>0&&finite(row.apiEquivalentCost)==null));
      return {name:String(p.name||""),tokens:sum(series.map(row=>row.tokens)),cost:sum(series.map(row=>row.apiEquivalentCost)),unpriced};
    });
  }
  // The two plan cells of one provider's legend row: the multiple, and a tiny marker. Every state fills both cells, so the columns line up.
  const planCells=(multiple,note="")=>`<span class="dash-plan-cell">${multiple}</span><span class="dash-plan-note-cell">${note}</span>`;
  function planCellsHTML(name){
    if(state.month==null&&!state.monthError)return planCells(`<span class="dash-plan-text">Loading</span>`);
    if(state.monthError||!Array.isArray(state.month?.providers))return planCells(`<span class="dash-plan-text">Unavailable</span>`);
    const row=rollingRows().find(item=>item.name===name);
    if(!row)return planCells(`<span class="dash-plan-text">Unavailable</span>`);
    if(!(row.tokens>0))return planCells(`<span class="dash-plan-text">No usage</span>`);
    if(row.cost==null)return planCells(`<span class="dash-plan-text" title="No published price for these models">Unpriced</span>`);
    const price=planPrice(name),multiple=row.cost/price,prefix=row.unpriced?"≥":"";
    return planCells(`<span title="${esc(`${row.unpriced?"≥ excludes usage with no published price. ":""}Last 30 days (calendar, incl. today) API-equivalent cost ÷ ${money(price)} plan`)}">${prefix}${esc(planMultiple(multiple,row.unpriced))}×</span>`,row.unpriced?`some unpriced`:"");
  }
  async function loadMonth(){
    const token=++state.monthToken;
    const content=value=>JSON.stringify({...value,generatedAt:undefined});
    try{
      const response=await fetch("/api/insights?range=30d",{cache:"no-store"});
      if(!response.ok)throw new Error("Rolling 30-day totals unavailable");
      const value=await response.json();
      if(token!==state.monthToken)return;
      if(!Array.isArray(value?.providers))throw new Error("Rolling 30-day totals unavailable");
      const changed=state.monthError||state.month==null||content(state.month)!==content(value);
      state.month=value;state.monthError=false;
      if(changed){state.renderKey="";render();}
    }catch{
      if(token!==state.monthToken)return;
      if(!state.monthError){state.monthError=true;state.renderKey="";render();}
    }
  }
  function setPlanPrice(name,value){
    if(!PLAN_PROVIDERS.includes(name))return;
    const number=Number(value);
    if(!Number.isFinite(number)||number<=0){state.settingsError=true;state.settingsStatus=`${name} plan price must be a number above 0.`;state.settingsRenderKey="";render();return;}
    writePref(`branchglancePlanPrice.${name}`,String(number));state.settingsError=false;state.settingsStatus="";state.settingsRenderKey="";state.renderKey="";render();
  }
  const toggleRow=(label,description,action,value)=>`<div class="dash-setting-row"><div class="dash-setting-copy"><span class="dash-setting-label">${label}</span><span class="dash-setting-description">${description}</span></div><div class="dash-setting-control"><button class="dash-toggle${value?" is-on":""}" role="switch" aria-checked="${Boolean(value)}" aria-label="${label}" data-dashboard-action="${action}"></button></div></div>`;
  const rowHTML=(label,description,control="")=>`<div class="dash-setting-row"><div class="dash-setting-copy"><span class="dash-setting-label">${label}</span><span class="dash-setting-description">${description}</span></div><div class="dash-setting-control">${control}</div></div>`;
  function settingsHTML(){
    const clock=readPref("branchglanceClockFormat","12")==="24"?"24":"12",policies=Array.isArray(state.policy?.providers)?state.policy.providers:[];
    const providers=policies.length?`<section class="dash-settings-group" aria-label="Provider permissions"><h2>Providers <span class="count">Policy controlled</span></h2>${policies.map(row=>rowHTML(esc(row.name),"",`<span class="dash-policy-flags">${["show","collect","send"].map(key=>`<span><small>${key}</small><b>${row[key]===true?"On":row[key]===false?"Off":"—"}</b></span>`).join("")}</span>`)).join("")}${(shown=>shown.length===1&&policies.length>1?`<p class="dash-setting-note dash-add-provider">Add ${esc(policies.find(row=>row.show!==true).name)} to track its agents too.</p>`:"")(policies.filter(row=>row.show===true))}<p class="dash-setting-note">Show, collect and send flags are set in the policy file.${/[\\/]/.test(String(state.policy?.policyFile||""))?` <code>${esc(state.policy.policyFile)}</code>`:""}</p></section>`:"";
    // Reset actions stays isolated in its existing frame; the dashboard never receives a settings token.
    const reset=state.settingsLoaded?`<iframe class="reset-embed" title="Auto-greet, continue and Claude tracking settings" src="/reset_actions.html?embedded=1&amp;source=${encodeURIComponent(state.source)}"></iframe>`:`<p class="dash-status">${esc(state.settingsStatus||"Loading reset settings…")}</p>`;
    const notifications=`<section class="dash-settings-group"><h2>Notifications</h2>${reset}${rowHTML("Claude tracking",`<span id="dash-claude-status">${esc(state.claudeStatus||"")}</span>`,`<button class="dash-action" data-dashboard-action="claude-settings">Change Claude tracking…</button>`)}</section>`;
    const general=`<section class="dash-settings-group"><h2>General</h2>${toggleRow("Lock widget position","Keep the widget where it is","lock",state.lock?.locked)}${toggleRow("Keep widget open","Do not collapse automatically","keep-open",state.keepOpen?.keepOpen)}${rowHTML("Clock format","Use a 12 or 24-hour clock",`<button class="dash-picker" data-dashboard-action="clock">${clock}-hour <i class="chevron" aria-hidden="true"></i></button>`)}</section>`;
    const plan=`<section class="dash-settings-group"><h2>Plan</h2><div class="dash-plan-group"><span class="dash-setting-label">Monthly plan price</span><span class="dash-setting-description">Used for the 30-day multiple</span><div class="dash-plan-fields">${PLAN_PROVIDERS.map(name=>`<label class="dash-plan-field"><span class="dash-plan-field-name">${esc(name)}</span><span aria-hidden="true">$</span><input type="number" min="1" step="any" inputmode="decimal" value="${esc(planPrice(name))}" aria-label="${esc(name)} plan price per month" data-dashboard-plan="${esc(name)}"></label>`).join("")}</div></div></section>`;
    const monitorOptions=state.monitors.map(row=>`<option value="${esc(row.id)}"${row.selected?" selected":""}>${esc(row.label||row.kind||"Monitor")}</option>`).join("");
    const display=`<section class="dash-settings-group"><h2>Display</h2>${state.monitors.length?rowHTML("Monitor","Choose where the widget opens",`<select class="dash-select" aria-label="Monitor" data-dashboard-action="monitor">${monitorOptions}</select>`):""}${rowHTML("Recenter widget","Return to the selected display",`<button class="dash-action" data-dashboard-action="recenter">Recenter</button>`)}</section>`;
    const status=state.settingsStatus?`<p class="dash-status${state.settingsError?" error":""}" role="status">${esc(state.settingsStatus)}</p>`:"";
    const version=typeof state.identity?.version==="string"&&typeof state.identity?.build==="string"?`<p class="dash-setting-note">Version ${esc(state.identity.version)}+${esc(state.identity.build)}</p>`:"";
    // Both columns fit the fixed panel body: the reset frame keeps the second column, the rest share the first.
    return `<div class="dash-settings-columns"><div class="dash-settings-column">${general}${plan}${providers}${display}</div><div class="dash-settings-column">${notifications}${status}${version}</div></div>`;
  }
  let activePoint=null,activeMode=null,tooltipBound=false,tooltipGeneration=0;
  function dismissTooltip(){
    tooltipGeneration++;activePoint=null;activeMode=null;document.querySelectorAll(".dash-point.is-hover").forEach(el=>el.classList.remove("is-hover"));const tip=$("#dash-tooltip");
    if(tip){tip.classList.remove("is-visible");tip.setAttribute("aria-hidden","true");}
  }
  function bindTooltips(){
    const root=$("#dashboard-view"),tip=$("#dash-tooltip");if(!root||!tip)return;
    const place=(x,y)=>{const margin=8,box=tip.getBoundingClientRect();tip.style.left=Math.max(margin,Math.min(x+12,window.innerWidth-margin-box.width))+"px";tip.style.top=Math.max(margin,Math.min(y+12,window.innerHeight-margin-box.height))+"px";};
    const show=(target,x,y,mode)=>{activePoint=target;activeMode=mode;document.querySelectorAll(".dash-point.is-hover").forEach(el=>el.classList.remove("is-hover"));target.classList?.add("is-hover");tip.innerHTML=target.getAttribute("data-tip-html")||"";tip.classList.add("is-visible");tip.setAttribute("aria-hidden","false");place(x,y);};
    root.addEventListener("pointerover",event=>{const target=event.target?.closest?.("[data-tip-html]");if(target&&activeMode!=="focus")show(target,event.clientX,event.clientY,"pointer");});
    root.addEventListener("focusin",event=>{const target=event.target?.closest?.("[data-tip-html]");if(target){const box=target.getBoundingClientRect();show(target,box.right,box.bottom,"focus");}});
    root.addEventListener("pointerout",event=>{if(activeMode==="pointer"&&event.target?.closest?.("[data-tip-html]")===activePoint&&event.relatedTarget?.closest?.("[data-tip-html]")!==activePoint)dismissTooltip();});
    root.addEventListener("focusout",event=>{if(activeMode==="focus"&&event.target?.closest?.("[data-tip-html]")===activePoint&&event.relatedTarget?.closest?.("[data-tip-html]")!==activePoint)dismissTooltip();});
    root.addEventListener("pointermove",event=>{if(activeMode==="pointer")place(event.clientX,event.clientY);});
    root.addEventListener("scroll",()=>{
      const focused=activeMode==="focus"?activePoint:null;dismissTooltip();const generation=tooltipGeneration;
      if(focused)requestAnimationFrame(()=>{if(generation===tooltipGeneration&&state.open&&document.activeElement===focused){const box=focused.getBoundingClientRect();show(focused,box.right,box.bottom,"focus");}});
    },true);
    document.addEventListener("keydown",event=>{if(event.key==="Escape")dismissTooltip();});
  }
  function bindTooltipsOnce(){if(tooltipBound)return;tooltipBound=true;bindTooltips();}
  function cancelFade(){
    state.fadeVersion++;if(state.fadeTimer!=null)clearTimeout(state.fadeTimer);state.fadeTimer=null;$("#dashboard-content")?.classList.remove("is-switching");
  }
  function crossFade(change){
    dismissTooltip();cancelFade();const content=$("#dashboard-content"),generation=state.fadeVersion;
    if(!content||reducedMotion()){change();return;}
    content.classList.add("is-switching");
    state.fadeTimer=setTimeout(()=>{state.fadeTimer=null;if(generation!==state.fadeVersion||!state.open)return;change();requestAnimationFrame(()=>{if(generation===state.fadeVersion&&state.open)content.classList.remove("is-switching");});},90);
  }
  // The 24h view has no hourly chart, so its panel is shorter. The widget window follows (see windowHeight).
  const compactPanel=()=>{
    if(state.tab!=="insights"||state.range!=="24h"||!state.insights)return false;
    const data=insightsData(false);
    return !(data.fallback||data.hourly);
  };
  const DASHBOARD_HEIGHT=608,COMPACT_DASHBOARD_HEIGHT=478;
  const windowHeight=()=>compactPanel()?COMPACT_DASHBOARD_HEIGHT:DASHBOARD_HEIGHT;
  function syncSize(){
    const size=compactPanel()?"compact":"full",root=$("#dashboard-view");
    if(root)root.dataset.size=size;
    if(state.sizeKey!==size){state.sizeKey=size;if(typeof Event==="function")window.dispatchEvent?.(new Event("dashboard-size"));}
  }
  function syncTabs(){
    const root=$("#dashboard-view");if(root)root.dataset.tab=state.tab;syncSize();
    for(const key of ["insights","settings"]){const button=$("#dashboard-tab-"+key),panel=$("#dashboard-"+key);if(button){button.classList.toggle("is-active",key===state.tab);button.setAttribute("aria-selected",String(key===state.tab));}if(panel)panel.hidden=key!==state.tab;}
  }
  function syncSegments(){
    document.querySelectorAll("[data-dashboard-range]").forEach(button=>{const active=button.dataset.dashboardRange===state.range;button.classList.toggle("is-active",active);button.setAttribute("aria-pressed",String(active));});
    const unpriced=state.insights!=null&&!costAvailable();
    document.querySelectorAll("[data-dashboard-metric]").forEach(button=>{const metric=button.dataset.dashboardMetric,active=metric===state.metric,blocked=metric==="cost"&&unpriced;button.classList.toggle("is-active",active);button.classList.toggle("is-unavailable",blocked);button.setAttribute("aria-pressed",String(active));button.setAttribute("aria-disabled",String(blocked));button.title=blocked?"No published pricing for these models":"";});
  }
  function selectTab(name){
    if(name!=="insights"&&name!=="settings")return;
    cancelFade();if(name===state.tab)return;
    crossFade(()=>{state.tab=name;syncTabs();state.renderKey="";render();if(name==="settings"&&!state.settingsLoaded)void loadSettings();});
  }
  function setRange(range){
    if(!isRange(range))return;
    cancelFade();if(range===state.range)return;
    crossFade(()=>{state.range=range;writePref("branchglanceInsightsRange",range);syncSegments();syncSize();state.renderKey="";void loadInsights();render();});
  }
  function setMetric(metric){
    if(!["tokens","cost"].includes(metric)||metric===state.metric)return;
    if(metric==="cost"&&state.insights&&!costAvailable())return;
    dismissTooltip();state.metricExplicit=true;state.metric=metric;writePref("branchglanceInsightsMetric",metric);syncSegments();state.renderKey="";render();
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
    const insightsKey=[state.version,state.source,state.range,state.metric,state.tab,chartStyle()].join("|");
    if(insightsKey!==state.renderKey){dismissTooltip();state.renderKey=insightsKey;const insights=$("#dashboard-insights");if(insights)insights.innerHTML=insightsHTML();}
    const settings=$("#dashboard-settings");
    if(settings&&state.tab==="settings"){
      const settingsKey=[state.source,state.policy?.generation,state.identity?.version,state.identity?.build,state.settingsLoaded,state.settingsStatus,state.settingsBusy,state.settingsError,state.lock?.revision,state.keepOpen?.keepOpenRevision,readPref("branchglanceClockFormat","12"),state.monitors.map(m=>`${m.id}:${m.label}:${m.selected}`).join("|")].join("|");
      if(settingsKey!==state.settingsRenderKey){state.settingsRenderKey=settingsKey;settings.innerHTML=settingsHTML();state.frame=$("#dashboard-settings iframe");}
    }
    bindTooltipsOnce();
    syncSize();
  }
  function open({source,tab}={}){
    state.open=true;state.tab=tab==="settings"?"settings":"insights";if(typeof source==="string")state.source=source;
    state.range=readPref("branchglanceInsightsRange","30d");if(!isRange(state.range))state.range="30d";
    state.metricExplicit=readPref("branchglanceInsightsMetric",null)!==null;state.metric=readPref("branchglanceInsightsMetric","cost");if(!["tokens","cost"].includes(state.metric))state.metric="cost";
    const root=$("#dashboard-view");if(root){root.hidden=false;root.inert=false;root.setAttribute("aria-hidden","false");}
    syncTabs();syncSegments();render();void loadInsights();void loadMonth();if(state.tab==="settings"&&!state.settingsLoaded)void loadSettings();
    if(state.refreshTimer!=null)clearInterval(state.refreshTimer);
    state.refreshTimer=setInterval(()=>{if(state.open&&!state.loading)void loadInsights();void loadMonth();},120000);
  }
  function close(){
    state.open=false;state.loadToken++;state.loading=false;state.fadeVersion++;if(state.fadeTimer!=null)clearTimeout(state.fadeTimer);state.fadeTimer=null;if(state.refreshTimer!=null)clearInterval(state.refreshTimer);state.refreshTimer=null;dismissTooltip();
    $("#dashboard-content")?.classList.remove("is-switching");const root=$("#dashboard-view");if(root){root.hidden=true;root.inert=true;root.setAttribute("aria-hidden","true");}
    const frame=$("#dashboard-settings iframe");if(frame)frame.src="about:blank";
    state.frame=null;state.settingsLoaded=false;state.settingsRenderKey="";
  }
  function normalizeStats(snapshot){
    const providers=Array.isArray(snapshot?.providers)?snapshot.providers:[];
    return {fallback:true,generatedAt:snapshot?.generatedAt||null,providers:providers.map(row=>{const cache=row?.cache||{};return {name:row?.name,totals:{tokens:finite(row?.tracked31Days),apiEquivalentCost:finite(row?.apiEquivalentCost??cache.apiEquivalentCost),sessions:finite(row?.sessions),subagentsActive:finite(row?.subagentsActive),activeHours:finite(row?.activeHours),cacheHitRate:finite(row?.cacheHitRate??cache.percent)},previous:null,series:(Array.isArray(row?.activity)?row.activity:[]).map(item=>({date:item.date,tokens:finite(item.value??item.tokens),apiEquivalentCost:finite(item.apiEquivalentCost)})).filter(item=>/^\d{4}-\d\d-\d\d$/.test(item.date||"")),models:(Array.isArray(row?.modelUsage)?row.modelUsage:[]).map(item=>({name:item?.model,tokens:finite(item?.tokens),apiEquivalentCost:finite(item?.apiEquivalentCost)})),limitPeaks:row?.limitPeaks||null};})};
  }
  async function loadInsights(){
    const token=++state.loadToken,requestRange=state.range;
    state.loading=true;state.loadError=false;updateStatusLine();
    const current=()=>token===state.loadToken;
    const accept=(value,failed=false,unavailable=false)=>{
      // Same range and same content (ignoring the per-response generatedAt stamp): keep the rendered panel and refresh only the status line.
      const content=v=>JSON.stringify({...v,generatedAt:undefined});
      const same=state.insights!=null&&state.resultRange===requestRange&&state.insights.fallback===value.fallback&&content(state.insights)===content(value);
      state.loading=false;state.loadError=failed;state.insightsUnavailable=unavailable;
      if(same){state.insights.generatedAt=value.generatedAt;updateStatusLine();return value;}
      state.resultRange=requestRange;state.resultValue=value;setInsights(value);state.renderKey="";render();return value;
    };
    try{
      let value=null;
      try{
        const response=await fetch("/api/insights?range="+encodeURIComponent(requestRange),{cache:"no-store"});
        if(!current())return state.insights;
        if(response.ok){const candidate=await response.json();if(!current())return state.insights;if(Array.isArray(candidate?.providers)&&(candidate.range==null||candidate.range===requestRange))value=candidate;}
      }catch{}
      if(!current())return state.insights;
      if(value){state.statsSnapshot=null;return accept(value);}
      // Keep a trusted result on failed refresh; a fallback cannot replace its coverage.
      if(state.insights&&!state.insights.fallback)throw new Error("Insights unavailable");
      try{
        const fallback=await fetch("/api/stats?background=1",{cache:"no-store"});if(!current())return state.insights;
        if(fallback.ok){const stats=await fallback.json();if(!current())return state.insights;state.statsSnapshot=stats;return accept(normalizeStats(stats),false,true);}
      }catch{}
      if(!current())return state.insights;
      if(state.insights)throw new Error("Insights unavailable");
      if(state.snapshot?.providers){state.statsSnapshot=state.snapshot;return accept(normalizeStats(state.snapshot),false,true);}
      throw new Error("Insights unavailable");
    }catch{
      if(!current())return state.insights;
      state.loading=false;state.loadError=true;updateStatusLine();return state.insights;
    }
  }
  function setSnapshot(snapshot){
    state.snapshot=snapshot;
    if(state.open&&!state.loading&&(!state.insights||state.insights.fallback)){state.statsSnapshot=snapshot;const value=normalizeStats(snapshot);state.resultRange=state.range;state.resultValue=value;setInsights(value);render();}
  }
  function setSource(source){if(typeof source==="string"&&source!==state.source){state.source=source;state.renderKey="";render();}}
  document.addEventListener("click",event=>{
    const tab=event.target?.closest?.("[data-dashboard-tab]");if(tab){selectTab(tab.dataset.dashboardTab);return;}
    const provider=event.target?.closest?.("[data-dashboard-provider]");if(provider){toggleProvider(provider.dataset.dashboardProvider);return;}
    const range=event.target?.closest?.("[data-dashboard-range]");if(range){setRange(range.dataset.dashboardRange);return;}
    const metric=event.target?.closest?.("[data-dashboard-metric]");if(metric){setMetric(metric.dataset.dashboardMetric);return;}
    const style=event.target?.closest?.("[data-dashboard-chart-style]");if(style){setChartStyle(style.dataset.dashboardChartStyle);return;}
    const action=event.target?.closest?.("[data-dashboard-action]");if(!action)return;
    const name=action.dataset.dashboardAction;
    if(name==="close")window.tokenTrackerCloseDashboard?.();
    else if(name==="lock"||name==="keep-open")void toggleSetting(name);
    else if(name==="clock")toggleClock();
    else if(name==="recenter")void window.tokenTrackerRecenter?.();
    else if(name==="claude-settings")void window.pywebview?.api?.open_reset_actions?.();
  });
  document.addEventListener("change",event=>{const target=event.target;if(target?.dataset?.dashboardAction==="monitor")void chooseMonitor(target.value);if(target?.dataset?.dashboardPlan)setPlanPrice(target.dataset.dashboardPlan,target.value);});
  window.addEventListener("message",event=>{
    if(event.origin!==location.origin||event.source!==state.frame?.contentWindow)return;
    if(event.data?.type==="dashboard-reset-cancel")window.tokenTrackerCloseDashboard?.();
    // The frame reports the Claude Code status; it shows in the "Claude tracking" row, not inside the frame.
    if(event.data?.type==="dashboard-claude-status"){state.claudeStatus=String(event.data.text||"").slice(0,120);const node=$("#dash-claude-status");if(node)node.textContent=state.claudeStatus;}
  });
  window.addEventListener("load",()=>{state.frame=$("#dashboard-settings iframe");});
  window.TokenTrackerDashboard={open,close,windowHeight,setSource,setSnapshot,selectTab,render,loadSettings,state};
  if(window.__TOKEN_TRACKER_DASHBOARD_TEST__)window.__tokenTrackerDashboardTest={insightsData,seriesChart,curvePath,insightsHTML,settingsHTML,normalizeStats,emptyHTML,planCellsHTML,loadMonth,setPlanPrice,setSnapshot,setSource,setRange,setMetric,setChartStyle,setClockFormat,loadInsights,state};
})();
