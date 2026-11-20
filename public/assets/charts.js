import {compact,fixed,shortDate,escapeHTML as e,number} from './utils.js';
const NS='http://www.w3.org/2000/svg';
let uid=0;
function node(tag,attrs={},text){const el=document.createElementNS(NS,tag);Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,String(v)));if(text!==undefined)el.textContent=text;return el;}

/** Native SVG renderer. Geometry uses the observed container width, not a fixed scaled canvas. */
export function mountChart(container,{rows,series,type='line',unit='$',height=310,label='',connectGaps=false,onSelect}){
  const id='chart-'+(++uid);let selected=-1;let active=true;let layout=null;
  const svg=node('svg',{class:'chart-svg',role:'img',tabindex:0,'aria-label':label+' Use left and right arrow keys to inspect observations.'});
  const tooltip=document.createElement('div');tooltip.className='chart-tooltip';tooltip.hidden=true;
  container.replaceChildren(svg,tooltip);
  function draw(){
    if(!active)return;
    const measured=container.getBoundingClientRect().width;
    if(measured===0&&container.offsetParent===null&&document.documentElement.clientWidth>0)return;
    const width=Math.max(250,measured||700),h=width<450?285:height;
    svg.style.height=h+'px';svg.setAttribute('viewBox',`0 0 ${width} ${h}`);svg.replaceChildren();
    const margin={left:58,right:20,top:25,bottom:40};const pw=width-margin.left-margin.right,ph=h-margin.top-margin.bottom;
    const times=rows.map(r=>new Date(r.date).getTime());const finiteTimes=times.filter(Number.isFinite);
    if(!rows.length||!finiteTimes.length){svg.append(node('text',{x:width/2,y:h/2,'text-anchor':'middle'},'No observations available'));return;}
    const lower=Math.min(...finiteTimes),upper=Math.max(...finiteTimes),span=upper-lower;
    const x=i=>margin.left+(span?(times[i]-lower)/span:.5)*pw;
    const totals=rows.flatMap(row=>{const values=series.map(s=>number(row.values[s.key])).filter(v=>v!==null);return type==='stacked'?[values.filter(v=>v>0).reduce((a,b)=>a+b,0),values.filter(v=>v<0).reduce((a,b)=>a+b,0)]:values;});
    if(!totals.length){svg.append(node('text',{x:width/2,y:h/2,'text-anchor':'middle'},'No values for this selection'));return;}
    let min=Math.min(0,...totals),max=Math.max(0,...totals);if(min===max)max=min+1;
    const rawStep=(max-min)/4;const magnitude=10**Math.floor(Math.log10(rawStep));const nice=[1,2,2.5,5,10].find(v=>v*magnitude>=rawStep)*magnitude;
    min=Math.floor(min/nice)*nice;max=Math.ceil(max/nice)*nice;
    if(type==='bar'||type==='stacked'){const pad=rows.length>1?Math.min(24,pw/(rows.length*2)):30;const original=x;layout={};layout.x=i=>margin.left+pad+(span?(times[i]-lower)/span:.5)*(pw-pad*2);}else layout={x};
    const plotX=layout.x,y=v=>margin.top+ph-(v-min)/(max-min)*ph;
    const desc=node('desc',{},`${rows.length} dated observations. ${series.map(s=>s.label).join(', ')}. Unit: ${unit}. Missing values are not zero.`);svg.append(desc);
    for(let v=min;v<=max+nice/100;v+=nice){const yy=y(v);svg.append(node('line',{class:'chart-grid',x1:margin.left,x2:width-margin.right,y1:yy,y2:yy}));svg.append(node('text',{x:margin.left-10,y:yy+4,'text-anchor':'end'},compact(v,Math.abs(v)<10?1:0)));}
    const ticks=Math.min(width<450?3:5,rows.length);const tickIndices=new Set(Array.from({length:ticks},(_,i)=>Math.round(i*(rows.length-1)/Math.max(1,ticks-1))));
    for(const i of tickIndices)svg.append(node('text',{x:plotX(i),y:h-14,'text-anchor':i===0?'start':i===rows.length-1?'end':'middle'},shortDate(rows[i].date)));
    const gap=rows.length>1?Math.min(...rows.slice(1).map((_,i)=>Math.max(1,plotX(i+1)-plotX(i)))):45;
    const band=Math.min(42,gap*.65);
    if(type==='bar'||type==='stacked'){
      rows.forEach((row,i)=>{let positive=0,negative=0;series.forEach((s,j)=>{const v=number(row.values[s.key]);if(v===null)return;let top,bottom,bx,bw;if(type==='stacked'){const start=v>=0?positive:negative,end=start+v;top=y(Math.max(start,end));bottom=y(Math.min(start,end));if(v>=0)positive=end;else negative=end;bx=plotX(i)-band/2;bw=band;}else{bw=band/series.length;bx=plotX(i)-band/2+j*bw;top=y(Math.max(0,v));bottom=y(Math.min(0,v));}svg.append(node('rect',{x:bx,y:top,width:Math.max(1,bw-1),height:Math.max(v===0?1:.6,bottom-top),fill:s.color,rx:1,'data-index':i}));});});
    }else{
      series.forEach((s,j)=>{const segments=[];let segment=[];rows.forEach((row,i)=>{const v=number(row.values[s.key]);if(v===null){if(!connectGaps&&segment.length){segments.push(segment);segment=[];}return;}segment.push([plotX(i),y(v),i]);});if(segment.length)segments.push(segment);
        segments.forEach(points=>{const path=points.map((p,k)=>(k?'L':'M')+p[0]+','+p[1]).join(' ');if(series.length===1&&type==='area'&&points.length>1){const gradient=node('linearGradient',{id:id+'-fill',x1:0,y1:0,x2:0,y2:1});gradient.append(node('stop',{offset:'0%','stop-color':s.color,'stop-opacity':.23}),node('stop',{offset:'100%','stop-color':s.color,'stop-opacity':.015}));const defs=node('defs');defs.append(gradient);svg.append(defs,node('path',{d:path+` L${points.at(-1)[0]},${y(0)} L${points[0][0]},${y(0)} Z`,fill:`url(#${id}-fill)`}));}svg.append(node('path',{d:path,fill:'none',stroke:s.color,'stroke-width':2,'stroke-linecap':'round','stroke-linejoin':'round'}));points.forEach(([xx,yy,i])=>svg.append(node('circle',{cx:xx,cy:yy,r:rows.length<=25?3:2,fill:s.color,'data-index':i})));});
      });
    }
    rows.forEach((row,i)=>{if(row.warnings?.length)svg.append(node('circle',{cx:plotX(i),cy:12,r:3.2,fill:'#e4b96a','data-index':i,'aria-label':'Source exception'}));});
    const cursor=node('line',{class:'chart-cursor',x1:0,x2:0,y1:margin.top,y2:h-margin.bottom,visibility:'hidden'});svg.append(cursor);
    layout={...layout,width,height:h,left:margin.left,right:width-margin.right,times,rows,cursor};
    if(selected>=0)show(selected,false);
  }
  function show(index,visible=true){if(!layout)return;selected=Math.max(0,Math.min(rows.length-1,index));const row=rows[selected],xx=layout.x(selected);layout.cursor.setAttribute('x1',xx);layout.cursor.setAttribute('x2',xx);layout.cursor.setAttribute('visibility',visible?'visible':'hidden');tooltip.innerHTML=`<strong>${e(row.label||shortDate(row.date))}</strong>${series.map(s=>`<div class="tooltip-row"><span>${e(s.label)}</span><b>${e(number(row.values[s.key])===null?'Unavailable':(unit==='$'?'$':'')+fixed(row.values[s.key],2)+(unit!=='$'?' '+unit:''))}</b></div>`).join('')}${row.warnings?.length?'<div class="tooltip-row"><span style="color:var(--amber)">Source exception</span></div>':''}`;tooltip.style.left=Math.max(8,Math.min(layout.width-235,xx+10))+'px';tooltip.hidden=!visible;if(onSelect)onSelect(row,selected);}
  function pointer(event){if(!layout)return;const bounds=svg.getBoundingClientRect();const x=event.clientX-bounds.left;let index=0;let distance=Infinity;rows.forEach((_,i)=>{const next=Math.abs(layout.x(i)-x);if(next<distance){distance=next;index=i;}});show(index);}
  svg.addEventListener('pointermove',pointer);svg.addEventListener('click',pointer);svg.addEventListener('pointerleave',()=>{tooltip.hidden=true;if(layout)layout.cursor.setAttribute('visibility','hidden');});svg.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?rows.length-1:selected<0?rows.length-1:selected+(event.key==='ArrowLeft'?-1:1);show(next);}if(event.key==='Escape'){tooltip.hidden=true;if(layout)layout.cursor.setAttribute('visibility','hidden');}});svg.addEventListener('blur',()=>{tooltip.hidden=true;});
  const observer=typeof ResizeObserver==='function'?new ResizeObserver(draw):null;observer?.observe(container);draw();
  return ()=>{active=false;observer?.disconnect();};
}
