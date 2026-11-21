import test from 'node:test';
import assert from 'node:assert/strict';
import {buildChartLayout,formatChartValue,nearestChartIndex,mountChart} from '../public/assets/charts.js';

const DAY=86400000;
const stamp=day=>Date.parse('2026-11-'+String(day).padStart(2,'0')+'T00:00:00Z');
const row=(day,value,extra={})=>({date:new Date(stamp(day)).toISOString(),values:{v:value},...extra});
const series=[{key:'v',label:'Value',color:'#4c9af8'}];
const options=(rows,extra={})=>({rows,series,...extra});
const inside=(value,start,end)=>assert.ok(value>=start-1e-8&&value<=end+1e-8,`${value} outside ${start}..${end}`);

test('sparse bars use the observed span and real elapsed gaps within a longer selected range',()=>{
  const layout=buildChartLayout(options([row(2,10),row(4,20),row(11,30),row(20,40)],{type:'bar',xDomain:{start:stamp(2)-150*DAY,end:stamp(21)-1}}));
  assert.equal(layout.lower,stamp(2));assert.equal(layout.upper,stamp(20));
  const [a,b,c,d]=layout.points;
  assert.ok(Math.abs((b.x-a.x)/(d.x-a.x)-2/18)<1e-12);
  assert.ok(Math.abs((c.x-a.x)/(d.x-a.x)-9/18)<1e-12);
  assert.ok(d.x-a.x>layout.plot.width*.8);
  for(const bar of layout.bars){inside(bar.x,layout.plot.left,layout.plot.right);inside(bar.x+bar.width,layout.plot.left,layout.plot.right);}
});

test('UTC boundary filtering removes invalid and out-of-window scale outliers before sorting and picking',()=>{
  const rows=[row(20,20),row(2,1e12),{date:null,values:{v:1e20}},row(14,14),row(21,1e15),{date:'invalid',values:{v:10}}];
  const layout=buildChartLayout(options(rows,{xDomain:{start:stamp(14),end:stamp(21)-1}}));
  assert.deepEqual(layout.points.map(point=>point.originalIndex),[3,0]);
  assert.ok(layout.domain.max<100);
  assert.equal(nearestChartIndex(layout,layout.points[0].x),0);
  assert.equal(nearestChartIndex(layout,layout.points[1].x),1);
  assert.deepEqual(rows.map(item=>item.date),[row(20,0).date,row(2,0).date,null,row(14,0).date,row(21,0).date,'invalid']);
  const window=buildChartLayout(options(rows,{xDomain:{start:stamp(14),end:stamp(21)-1},domainMode:'range'}));
  assert.equal(window.lower,stamp(14));assert.equal(window.upper,stamp(21)-1);
});

test('single observations are centered and missing, empty and reported zero remain distinct',()=>{
  for(const type of ['bar','line','area','stacked']){
    const one=buildChartLayout(options([row(20,0)],{type,xDomain:{start:stamp(1),end:stamp(21)}}));
    assert.equal(one.status,'ready');assert.equal(one.points.length,1);
    assert.equal(one.points[0].x,(one.plot.left+one.plot.right)/2);
    assert.equal(one.xTicks.length,1);
    if(type==='bar'||type==='stacked'){assert.equal(one.bars.length,1);assert.equal(one.bars[0].zero,true);assert.equal(one.bars[0].height,1);}
    else {assert.equal(one.segments.length,1);assert.equal(one.segments[0].points.length,1);}
  }
  assert.equal(buildChartLayout(options([])).status,'empty');
  assert.equal(buildChartLayout(options([row(20,null)])).status,'missing');
  const mixed=buildChartLayout(options([row(2,null),row(4,0),row(20,'')],{type:'bar'}));
  assert.equal(mixed.bars.length,1);assert.equal(mixed.bars[0].index,1);
});

test('positive and negative stacks share a true zero baseline and incomplete components are explicit',()=>{
  const layout=buildChartLayout({type:'stacked',series:[...series,{key:'b',label:'B',color:'#80b8bb'},{key:'c',label:'C',color:'#fff'}],rows:[row(2,10,{values:{v:10,b:20,c:-5}}),row(20,5,{values:{v:5,b:null,c:-10}})]});
  assert.deepEqual(layout.bars.filter(bar=>bar.index===0).map(bar=>[bar.start,bar.end]),[[0,10],[10,30],[0,-5]]);
  assert.equal(layout.bars.filter(bar=>bar.index===1).length,2);
  assert.ok(layout.bars.filter(bar=>bar.index===1).every(bar=>bar.partial));
  for(const bar of layout.bars){inside(bar.y,layout.plot.top,layout.plot.bottom);inside(bar.y+bar.height,layout.plot.top,layout.plot.bottom);}
});

test('line segments preserve explicit missing points and elapsed UTC-day gaps without fake zero links',()=>{
  const rows=[row(1,10),row(2,null),row(3,30),row(10,40)];
  assert.deepEqual(buildChartLayout(options(rows)).segments.map(segment=>segment.points.map(point=>point.index)),[[0],[2,3]]);
  assert.deepEqual(buildChartLayout(options(rows,{connectGaps:true})).segments.map(segment=>segment.points.map(point=>point.index)),[[0,2,3]]);
  assert.deepEqual(buildChartLayout(options(rows,{connectGaps:true,maxGapDays:1})).segments.map(segment=>segment.points.map(point=>point.index)),[[0],[2],[3]]);
  const withinDay=[{date:'2026-11-01T23:59:59Z',values:{v:1}},{date:'2026-11-02T00:00:01Z',values:{v:2}}];
  assert.equal(buildChartLayout(options(withinDay,{maxGapDays:1})).segments.length,1);
});

test('interval bars retain both source timestamps and full values while clipping at the selected boundary',()=>{
  const original=row(17,140,{startDate:new Date(stamp(3)).toISOString()});
  const layout=buildChartLayout(options([original],{type:'bar',interval:true,xDomain:{start:stamp(10),end:stamp(21)-1}}));
  assert.equal(layout.points[0].start,stamp(3));assert.equal(layout.points[0].clippedStart,true);
  assert.equal(layout.lower,stamp(10));assert.equal(layout.upper,stamp(17));
  assert.equal(layout.bars[0].value,140);assert.equal(original.values.v,140);
  inside(layout.bars[0].x,layout.plot.left,layout.plot.right);inside(layout.bars[0].x+layout.bars[0].width,layout.plot.left,layout.plot.right);
  assert.equal(buildChartLayout(options([row(17,140)],{type:'bar',interval:true})).bars.length,0);
  assert.equal(buildChartLayout(options([row(17,140,{startDate:row(20,0).date})],{type:'bar',interval:true})).status,'missing');
  assert.equal(buildChartLayout(options([{date:null,startDate:null,values:{v:140}}],{type:'bar',interval:true})).status,'empty');
});

test('duplicate timestamp bars keep separate marks and hit selection without inventing dates',()=>{
  const layout=buildChartLayout(options([row(2,1),row(2,2),row(20,3)],{type:'bar'}));
  assert.equal(layout.points[0].time,layout.points[1].time);
  assert.ok(layout.bars[0].x+layout.bars[0].width<=layout.bars[1].x);
  assert.equal(nearestChartIndex(layout,layout.points[0].x,1),1);
});

test('responsive layouts keep endpoints and marks within the viewport with unscaled readable geometry',()=>{
  for(const width of [180,320,430,700,1100])for(const type of ['line','area','bar','stacked']){
    const layout=buildChartLayout(options([row(1,-15),row(20,20)],{type}),width);
    assert.equal(layout.width,width);assert.equal(layout.height,width<450?250:260);
    for(const segment of layout.segments)for(const point of segment.points){inside(point.x-3,layout.plot.left,layout.plot.right);inside(point.x+3,layout.plot.left,layout.plot.right);inside(point.y-3,layout.plot.top,layout.plot.bottom);inside(point.y+3,layout.plot.top,layout.plot.bottom);}
    for(const bar of layout.bars){inside(bar.x,layout.plot.left,layout.plot.right);inside(bar.x+bar.width,layout.plot.left,layout.plot.right);inside(bar.y+bar.height,layout.plot.top,layout.plot.bottom);}
  }
});

test('tooltip whole-unit rounding preserves signed tiny nonzero values, precision and missing values',()=>{
  for(const [value,unit,expected]of [[1234.5,'$','$1,235'],[-1234.5,'$','−$1,235'],[0,'$','$0'],[-0,'$','$0'],[.1,'$','<$1'],[-.1,'$','−<$1'],['-0.000000000000001','USDC','−<1 USDC'],['1e-400','USDC','<1 USDC'],[.2,'%','<1%'],[12.6,'%','13%'],['9007199254740993.8','$','$9,007,199,254,740,994'],[null,'$','Unavailable'],['','$','Unavailable'],[false,'$','Unavailable']])assert.equal(formatChartValue(value,unit),expected);
});

function domHarness({width=600,svgWidth=300}={}){
  const priorDocument=globalThis.document,priorObserver=globalThis.ResizeObserver;
  const observers=[];
  class Element{
    constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.style={};this.events={};this.parentElement=null;this.hidden=false;this.innerHTML='';this.textContent='';this.offsetParent={};}
    setAttribute(key,value){this.attrs[key]=String(value);}getAttribute(key){return this.attrs[key]??null;}
    append(...nodes){for(const item of nodes){item.parentElement=this;this.children.push(item);}}
    replaceChildren(...nodes){this.children=[];this.append(...nodes);}
    addEventListener(key,handler){(this.events[key]??=[]).push(handler);}
    dispatch(key,event={}){for(const handler of this.events[key]||[])handler({target:this,preventDefault(){},...event});}
    closest(selector){if(selector==='[data-index]'&&this.attrs['data-index']!==undefined)return this;return this.parentElement?.closest(selector)||null;}
    getBoundingClientRect(){return {left:this.tagName==='svg'?30:10,width:this.tagName==='svg'?svgWidth:this.className==='chart-tooltip'?180:width,height:260};}
  }
  globalThis.document={createElementNS:(_,tag)=>new Element(tag),createElement:tag=>new Element(tag),documentElement:{clientWidth:1200}};
  globalThis.ResizeObserver=class{constructor(callback){this.callback=callback;this.disconnected=false;observers.push(this);}observe(){}disconnect(){this.disconnected=true;}};
  const container=new Element('container');
  return {container,observers,find(root,tag){return root.children.flatMap(child=>[...(child.tagName===tag?[child]:[]),...this.find(child,tag)]);},restore(){globalThis.document=priorDocument;globalThis.ResizeObserver=priorObserver;}};
}

test('SVG rendering clips marks once, preserves all endpoint dots and defines an area gradient once across gaps',()=>{
  const dom=domHarness();try{
    const stop=mountChart(dom.container,options([row(1,10),row(2,null),row(3,30),row(20,40)],{type:'area'}));
    const svg=dom.container.children[0];
    assert.equal(dom.find(svg,'clipPath').length,1);
    assert.equal(dom.find(svg,'linearGradient').length,1);
    assert.equal(dom.find(svg,'circle').length,3);
    assert.ok(dom.find(svg,'g')[0].attrs['clip-path'].startsWith('url(#chart-'));
    assert.match(dom.find(svg,'desc')[0].textContent,/Missing values are not zero/);
    stop();assert.equal(dom.observers[0].disconnected,true);
  }finally{dom.restore();}
});

test('scaled SVG pointer coordinates, keyboard selection and UTC tooltip seconds select the actual source row',()=>{
  const dom=domHarness();try{
    const selected=[],rows=[row(20,20,{detail:'<img src=x onerror=alert(1)>'}),{date:'2026-11-14T07:22:31Z',values:{v:-.2}},row(2,1e12)];
    const config=options(rows,{type:'bar',unit:'USDC',xDomain:{start:stamp(14),end:stamp(21)-1},onSelect:(source,index)=>selected.push({source,index})});
    const stop=mountChart(dom.container,config),[svg,tooltip,announcement]=dom.container.children;
    const layout=buildChartLayout(config,600),last=layout.points.at(-1);
    svg.dispatch('pointermove',{clientX:30+last.x/600*300});
    assert.equal(selected.at(-1).index,0);assert.equal(selected.at(-1).source,rows[0]);
    assert.doesNotMatch(tooltip.innerHTML,/<img\b/);assert.match(tooltip.innerHTML,/&lt;img/);
    svg.dispatch('keydown',{key:'Home'});assert.equal(selected.at(-1).index,1);
    assert.match(tooltip.innerHTML,/07:22:31 UTC/);assert.match(tooltip.innerHTML,/−&lt;1 USDC/);
    assert.match(announcement.textContent,/07:22:31 UTC/);
    svg.dispatch('keydown',{key:'End'});assert.equal(selected.at(-1).index,0);
    svg.dispatch('keydown',{key:'Escape'});assert.equal(tooltip.hidden,true);
    svg.dispatch('focus');assert.equal(tooltip.hidden,false);svg.dispatch('blur');assert.equal(tooltip.hidden,true);
    const before=selected.length;stop();svg.dispatch('keydown',{key:'Home'});assert.equal(selected.length,before);
  }finally{dom.restore();}
});

test('adjacent sparse observations keep both marks and skip crowded date labels',()=>{
  const input = options([row(2,10),row(4,20),row(11,25),row(20,30),row(21,42)],{type:'bar'});
  for (const width of [330,575]) {
    const layout=buildChartLayout(input,width);
    assert.equal(layout.bars.length,5);
    assert.equal(layout.xTicks[0].time,stamp(2));
    assert.equal(layout.xTicks.at(-1).time,stamp(21));
    assert.ok(layout.xTicks.slice(1).every((tick,index)=>tick.x-layout.xTicks[index].x>=64));
  }
});
