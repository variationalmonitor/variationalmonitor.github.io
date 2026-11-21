/** Read timed public snapshots without authenticating to Lighter. */
const ids = new Set(process.argv.slice(2));
if (!ids.size || ids.size > 4 || [...ids].some(id=>!/^\d{1,6}$/.test(id))) process.exit(2);
const ws = new WebSocket('wss://mainnet.zklighter.elliot.ai/stream');
const results = {};
let done = false;
function finish() {
  if (done) return;
  done = true;
  clearTimeout(timeout);
  ws.close();
  process.stdout.write(JSON.stringify(results),()=>process.exit(0));
}
const timeout = setTimeout(finish,6500);
ws.addEventListener('error',finish);
ws.addEventListener('close',finish);
ws.addEventListener('message',({data})=>{
  if (typeof data!=='string' || data.length>2_000_000) return;
  let msg;
  try { msg=JSON.parse(data); } catch { return; }
  if (msg.type==='connected') {
    for (const id of ids) ws.send(JSON.stringify({type:'subscribe',channel:'order_book/'+id}));
    return;
  }
  if (msg.type==='ping') { ws.send('{"type":"pong"}'); return; }
  if (msg.type!=='subscribed/order_book') return;
  const id = /^order_book:(\d+)$/.exec(msg.channel)?.[1];
  const updated = msg.last_updated_at ?? msg.order_book?.last_updated_at;
  if (!ids.has(id) || !Number.isSafeInteger(updated) || updated<1e14) return;
  const {asks,bids} = msg.order_book || {};
  if (!Array.isArray(asks) || !Array.isArray(bids)) return;
  results[id] = {time:Math.floor(updated/1000),asks:asks.slice(0,1500).map(({price,size})=>[price,size]),bids:bids.slice(0,1500).map(({price,size})=>[price,size])};
  ids.delete(id);
  if (!ids.size) finish();
});
