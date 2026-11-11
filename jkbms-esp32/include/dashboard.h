#pragma once
#include <Arduino.h>

static const char DASHBOARD_HTML[] PROGMEM = R"HTML(<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JK-BMS</title>
<style>
:root{--bg:#0f1419;--card:#18202a;--line:#263241;--tx:#e6edf3;--mut:#8b98a5;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.4 system-ui,sans-serif}
header{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--line)}
h1{font-size:17px;margin:0;font-weight:600}
.pill{padding:3px 10px;border-radius:99px;font-size:12px;background:var(--line);color:var(--mut)}
.pill.ok{background:#12351b;color:var(--ok)}.pill.bad{background:#3d1517;color:var(--bad)}
main{max-width:980px;margin:0 auto;padding:16px;display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card h2{margin:0 0 6px;font-size:12px;font-weight:500;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.big{font-size:34px;font-weight:600;font-variant-numeric:tabular-nums}
.big small{font-size:15px;color:var(--mut);font-weight:400;margin-left:3px}
.sub{color:var(--mut);font-size:13px;margin-top:2px}
.wide{grid-column:1/-1}
.bar{height:10px;border-radius:5px;background:var(--line);overflow:hidden;margin-top:10px}
.bar>i{display:block;height:100%;background:var(--ok);width:0;transition:width .5s}
.cells{display:flex;gap:8px;align-items:flex-end;height:130px;margin-top:8px}
.cell{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;height:100%}
.cell i{display:block;width:100%;border-radius:4px 4px 0 0;background:var(--acc);min-height:2px;transition:height .5s}
.cell.max i{background:var(--warn)}.cell.min i{background:var(--bad)}
.cell span{font-size:11px;color:var(--mut);margin-top:4px;font-variant-numeric:tabular-nums}
.row{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--line)}
.row:last-child{border:0}.row b{font-variant-numeric:tabular-nums;font-weight:500}
canvas{width:100%;height:150px;display:block}
.day{display:grid;grid-template-columns:54px 1fr 92px;gap:8px;align-items:center;padding:4px 0;font-size:13px}
.day .t{height:8px;background:var(--line);border-radius:4px;position:relative;overflow:hidden}
.day .t i{position:absolute;left:0;top:0;height:100%;border-radius:4px}
.day .t i.d{background:var(--warn)}.day .t i.c{background:var(--ok);opacity:.55;height:50%;top:50%}
.day.today{color:var(--tx);font-weight:600}.day b{font-variant-numeric:tabular-nums;font-weight:500;text-align:right}
.leg{font-size:12px;color:var(--mut);margin-bottom:6px}.leg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 10px;vertical-align:-1px}
</style></head><body>
<header><h1>JK-BMS</h1><div><span id="age" class="pill">...</span> <span id="st" class="pill">...</span></div></header>
<main>
<div class="card"><h2>Заряд (SOC)</h2><div class="big"><span id="soc">–</span><small>%</small></div>
<div class="bar"><i id="socb"></i></div><div class="sub"><span id="rem">–</span> / <span id="nom">–</span> А·год</div></div>
<div class="card"><h2>Напруга</h2><div class="big"><span id="v">–</span><small>В</small></div><div class="sub">комірка сер. <span id="avg">–</span> В</div></div>
<div class="card"><h2>Струм</h2><div class="big"><span id="i">–</span><small>А</small></div><div class="sub" id="dir">–</div></div>
<div class="card"><h2>Потужність</h2><div class="big"><span id="p">–</span><small>Вт</small></div><div class="sub">SOH <span id="soh">–</span>% · цикли <span id="cyc">–</span></div></div>
<div class="card wide"><h2>Комірки</h2><div class="cells" id="cells"></div>
<div class="sub">різниця <span id="delta">–</span> мВ</div></div>
<div class="card"><h2>Температури</h2>
<div class="row"><span>MOS</span><b id="tm">–</b></div>
<div class="row"><span>Датчик 1</span><b id="t1">–</b></div>
<div class="row"><span>Датчик 2</span><b id="t2">–</b></div></div>
<div class="card"><h2>Стан</h2>
<div class="row"><span>MOS заряду</span><b id="cm">–</b></div>
<div class="row"><span>MOS розряду</span><b id="dm">–</b></div>
<div class="row"><span>Балансування</span><b id="bal">–</b></div>
<div class="row"><span>Wi-Fi</span><b id="rssi">–</b></div></div>
<div class="card wide"><h2>Час від батареї по днях</h2>
<div class="leg"><i style="background:var(--warn)"></i>розряд (від батареї)<i style="background:var(--ok)"></i>заряд<span id="dnote"></span></div>
<div id="days"></div></div>
<div class="card wide"><h2>Заряд SOC, % (24 год)</h2><canvas id="c_soc"></canvas></div>
<div class="card wide"><h2>Потужність, Вт (24 год; мінус = розряд)</h2><canvas id="c_p"></canvas></div>
<div class="card wide"><h2>Напруга, В (24 год)</h2><canvas id="c_v"></canvas></div>
</main>
<script>
const $=id=>document.getElementById(id);
let lim={ovp:3650,uvp:2600};
function onoff(x){return x?'увімк.':'вимк.'}
async function loadSettings(){try{const s=await(await fetch('/api/settings')).json();if(s.cell_ovp_mv){lim.ovp=s.cell_ovp_mv;lim.uvp=s.cell_uvp_mv}}catch(e){}}
function chart(id,arr,age,step,col,dec,min0,max0){
const c=$(id),dpr=window.devicePixelRatio||1,W=c.clientWidth,H=c.clientHeight;
c.width=W*dpr;c.height=H*dpr;const x=c.getContext('2d');x.scale(dpr,dpr);x.clearRect(0,0,W,H);
if(arr.length<2){x.fillStyle='#8b98a5';x.font='12px sans-serif';x.fillText('Накопичується історія...',8,20);return}
let lo=Math.min(...arr),hi=Math.max(...arr);if(min0!==undefined)lo=Math.min(lo,min0);if(max0!==undefined)hi=Math.max(hi,max0);
if(hi-lo<1e-6){hi+=1;lo-=1}const pad=(hi-lo)*.08;lo-=pad;hi+=pad;
const L=44,B=18,T=6,R=6,pw=W-L-R,ph=H-T-B;
x.font='11px sans-serif';x.fillStyle='#8b98a5';x.strokeStyle='#263241';x.lineWidth=1;
for(let k=0;k<=3;k++){const v=lo+(hi-lo)*k/3,y=T+ph-(v-lo)/(hi-lo)*ph;x.beginPath();x.moveTo(L,y);x.lineTo(W-R,y);x.stroke();x.fillText(v.toFixed(dec),2,y+4)}
const n=arr.length,span=(n-1)*step,t1=Date.now()/1000-age,t0=t1-span;
for(let t=Math.ceil(t0/10800)*10800;t<=t1;t+=10800){const px=L+(t-t0)/span*pw;x.beginPath();x.moveTo(px,T);x.lineTo(px,T+ph);x.stroke();
x.fillText(new Date(t*1000).toLocaleTimeString('uk',{hour:'2-digit',minute:'2-digit'}),px-14,H-3)}
if(lo<0&&hi>0){const y=T+ph-(0-lo)/(hi-lo)*ph;x.strokeStyle='#4a5a6e';x.beginPath();x.moveTo(L,y);x.lineTo(W-R,y);x.stroke()}
x.strokeStyle=col;x.lineWidth=2;x.beginPath();
arr.forEach((v,k)=>{const px=L+k/(n-1)*pw,py=T+ph-(v-lo)/(hi-lo)*ph;k?x.lineTo(px,py):x.moveTo(px,py)});x.stroke()}
function hm(s){const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h+' год '+String(m).padStart(2,'0')+' хв'}
function dstr(k){const s=String(k);return s.slice(6)+'.'+s.slice(4,6)}
async function loadDays(){try{const r=await(await fetch('/api/days')).json();
$('dnote').textContent=r.valid?'':' · час не синхронізовано, облік стоїть';
const ds=r.days.sort((a,b)=>b.d-a.d).slice(0,14);let h='';
for(const d of ds){const td=d.d===r.today;h+='<div class="day'+(td?' today':'')+'"><span>'+dstr(d.d)+(td?' *':'')+'</span><div class="t"><i class="d" style="width:'+(d.disc/864)+'%"></i><i class="c" style="width:'+(d.chg/864)+'%"></i></div><b>'+hm(d.disc)+'</b></div>'}
$('days').innerHTML=h||'<div class="sub">Даних ще немає (облік іде від моменту синхронізації часу). Рахується час, коли струм розряду більше '+r.threshold+' А.</div>'}catch(e){}}
async function loadHist(){try{const r=await(await fetch('/api/history')).json();
chart('c_soc',r.soc,r.age,r.step,'#3fb950',0,0,100);
chart('c_p',r.p,r.age,r.step,'#58a6ff',0);
chart('c_v',r.v.map(v=>v/100),r.age,r.step,'#d29922',1)}catch(e){}}
async function tick(){
try{const r=await(await fetch('/api/state')).json();const d=r.data||{};
$('st').textContent=r.online?'BMS онлайн':'BMS немає даних';$('st').className='pill '+(r.online?'ok':'bad');
$('age').textContent=r.age<0?'нема даних':'оновлено '+r.age+' с тому';
if(!d.voltage&&d.voltage!==0)return;
$('soc').textContent=d.soc;$('socb').style.width=d.soc+'%';
$('socb').style.background=d.soc<20?'var(--bad)':d.soc<40?'var(--warn)':'var(--ok)';
$('rem').textContent=d.remaining_ah.toFixed(1);$('nom').textContent=d.nominal_ah.toFixed(0);
$('v').textContent=d.voltage.toFixed(2);$('avg').textContent=d.cell_avg.toFixed(3);
$('i').textContent=d.current.toFixed(2);$('dir').textContent=d.current<-0.05?'розряд':d.current>0.05?'заряд':'спокій';
$('p').textContent=d.power.toFixed(0);$('soh').textContent=d.soh;$('cyc').textContent=d.cycles;
$('tm').textContent=d.t_mos.toFixed(1)+' °C';$('t1').textContent=d.t1.toFixed(1)+' °C';$('t2').textContent=d.t2.toFixed(1)+' °C';
$('cm').textContent=onoff(d.charge_mos);$('dm').textContent=onoff(d.discharge_mos);
$('bal').textContent=d.balancing?'йде ('+d.balance_current.toFixed(2)+' А)':'вимк.';
$('rssi').textContent=r.rssi+' дБм';$('delta').textContent=(d.cell_delta*1000).toFixed(0);
let hs='';const n=d.cells_count,lo=lim.uvp/1000,hi=lim.ovp/1000;
for(let k=1;k<=n;k++){const v=d['cell'+k];if(v===undefined)continue;
const pct=Math.max(2,Math.min(100,(v-lo)/(hi-lo)*100));
const cls=v===d.cell_max?'max':v===d.cell_min?'min':'';
hs+='<div class="cell '+cls+'"><i style="height:'+pct+'%"></i><span>'+v.toFixed(3)+'</span></div>'}
$('cells').innerHTML=hs;
}catch(e){$('st').textContent='немає зв\'язку з платою';$('st').className='pill bad'}}
loadSettings();tick();loadHist();loadDays();setInterval(tick,2000);setInterval(loadHist,60000);setInterval(loadDays,60000);addEventListener('resize',loadHist);
</script></body></html>)HTML";
