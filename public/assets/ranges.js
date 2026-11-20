/** Shared UTC calendar windows. Financial publications are never daily flows. */
export const RANGE_OPTIONS=[['7d','7D'],['30d','30D'],['90d','90D'],['180d','180D'],['1y','1Y'],['all','All']];
const DAY=86400000;
export const normalizeRange=value=>RANGE_OPTIONS.some(([key])=>key===value)?value:'all';
export function timeValue(value){if(value===null||value===undefined||value==='')return null;const ms=typeof value==='number'?value:Date.parse(value);return Number.isFinite(ms)?ms:null;}
export function latestTimestamp(values){const valid=values.map(timeValue).filter(value=>value!==null);return valid.length?new Date(Math.max(...valid)).toISOString():null;}
export function rangeBounds(key,anchor){
  key=normalizeRange(key);const ms=timeValue(anchor);
  if(ms===null)return {key,start:null,endExclusive:null};
  const d=new Date(ms),day=Date.UTC(d.getUTCFullYear(),d.getUTCMonth(),d.getUTCDate()),endExclusive=day+DAY;
  if(key==='all')return {key,start:null,endExclusive};
  let start;
  if(key==='1y'){
    const end=new Date(endExclusive),year=end.getUTCFullYear()-1,month=end.getUTCMonth();
    const last=new Date(Date.UTC(year,month+1,0)).getUTCDate();
    start=Date.UTC(year,month,Math.min(end.getUTCDate(),last));
  }else start=day-(Number(key.slice(0,-1))-1)*DAY;
  return {key,start,endExclusive};
}
export function filterRange(rows,key,anchor,getDate=row=>row.date){
  const bounds=rangeBounds(key,anchor);
  if(bounds.key!=='all'&&bounds.endExclusive===null)return [];
  return rows.filter(row=>{const ms=timeValue(getDate(row));return ms!==null&&(bounds.start===null||ms>=bounds.start)&&(bounds.endExclusive===null||ms<bounds.endExclusive);});
}
export function chartDomain(rows,key,anchor){
  const bounds=rangeBounds(key,anchor);let start=bounds.start;
  if(start===null){const times=rows.map(row=>timeValue(row.date)).filter(value=>value!==null);if(!times.length)return undefined;const first=new Date(Math.min(...times));start=Date.UTC(first.getUTCFullYear(),first.getUTCMonth(),first.getUTCDate());}
  if(bounds.endExclusive===null)return undefined;
  return {start,end:bounds.endExclusive-1};
}
const numeric=value=>value===null||value===undefined||value===''?null:Number.isFinite(Number(value))?Number(value):null;
export function periodTotals(reports,keys){return Object.fromEntries(keys.map(key=>{const values=reports.map(report=>numeric(report.period_amounts?.[key])).filter(value=>value!==null);return [key,values.length?values.reduce((total,value)=>total+value,0):null];}));}
export function financialSeries(reports,keys,{mode='period',snapshot=false,range='all',anchor=null}={}){
  const running=Object.fromEntries(keys.map(key=>[key,0]));
  const ordered=[...reports].filter(report=>timeValue(report.published_at)!==null).sort((a,b)=>timeValue(a.published_at)-timeValue(b.published_at));
  // Accumulate from the archive's real beginning, then clip displayed publications.
  const rows=ordered.map(report=>{const values={};for(const key of keys){const value=numeric((snapshot?report.snapshot:report.period_amounts)?.[key]);if(mode==='cumulative'&&!snapshot&&value!==null){running[key]+=value;values[key]=running[key];}else values[key]=value;}return {date:report.published_at,values,warnings:report.warnings||[],source:report.source_urls?.[0],id:report.id};});
  return filterRange(rows,range,anchor);
}
export function rangeHref(href,key){
  if(typeof href!=='string'||!href.startsWith('#/'))return href;
  const [path,query='']=href.slice(1).split('?'),params=new URLSearchParams(query);
  params.set('range',normalizeRange(key));return '#'+path+'?'+params.toString();
}
