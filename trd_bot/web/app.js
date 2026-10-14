const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="panel-token"]').content;
const statusNames = {idle:'尚未啟動',connecting:'正在連接行情',running:'模擬交易中',demo:'離線示範執行中',stopping:'正在安全停止',halting:'風控停機中',stale:'行情資料過期',halted:'已停機，等待審查',finished:'本次執行已完成',failed:'啟動或執行失敗'};
const reasons = {operator_interrupt:'操作人員中斷了執行',kill_switch:'已收到停止指令',restart_requires_review:'重啟前需要審查先前狀態',daily_loss:'已達當日虧損限制',drawdown:'已達回撤限制',exposure:'已達曝險限制',stale_market_data:'行情超時，已停止報價',initial_book_timeout:'30 秒內未收到有效行情',invalid_book_timestamp:'行情時間戳無效',feed_or_io_error:'行情連線或資料寫入失敗',feed_backlog:'行情處理落後',abnormal_spread:'市場價差異常'};
let snapshot = null, busy = false, chartRuntime = null, points = [], lastBook = null, connectionFailed = false;
const number = (v, digits=2) => v === null || v === undefined || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('en-US',{minimumFractionDigits:digits,maximumFractionDigits:digits});
function signed(v){return number(v)==='—'?'—':(Number(v)>0?'+':'')+number(v);}
function text(id,value){$(id).textContent=value;}
function showError(message){text('error',message);$('error').hidden=!message;}
function timeLabel(ms){return ms?new Date(ms).toLocaleTimeString('zh-TW',{hour12:false}):'—';}
function chart(data){
  if(chartRuntime!==data.runtime){points=[];lastBook=null;chartRuntime=data.runtime;}
  const r=data.report;
  if(data.current_report && r.source==='binance_public' && r.last_book_ms && r.last_book_ms!==lastBook && Number.isFinite(Number(r.mark))){
    lastBook=r.last_book_ms;points.push({price:Number(r.mark),time:r.last_book_ms});if(points.length>120)points.shift();
  }
  $('chart-empty').hidden=points.length>=2;
  if(points.length<2){$('chart-line').setAttribute('d','');$('chart-area').setAttribute('d','');text('chart-range','尚無走勢資料');return;}
  const min=Math.min(...points.map(p=>p.price)),max=Math.max(...points.map(p=>p.price));
  const span=max-min||1;
  const coords=points.map((p,i)=>`${(i/(points.length-1)*800).toFixed(1)},${(195-(p.price-min)/span*170).toFixed(1)}`);
  $('chart-line').setAttribute('d','M'+coords.join(' L'));
  $('chart-area').setAttribute('d','M0,220 L'+coords.join(' L')+' L800,220 Z');
  text('chart-range',timeLabel(points[0].time)+' — '+timeLabel(points.at(-1).time));
}
function render(data){
  snapshot=data;const r=data.report,c=data.config;
  text('status',statusNames[data.status]||data.status);
  if(data.status==='halted' && r.source==='synthetic_demo')text('status','離線示範已停止');
  $('status').className='status '+(data.status==='running'?'good':['halted','failed','stale','halting'].includes(data.status)?'bad':['connecting','stopping'].includes(data.status)?'warn':'');
  const historical=Boolean(r.source)&&!data.current_report;
  text('update',data.current_report?'報表更新 '+new Date(r.heartbeat_utc).toLocaleTimeString('zh-TW',{hour12:false}):historical?'目前顯示上次保存的報表':'尚未收到有效報表');
  let notice='先執行離線示範熟悉面板，或開始公開行情模擬。所有資產與成交均為模擬。';
  if(data.status==='connecting')notice='正在連接 Binance，等待有時間戳的有效行情。初始 REST 快照略過是正常情況。';
  if(data.status==='running')notice='正在讀取真實公開行情並模擬成交。沒有成交也屬正常，策略需要等待合適的交易與排隊條件。';
  if(data.status==='demo')notice='正在執行合成行情示範。這只驗證程式機制，不代表真實市場收益。';
  if(data.status==='stopping')notice='已送出停止指令，正在等待 bot 清除模擬掛單。若正在啟動連線，可能需等約 30 秒。';
  if(data.status==='finished')notice=r.source==='synthetic_demo'?'合成行情示範完成。資產與損益為虛擬結果；你可以開始公開行情模擬。':'本次執行已完成，現在顯示保存的報表。';
  if(['halted','halting','stale'].includes(data.status))notice=(reasons[r.halt_reason]||r.halt_reason||'行情暫時無法更新')+'。請檢查日誌與風控，再決定是否恢復。';
  if(data.status==='failed')notice='程序已退出（代碼 '+data.exit_code+'）。請展開執行日誌確認原因。';
  if(data.status==='halted' && r.source==='synthetic_demo')notice='離線示範已停止，模擬掛單已清除。要重新試跑，請再次點「執行離線示範」。';
  text('notice',notice);if(data.error)showError(data.error);
  const hasMark=r.mark!==null&&r.mark!==undefined&&r.mark!=='None';
  text('equity',hasMark?number(r.equity):'—');text('pnl',signed(r.pnl));$('pnl').className=Number(r.pnl)<0?'negative':Number(r.pnl)>0?'positive':'';
  text('base',number(r.base,4));text('cash','可用現金 '+number(r.cash)+' USDT');text('fills',number(r.fills,0));text('fees','手續費 '+number(r.fees,4)+' USDT');text('price',hasMark?number(r.mark):'—');
  text('source',r.source==='synthetic_demo'?'合成行情 · 離線示範':r.source==='binance_public'?'Binance 公開行情 · 本機模擬':'尚未開始');
  text('freshness',historical?'保存的行情':data.book_age_seconds===null?'等待行情':data.book_age_seconds.toFixed(1)+' 秒前更新');
  text('samples',number(r.decision_latency_us?.samples,0));text('latency',r.decision_latency_us?.p99==null?'—':number(r.decision_latency_us.p99/1000,2)+' ms');text('orders',number(r.open_paper_orders,0));
  const percent=(used,cap)=>Math.max(0,Math.min(100,Number(used||0)/Number(cap)*100));
  const inventory=percent(r.base,c.max_base),loss=percent(Math.max(0,-Number(r.daily_pnl||0)),c.max_daily_loss),drawdown=percent(r.drawdown,c.max_drawdown);
  $('inventory').value=inventory;$('loss').value=loss;$('drawdown').value=drawdown;
  text('inventory-label',number(inventory,1)+'%');text('loss-label',number(Math.max(0,-Number(r.daily_pnl||0)))+' USDT');text('drawdown-label',number(r.drawdown)+' USDT');
  text('inventory-cap','上限 '+c.max_base+' BTC');text('loss-cap','上限 '+c.max_daily_loss+' USDT · UTC 日界');text('drawdown-cap','上限 '+c.max_drawdown+' USDT');text('runtime',data.runtime);
  $('start').hidden=data.paper_exists;$('resume').hidden=!data.paper_exists;
  $('start').disabled=busy||data.running||data.paper_locked;$('resume').disabled=busy||data.running||data.paper_locked||Boolean(data.paper_review.error);
  $('demo').disabled=busy||data.running;$('stop').disabled=busy||!data.running||data.status==='stopping';
  $('trades').replaceChildren();
  if(!data.fills.length){const row=document.createElement('tr'),cell=document.createElement('td');cell.colSpan=5;cell.className='empty-table';cell.textContent='尚無成交。公開行情接通後，也不一定立即產生成交。';row.append(cell);$('trades').append(row);}
  for(const fill of data.fills){const row=document.createElement('tr');[timeLabel(fill.received_ms),fill.side==='buy'?'買入':'賣出',number(fill.price),number(fill.amount,6),number(fill.fee,6)].forEach((value,i)=>{const cell=document.createElement('td');cell.textContent=value;if(i===1)cell.className=fill.side==='buy'?'positive':'negative';row.append(cell);});$('trades').append(row);}
  text('logs',data.logs.join('\n')||'尚無執行日誌。');chart(data);
}
async function refresh(){const res=await fetch('/api/status',{cache:'no-store'});const data=await res.json();if(!res.ok)throw new Error(data.error||'無法讀取狀態');render(data);}
async function action(name){
  if(busy)return;busy=true;showError('');if(snapshot)render(snapshot);
  try{const res=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-Panel-Token':token},body:JSON.stringify({action:name})});const data=await res.json();if(!res.ok)throw new Error(data.error||'操作失敗');await refresh();}
  catch(error){showError(error.message);}finally{busy=false;if(snapshot)render(snapshot);}
}
document.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click',()=>action(button.dataset.action)));
$('resume').addEventListener('click',()=>{const review=snapshot.paper_review;text('resume-reason','公開行情帳戶停機原因：'+(reasons[review.halt_reason]||review.halt_reason||'上次執行已結束，需要審查後重啟')+'。現金 '+number(review.cash)+' USDT / 持倉 '+number(review.base,4)+' BTC。');text('resume-logs',snapshot.paper_logs.join('\n')||'尚無此帳戶的面板日誌。');$('ack').checked=false;$('confirm-resume').disabled=true;$('resume-dialog').showModal();});
$('ack').addEventListener('change',()=>{$('confirm-resume').disabled=!$('ack').checked;});
$('confirm-resume').addEventListener('click',()=>{if(!$('ack').checked)return;$('resume-dialog').close();action('resume');});
async function poll(){try{await refresh();if(connectionFailed){connectionFailed=false;showError(snapshot.error||'');}}catch(error){connectionFailed=true;showError('面板連線中斷：'+error.message);text('status','面板服務未連線');document.querySelectorAll('button[data-action],#resume').forEach(button=>button.disabled=true);}setTimeout(poll,1000);}
poll();
