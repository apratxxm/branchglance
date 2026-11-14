(() => {
  const $=selector=>document.querySelector(selector),DAY=86400000;
  const RANGE_DAYS={"24h":1,"7d":7,"30d":30,all:31};
  const RANGE_NAME={"24h":"24 hours","7d":"7 days","30d":"30 days",all:"31 days"};
  const RANGE_PREVIOUS={"24h":"previous day","7d":"previous week","30d":"previous 30 days",all:"previous period"};
  const state={open:false,opener:null,tab:"insights",source:"Codex",range:"30d",metric:"cost",snapshot:null,statsSnapshot:null,insights:null,version:0,loadToken:0,policy:null,monitors:[],lock:null,keepOpen:null,settingsLoaded:false,settingsBusy:false,frame:null,renderKey:"",settingsRenderKey:"",settingsStatus:"",settingsError:false,month:null,monthError:false,monthToken:0,resultRange:null,resultValue:null,loading:false,loadError:false,refreshTimer:null,fadeTimer:null,fadeVersion:0,togglePending:new Set(),toggleErrors:new Set(),focusPlanPrice:false};
  const esc=value=>String(value??"").replace(/[&<>"']/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const finite=value=>typeof value==="number"&&Number.isFinite(value)?value:null;
  const isRange=value=>Object.prototype.hasOwnProperty.call(RANGE_DAYS,value);
  const readPref=(key,fallback)=>{try{return localStorage.getItem(key)??fallback;}catch{return fallback;}};
  const writePref=(key,value)=>{try{localStorage.setItem(key,value);}catch{}};
  const clearPref=key=>{try{localStorage.removeItem(key);}catch{}};
  const storedRange=()=>{let value=readPref("branchglanceInsightsRange","30d");if(value==="all"){value="30d";writePref("branchglanceInsightsRange",value);}return isRange(value)?value:"30d";};
  const reducedMotion=()=>window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches===true;
  const nfmt=new Intl.NumberFormat("en",{notation:"compact",maximumSignificantDigits:3});
  const usd=new Intl.NumberFormat("en-US",{style:"currency",currency:"USD",maximumFractionDigits:2});
  const number=value=>finite(value)==null?"":nfmt.format(value);
  const money=value=>finite(value)==null?"":usd.format(value);
  const localDate=date=>`${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,"0")}-${String(date.getDate()).padStart(2,"0")}`;
  const dayLabel=value=>{const text=String(value||""),stamp=text.includes("T"),date=new Date(stamp?text:`${text}T12:00:00`);if(Number.isNaN(date.getTime()))return text;const clock=stamp?(clockPref()==="24"?{hour:"2-digit",minute:"2-digit",hourCycle:"h23"}:{hour:"numeric",hourCycle:"h12"}):{};return new Intl.DateTimeFormat("en-US",{month:"short",day:"numeric",...clock}).format(date);};
  // Short daily label for up to eight days: "Sat 8".
  const weekdayLabel=value=>{const date=new Date(`${String(value||"")}T12:00:00`);return Number.isNaN(date.getTime())?dayLabel(value):`${new Intl.DateTimeFormat("en-US",{weekday:"short"}).format(date)} ${date.getDate()}`;};
  // Range line: "Nov 8 – 14" inside a month, "Oct 28 – Nov 3" across months; stamped hours keep their own labels.
  const rangeLabel=dates=>{const first=String(dates[0]||""),last=String(dates.at(-1)||"");if(!first)return "";if(first===last)return dayLabel(first);if(first.includes("T"))return `${dayLabel(first)} – ${dayLabel(last)}`;const a=new Date(`${first}T12:00:00`),b=new Date(`${last}T12:00:00`);if(Number.isNaN(a.getTime())||Number.isNaN(b.getTime()))return `${dayLabel(first)} – ${dayLabel(last)}`;const month=new Intl.DateTimeFormat("en-US",{month:"short"});return a.getMonth()===b.getMonth()&&a.getFullYear()===b.getFullYear()?`${month.format(a)} ${a.getDate()} – ${b.getDate()}`:`${month.format(a)} ${a.getDate()} – ${month.format(b)} ${b.getDate()}`;};
  // Short weekday for chart tips: "Sat".
  const weekdayOf=value=>{const text=String(value||""),date=new Date(text.includes("T")?text:`${text}T12:00:00`);return Number.isNaN(date.getTime())?"":new Intl.DateTimeFormat("en-US",{weekday:"short"}).format(date);};
  const modelName=raw=>String(window.TokenTrackerFormatModel?.({model:raw})||raw||"");
  const providerKey=name=>/claude/i.test(String(name||""))?"claude":"codex";
  const modelKey=raw=>/^gpt-/i.test(String(raw||""))?"codex":"claude";
  const availableProviders=()=>Array.isArray(state.insights?.providers)?state.insights.providers:[];
  // Cost is available only when the 31-day or range totals carry a published price; it never comes from a per-day series alone.
  const costAvailable=()=>insightsData(false).totals.apiEquivalentCost!=null;
  const setInsights=value=>{state.insights=value;state.version++;const before=state.metric;if(state.metric==="cost"&&(!costAvailable()||(!state.metricExplicit&&insightsData(false).mostlyUnpriced)))state.metric="tokens";if(state.metric!==before)writePref("branchglanceInsightsMetric",state.metric);syncSegments();};
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
    const fallback=state.insights?.fallback===true,range=state.resultValue===state.insights?(state.resultRange||state.range):(isRange(state.insights?.range)?state.insights.range:state.range),allProviders=availableProviders().filter(p=>!hiddenProviders.has(p.name));
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
    if(!windowed)for(const p of allProviders)for(const row of p.models||[]){const raw=String(row?.name||"");if(!raw)continue;const label=modelName(raw)||raw,longContext=/\s*·\s*long context$/i.test(label),name=label.replace(/\s*·\s*long context$/i,""),item=modelMap.get(name)||{name,raw,tokens:null,apiEquivalentCost:null,longContext:false,partial:false};item.longContext=item.longContext||longContext;item.partial=item.partial||(row.unpriced===true&&finite(row.apiEquivalentCost)!=null);for(const k of ["tokens","apiEquivalentCost"]){const value=finite(row[k]);if(value!=null)item[k]=(item[k]??0)+value;}modelMap.set(name,item);}
    const sortKey=heroKey||"tokens";
    const models=[...modelMap.values()].filter(row=>row.tokens!=null||row.apiEquivalentCost!=null).sort((a,b)=>(b[sortKey]||0)-(a[sortKey]||0));
    const costPartial=allProviders.some(p=>p.totals?.costPartial===true||finite(p.totals?.apiEquivalentCost)==null);
    const unpricedModels=[...new Set(allProviders.flatMap(p=>Array.isArray(p.totals?.unpricedModels)?p.totals.unpricedModels:[]).map(name=>modelName(String(name))||String(name)).filter(Boolean))];
    const peaks=allProviders.map(p=>({name:p.name,fiveHour:finite(p.limitPeaks?.fiveHour),weekly:finite(p.limitPeaks?.weekly)})).filter(p=>p.fiveHour!=null||p.weekly!=null);
    const unpricedTokens=models.filter(row=>row.apiEquivalentCost==null&&row.tokens!=null).reduce((sum,row)=>sum+row.tokens,0);
    const tokenDenominator=finite(totals.tokens)??models.reduce((sum,row)=>sum+(finite(row.tokens)||0),0);
    const mostlyUnpriced=tokenDenominator>0&&unpricedTokens>tokenDenominator/2;
    const partialModels=allProviders.some(p=>p.totals?.costPartial===true&&finite(p.totals?.apiEquivalentCost)!=null);return {partialModels,fallback,range,hourly,providers,totals,previous,seriesByKey,series:seriesByKey[chartKey],chartKey,heroKey,costHero,costPartial,unpricedModels,mostlyUnpriced,models,peaks,dates};
  }
  const joinNames=names=>names.length<=2?names.join(" and "):`${names.slice(0,-1).join(", ")} and ${names.at(-1)}`;
  const tokenHero=value=>{const [figure,suffix]=value>=1e9?[(value/1e9).toFixed(2),"B"]:value>=1e6?[(value/1e6).toFixed(1),"M"]:value>=1e3?[(value/1e3).toFixed(1),"K"]:[String(Math.round(value)),""];return `<strong class="dash-hero-number">${esc(figure)}${suffix?`<span class="dash-hero-mag">${suffix}</span>`:""}</strong><span class="dash-hero-unit">tokens</span>`;};
  // Provider chips scope all Insights sections wherever provider identity is available.
  const hiddenProviders=new Set((()=>{try{const value=JSON.parse(localStorage.getItem("branchglanceHiddenProviders")||"[]");return Array.isArray(value)?value:[];}catch{return [];}})());
  // At least one provider stays on; the last one cannot be hidden.
  function toggleProvider(name){
    if(!name)return;
    if(hiddenProviders.has(name))hiddenProviders.delete(name);
    else if(availableProviders().some(p=>p.name!==name&&!hiddenProviders.has(p.name)))hiddenProviders.add(name);
    else return;
    writePref("branchglanceHiddenProviders",JSON.stringify([...hiddenProviders]));if(state.metric==="cost"&&state.insights&&!costAvailable())state.metric="tokens";syncSegments();state.renderKey="";render();
  }
  // Chart style: "stacked" (default) or "lines". Chosen with the small control in the chart header and remembered in localStorage.
  const chartStyle=()=>readPref("branchglanceChartStyle","stacked")==="lines"?"lines":"stacked";
  // Clock format for chart labels: "24" gives 14:00, "12" gives 2 PM. Read live; the Insights key includes it, so a change re-labels the panel.
  const clockPref=()=>readPref("branchglanceClockFormat","12")==="24"?"24":"12";
  const clockLabel=hour=>clockPref()==="24"?`${String(hour).padStart(2,"0")}:00`:`${hour%12===0?12:hour%12} ${hour<12?"AM":"PM"}`;
  function setChartStyle(value){
    if(value!=="stacked"&&value!=="lines")return;
    if(value===chartStyle())return;
    dismissTooltip();writePref("branchglanceChartStyle",value);state.renderKey="";render();
  }
  const chartStyleHTML=()=>{const current=chartStyle();return `<div class="dash-chart-style" role="group" aria-label="Chart style">${[["lines","Lines"],["stacked","Stacked"]].map(([key,label])=>`<button type="button" data-dashboard-chart-style="${key}" aria-pressed="${current===key}">${label}</button>`).join("")}</div>`;};
  // Explicit provider filter chips beside the chart heading: a dot, the name and a checked state.
  const chipsHTML=data=>{
    const providers=availableProviders();if(providers.length<2)return "";
    return `<div class="dash-chips" role="group" aria-label="Providers included in all available Insights sections">${providers.map(p=>{const on=!hiddenProviders.has(p.name),locked=on&&providers.filter(q=>!hiddenProviders.has(q.name)).length===1;return `<button type="button" class="dash-chip ${providerKey(p.name)}-color" data-dashboard-provider="${esc(p.name)}" aria-pressed="${on}"${locked?' aria-disabled="true"':""} title="${locked?"At least one provider stays shown":`${on?"Hide":"Show"} ${esc(p.name)} across Insights`}"><i class="dash-dot ${providerKey(p.name)}" aria-hidden="true"></i>${esc(p.name)}<span class="dash-chip-check" aria-hidden="true">${on?"\u2713":""}</span></button>`;}).join("")}</div>`;
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
    if(!providers.length)return `<p class="dash-chart-empty">Every provider is hidden. Click a provider in the split to show it.</p>`;
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
      tickLabels=[...marks.map(i=>[i,clockLabel(hourOf(rows[i].date))]),[last,"now"]];
    }else{
      // Up to eight days: every day is labelled. Longer ranges keep four evenly spaced ticks.
      const every=rows.length<=8,idx=every?rows.map((_,i)=>i):[0,Math.round((rows.length-1)/3),Math.round((rows.length-1)*2/3),rows.length-1];
      tickLabels=idx.map(i=>[i,every?weekdayLabel(rows[i].date):dayLabel(rows[i].date)]);
    }
    const dates=`<div class="dash-dates">${tickLabels.map(([i,label],n)=>`<span class="dash-date${n===0&&i===0?" is-first":i===rows.length-1&&n>0?" is-last":""}" style="left:${(rows.length>1?i/(rows.length-1)*100:0).toFixed(2)}%">${esc(label)}</span>`).join("")}</div>`;
    const pointLabel=date=>data.hourly?`${weekdayOf(date)}, ${clockLabel(hourOf(date))}`:`${weekdayOf(date)}, ${dayLabel(date)}`;
    const hover=rows.map((row,index)=>{
      const x=xAt(index),total=totals[index],partial=cost&&(excluded.length>0||providers.some(p=>row[p.name+"__partial"])),unknown=total==null;
      const totalLabel=unknown?"Total unavailable":(partial?"Recorded subtotal: ":"Total: ")+format(total);
      const parts=providers.map(p=>`${p.name}: ${finite(row[p.name])==null?"unavailable":format(row[p.name])+(cost&&row[p.name+"__partial"]?" (partial estimate)":"")}`);
      const excludedLabel=excluded.length?`Excludes ${joinNames(excluded.map(p=>p.name))}: no recorded price`:"";
      const accessible=`${row.date}. ${totalLabel}. ${parts.join(". ")}${unknown?". Missing provider data":""}${excludedLabel?". "+excludedLabel:""}`;
      const tip=`<span class="tip-date">${esc(pointLabel(row.date))}</span><b class="tip-total">${esc(unknown?"—":(partial?"Recorded subtotal · ":"")+format(total))}</b>${providers.map(p=>`<span class="tip-row"><span><i class="dash-dot ${providerKey(p.name)}"></i>${esc(p.name)}</span><strong>${esc(finite(row[p.name])==null?"—":format(row[p.name])+(cost&&row[p.name+"__partial"]?" (partial)":""))}</strong></span>`).join("")}${unknown?'<span>Missing provider data</span>':""}${excludedLabel?`<span>${esc(excludedLabel)}</span>`:""}`;
      const left=index===0?L:(xAt(index-1)+x)/2,right=index===rows.length-1?W-R:(x+xAt(index+1))/2;
      const markers=lines?providers.map(p=>finite(row[p.name])==null?"":`<circle class="dash-provider-marker ${providerKey(p.name)}" cx="${x.toFixed(1)}" cy="${yAt(row[p.name]).toFixed(1)}" r="2.8"/>`).join(""):total==null?"":`<circle cx="${x.toFixed(1)}" cy="${yAt(total).toFixed(1)}" r="2.5"/>`;
      return `<g class="dash-point" tabindex="${index===rows.length-1?"0":"-1"}" role="group" aria-label="${esc(accessible)}" data-tip-html="${esc(tip)}"><rect class="dash-hit" x="${left.toFixed(1)}" y="${T}" width="${(right-left).toFixed(1)}" height="${PH}"/><line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${T}" y2="${T+PH}"/>${markers}</g>`;
    }).join("");
    return `<div class="dash-plot"><div class="dash-axis" aria-hidden="true">${axis}</div><svg class="dash-chart${lines?" is-lines":""}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="group" aria-label="Daily ${cost?"API-equivalent cost":"tokens"} by provider">${ticks}${paths}${hover}</svg></div>${dates}`;
  }
  // The split bar sits under the figure it divides; the legend is a four-column table (provider, value, share, plan multiple) so every column aligns.
  function splitParts(data){
    const key=data.heroKey;if(!key)return null;
    const all=data.providers.map(p=>({name:p.name,value:finite(p.scoped[key])}));
    const sum=all.reduce((total,row)=>total+(row.value>0?row.value:0),0);
    if(!sum)return null;
    const bar=data.providers.length>1?`<div class="dash-split" aria-hidden="true">${all.filter(row=>row.value>0).map(row=>`<span class="${providerKey(row.name)}-fill" style="flex:${row.value}"></span>`).join("")}</div>`:"";
    const head=`<span class="dash-legend-head"></span><span class="dash-legend-head dash-legend-head-value">${key==="apiEquivalentCost"?"Cost":"Tokens"}</span><span class="dash-legend-head">Share</span><span class="dash-legend-head dash-plan-head">30d vs plan <span class="sr-only">30-day API-equivalent estimate divided by the monthly plan price; independent of the selected range.</span></span>`;
    let unpriced=false;
    const items=all.map(row=>{
      const cls=`${providerKey(row.name)}-color`;
      const value=row.value==null?`<b aria-label="Unavailable">—</b><span class="dash-legend-share"></span>`:`<b>${esc(key==="apiEquivalentCost"?money(row.value):number(row.value))}</b><span class="dash-legend-share">${Math.round(100*row.value/sum)}%</span>`;
      const plan=planCellsHTML(row.name);
      if(plan.includes("some unpriced"))unpriced=true;
      return `<span class="dash-legend-item ${cls}"><span class="dash-legend-name">${esc(row.name)}</span>${value}${plan}</span>`;
    }).join("");
    return {bar,legend:`<div class="dash-split-legend">${head}${items}</div>${unpriced?'<p class="dash-plan-footnote">At least means some usage has no published price, so the real figure is higher.</p>':""}`};
  }
  // One provider has no split to compare against, so its plan multiple gets one line of its own.
  function singlePlanHTML(data){const p=data.providers[0];if(!p)return "";return `<div class="dash-plan-single ${providerKey(p.name)}-color"><span class="dash-legend-name">${esc(p.name)}</span><span class="dash-plan-figure"><span class="dash-plan-label">30d vs plan <span class="sr-only">30-day API-equivalent estimate divided by the monthly plan price; independent of the selected range.</span></span>${planCellsHTML(p.name)}</span></div>`;}
  function emptyHTML(){return `<section class="dash-empty-state"><span class="dash-eyebrow">Usage</span><p>No recorded usage in this range</p></section>`;}
  // Clock-format time of day for a payload stamp ("2:41 PM" or "14:41"); empty when the stamp is missing.
  const clockTime=value=>{const date=value?new Date(value):null;if(!date||Number.isNaN(date.getTime()))return "";return date.toLocaleTimeString("en-US",{hour:"numeric",minute:"2-digit",hour12:clockPref()!=="24"}).replace(/\u202f/g," ");};
  // Status text for the Insights status line; background refreshes update only this element.
  function statusText(){
    if(!state.insights)return state.loading?"Loading usage…":state.loadError?"Usage could not be loaded.":"";
    const data=insightsData(false),fallbackLabel=data.fallback?(state.insightsUnavailable?"Insights unavailable \u00b7 Daily fallback":"Daily fallback"):"";
    const generated=state.insights.generatedAt?new Date(state.insights.generatedAt).getTime():NaN;
    const age=Number.isFinite(generated)?Math.max(0,Math.floor((Date.now()-generated)/60000)):null;
    const freshness=age==null?"":`Updated ${age<1?"just now":`${age}m ago`}`;
    // A failed refresh with data on screen names the data it still shows; that stamp replaces the freshness line.
    const shownAt=clockTime(state.insights.generatedAt),failed=shownAt?`Update failed \u00b7 showing data from ${shownAt}. Use Refresh to retry.`:"Update failed. Use Refresh to retry.";
    if(state.loadError&&!state.loading)return [failed,fallbackLabel].filter(Boolean).join(" \u00b7 ");
    return [state.loading?"Updating…":"",fallbackLabel,freshness].filter(Boolean).join(" \u00b7 ");
  }
  // Failures and fallbacks are notices shown above the content; a plain freshness stamp sits at the end.
  const isNotice=()=>Boolean(state.loadError||state.insightsUnavailable||(state.insights&&insightsData(false).fallback));
  // The header Refresh button is disabled and busy while Insights loads.
  function syncRefresh(){const button=$("#dashboard-refresh");if(!button)return;const busy=state.loading===true;button.disabled=busy;button.setAttribute("aria-busy",String(busy));const text=busy?"Refreshing…":"Refresh";if(button.textContent!==text)button.textContent=text;}
  function updateStatusLine(){
    const text=statusText(),panel=$("#dashboard-insights");
    if(!panel)return;
    let el=panel.querySelector(".dash-status-line");
    if(!el){if(!text)return;panel.insertAdjacentHTML("beforeend",'<p class="dash-status-line" role="status"></p>');el=panel.querySelector(".dash-status-line");if(!el)return;}
    if(!text){el.remove();return;}
    el.textContent=text;el.classList.toggle("error",!state.insights&&state.loadError===true);el.classList.toggle("is-notice",isNotice());
  }
  // Quota: the widget's current 5-hour and weekly figures per provider (window.tokenTrackerQuotaNow), for the providers the chips show. Recorded peaks stand in when no live reading is usable.
  const quotaRows=()=>{let rows=null;try{rows=window.tokenTrackerQuotaNow?.();}catch{rows=null;}if(!Array.isArray(rows))return null;const usable=rows.filter(row=>row&&typeof row.name==="string"&&!hiddenProviders.has(row.name)&&finite(row.usedPercent)!=null);return usable.length?usable:null;};
  const peakFor=(data,name)=>finite(data.peaks.find(p=>p.name===name)?.fiveHour);
  const quotaValue=(row,key)=>key==="weekly"?finite(row.weekly?.usedPercent):finite(row.usedPercent);
  const quotaSub=(row,key,data)=>{if(key==="weekly")return row.weekly?.countdown?`resets ${row.weekly.countdown}`:"";const peak=peakFor(data,row.name);return [row.countdown?`resets ${row.countdown}`:"",peak!=null?`peak ${Math.round(peak)}%`:""].filter(Boolean).join(" · ");};
  // The shape names the providers and marks a provider with no weekly reading; a change in either re-renders the block.
  const quotaShape=rows=>rows.map(row=>`${row.name}${quotaValue(row,"weekly")==null?"!":""}`).join("|");
  const quotaCell=(row,key,data)=>{const value=quotaValue(row,key);if(value==null)return '<td><span aria-hidden="true">—</span><span class="sr-only">Not available</span></td>';const attrs=`data-quota-key="${esc(row.name)}" data-quota-window="${key}"`;return `<td><span class="dash-quota-main" ${attrs} data-quota-part="percent">${Math.round(value)}%</span><small class="dash-quota-sub" ${attrs} data-quota-part="sub">${esc(quotaSub(row,key,data))}</small></td>`;};
  function limitsHTML(data){
    const rows=quotaRows();
    if(rows)return `<div class="dash-limits dash-quota" data-quota-shape="${esc(quotaShape(rows))}"><div class="dash-section-label"><span>Quota</span><small>Now</small></div><table class="dash-peaks-table dash-quota-table"><caption class="sr-only">Current quota used per provider, 5-hour and week</caption><thead><tr><th scope="col"><span class="sr-only">Provider</span></th><th scope="colgroup" colspan="2" class="dash-quota-head">5h · Week</th></tr></thead><tbody>${rows.map(row=>`<tr><th scope="row" class="${providerKey(row.name)}-color">${esc(row.name)}</th>${quotaCell(row,"fiveHour",data)}${quotaCell(row,"weekly",data)}</tr>`).join("")}</tbody></table></div>`;
    const peaks=[...data.peaks].sort((a,b)=>(providerKey(a.name)==="codex"?0:1)-(providerKey(b.name)==="codex"?0:1));
    const peakCell=value=>value==null?'<span aria-hidden="true">—</span><span class="sr-only">Not recorded</span>':`${Math.round(value)}%`;
    const allMaxed=peaks.length>0&&peaks.every(p=>[p.fiveHour,p.weekly].every(value=>value!=null&&value>=99.5));
    if(allMaxed)return `<div class="dash-limits is-collapsed"><div class="dash-section-label"><span>Peak quota used</span><small title="Highest recorded quota usage in this period, not current allowance">Every window reached 100%</small></div></div>`;
    if(!peaks.length)return "";
    return `<div class="dash-limits"><table class="dash-peaks-table"><caption>Highest recorded quota usage in this period, not current allowance</caption><thead><tr><th scope="col" title="Recorded in this period, not the current allowance">Peak quota used</th><th scope="col">5h</th><th scope="col">Week</th></tr></thead><tbody>${peaks.map(p=>`<tr><th scope="row" class="${providerKey(p.name)}-color">${esc(p.name)}</th><td>${peakCell(p.fiveHour)}</td><td>${peakCell(p.weekly)}</td></tr>`).join("")}</tbody></table><p class="dash-peaks-note">In selected range</p></div>`;
  }
  // Updates the Quota figures in place as the widget reading moves. Only a change in which providers have a live reading re-renders Insights.
  function syncQuota(){
    if(!state.open||state.tab!=="insights"||!state.insights)return;
    const panel=$("#dashboard-insights");if(!panel)return;
    const rows=quotaRows(),shown=panel.querySelector(".dash-quota");
    if(!rows&&!shown)return;
    if(!rows||!shown||shown.dataset.quotaShape!==quotaShape(rows)){state.renderKey="";render();return;}
    const data=insightsData(false);
    shown.querySelectorAll("[data-quota-part]").forEach(node=>{const row=rows.find(item=>item.name===node.dataset.quotaKey);if(!row)return;const key=node.dataset.quotaWindow;const text=node.dataset.quotaPart==="percent"?`${Math.round(quotaValue(row,key))}%`:quotaSub(row,key,data);if(node.textContent!==text)node.textContent=text;});
  }
  // First load: the hero eyebrow and an empty bar sit at their final positions while usage loads.
  function loadingHTML(){const cost=state.metric==="cost";return `<section class="dash-hero dash-hero-loading" aria-busy="true"><div class="dash-hero-meta"><span class="dash-eyebrow">${cost?"API-equivalent cost":"Tokens over"} · ${esc(RANGE_NAME[state.range]||"")}</span></div><div class="dash-hero-number-row"><span class="dash-hero-number" aria-hidden="true">&nbsp;</span></div><div class="dash-hero-bar"><div class="dash-split dash-split-empty" aria-hidden="true"></div></div></section><p class="dash-status-line" role="status">Loading usage…</p>`;}
  function insightsHTML(){
    if(!state.insights){if(state.loading)return loadingHTML();return `<p class="dash-status-line${state.loadError?" error":""}" role="status">${state.loadError?"Usage could not be loaded.":""}</p>`;}
    const data=insightsData(),key=data.heroKey;
    const stamp=state.insights.generatedAt?new Date(state.insights.generatedAt):null;
    const status=statusText();
    const statusHTML=status?`<p class="dash-status-line${isNotice()?" is-notice":""}" role="status">${esc(status)}</p>`:"";
    if(key==null)return statusHTML+(data.providers.length?'<section class="dash-empty-state"><span class="dash-eyebrow">Usage</span><p>No recorded measurement available</p></section>':emptyHTML());
    const cost=key==="apiEquivalentCost",value=data.totals[key],prev=finite(data.previous[key]),delta=prev!=null&&prev!==0?(value-prev)/prev*100:null;
    // The unit line names any shown provider that has no published price, so the total is read as a partial figure.
    const unpricedShown=data.providers.filter(p=>finite(p.scoped.apiEquivalentCost)==null).map(p=>p.name);
    // The unit line names a shown provider with no published price, and says when some priced models are partial.
    const unitLine=["estimated",...(unpricedShown.length?[`excl. ${joinNames(unpricedShown)}`]:[]),...(data.partialModels?["some models unpriced"]:[])].join(" · ");
    const figure=cost?`<span class="dash-hero-currency">$</span><strong class="dash-hero-number">${esc(money(value).replace(/^\$/,""))}</strong><span class="dash-hero-unit">${esc(unitLine)}</span>`:tokenHero(value);
    const chart=data.range==="24h"&&!data.fallback&&!data.hourly?"":seriesChart(data),chartHTML=chart?`<section class="dash-chart-section"><div class="dash-section-head"><span class="dash-section-title">Usage over time</span><span class="dash-section-note">${data.hourly?"Hourly":"Daily"} · ${data.chartKey==="apiEquivalentCost"?"API-equivalent cost estimate":"tokens"}</span>${chartStyleHTML()}</div>${chart}</section>`:"";
    // A zero stat next to non-zero tokens means the source did not report that figure, so it is hidden rather than shown as 0.
    const tokensPresent=finite(data.totals.tokens)>0;
    const missing=v=>finite(v)==null||(v===0&&tokensPresent);
    const stats=[[data.totals.sessions,v=>number(v),"Sessions"],[data.totals.subagentsActive,v=>number(v),"Subagents"],[data.totals.cacheHitRate,v=>`${Math.round(v)}%`,"Cache hit rate"],[data.totals.activeHours,v=>`${number(v)}h`,"Active hours"]].filter(([v])=>!missing(v));
    const statsHTML=stats.length?`<dl class="dash-summary">${stats.map(([v,format,label])=>`<div><dt>${label}</dt><dd>${esc(format(v))}</dd></div>`).join("")}</dl>`:"";
    const coverage=data.fallback?(data.range==="30d"||data.range==="all"?"31-day totals":data.dates.length?`Daily coverage: ${rangeLabel(data.dates)}${data.range==="24h"?" (latest recorded date)":" (recorded daily values)"}`: "31-day totals"):RANGE_NAME[data.range];
    const period=data.dates.length?rangeLabel(data.dates):coverage;
    const providerScope=chipsHTML(data);
    const hero=`<section class="dash-hero"><div class="dash-period">${esc(period)}</div>${providerScope}<div class="dash-hero-meta"><span class="dash-eyebrow">${data.providers.length===1?`${esc(data.providers[0].name)} · `:""}${cost?`API-equivalent cost · ${coverage}`:`Tokens over ${coverage}`}</span>${delta==null?"":`<span class="dash-delta${delta<0?" is-down":""}">${delta<0?"↓":"↑"} ${Math.abs(delta).toFixed(1)}% <span class="muted">vs ${RANGE_PREVIOUS[data.range]}</span></span>`}</div><div class="dash-hero-number-row">${figure}</div>${data.providers.length>1?(split=>split?`${split.bar?`<div class="dash-hero-bar">${split.bar}</div>`:""}<div class="dash-hero-providers">${split.legend}</div>`:"")(splitParts(data)):singlePlanHTML(data)}</section>`;
    // Zero usage across the shown providers: the hero and Quota stay; the chart, Models and Activity give way to one line.
    if(finite(data.totals.tokens)===0){const label=data.dates.length?rangeLabel(data.dates):RANGE_NAME[data.range],more=data.range==="30d"||data.range==="all"?"":' <button type="button" class="dash-link" data-dashboard-range="30d">Show 30 days</button>',quota=limitsHTML(data);return hero+`<section class="dash-lower is-single is-zero"><div class="dash-zero"><p class="dash-zero-line">No usage ${esc(label)}.${more}</p></div>${quota?`<div class="dash-side">${quota}</div>`:""}</section>`+statusHTML;}
    const models=data.models.filter(row=>row.tokens!==0);
    // One row per model (long-context variants are merged into it). Seven rows fit beside the chart; beyond that the top six show and the rest sum into one Other row.
    const modelRows=(()=>{if(models.length<=7)return models.map(row=>({...row,family:modelKey(row.raw)}));const top=models.slice(0,6).map(row=>({...row,family:modelKey(row.raw)})),rest=models.slice(6);const sumOf=key=>{const present=rest.map(row=>finite(row[key])).filter(value=>value!=null);return present.length?present.reduce((total,value)=>total+value,0):null;};return [...top,{name:`Other · ${rest.length} models`,tokens:sumOf("tokens"),apiEquivalentCost:sumOf("apiEquivalentCost"),family:"other",other:true,partial:rest.some(row=>row.partial)}];})();
    // Bars follow the active metric on a linear scale relative to the largest row; a visible row keeps a 2% minimum width.
    // In Cost view a model with no published price gets an empty bar, not a zero-length one that reads as "free".
    const barKey=data.heroKey==="apiEquivalentCost"?"apiEquivalentCost":"tokens";
    const barMax=Math.max(0,...modelRows.map(row=>finite(row[barKey])||0));
    const limits=limitsHTML(data);
    const activity=statsHTML?`<div class="dash-activity"><div class="dash-section-label"><span>Activity</span></div>${statsHTML}</div>`:"";
    const side=limits||activity?`<div class="dash-side">${limits}${activity}</div>`:"";
    const work=workData(),workView=work!=null&&modelsView()==="work";
    if(workView)return hero+`<section class="dash-lower${side?"":" is-single"}${chartHTML?"":" is-day"}"><div class="dash-lower-main">${workHTML(work)}</div>${side}${chartHTML}</section>`+statusHTML;
    const modelHTML=models.length?`<div class="dash-model-block"><table class="dash-model-table"><caption class="sr-only">Model breakdown. Token and estimated API-equivalent cost values. ${data.costPartial||models.some(row=>finite(row.tokens)>0&&finite(row.apiEquivalentCost)==null)?"Some model costs have no published price or the payload reports partial coverage.":"Cost pricing coverage is complete for recorded usage."}</caption><thead><tr><th scope="col">${work?breakdownToggle("models"):"Models"}</th><th scope="col"><span class="sr-only">Usage</span></th><th scope="col">Tokens</th><th scope="col">Est. cost</th></tr></thead><tbody>${modelRows.map(row=>{const value=finite(row[barKey]),width=barMax>0&&value!=null&&value>0?Math.max(2,Math.min(100,100*value/barMax)):0,empty=barKey==="apiEquivalentCost"&&value==null;return `<tr class="dash-model-row"><th scope="row"><span class="dash-model-name ${row.family}" title="${esc(row.name)}"><i></i>${esc(row.name)}</span>${row.longContext?'<small class="dash-model-tag">long context</small>':""}</th><td><i class="dash-model-bar${empty?" is-empty":""}" aria-hidden="true">${empty?"":`<em class="${row.family}" style="width:${width.toFixed(1)}%"></em>`}</i></td><td>${esc(number(row.tokens))}</td><td>${finite(row.apiEquivalentCost)==null?'<span class="dash-none" role="img" aria-label="No published price">&mdash;</span>':esc((row.partial?"≥ ":"")+money(row.apiEquivalentCost))}</td></tr>`;}).join("")}</tbody></table></div>`:"";
    // Models and the side column share one row; the chart runs the full width under both. Without a chart (24h has no hourly series) the row stretches to the panel body.
    const lower=modelHTML||side?`<section class="dash-lower${modelHTML&&side?"":" is-single"}${chartHTML?"":" is-day"}">${modelHTML?`<div class="dash-lower-main">${modelHTML}</div>`:""}${side}${chartHTML}</section>`:chartHTML;
    return hero+lower+statusHTML;
  }
  // By work: ledger tokens per model and kind of work. It takes the place of the Models list; the side column stays on the right.
  // Kinds outside the classifier's list fold into Other, so every cell, row and column adds up to the ledger's totals.
  const WORK_KINDS=["build","review","check","research","chore","design","explore","lead","other"];
  // Work-table figures: one decimal from a million up ("2.0M", "1.2B"), whole thousands below ("800K").
  const workNumber=value=>{const n=finite(value)||0;if(n>=1e9)return (n/1e9).toFixed(1)+"B";if(n>=999500)return (n/1e6).toFixed(1)+"M";if(n>=1e3)return Math.round(n/1e3)+"K";return String(Math.round(n));};
  const modelsView=()=>readPref("branchglanceModelsView","models")==="work"?"work":"models";
  const workCell=()=>({tokens:0,known:0,priced:false,unpriced:false});
  const addCell=(into,cell)=>{into.tokens+=cell.tokens;into.known+=cell.known;into.priced=into.priced||cell.priced;into.unpriced=into.unpriced||cell.unpriced;return into;};
  function workData(){
    const ledger=state.ledger;
    if(!ledger||state.ledgerRange!==state.range||!Array.isArray(ledger.rows)||!ledger.rows.length)return null;
    const byModel=new Map();
    for(const row of ledger.rows){
      if(hiddenProviders.has(String(row.provider||"")))continue;
      const tokens=finite(row.totalTokens)||0;if(tokens<=0)continue;
      const kind=WORK_KINDS.includes(row.kind)?row.kind:"other",id=`${row.provider}\u0000${row.modelId}`;
      let model=byModel.get(id);
      if(!model)byModel.set(id,model={provider:String(row.provider||""),name:modelName(row.modelId)||String(row.displayName||"unknown"),raw:row.modelId,total:0,cells:{}});
      const cost=finite(row.knownCostUsd);
      addCell(model.cells[kind]||(model.cells[kind]=workCell()),{tokens,known:cost||0,priced:cost!=null,unpriced:row.costComplete===false||finite(row.unpricedTokens)>0||cost==null});
      model.total+=tokens;
    }
    const metric=state.metric==="cost"?"cost":"tokens",measure=row=>metric==="cost"?Object.values(row.cells).reduce((sum,cell)=>sum+cell.known,0):row.total;
    const ranked=[...byModel.values()].sort((a,b)=>measure(b)-measure(a));
    if(!ranked.length)return null;
    // Four rows fit in the Models list's height; past that the top three show and the rest sum into one Other row.
    let rows=ranked;
    if(ranked.length>4){
      const rest=ranked.slice(3),other={provider:"",name:`Other · ${rest.length} models`,other:true,total:0,cells:{}};
      for(const model of rest){other.total+=model.total;for(const [kind,cell] of Object.entries(model.cells))addCell(other.cells[kind]||(other.cells[kind]=workCell()),cell);}
      rows=[...ranked.slice(0,3),other];
    }
    // The same model name under two providers (unknown, say) gets its provider appended.
    const names=rows.map(row=>row.name);
    rows.forEach(row=>{if(!row.other&&names.filter(name=>name===row.name).length>1)row.name+=` · ${row.provider}`;});
    let kinds=WORK_KINDS.filter(kind=>rows.some(row=>row.cells[kind]?.tokens>0));
    // More than four kinds: the three largest keep their columns and the rest fold into Other, so the table has at most four kind columns.
    if(kinds.length>4){const kindTokens=kind=>rows.reduce((sum,row)=>sum+(row.cells[kind]?.tokens||0),0);const keep=kinds.filter(kind=>kind!=="other").sort((a,b)=>kindTokens(b)-kindTokens(a)).slice(0,3);for(const row of rows)for(const kind of kinds){if(keep.includes(kind)||kind==="other")continue;const cell=row.cells[kind];if(cell){addCell(row.cells.other||(row.cells.other=workCell()),cell);delete row.cells[kind];}}kinds=WORK_KINDS.filter(kind=>keep.includes(kind)||(kind==="other"&&rows.some(row=>row.cells.other?.tokens>0)));}
    const totals={cells:{},all:workCell()};
    for(const row of rows){row.sum=workCell();for(const kind of kinds){const cell=row.cells[kind];if(cell){addCell(totals.cells[kind]||(totals.cells[kind]=workCell()),cell);addCell(row.sum,cell);addCell(totals.all,cell);}}}
    return {rows,kinds,totals,coverage:ledger.coverage||{},service:ledger.service||{},metric};
  }
  function workCost(cell){
    if(!cell.priced)return "No published price";
    return `${cell.unpriced?"≥ ":""}${money(cell.known)} API-equivalent${cell.unpriced?" (some usage has no published price)":""}`;
  }
  // Coverage stays visible: anything short of complete says so, with its causes.
  function workNote(work){
    const coverage=work.coverage;
    if(work.service.state==="error")return {text:"Ledger paused",title:"The ledger hit an error and is retrying; figures are from its last pass."};
    if(coverage.status==="catching-up")return {text:"Catching up",title:"Still reading older transcripts; totals will grow."};
    if(coverage.status==="verifying"){const stamp=coverage.lastIngestedAt?new Date(coverage.lastIngestedAt):null,last=stamp&&!Number.isNaN(stamp.getTime())?stamp.toLocaleString():"time unavailable";return {text:`Checking usage records - Last ingestion ${last}`,title:"Some recent usage is waiting for its file to be re-checked. The ledger has not confirmed completion."};}
    if(coverage.status!=="partial")return null;
    const causes=[[coverage.gapCount,"gap","gaps"],[coverage.unknownUsageRecords,"turn without usage","turns without usage"],[coverage.readFailures,"read failure","read failures"]]
      .filter(([count])=>finite(count)>0).map(([count,one,many])=>`${number(count)} ${count===1?one:many}`);
    const missing=Array.isArray(coverage.providersNotCollected)&&coverage.providersNotCollected.length?[`${coverage.providersNotCollected.join(", ")} not collected`]:[];
    const all=[...causes,...missing];
    return {text:`Partial${all.length?` · ${all[0]}`:""}`,title:`Some usage could not be counted exactly: ${all.join(", ")||"see coverage"}.`};
  }
  const breakdownToggle=active=>`<span class="dash-breakdown" role="group" aria-label="Breakdown">${[["models","Models"],["work","By work"]].map(([value,label])=>`<button type="button" data-dashboard-models="${value}" aria-pressed="${active===value}" class="${active===value?"is-active":""}"${value==="work"?' title="Tokens by kind of work. Kind is inferred from job titles and kind: tags."':""}>${label}</button>`).join("")}</span>`;
  function workHTML(work){
    const note=workNote(work),label=kind=>kind[0].toUpperCase()+kind.slice(1);
    const cellHTML=(cell,title,kindLabel,strong=false)=>!cell||!(cell.tokens>0)?'<span role="cell" class="dash-work-cell is-zero" aria-label="None"><span aria-hidden="true">—</span></span>'
      :work.metric==="cost"?cell.known<=0?'<span role="cell" tabindex="0" class="dash-work-cell is-zero" aria-label="No published price for this usage">Unpriced</span>':`<span role="cell" tabindex="0" class="dash-work-cell${strong?" is-total":""}" aria-label="${esc(`${title}, ${kindLabel}: ${cell.unpriced?"at least ":""}${money(cell.known)} API-equivalent cost${cell.unpriced?"; some usage has no published price":""}`)}" data-tip-html="${esc(`<b>${esc(title)}</b> · ${esc(kindLabel)}<br>${esc(number(cell.tokens))} tokens<br>${esc(workCost(cell))}`)}">${cell.unpriced?"≥ ":""}${esc(money(cell.known))}</span>`
      :`<span role="cell" tabindex="0" class="dash-work-cell${strong?" is-total":""}" aria-label="${esc(`${title}, ${kindLabel}: ${number(cell.tokens)} tokens; ${workCost(cell)}`)}" data-tip-html="${esc(`<b>${esc(title)}</b> · ${esc(kindLabel)}<br>${esc(number(cell.tokens))} tokens<br>${esc(workCost(cell))}`)}">${esc(workNumber(cell.tokens))}</span>`;
    const head=`<div role="row" class="dash-work-row is-head"><span role="columnheader"><span class="sr-only">Model</span></span>${work.kinds.map(kind=>`<span role="columnheader">${label(kind)}</span>`).join("")}<span role="columnheader">Total</span></div>`;
    const body=work.rows.map(row=>`<div role="row" class="dash-work-row${row.other?" other":""}"><span role="rowheader" class="dash-model-name ${row.other?"other":modelKey(row.raw)}"${row.other?"":` title="${esc(`${row.provider} · ${row.name}`)}"`}><i></i>${esc(row.name)}</span>${work.kinds.map(kind=>cellHTML(row.cells[kind],row.name,label(kind))).join("")}${cellHTML(row.sum,row.name,"all work",true)}</div>`).join("");
    const foot=`<div role="row" class="dash-work-row is-foot"><span role="rowheader">Total</span>${work.kinds.map(kind=>cellHTML(work.totals.cells[kind],"All models",label(kind),true)).join("")}${cellHTML(work.totals.all,"All models","all work",true)}</div>`;
    return `<div class="dash-work-block" style="--work-cols:${work.kinds.length}"><div class="dash-section-label dash-work-head">${breakdownToggle("work")}<small>${work.metric==="cost"?"API-equivalent cost":"Tokens"}</small>${note?`<small class="dash-work-note is-qualified" title="${esc(note.title)}">${esc(note.text)}</small>`:""}</div><div class="dash-work-grid" role="table" aria-label="${work.metric==="cost"?"API-equivalent cost":"Tokens"} by model and kind of work">${head}${body}${foot}</div></div>`;
  }
  async function loadLedger(range){
    const token=state.ledgerToken=(state.ledgerToken||0)+1;
    const drop=()=>{if(state.ledger!=null){state.ledger=null;state.ledgerKey="";state.renderKey="";render();}};
    try{
      const response=await fetch("/api/ledger?range="+encodeURIComponent(range),{cache:"no-store",signal:AbortSignal.timeout(10000)});
      if(token!==state.ledgerToken)return;
      if(!response.ok){drop();return;}
      const value=await response.json();if(token!==state.ledgerToken)return;
      if(!Array.isArray(value?.rows)){drop();return;}
      // Compare what the view shows, not just the revision: hours age out of a rolling range without any new ingestion.
      const key=JSON.stringify([range,value.range,value.rows,value.coverage,value.pricingVersion,value.service?.state]);
      if(key===state.ledgerKey&&state.ledgerRange===range)return;
      state.ledger=value;state.ledgerRange=range;state.ledgerKey=key;state.renderKey="";render();
    }catch{if(token===state.ledgerToken)drop();}
  }
  function setModelsView(value){
    if(value!=="models"&&value!=="work")return;
    dismissTooltip();writePref("branchglanceModelsView",value);state.renderKey="";render();
    document.querySelector(`[data-dashboard-models="${value}"]`)?.focus();
  }
  // Plan multiple: 30-calendar-day (incl. today) API-equivalent cost over the plan price, per provider. It reads the 30-day window whatever range is selected, so the multiple means the same on every tab.
  const PLAN_PROVIDERS=["Claude","Codex"];
  const planPrice=name=>{const raw=Number(readPref(`branchglancePlanPrice.${name}`,""));return Number.isFinite(raw)&&raw>0?raw:null;};
  // A lower bound (unpriced models present) that rounds to zero reads "0.0", never "<0.1", which would claim an upper bound.
  const planMultiple=(value,lowerBound)=>{if(lowerBound){const precision=value>=10?0:1,factor=10**precision;return (Math.floor(value*factor)/factor).toFixed(precision);}return value<.1?"<0.1":value>=10?Math.round(value).toLocaleString("en-US"):value.toFixed(1);};
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
    const price=planPrice(name);if(price==null)return planCells(`<button type="button" class="dash-plan-set" data-dashboard-action="set-plan" data-dashboard-plan-provider="${esc(name)}" aria-label="Set ${esc(name)} monthly plan price">Set plan</button>`);const multiple=row.cost/price,prefix=row.unpriced?"≥":"";
    return planCells(`<span title="${esc(`${row.unpriced?"At least means some usage has no published price, so the real figure is higher. ":""}Last 30 days (calendar, incl. today) API-equivalent cost ÷ ${money(price)} plan`)}">${prefix}${esc(planMultiple(multiple,row.unpriced))}×</span>`,row.unpriced?`some unpriced`:"");
  }
  async function loadMonth(){
    const token=++state.monthToken;
    const content=value=>JSON.stringify({...value,generatedAt:undefined});
    try{
      const response=await fetch("/api/insights?range=30d",{cache:"no-store",signal:AbortSignal.timeout(10000)});
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
    if(String(value??"").trim()===""){clearPref(`branchglancePlanPrice.${name}`);state.settingsError=false;state.settingsStatus="";state.settingsRenderKey="";state.renderKey="";render();return;}
    const number=Number(value);
    if(!Number.isFinite(number)||number<=0){state.settingsError=true;state.settingsStatus=`${name} plan price must be a number above 0.`;state.settingsRenderKey="";render();return;}
    writePref(`branchglancePlanPrice.${name}`,String(number));state.settingsError=false;state.settingsStatus="";state.settingsRenderKey="";state.renderKey="";render();
  }
  const toggleRow=(label,description,action,value)=>{const busy=state.togglePending.has(action),failed=state.toggleErrors.has(action);return `<div class="dash-setting-row"><div class="dash-setting-copy"><span class="dash-setting-label">${label}</span><span class="dash-setting-description">${description}</span></div><div class="dash-setting-control"><button class="dash-toggle${value?" is-on":""}" role="switch" aria-checked="${Boolean(value)}" aria-label="${label}"${busy?' aria-busy="true" disabled':""} data-dashboard-action="${action}"></button></div></div>${failed?'<p class="dash-setting-error" role="alert">Couldn\'t save. Try again.</p>':""}`;};

  const rowHTML=(label,description,control="")=>`<div class="dash-setting-row"><div class="dash-setting-copy"><span class="dash-setting-label">${label}</span><span class="dash-setting-description">${description}</span></div><div class="dash-setting-control">${control}</div></div>`;
  function settingsHTML(){
    const clock=readPref("branchglanceClockFormat","12")==="24"?"24":"12",policies=Array.isArray(state.policy?.providers)?state.policy.providers:[];
    const flag=(value,yes,no)=>value===true?yes:value===false?no:"Unavailable";
    const providerRows=policies.length?policies.map(row=>`<div class="dash-setting-row"><div class="dash-setting-copy"><span class="dash-setting-label">${esc(row.name)}</span><span class="dash-provider-status">${esc([flag(row.show,"Shown","Hidden"),flag(row.collect,"collecting","not collecting"),flag(row.send,"auto-send on","auto-send off")].join(" \u00b7 "))}</span></div></div>`).join(""):'<p class="dash-setting-note">Provider status unavailable.</p>';
    const policyPath=String(state.policy?.policyFile||""),policyTitle=/[\\/]/.test(policyPath)?` title="${esc(policyPath)}"`:"";
    const providers=`<section class="dash-settings-group" aria-label="Provider permissions"><h2>Providers<span class="dash-h2-note"${policyTitle}>From policy file</span></h2>${providerRows}${(shown=>shown.length===1&&policies.length>1?`<p class="dash-setting-note dash-add-provider">Add ${esc(policies.find(row=>row.show!==true).name)} to track its agents too.</p>`:"")(policies.filter(row=>row.show===true))}<button class="dash-action" data-dashboard-action="claude-settings">Manage providers, automation and history&#8230;</button></section>`;
    const reset=state.settingsLoaded?`<iframe class="reset-embed" title="After a Codex quota reset" src="/reset_actions.html?embedded=1&amp;source=${encodeURIComponent(state.source)}"></iframe>`:`<p class="dash-status" role="status">Loading</p>`;
    const singleMonitor=state.monitors.length<2;
    const widget=`<section class="dash-settings-group"><h2>Widget</h2>${toggleRow("Lock widget position","Keep the widget where it is","lock",state.lock?.locked)}${toggleRow("Keep widget open","Do not collapse automatically","keep-open",state.keepOpen?.keepOpen)}${rowHTML("Clock format","",`<div class="dash-clock-segment" role="group" aria-label="Clock format">${[["12","12-hour"],["24","24-hour"]].map(([value,label])=>`<button type="button" data-dashboard-clock="${value}" aria-pressed="${clock===value}">${label}</button>`).join("")}</div>`)}${rowHTML(singleMonitor?"Position":"Monitor",state.lock?.locked?"Unlock the widget to move it":singleMonitor?"Back to the centre of this screen":"Choose where the widget opens",`<div class="dash-monitor-controls">${state.monitors.length>1?`<select class="dash-select" aria-label="Monitor" data-dashboard-action="monitor" ${state.lock?.locked?"disabled":""}>${state.monitors.map(row=>`<option value="${esc(row.id)}"${row.selected?" selected":""}>${esc(row.label||row.kind||"Monitor")}</option>`).join("")}</select>`:""}<button class="dash-action" data-dashboard-action="recenter" ${state.lock?.locked?"disabled":""}>Recenter</button></div>`)}</section>`;
    const plan=`<section class="dash-settings-group dash-plan-group-section" aria-label="Plan price"><h2>Plan price</h2>${PLAN_PROVIDERS.map((name,index)=>{const price=planPrice(name);return `<div class="dash-setting-row"><div class="dash-setting-copy"><span class="dash-setting-label">${esc(name)}</span><span class="dash-setting-description">${index===0?"Monthly price. Used for “30d vs plan” in Insights.":""}</span></div><div class="dash-setting-control"><label class="dash-plan-field"><span aria-hidden="true">$</span><input${index===0?' id="dash-plan-price"':""} type="number" min="1" step="any" inputmode="decimal" value="${price==null?"":esc(price)}" aria-label="${esc(name)} plan price per month" data-dashboard-plan="${esc(name)}"></label></div></div>`;}).join("")}</section>`;
    const status=state.settingsStatus?`<p class="dash-status${state.settingsError?" error":""}" role="status">${esc(state.settingsStatus)}</p>`:"";
    const versionNote=typeof state.identity?.version==="string"&&typeof state.identity?.build==="string"?`v${esc(state.identity.version)}+${esc(state.identity.build)}`:state.settingsLoaded?"Version unavailable":"";
    const debug=`<section class="dash-settings-group dash-debug-group" aria-label="About and bug report"><h2>About${versionNote?`<span class="dash-h2-note">${versionNote}</span>`:""}</h2>${rowHTML("Bug report","No chat text, file paths, account details or tokens.",`<button class="dash-action" data-dashboard-action="debug-preview">Create report</button>`)}<div class="dash-debug-panel"${state.debugReport?"":" hidden"}><textarea class="dash-debug-text" id="dash-debug-report" readonly spellcheck="false" aria-label="Debug report text" rows="12">${esc(state.debugReport||"")}</textarea><div class="dash-debug-actions"><button class="dash-action" data-dashboard-action="debug-copy">Copy</button><button class="dash-action" data-dashboard-action="debug-issue">Open GitHub issue</button></div></div><p class="dash-status${state.debugError?" error":""}" id="dash-debug-status" role="status">${esc(state.debugStatus||"")}</p></section>`;
    return `<div class="dash-settings-columns">${widget}${providers}<section class="dash-settings-group dash-reset-group"><h2>After a Codex quota reset</h2>${reset}</section>${plan}${status}${debug}</div>`;
  }
  const DEBUG_ISSUE_URL="https://github.com/apratxxm/branchglance/issues/new?template=bug_report.yml";
  const DEBUG_TITLE="# Branchglance debug report";
  function setDebugStatus(text,error=false){state.debugStatus=String(text||"");state.debugError=!!error;const node=$("#dash-debug-status");if(node){node.textContent=state.debugStatus;node.classList.toggle("error",state.debugError);}}
  function debugOS(){const platform=String(typeof navigator!=="undefined"?(navigator.userAgentData?.platform||navigator.platform||""):"");return /^win/i.test(platform)?"Windows":/^mac/i.test(platform)?"macOS":/linux/i.test(platform)?"Linux":"unknown";}
  // Only a validated version and the OS enum are prefilled; the report itself is pasted by the user.
  function debugIssueURL(){const version=state.identity?.version;const safeVersion=typeof version==="string"&&/^\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.]+)?$/.test(version)?version:null;return `${DEBUG_ISSUE_URL}${safeVersion?`&version=${encodeURIComponent(safeVersion)}`:""}&os=${debugOS()}`;}
  async function previewDebugReport(){
    setDebugStatus("Building report…");
    try{
      const response=await fetch("/api/debug-report",{cache:"no-store",signal:AbortSignal.timeout(10000)});
      const text=response.ok?await response.text():"";
      if(!text.startsWith(DEBUG_TITLE))throw new Error("unavailable");
      state.debugReport=text;
      const box=$("#dash-debug-report"),panel=$(".dash-debug-panel");
      if(box)box.value=text;
      if(panel)panel.hidden=false;
      setDebugStatus("Review the report before sharing.");
    }catch{setDebugStatus("Debug report is unavailable.",true);}
  }
  async function copyDebugReport(){
    const text=state.debugReport;
    if(!text){setDebugStatus("Preview the report first.",true);return;}
    try{
      if(!navigator.clipboard?.writeText)throw new Error("no clipboard");
      await navigator.clipboard.writeText(text);
      setDebugStatus("Copied.");
    }catch{
      const box=$("#dash-debug-report");
      if(box&&typeof document.execCommand==="function"){box.focus();box.select();if(document.execCommand("copy")){setDebugStatus("Copied.");return;}}
      setDebugStatus("Copy failed. Select the text and copy it.",true);
    }
  }
  function openDebugIssue(){
    try{window.open(debugIssueURL(),"_blank","noopener,noreferrer");}
    catch{setDebugStatus("Could not open the issue page.",true);}
  }
  let activePoint=null,activeMode=null,tooltipBound=false,tooltipGeneration=0;
  function dismissTooltip(){
    tooltipGeneration++;activePoint=null;activeMode=null;document.querySelectorAll(".dash-point.is-hover").forEach(el=>el.classList.remove("is-hover"));const tip=$("#dash-tooltip");
    if(tip){tip.classList.remove("is-visible");tip.setAttribute("aria-hidden","true");}
  }
  function bindTooltips(){
    const root=$("#dashboard-view"),tip=$("#dash-tooltip");if(!root||!tip)return;
    const place=(x,y)=>{const margin=8,box=tip.getBoundingClientRect();tip.style.left=Math.max(margin,Math.min(x+12,window.innerWidth-margin-box.width))+"px";tip.style.top=Math.max(margin,Math.min(y+12,window.innerHeight-margin-box.height))+"px";};
    // A focused point puts its tip above the plot, left of the point once it passes 60% of the plot width. The tip stays 20px inside the panel.
    // Returns false when layout is unavailable; the caller then uses the pointer placement.
    const placeFocus=target=>{const rect=el=>el?.getBoundingClientRect?.(),plot=rect(target.closest?.(".dash-plot")),panel=rect($("#dashboard-insights")),box=rect(tip),pt=rect(target);if(!plot||!panel||!box||!pt)return false;const cx=(pt.left+pt.right)/2;const left=cx-plot.left>plot.width*.6?cx-box.width-12:cx+12;tip.style.left=Math.max(panel.left+20,Math.min(left,panel.right-20-box.width))+"px";tip.style.top=Math.max(panel.top+20,plot.top-box.height-8)+"px";return true;};
    const show=(target,x,y,mode)=>{activePoint=target;activeMode=mode;document.querySelectorAll(".dash-point.is-hover").forEach(el=>el.classList.remove("is-hover"));target.classList?.add("is-hover");tip.innerHTML=target.getAttribute("data-tip-html")||"";tip.classList.add("is-visible");tip.setAttribute("aria-hidden","false");if(!(mode==="focus"&&placeFocus(target)))place(x,y);};
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
    // Escape dismisses a visible tooltip and is consumed so the dashboard stays open. With no tooltip, Escape is left alone.
    window.addEventListener("keydown",event=>{if(event.key!=="Escape"||!tip.classList?.contains?.("is-visible"))return;dismissTooltip();event.stopPropagation();event.preventDefault();},true);
    // Roving focus: one point is in the tab order. Arrows move focus (and the tooltip with it); Home and End jump to the ends.
    root.addEventListener("keydown",event=>{
      const point=event.target?.closest?.(".dash-point");if(!point)return;
      const points=[...(point.closest(".dash-chart")?.querySelectorAll(".dash-point")||[])],index=points.indexOf(point);if(index<0)return;
      const last=points.length-1,next={ArrowLeft:index-1,ArrowRight:index+1,Home:0,End:last}[event.key];
      if(next==null)return;
      const target=points[Math.max(0,Math.min(last,next))];event.preventDefault();
      points.forEach(p=>p.setAttribute("tabindex",p===target?"0":"-1"));target.focus();
    });
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
    for(const key of ["insights","settings"]){const button=$("#dashboard-tab-"+key),panel=$("#dashboard-"+key);if(button){button.classList.toggle("is-active",key===state.tab);button.setAttribute("aria-selected",String(key===state.tab));button.tabIndex=key===state.tab?0:-1;}if(panel)panel.hidden=key!==state.tab;}
  }
  function syncSegments(){
    document.querySelectorAll("[data-dashboard-range]").forEach(button=>{const active=button.dataset.dashboardRange===state.range;button.classList.toggle("is-active",active);button.setAttribute("aria-pressed",String(active));});
    const unpriced=state.insights!=null&&!costAvailable();
    document.querySelectorAll("[data-dashboard-metric]").forEach(button=>{const metric=button.dataset.dashboardMetric,active=metric===state.metric,blocked=metric==="cost"&&unpriced;button.classList.toggle("is-active",active);button.classList.toggle("is-unavailable",blocked);button.setAttribute("aria-pressed",String(active));button.setAttribute("aria-disabled",String(blocked));button.title=blocked?"No priced usage for the shown providers":"";});
  }
  function selectTab(name){
    if(name!=="insights"&&name!=="settings")return;
    cancelFade();if(name===state.tab)return;
    crossFade(()=>{state.tab=name;syncTabs();state.renderKey="";render();if(name==="settings")void loadSettings();});
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
    try{const [policyResponse,identityResponse]=await Promise.all([fetch("/api/provider-policy",{cache:"no-store",signal:AbortSignal.timeout(10000)}),fetch("/api/identity",{cache:"no-store",signal:AbortSignal.timeout(10000)})]);if(!policyResponse.ok)throw new Error("Provider policy is unavailable.");state.policy=await policyResponse.json();state.identity=identityResponse.ok?await identityResponse.json():null;const [monitors,lock,keep]=await Promise.all([api?.get_monitors?.()||[],api?.get_widget_lock?.()||null,api?.get_keep_open?.()||null]);state.monitors=Array.isArray(monitors)?monitors:[];state.lock=lock;state.keepOpen=keep;state.settingsStatus="";state.settingsLoaded=true;}
    catch(error){state.policy=null;state.identity=null;state.monitors=[];state.lock=null;state.keepOpen=null;state.settingsLoaded=true;state.settingsError=true;state.settingsStatus=error?.message||"Settings could not be loaded.";}
    finally{state.settingsBusy=false;state.settingsRenderKey="";render();}
  }
  async function chooseMonitor(id){
    const api=window.pywebview?.api;if(typeof api?.select_monitor!=="function")return;
    try{const result=await api.select_monitor(id);state.monitors=Array.isArray(result)?result:state.monitors;if(typeof api.get_widget_lock==="function")state.lock=await api.get_widget_lock();state.settingsError=false;state.settingsStatus="";}
    catch(error){state.lock=null;state.settingsError=true;state.settingsStatus=error?.message||"Monitor could not be selected.";}
    finally{state.settingsRenderKey="";render();}
  }
  // "Set plan" switches to Settings; render focuses that provider's plan price input once it exists.
  function openPlanPrice(name){state.focusPlanPrice=PLAN_PROVIDERS.includes(name)?name:null;selectTab("settings");}
  // A switch is busy only while its own request is pending; a refused change keeps the previous checked state and says so beside its row.
  async function toggleSetting(action){
    if(state.togglePending.has(action))return;
    const api=window.pywebview?.api;
    state.togglePending.add(action);state.toggleErrors.delete(action);state.settingsRenderKey="";render();
    try{if(action==="lock"&&typeof api?.set_widget_lock==="function")state.lock=await api.set_widget_lock(state.lock?.locked!==true,state.lock?.expanded===true,null,state.lock?.revision);if(action==="keep-open"&&typeof api?.set_keep_open==="function")state.keepOpen=await api.set_keep_open(state.keepOpen?.keepOpen!==true,state.keepOpen?.keepOpenRevision);state.settingsError=false;state.settingsStatus="";}
    catch{state.toggleErrors.add(action);}
    // Focus returns to the switch only if it still holds focus or focus fell to the page; a user who moved on keeps their place.
    finally{const active=document.activeElement,keepFocus=!active||active===document.body||active.dataset?.dashboardAction===action;state.togglePending.delete(action);state.settingsRenderKey="";render();const next=$(`#dashboard-settings [data-dashboard-action="${action}"]`),now=document.activeElement;if(keepFocus&&next&&(!now||now===document.body))next.focus?.();}
  }
  function setClockFormat(value){
    value=String(value);if(!["12","24"].includes(value))return false;
    writePref("branchglanceClockFormat",value);window.TokenTrackerSetClockFormat?.(value);state.settingsRenderKey="";render();return true;
  }
  function toggleClock(){setClockFormat(readPref("branchglanceClockFormat","12")==="12"?"24":"12");}
  function render(){
    if(!state.open)return;
    const active=document.activeElement,focusData=active?.dataset||{};
    const insightsKey=[state.version,state.source,state.range,state.metric,state.tab,chartStyle(),clockPref()].join("|");
    if(insightsKey!==state.renderKey){dismissTooltip();state.renderKey=insightsKey;const insights=$("#dashboard-insights");if(insights){insights.innerHTML=insightsHTML();let replacement=null;if(focusData.dashboardProvider!=null)replacement=[...insights.querySelectorAll("[data-dashboard-provider]")].find(node=>node.dataset.dashboardProvider===focusData.dashboardProvider);else if(focusData.dashboardRange)replacement=insights.querySelector(`[data-dashboard-range="${focusData.dashboardRange}"]`);else if(focusData.dashboardMetric)replacement=insights.querySelector(`[data-dashboard-metric="${focusData.dashboardMetric}"]`);else if(focusData.dashboardChartStyle)replacement=insights.querySelector(`[data-dashboard-chart-style="${focusData.dashboardChartStyle}"]`);else if(focusData.dashboardModels)replacement=insights.querySelector(`[data-dashboard-models="${focusData.dashboardModels}"]`);replacement?.focus?.();}}
    const settings=$("#dashboard-settings");
    if(settings&&state.tab==="settings"){
      const settingsKey=[state.source,JSON.stringify(state.policy),state.identity?.version,state.identity?.build,state.settingsLoaded,state.settingsStatus,state.settingsBusy,state.settingsError,state.lock?.revision,state.keepOpen?.keepOpenRevision,readPref("branchglanceClockFormat","12"),state.monitors.map(m=>`${m.id}:${m.label}:${m.selected}`).join("|")].join("|");
      if(settingsKey!==state.settingsRenderKey){const focus=document.activeElement,selector=focus?.dataset?.dashboardAction?`[data-dashboard-action="${focus.dataset.dashboardAction}"]`:focus?.dataset?.dashboardClock?`[data-dashboard-clock="${focus.dataset.dashboardClock}"]`:focus?.dataset?.dashboardPlan?`[data-dashboard-plan="${focus.dataset.dashboardPlan}"]`:null,oldGroup=settings.querySelector(".dash-reset-group");state.settingsRenderKey=settingsKey;const template=document.createElement("template");template.innerHTML=settingsHTML();const newGroup=template.content.querySelector(".dash-reset-group"),src=group=>group?.querySelector("iframe.reset-embed")?.getAttribute("src");
        // Moving an iframe in the DOM reloads it, so a mounted reset form stays where it is and only its siblings are replaced.
        if(oldGroup&&newGroup&&src(oldGroup)&&src(oldGroup)===src(newGroup)){const keep=oldGroup.parentNode,nodes=[...newGroup.parentNode.childNodes],at=nodes.indexOf(newGroup);[...keep.childNodes].forEach(node=>{if(node!==oldGroup)node.remove();});oldGroup.before(...nodes.slice(0,at));oldGroup.after(...nodes.slice(at+1));}
        else settings.replaceChildren(template.content);
        state.frame=settings.querySelector("iframe.reset-embed");if(selector)settings.querySelector(selector)?.focus();}
    }
    if(state.focusPlanPrice&&state.tab==="settings"){const input=$(`#dashboard-settings [data-dashboard-plan="${state.focusPlanPrice}"]`);if(input){state.focusPlanPrice=null;input.focus();input.scrollIntoView?.({block:"center"});}}
    bindTooltipsOnce();
    syncSize();
  }
  function open({source,tab}={}){
    state.opener=document.activeElement;state.open=true;state.tab=tab==="settings"?"settings":"insights";if(typeof source==="string")state.source=source;
    state.range=storedRange();
    state.metricExplicit=readPref("branchglanceInsightsMetric",null)!==null;state.metric=readPref("branchglanceInsightsMetric","cost");if(!["tokens","cost"].includes(state.metric))state.metric="cost";
    const root=$("#dashboard-view");if(root){root.hidden=false;root.inert=false;root.setAttribute("aria-hidden","false");}
    syncTabs();syncSegments();render();const byPointer=Date.now()-(state.pointerAt||0)<1000;requestAnimationFrame(()=>{const tab=$("#dashboard-tab-"+state.tab);if(!tab)return;if(byPointer)tab.focus({focusVisible:false});else tab.focus();});void loadInsights();void loadMonth();if(state.tab==="settings")void loadSettings();
    if(state.refreshTimer!=null)clearInterval(state.refreshTimer);
    state.refreshTimer=setInterval(()=>{if(state.open&&!state.loading)void loadInsights();void loadMonth();},120000);
  }
  function close(){
    state.open=false;state.loadToken++;state.loading=false;state.fadeVersion++;if(state.fadeTimer!=null)clearTimeout(state.fadeTimer);state.fadeTimer=null;if(state.refreshTimer!=null)clearInterval(state.refreshTimer);state.refreshTimer=null;dismissTooltip();
    $("#dashboard-content")?.classList.remove("is-switching");const root=$("#dashboard-view");if(root){root.hidden=true;root.inert=true;root.setAttribute("aria-hidden","true");}
    state.frame=null;state.settingsRenderKey="";state.opener?.focus?.();state.opener=null;
  }
  function normalizeStats(snapshot){
    const providers=Array.isArray(snapshot?.providers)?snapshot.providers:[];
    return {fallback:true,generatedAt:snapshot?.generatedAt||null,providers:providers.map(row=>{const cache=row?.cache||{};return {name:row?.name,totals:{tokens:finite(row?.tracked31Days),apiEquivalentCost:finite(row?.apiEquivalentCost??cache.apiEquivalentCost),sessions:finite(row?.sessions),subagentsActive:finite(row?.subagentsActive),activeHours:finite(row?.activeHours),cacheHitRate:finite(row?.cacheHitRate??cache.percent)},previous:null,series:(Array.isArray(row?.activity)?row.activity:[]).map(item=>({date:item.date,tokens:finite(item.value??item.tokens),apiEquivalentCost:finite(item.apiEquivalentCost)})).filter(item=>/^\d{4}-\d\d-\d\d$/.test(item.date||"")),models:(Array.isArray(row?.modelUsage)?row.modelUsage:[]).map(item=>({name:item?.model,tokens:finite(item?.tokens),apiEquivalentCost:finite(item?.apiEquivalentCost)})),limitPeaks:row?.limitPeaks||null};})};
  }
  async function loadInsights(){
    const token=++state.loadToken,requestRange=state.range;
    void loadLedger(requestRange);
    return await loadInsightsOnce(token,requestRange);
  }
  async function loadInsightsOnce(token,requestRange){
    state.loading=true;state.loadError=false;updateStatusLine();syncRefresh();
    const current=()=>token===state.loadToken;
    const accept=(value,failed=false,unavailable=false)=>{
      // Same range and same content (ignoring the per-response generatedAt stamp): keep the rendered panel and refresh only the status line.
      const content=v=>JSON.stringify({...v,generatedAt:undefined});
      const same=state.insights!=null&&state.resultRange===requestRange&&state.insights.fallback===value.fallback&&content(state.insights)===content(value);
      state.loading=false;state.loadError=failed;state.insightsUnavailable=unavailable;syncRefresh();
      if(same){state.insights.generatedAt=value.generatedAt;updateStatusLine();return value;}
      state.resultRange=requestRange;state.resultValue=value;setInsights(value);state.renderKey="";render();return value;
    };
    try{
      let value=null;
      try{
        const response=await fetch("/api/insights?range="+encodeURIComponent(requestRange),{cache:"no-store",signal:AbortSignal.timeout(12000)});
        if(!current())return state.insights;
        if(response.ok){const candidate=await response.json();if(!current())return state.insights;if(Array.isArray(candidate?.providers)&&(candidate.range==null||candidate.range===requestRange))value=candidate;}
      }catch{}
      if(!current())return state.insights;
      if(value){state.statsSnapshot=null;return accept(value);}
      // Keep a trusted result on failed refresh; a fallback cannot replace its coverage.
      if(state.insights&&!state.insights.fallback)throw new Error("Insights unavailable");
      try{
        const fallback=await fetch("/api/stats?background=1",{cache:"no-store",signal:AbortSignal.timeout(12000)});if(!current())return state.insights;
        if(fallback.ok){const stats=await fallback.json();if(!current())return state.insights;state.statsSnapshot=stats;return accept(normalizeStats(stats),false,true);}
      }catch{}
      if(!current())return state.insights;
      if(state.insights)throw new Error("Insights unavailable");
      if(state.snapshot?.providers){state.statsSnapshot=state.snapshot;return accept(normalizeStats(state.snapshot),false,true);}
      throw new Error("Insights unavailable");
    }catch{
      if(!current())return state.insights;
      state.loading=false;state.loadError=true;updateStatusLine();syncRefresh();return state.insights;
    }
  }
  function setSnapshot(snapshot){
    state.snapshot=snapshot;
    if(state.open&&!state.loading&&(!state.insights||state.insights.fallback)){state.statsSnapshot=snapshot;const value=normalizeStats(snapshot);state.resultRange=state.range;state.resultValue=value;setInsights(value);render();}
  }
  function setSource(source){if(typeof source==="string"&&source!==state.source){state.source=source;state.renderKey="";render();}}
  document.addEventListener("pointerdown",()=>{state.pointerAt=Date.now();},true);
  document.addEventListener("click",event=>{
    const tab=event.target?.closest?.("[data-dashboard-tab]");if(tab){selectTab(tab.dataset.dashboardTab);return;}
    if(event.target?.closest?.("#dashboard-refresh")){void loadInsights();return;}
    const clock=event.target?.closest?.("[data-dashboard-clock]");if(clock){setClockFormat(clock.dataset.dashboardClock);return;}
    const provider=event.target?.closest?.("[data-dashboard-provider]");if(provider){toggleProvider(provider.dataset.dashboardProvider);return;}
    const range=event.target?.closest?.("[data-dashboard-range]");if(range){setRange(range.dataset.dashboardRange);return;}
    const metric=event.target?.closest?.("[data-dashboard-metric]");if(metric){setMetric(metric.dataset.dashboardMetric);return;}
    const breakdown=event.target?.closest?.("[data-dashboard-models]");if(breakdown){setModelsView(breakdown.dataset.dashboardModels);return;}
    const style=event.target?.closest?.("[data-dashboard-chart-style]");if(style){setChartStyle(style.dataset.dashboardChartStyle);return;}
    const action=event.target?.closest?.("[data-dashboard-action]");if(!action)return;
    const name=action.dataset.dashboardAction;
    if(name==="close")window.tokenTrackerCloseDashboard?.();
    else if(name==="lock"||name==="keep-open")void toggleSetting(name);
    else if(name==="clock")toggleClock();
    else if(name==="set-plan")openPlanPrice(action.dataset.dashboardPlanProvider);
    else if(name==="recenter"){if(state.lock?.locked)return;void Promise.resolve(window.tokenTrackerRecenter?.()).then(()=>loadPlacement());}
    else if(name==="dashboard-recenter")void window.tokenTrackerRecenter?.();
    else if(name==="claude-settings")void window.pywebview?.api?.open_reset_actions?.();
    else if(name==="debug-preview")void previewDebugReport();
    else if(name==="debug-copy")void copyDebugReport();
    else if(name==="debug-issue")openDebugIssue();
  });
  document.addEventListener("keydown",event=>{const tab=event.target?.closest?.("[role=tab][data-dashboard-tab]");if(!tab)return;const keys=["insights","settings"],index=keys.indexOf(tab.dataset.dashboardTab);let next=null;if(event.key==="ArrowRight")next=keys[(index+1)%keys.length];else if(event.key==="ArrowLeft")next=keys[(index+keys.length-1)%keys.length];else if(event.key==="Home")next=keys[0];else if(event.key==="End")next=keys.at(-1);if(next){event.preventDefault();selectTab(next);requestAnimationFrame(()=>$("#dashboard-tab-"+next)?.focus());}});
  document.addEventListener("change",event=>{const target=event.target;if(target?.dataset?.dashboardAction==="monitor")void chooseMonitor(target.value);if(target?.dataset?.dashboardPlan)setPlanPrice(target.dataset.dashboardPlan,target.value);});
  window.addEventListener("message",event=>{
    if(event.origin!==location.origin||event.source!==state.frame?.contentWindow)return;
    if(event.data?.type==="dashboard-reset-cancel")window.tokenTrackerCloseDashboard?.();
    if(event.data?.type==="dashboard-open-settings")void window.pywebview?.api?.open_reset_actions?.();
    // The frame reports the Claude Code status; it shows in the "Claude tracking" row, not inside the frame.
    if(event.data?.type==="dashboard-reset-state"){const frame=state.frame;const status=frame?.parentElement?.querySelector(".dash-reset-state");if(status)status.textContent=String(event.data.text||"");}
    if(event.data?.type==="dashboard-reset-height"){const height=Math.round(Number(event.data.height));if(state.frame&&Number.isFinite(height)&&height>0)state.frame.style.height=`${Math.min(1200,Math.max(24,height))}px`;}
    if(event.data?.type==="dashboard-claude-status"){state.claudeStatus=String(event.data.text||"").slice(0,120);const node=$("#dash-claude-status");if(node)node.textContent=state.claudeStatus;}
  });
  window.addEventListener("load",()=>{state.frame=$("#dashboard-settings iframe");});
  // Focus moving into the embedded reset form blurs this window. Coming back from it is not a return to the app,
  // so Settings are not reloaded then: the reload would replace the control that keyboard focus is moving to.
  let frameFocused=false;
  window.addEventListener("blur",()=>{setTimeout(()=>{frameFocused=!!state.frame&&document.activeElement===state.frame;},0);});
  window.addEventListener("focus",()=>{if(frameFocused){frameFocused=false;return;}if(state.open&&state.tab==="settings")void loadSettings();});
  async function loadPlacement(){const api=window.pywebview?.api;try{if(typeof api?.get_widget_lock==="function")state.lock=await api.get_widget_lock();if(typeof api?.get_monitors==="function")state.monitors=await api.get_monitors();state.settingsStatus="";state.settingsError=false;}catch{state.lock=null;state.settingsStatus="Widget placement status is unavailable.";state.settingsError=true;}state.settingsRenderKey="";render();}
  window.TokenTrackerDashboard={open,close,windowHeight,setSource,setSnapshot,selectTab,render,loadSettings,state};
  window.tokenTrackerDashboardSyncQuota=syncQuota;
  if(window.__TOKEN_TRACKER_DASHBOARD_TEST__)window.__tokenTrackerDashboardTest={insightsData,seriesChart,curvePath,insightsHTML,settingsHTML,statusText,normalizeStats,emptyHTML,planCellsHTML,planMultiple,storedRange,toggleProvider,loadMonth,setPlanPrice,setSnapshot,setSource,setRange,setMetric,setChartStyle,setClockFormat,loadInsights,loadInsightsOnce,workData,workHTML,workNote,loadLedger,debugIssueURL,previewDebugReport,copyDebugReport,openDebugIssue,state};
})();
