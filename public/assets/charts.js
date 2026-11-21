import {axisNumber,escapeHTML as e,number} from './utils.js';

const NS='http://www.w3.org/2000/svg',DAY=86400000;
const integer=new Intl.NumberFormat('en-US',{maximumFractionDigits:0});
const dates=new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'short',year:'numeric',timeZone:'UTC'});
const times=new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23',timeZone:'UTC'});
const shortDates=new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'short',timeZone:'UTC'});
let uid=0;
function time(value){
  if(value===null||value===undefined||value===''||(typeof value!=='string'&&typeof value!=='number'&&!(value instanceof Date)))return null;
  const result=new Date(value).getTime();return Number.isFinite(result)?result:null;
}
function node(tag,attrs={},text){const el=document.createElementNS(NS,tag);for(const [key,value]of Object.entries(attrs))el.setAttribute(key,String(value));if(text!==undefined)el.textContent=text;return el;}
function clamp(value,min,max){return Math.max(min,Math.min(max,value));}
function dateText(value){const parsed=time(value);return parsed===null?'Timestamp unavailable':(/^\d{4}-\d{2}-\d{2}$/.test(String(value))?dates:times).format(parsed)+' UTC';}
function unitText(magnitude,negative,unit,tiny=false){const sign=negative?'−':'';if(unit==='$')return sign+(tiny?'<$1':'$'+magnitude);return sign+(tiny?'<1':magnitude)+(unit?(unit==='%'?'':' ')+unit:'');}

/** Whole-unit tooltips preserve nonzero amounts below one and exact integer strings. */
export function formatChartValue(value,unit='$'){
  const numeric=number(value);if(numeric===null)return 'Unavailable';
  const text=typeof value==='string'?value.trim():'';
  const match=text.length<=40024?/^([+-]?)(\d+)(?:\.(\d+))?$/.exec(text):null;
  if(match){
    const whole=match[2].replace(/^0+(?=\d)/,''),fraction=match[3]||'';
    const nonzero=whole!=='0'||/[1-9]/.test(fraction),negative=match[1]==='-'&&nonzero;
    if(whole==='0'&&nonzero)return unitText('',negative,unit,true);
    const rounded=BigInt(whole)+(fraction[0]>='5'?1n:0n);
    return unitText(rounded.toString().replace(/\B(?=(\d{3})+(?!\d))/g,','),negative,unit);
  }
  const underflow=numeric===0&&text&&/^[+-]?(?:\d+\.?\d*|\.\d+)[eE][+-]?\d+$/.test(text)&&/[1-9]/.test(text.split(/[eE]/)[0]);
  const negative=numeric<0||(underflow&&text.startsWith('-'));
  if((Math.abs(numeric)>0&&Math.abs(numeric)<1)||underflow)return unitText('',negative,unit,true);
  return unitText(integer.format(Math.abs(numeric)),negative,unit);
}
function requestedDomain(input){const start=time(input?.start),end=time(input?.end);return start!==null&&end!==null&&end>=start?{start,end}:null;}
function valueDomain(points,type){
  const values=[];
  for(const point of points){
    if(!point.plottable)continue;
    const present=point.values.filter(value=>value!==null);if(!present.length)continue;
    if(type==='stacked'){
      const positive=present.filter(value=>value>0).reduce((a,b)=>a+b,0),negative=present.filter(value=>value<0).reduce((a,b)=>a+b,0);
      if(!Number.isFinite(positive)||!Number.isFinite(negative))return null;values.push(positive,negative);
    }else values.push(...present);
  }
  if(!values.length)return null;
  let min=Math.min(0,...values),max=Math.max(0,...values);if(min===max)max=min+1;
  const raw=(max-min)/4;if(!Number.isFinite(raw)||raw<=0)return null;
  const magnitude=10**Math.floor(Math.log10(raw));
  const step=magnitude>0?([1,2,2.5,5,10].find(v=>v*magnitude>=raw)||10)*magnitude:raw;
  if(!Number.isFinite(step)||step<=0)return null;
  min=Math.floor(min/step)*step;max=Math.ceil(max/step)*step;
  if(!Number.isFinite(min)||!Number.isFinite(max)||max<=min)return null;
  const ticks=[];for(let i=0;i<12;i++){const value=min+i*step;if(value>max+step/100)break;ticks.push(Object.is(value,-0)?0:value);}
  return {min,max,step,ticks};
}

/** Source timestamps remain a continuous elapsed-time scale. xDomain filters rows;
 * the default observed span prevents sparse snapshots clustering at window edges.
 * domainMode:'range' deliberately retains a full selected-window axis instead.
 */
export function buildChartLayout(options={},width=700,height=260){
  const {rows=[],series=[],type='line',interval=false,domainMode='observed',connectGaps=false,maxGapDays}=options;
  width=Number.isFinite(width)&&width>0?width:700;
  height=width<450?250:Number.isFinite(height)&&height>=100?height:260;
  const margin={left:Math.min(58,width*.24),right:Math.min(20,width*.08),top:25,bottom:40};
  const plot={left:margin.left,right:Math.max(margin.left+1,width-margin.right),top:margin.top,bottom:height-margin.bottom};
  plot.width=plot.right-plot.left;plot.height=plot.bottom-plot.top;
  const requested=requestedDomain(options.xDomain);
  const points=(Array.isArray(rows)?rows:[]).map((row,originalIndex)=>({row,originalIndex,time:time(row?.date),start:time(row?.startDate)}))
    .filter(point=>point.time!==null&&(!requested||point.time>=requested.start&&point.time<=requested.end))
    .sort((a,b)=>a.time-b.time||a.originalIndex-b.originalIndex)
    .map(point=>({...point,values:series.map(item=>number(point.row.values?.[item.key])),plottable:!interval||(point.start!==null&&point.start<point.time)}));
  const empty={width,height,plot,points,series,type,interval,requested,bars:[],segments:[],xTicks:[],yTicks:[],status:'empty',message:'No observations available'};
  if(!points.length)return empty;
  let lower=points[0].time,upper=points.at(-1).time;
  if(interval){const starts=points.filter(point=>point.plottable).map(point=>point.start);if(starts.length)lower=Math.min(lower,...starts);if(requested)lower=Math.max(lower,requested.start);}
  if(domainMode==='range'&&requested){lower=requested.start;upper=requested.end;}
  const bar=type==='bar'||type==='stacked',inset=interval?0:bar?Math.min(24,plot.width/4):Math.min(4,plot.width/4);
  const xLeft=plot.left+inset,xRight=plot.right-inset;
  const x=value=>upper===lower?(xLeft+xRight)/2:xLeft+(value-lower)/(upper-lower)*(xRight-xLeft);
  for(const point of points){point.x=x(point.time);point.intervalStart=interval&&point.plottable?x(Math.max(lower,point.start)):null;point.clippedStart=interval&&point.plottable&&point.start<lower;}
  const domain=valueDomain(points,type);
  if(!domain){const hasValues=points.some(point=>point.plottable&&point.values.some(value=>value!==null));return {...empty,points,lower,upper,status:hasValues?'unplottable':'missing',message:hasValues?'Values exceed chart scale':'No values for this selection'};}
  const verticalInset=bar?0:Math.min(4,plot.height/4);
  const y=value=>plot.bottom-verticalInset-(value-domain.min)/(domain.max-domain.min)*(plot.height-verticalInset*2),base=y(0);
  let tickTimes;const tickLimit=width<450?3:5;
  if(domainMode==='range'&&requested&&upper>lower)tickTimes=Array.from({length:tickLimit},(_,index)=>lower+(upper-lower)*index/(tickLimit-1));
  else{const candidates=[...new Set([...(interval?[lower]:[]),...points.map(point=>point.time)])],count=Math.min(tickLimit,candidates.length);tickTimes=[...new Set(Array.from({length:count},(_,index)=>candidates[Math.round(index*(candidates.length-1)/Math.max(1,count-1))]))];}
  const uniqueTimes=[...new Set(points.map(point=>point.time))],counts=new Map(),seen=new Map();
  for(const point of points)counts.set(point.time,(counts.get(point.time)||0)+1);
  const timeIndices=new Map(uniqueTimes.map((value,index)=>[value,index]));
  const bars=[];
  if(bar)for(let index=0;index<points.length;index++){
    const point=points[index];if(!point.plottable)continue;
    const dateIndex=timeIndices.get(point.time),neighbours=[uniqueTimes[dateIndex-1],uniqueTimes[dateIndex+1]].filter(value=>value!==undefined).map(value=>Math.abs(x(value)-point.x));
    const outerBand=Math.min(42,neighbours.length?Math.min(...neighbours)*.65:42),duplicate=seen.get(point.time)||0;seen.set(point.time,duplicate+1);
    const band=outerBand/counts.get(point.time),center=point.x-outerBand/2+band*(duplicate+.5);
    let positive=0,negative=0;
    for(let seriesIndex=0;seriesIndex<series.length;seriesIndex++){
      const value=point.values[seriesIndex];if(value===null)continue;
      let start=0,end=value,bx,bw;
      if(type==='stacked'){start=value>=0?positive:negative;end=start+value;if(value>=0)positive=end;else negative=end;}
      if(interval){const available=Math.max(0,point.x-point.intervalStart),insetGap=Math.min(2,available*.08);bx=point.intervalStart+insetGap;bw=Math.max(0,available-insetGap*2);if(type!=='stacked'){bw/=Math.max(1,series.length);bx+=seriesIndex*bw;}}
      else if(type==='stacked'){bx=center-band/2;bw=band;}
      else{bw=band/Math.max(1,series.length);bx=center-band/2+seriesIndex*bw;}
      const rawTop=y(Math.max(start,end)),rawBottom=y(Math.min(start,end)),barHeight=Math.max(value===0?1:Math.min(.6,plot.height),rawBottom-rawTop);
      const yy=clamp(value===0?base-.5:rawTop,plot.top,plot.bottom-barHeight),left=clamp(bx,plot.left,plot.right),right=clamp(bx+bw,plot.left,plot.right);
      bars.push({index,seriesIndex,value,start,end,x:left,y:yy,width:Math.max(0,right-left-(bw>3?1:0)),height:Math.min(plot.height,barHeight),zero:value===0,partial:type==='stacked'&&point.values.some(item=>item===null)});
    }
  }
  const segments=[];
  if(!bar)for(let seriesIndex=0;seriesIndex<series.length;seriesIndex++){
    let current=[];
    for(let index=0;index<points.length;index++){
      const point=points[index],value=point.values[seriesIndex];
      if(value===null||!point.plottable){if(!connectGaps&&current.length){segments.push({seriesIndex,points:current});current=[];}continue;}
      const prior=current.at(-1);
      if(prior&&Number.isFinite(maxGapDays)&&Math.floor(point.time/DAY)-Math.floor(points[prior.index].time/DAY)>maxGapDays){segments.push({seriesIndex,points:current});current=[];}
      current.push({index,x:point.x,y:y(value),value});
    }
    if(current.length)segments.push({seriesIndex,points:current});
  }
  const yearChanges = new Date(lower).getUTCFullYear() !== new Date(upper).getUTCFullYear();
  const ticks = tickTimes.map(value=>({time:value,x:x(value)}));
  const tickGap = yearChanges ? 114 : 64;
  const xTicks = ticks.filter((tick,index)=>{
    if (index===0 || index===ticks.length-1) return true;
    if (tick.x-ticks[0].x<tickGap || ticks.at(-1).x-tick.x<tickGap) return false;
    const previous = ticks[index-1];
    return tick.x-previous.x>=tickGap || previous.x-ticks[0].x<tickGap;
  });
  return {...empty,status:'ready',message:null,points,lower,upper,domain,base,bars,segments,xTicks,yTicks:domain.ticks.map(value=>({value,y:y(value),label:axisNumber(value,domain.step)==='0'&&value!==0?value.toExponential(1):axisNumber(value,domain.step)}))};
}

/** Rendering and selection share the same sorted, visible observation coordinates. */
export function nearestChartIndex(layout,x,hitIndex=null){
  if(!layout?.points?.length)return -1;
  if(Number.isInteger(hitIndex)&&hitIndex>=0&&hitIndex<layout.points.length)return hitIndex;
  let index=0,distance=Infinity;layout.points.forEach((point,nextIndex)=>{const next=Math.abs(point.x-x);if(next<distance){distance=next;index=nextIndex;}});return index;
}

export function mountChart(container,options){
  const {series=[],type='line',unit='$',height=260,label='',onSelect}=options,id='chart-'+(++uid);
  let selected=-1,active=true,layout=null,cursor=null;
  const svg=node('svg',{class:'chart-svg',role:'img',tabindex:0,'aria-label':label+' Use left and right arrow keys to inspect observations.','aria-describedby':id+'-description'});
  const tooltip=document.createElement('div');tooltip.className='chart-tooltip';tooltip.hidden=true;tooltip.style.minWidth='0';
  const announcement=document.createElement('span');announcement.setAttribute('role','status');announcement.setAttribute('aria-live','polite');announcement.setAttribute('aria-atomic','true');
  Object.assign(announcement.style,{position:'absolute',width:'1px',height:'1px',overflow:'hidden',clipPath:'inset(50%)',whiteSpace:'nowrap'});
  container.replaceChildren(svg,tooltip,announcement);
  function hide(){tooltip.hidden=true;cursor?.setAttribute('visibility','hidden');}
  function draw(){
    if(!active)return;
    const measured=container.getBoundingClientRect().width;if(measured===0&&container.offsetParent===null&&document.documentElement.clientWidth>0)return;
    layout=buildChartLayout(options,measured||700,height);const {width,height:h,plot}=layout;
    svg.style.height=h+'px';svg.setAttribute('viewBox',`0 0 ${width} ${h}`);svg.replaceChildren();cursor=null;
    svg.append(node('desc',{id:id+'-description'},`${layout.points.length} dated observations. ${series.map(s=>s.label).join(', ')}. Unit: ${unit}. Missing values are not zero.${options.interval?' Bars retain full source-interval amounts; clipping does not prorate them.':''}`));
    if(layout.status!=='ready'){hide();svg.append(node('text',{x:width/2,y:h/2,'text-anchor':'middle'},options.emptyMessage||layout.message));return;}
    const defs=node('defs'),clip=node('clipPath',{id:id+'-clip'});clip.append(node('rect',{x:plot.left,y:plot.top,width:plot.width,height:plot.height}));defs.append(clip);
    if(type==='area')series.forEach((item,index)=>{const gradient=node('linearGradient',{id:id+'-fill-'+index,x1:0,y1:0,x2:0,y2:1});gradient.append(node('stop',{offset:'0%','stop-color':item.color,'stop-opacity':.23}),node('stop',{offset:'100%','stop-color':item.color,'stop-opacity':.015}));defs.append(gradient);});
    svg.append(defs);
    for(const tick of layout.yTicks){svg.append(node('line',{class:'chart-grid',x1:plot.left,x2:plot.right,y1:tick.y,y2:tick.y}));svg.append(node('text',{x:plot.left-10,y:tick.y+4,'text-anchor':'end'},tick.label));}
    const acrossYears=new Date(layout.lower).getUTCFullYear()!==new Date(layout.upper).getUTCFullYear();
    layout.xTicks.forEach((tick,index)=>{const text=(acrossYears?dates:shortDates).format(tick.time);svg.append(node('text',{x:tick.x,y:h-14,'text-anchor':layout.xTicks.length===1?'middle':index===0?'start':index===layout.xTicks.length-1?'end':'middle'},text));});
    const marks=node('g',{'clip-path':`url(#${id}-clip)`});
    for(const bar of layout.bars)marks.append(node('rect',{x:bar.x,y:bar.y,width:bar.width,height:bar.height,fill:series[bar.seriesIndex].color,rx:bar.zero?0:1,'data-index':bar.index,'data-zero':bar.zero,...(bar.partial?{'fill-opacity':.65,'data-partial':'true'}:{})}));
    for(const segment of layout.segments){const item=series[segment.seriesIndex],points=segment.points,path=points.map((point,index)=>(index?'L':'M')+point.x+','+point.y).join(' ');if(type==='area'&&series.length===1&&points.length>1)marks.append(node('path',{d:path+` L${points.at(-1).x},${layout.base} L${points[0].x},${layout.base} Z`,fill:`url(#${id}-fill-${segment.seriesIndex})`}));marks.append(node('path',{d:path,fill:'none',stroke:item.color,'stroke-width':2,'stroke-linecap':'round','stroke-linejoin':'round'}));points.forEach(point=>marks.append(node('circle',{cx:point.x,cy:point.y,r:layout.points.length<=25?3:2,fill:item.color,'data-index':point.index})));}
    svg.append(marks);
    layout.points.forEach((point,index)=>{if(point.row.warnings?.length)svg.append(node('circle',{cx:clamp(point.x,plot.left,plot.right),cy:12,r:3.2,fill:'#e4b96a','data-index':index,'aria-label':'Source exception'}));});
    cursor=node('line',{class:'chart-cursor',x1:0,x2:0,y1:plot.top,y2:plot.bottom,visibility:'hidden','clip-path':`url(#${id}-clip)`});svg.append(cursor);
    if(selected>=0)show(selected,false,false);
  }
  function show(index,visible=true,announce=false){
    if(!active||!layout||layout.status!=='ready'||!layout.points.length)return;
    selected=clamp(index,0,layout.points.length-1);const point=layout.points[selected],row=point.row;
    cursor.setAttribute('x1',point.x);cursor.setAttribute('x2',point.x);cursor.setAttribute('visibility',visible?'visible':'hidden');
    const title=options.interval&&point.start!==null
      ? `${dateText(row.startDate)} → ${dateText(row.date)}` : dateText(row.date);
    const detail=row.detail?`<div class="chart-tooltip-date">${e(row.detail)}</div>`:'';
    const intervalNote=options.interval?(point.start===null?'<div class="chart-tooltip-date">Interval start unavailable</div>':point.clippedStart?'<div class="chart-tooltip-date">Full interval amount. Start outside the selected range.</div>':''):'';
    const values=series.map(item=>({label:item.label,text:formatChartValue(row.values?.[item.key],unit)}));
    const partialNote=type==='stacked'&&point.values.some(value=>value===null)?'<div class="chart-tooltip-date">Some components are unavailable</div>':'';
    tooltip.innerHTML=`<strong>${e(title)}</strong>${detail}${intervalNote}${partialNote}${values.map(item=>`<div class="tooltip-row"><span>${e(item.label)}</span><b>${e(item.text)}</b></div>`).join('')}${row.warnings?.length?'<div class="tooltip-row"><span style="color:var(--amber)">Source exception</span></div>':''}`;
    tooltip.hidden=!visible;
    const bounds=svg.getBoundingClientRect(),parentBounds=container.getBoundingClientRect();tooltip.style.maxWidth=Math.max(50,parentBounds.width-16)+'px';
    const tooltipWidth=tooltip.getBoundingClientRect().width||Math.min(235,parentBounds.width-16),localX=bounds.left-parentBounds.left+point.x/layout.width*bounds.width;
    tooltip.style.left=clamp(localX+10,8,Math.max(8,parentBounds.width-tooltipWidth-8))+'px';
    if(announce)announcement.textContent=`${title}. ${row.detail?row.detail+'. ':''}${values.map(item=>item.label+': '+item.text).join('. ')}${row.warnings?.length?'. Source exception.':''}`;
    if(onSelect)onSelect(row,point.originalIndex);
  }
  function pointer(event){
    if(!layout||layout.status!=='ready')return;const bounds=svg.getBoundingClientRect();if(!bounds.width)return;
    const x=(event.clientX-bounds.left)/bounds.width*layout.width,element=event.target?.closest?.('[data-index]'),raw=element?.getAttribute('data-index');
    const hit=raw!==null&&raw!==undefined&&/^\d+$/.test(raw)?Number(raw):null;show(nearestChartIndex(layout,x,hit));
  }
  svg.addEventListener('pointermove',pointer);svg.addEventListener('click',pointer);svg.addEventListener('pointerleave',hide);
  svg.addEventListener('focus',()=>{if(layout?.points.length)show(selected<0?layout.points.length-1:selected,true,true);});
  svg.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)&&layout?.points.length){event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?layout.points.length-1:selected<0?(event.key==='ArrowRight'?0:layout.points.length-1):selected+(event.key==='ArrowLeft'?-1:1);show(next,true,true);}else if(event.key==='Escape')hide();});
  svg.addEventListener('blur',hide);
  const observer=typeof ResizeObserver==='function'?new ResizeObserver(draw):null;observer?.observe(container);draw();
  return ()=>{active=false;observer?.disconnect();hide();};
}
