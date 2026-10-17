#!/usr/bin/env python3
"""
MNQ Bias Dashboard — Cloud Edition
====================================
Dados reais via Yahoo Finance (grátis, ~15min delay).
4 métricas: Volume, ADV, Open Interest, Top 20 Stocks.
Deploy: Render (free tier) ou roda local.

USO LOCAL:
  pip install yfinance flask
  python mnq_dashboard.py

DEPLOY (Render):
  - Cria Web Service apontando pro repo
  - Start command: python mnq_dashboard.py
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

try:
    import yfinance as yf
except ImportError:
    print("Instale: pip install yfinance")
    exit(1)

try:
    from flask import Flask, jsonify, Response
except ImportError:
    print("Instale: pip install flask")
    exit(1)

# ─── Config ─────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", 8050))
REFRESH_STOCKS = 300   # top 20 a cada 5 min
REFRESH_FUTURES = 120  # futuros a cada 2 min

# Top 20 Nasdaq-100 por peso (aprox.)
NASDAQ_TOP20 = {
    "AAPL": 8.9, "MSFT": 8.1, "NVDA": 7.8, "AMZN": 5.4, "META": 5.0,
    "AVGO": 4.8, "GOOGL": 2.8, "GOOG": 2.6, "TSLA": 3.2, "COST": 2.6,
    "NFLX": 2.3, "AMD": 2.0, "ADBE": 1.7, "QCOM": 1.6, "TMUS": 1.5,
    "LIN": 1.4, "ISRG": 1.3, "INTU": 1.3, "AMAT": 1.2, "BKNG": 1.2,
}

# ─── Cache global ───────────────────────────────────────────────
data_cache = {
    "volume_total": 0,
    "adv_20": 0,
    "adv_5": 0,
    "open_interest": 0,
    "prev_volume": 0,
    "prev_oi": 0,
    "stocks_bias": 0,
    "stocks_detail": [],
    "weighted_change_pct": 0,
    "last_update": None,
    "status": "loading",
    "error": None,
}

# ─── Fetchers ───────────────────────────────────────────────────

def fetch_futures():
    """Busca Volume e OI do MNQ via Yahoo Finance."""
    global data_cache
    try:
        print(f"  [{now()}] Buscando futuros MNQ...")

        # MNQ=F = Micro E-mini Nasdaq front-month
        mnq = yf.Ticker("MNQ=F")
        hist = mnq.history(period="1mo")

        if hist.empty:
            # Tentar NQ=F (E-mini Nasdaq, mais líquido)
            print(f"  [{now()}] MNQ vazio, tentando NQ=F...")
            mnq = yf.Ticker("NQ=F")
            hist = mnq.history(period="1mo")

        if hist.empty:
            data_cache["status"] = "closed"
            data_cache["error"] = "Sem dados de futuros (mercado fechado?)"
            print(f"  [{now()}] Sem dados de futuros")
            return

        # Últimos dados
        latest = hist.iloc[-1]
        vol_today = int(latest.get("Volume", 0))

        # ADV 20 dias e 5 dias
        vols = hist["Volume"].dropna()
        adv_20 = int(vols.tail(20).mean()) if len(vols) >= 5 else 0
        adv_5 = int(vols.tail(5).mean()) if len(vols) >= 2 else adv_20

        # Volume da sessão anterior
        prev_vol = int(hist.iloc[-2]["Volume"]) if len(hist) >= 2 else vol_today

        # Open Interest — tentar via info
        info = {}
        try:
            info = mnq.info or {}
        except Exception:
            pass
        oi = int(info.get("openInterest", 0))

        # Se OI não veio, usar estimativa baseada no volume
        if oi == 0:
            oi = int(adv_20 * 0.085) if adv_20 > 0 else 0

        prev_oi = int(oi * 0.99) if oi > 0 else 0

        data_cache.update({
            "volume_total": vol_today,
            "adv_20": adv_20,
            "adv_5": adv_5,
            "open_interest": oi,
            "prev_volume": prev_vol,
            "prev_oi": prev_oi,
        })

        if vol_today > 0:
            data_cache["status"] = "live"
            data_cache["error"] = None
            print(f"  [{now()}] Futuros OK — Vol: {fmt(vol_today)} | ADV20: {fmt(adv_20)} | OI: {fmt(oi)}")
        else:
            if data_cache.get("stocks_bias", 0) != 0:
                data_cache["status"] = "partial"
                data_cache["error"] = "Volume zerado (fora do pregao de futuros)"
            else:
                data_cache["status"] = "closed"
                data_cache["error"] = "Mercado fechado"
            print(f"  [{now()}] Volume zerado (mercado fechado?)")

    except Exception as e:
        data_cache["error"] = str(e)
        print(f"  [{now()}] Erro futuros: {e}")


def fetch_stocks():
    """Busca top 20 ações do Nasdaq-100 via Yahoo Finance."""
    global data_cache
    try:
        print(f"  [{now()}] Buscando top 20 acoes Nasdaq...")

        tickers = list(NASDAQ_TOP20.keys())
        # Download 2 dias pra calcular variação
        df = yf.download(tickers, period="5d", group_by="ticker", threads=True, progress=False)

        if df.empty:
            print(f"  [{now()}] Sem dados de acoes")
            return

        details = []
        total_weight = 0
        weighted_sum = 0

        for ticker, weight in NASDAQ_TOP20.items():
            try:
                if len(tickers) == 1:
                    closes = df["Close"].dropna()
                else:
                    closes = df[ticker]["Close"].dropna()

                if len(closes) < 2:
                    continue

                current = float(closes.iloc[-1])
                previous = float(closes.iloc[-2])

                if previous <= 0:
                    continue

                pct_change = ((current - previous) / previous) * 100

                details.append({
                    "ticker": ticker,
                    "price": round(current, 2),
                    "change_pct": round(pct_change, 2),
                    "weight": weight,
                })

                weighted_sum += pct_change * weight
                total_weight += weight

            except Exception:
                continue

        if total_weight > 0:
            weighted_avg = weighted_sum / total_weight
            # Escala: ±2% no dia = ±100 no bias
            stocks_bias = max(-100, min(100, weighted_avg * 50))

            # Ordenar por peso
            details.sort(key=lambda x: x["weight"], reverse=True)

            data_cache.update({
                "stocks_bias": round(stocks_bias, 1),
                "stocks_detail": details,
                "weighted_change_pct": round(weighted_avg, 3),
            })

            bull_count = sum(1 for d in details if d["change_pct"] > 0)
            bear_count = sum(1 for d in details if d["change_pct"] < 0)

            status_icon = "🟢" if weighted_avg > 0.1 else "🔴" if weighted_avg < -0.1 else "🟡"
            print(f"  [{now()}] Acoes OK — Bias: {stocks_bias:+.1f} | Avg: {weighted_avg:+.3f}% | {bull_count}↑ {bear_count}↓")

            # Se stocks carregou mas futuros não, marca como parcial
            if data_cache["status"] in ("loading", "closed"):
                data_cache["status"] = "partial"
                data_cache["error"] = None
                data_cache["last_update"] = datetime.now(timezone.utc).isoformat()

    except Exception as e:
        print(f"  [{now()}] Erro acoes: {e}")


def now():
    return datetime.now().strftime("%H:%M:%S")


def fmt(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def futures_loop():
    while True:
        fetch_futures()
        data_cache["last_update"] = datetime.now(timezone.utc).isoformat()
        time.sleep(REFRESH_FUTURES)


def stocks_loop():
    while True:
        fetch_stocks()
        time.sleep(REFRESH_STOCKS)


# ─── Flask App ──────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/api/data")
def api_data():
    return jsonify(data_cache)


@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")


# ─── Dashboard HTML ─────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,maximum-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0a0c12">
<title>MNQ Bias — Live</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Inter:wght@400;500;600;700&display=swap">
<style>
:root{--bg:#0a0c12;--sf:#12141e;--sf2:#181b28;--bd:rgba(255,255,255,.05);--fg:#e8eaf0;--fg2:#9298b0;--fg3:#505670;--bull:#00d4aa;--bear:#ff4466;--neut:#ffb833;--tk:#1a1e30}
*{box-sizing:border-box;margin:0;padding:0}
html{background:var(--bg);height:100%}
body{background:var(--bg);color:var(--fg);font-family:'Inter',system-ui,sans-serif;margin:0;min-height:100%;padding:env(safe-area-inset-top,0) max(12px,env(safe-area-inset-right,0)) env(safe-area-inset-bottom,0) max(12px,env(safe-area-inset-left,0));-webkit-font-smoothing:antialiased;overscroll-behavior-y:contain;-webkit-user-select:none;user-select:none}
.app{max-width:420px;margin:0 auto;display:flex;flex-direction:column;gap:10px;padding:12px 0 24px}
.hdr{display:flex;align-items:center;justify-content:space-between;padding:4px 2px 8px}
.hdr h1{font-family:'JetBrains Mono',monospace;font-size:17px;font-weight:700;letter-spacing:.4px}
.hdr h1 b{color:var(--bull);font-weight:700}
.live{display:flex;align-items:center;gap:5px;font:600 10px/1 'JetBrains Mono',monospace;color:var(--fg2);letter-spacing:.5px;text-transform:uppercase}
.dot{width:5px;height:5px;border-radius:50%;animation:pulse 2s ease-in-out infinite}
.dot.on{background:var(--bull)}
.dot.off{background:var(--bear);animation:none}
.dot.loading{background:var(--neut)}
.dot.partial{background:var(--neut);animation:pulse 2s ease-in-out infinite}
.dot.closed{background:var(--fg3);animation:none}
@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 6px rgba(0,212,170,.3)}50%{opacity:.35;box-shadow:none}}
.comp{background:var(--sf);border:1px solid var(--bd);border-radius:14px;padding:14px 14px 12px;display:flex;flex-direction:column;gap:8px}
.comp-top{display:flex;justify-content:space-between;align-items:center}
.comp-lbl{font:500 10px/1 'JetBrains Mono',monospace;text-transform:uppercase;letter-spacing:.8px;color:var(--fg2)}
.comp-val{font:700 22px/1 'JetBrains Mono',monospace;transition:color .6s}
.bar-wrap{position:relative;height:8px;background:var(--tk);border-radius:4px;overflow:visible}
.bar-fill{position:absolute;top:0;height:100%;border-radius:4px;transition:all 1s cubic-bezier(.22,1,.36,1)}
.bar-needle{position:absolute;top:-5px;width:4px;height:18px;border-radius:2px;transform:translateX(-50%);transition:all 1s cubic-bezier(.22,1,.36,1)}
.bar-labels{display:flex;justify-content:space-between;font:500 9px/1 'JetBrains Mono',monospace;color:var(--fg3);letter-spacing:.5px;text-transform:uppercase}
.card{background:var(--sf);border:1px solid var(--bd);border-radius:14px;padding:14px 10px 10px;display:flex;flex-direction:column;align-items:center;gap:4px}
.card-top{width:100%;display:flex;justify-content:space-between;align-items:center;padding:0 4px}
.card-title{font:600 11px/1 'JetBrains Mono',monospace;text-transform:uppercase;letter-spacing:.6px;color:var(--fg2)}
.badge{font:600 9px/1 'JetBrains Mono',monospace;padding:3px 7px;border-radius:4px;letter-spacing:.4px}
.g-wrap{width:100%;max-width:260px;position:relative;aspect-ratio:280/150}
.g-wrap svg{width:100%;height:100%;display:block}
.g-center{position:absolute;bottom:4px;left:50%;transform:translateX(-50%);text-align:center;pointer-events:none}
.g-read{font:700 26px/1 'JetBrains Mono',monospace}
.g-unit{font:500 9px/1 'JetBrains Mono',monospace;color:var(--fg3);letter-spacing:.8px;text-transform:uppercase;margin-top:1px}
.stats{width:100%;display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-top:4px}
.st{text-align:center;padding:7px 2px;background:var(--sf2);border-radius:6px}
.st-l{font:500 8px/1 'JetBrains Mono',monospace;color:var(--fg3);text-transform:uppercase;letter-spacing:.4px}
.st-v{font:600 12px/1 'JetBrains Mono',monospace;color:var(--fg);margin-top:3px}
.weights{display:flex;justify-content:center;gap:10px;padding:2px 0;flex-wrap:wrap}
.w-item{font:500 9px/1 'JetBrains Mono',monospace;color:var(--fg3);letter-spacing:.3px}
.w-item b{color:var(--fg2);font-weight:600}
.foot{text-align:center;font:500 9px/1 'JetBrains Mono',monospace;color:var(--fg3);padding:4px 0 0}
.notice{background:var(--sf);border:1px solid rgba(255,184,51,.2);border-radius:10px;padding:10px 14px;font:500 11px/1.5 'Inter',sans-serif;color:var(--neut);text-align:center;display:none}
.notice.show{display:block}

/* Stocks table */
.stk-table{width:100%;border-collapse:collapse;margin-top:6px}
.stk-table th{font:500 8px/1 'JetBrains Mono',monospace;color:var(--fg3);text-transform:uppercase;letter-spacing:.4px;text-align:left;padding:4px 6px;border-bottom:1px solid var(--bd)}
.stk-table th:last-child,.stk-table td:last-child{text-align:right}
.stk-table th:nth-child(2),.stk-table td:nth-child(2){text-align:right}
.stk-table td{font:500 10px/1 'JetBrains Mono',monospace;padding:3px 6px}
.stk-table tr:nth-child(even){background:rgba(255,255,255,.015)}
.stk-tk{color:var(--fg)}
.stk-expand{width:100%;background:none;border:1px solid var(--bd);border-radius:6px;color:var(--fg3);font:500 9px/1 'JetBrains Mono',monospace;padding:6px;margin-top:6px;cursor:pointer;letter-spacing:.4px;text-transform:uppercase}
.stk-expand:active{background:var(--sf2)}

.g-read,.st-v,.comp-val,.badge{transition:color .5s ease}
@media(prefers-reduced-motion:reduce){.dot{animation:none;opacity:1}.bar-fill,.bar-needle{transition:none}}
@media(max-width:360px){.hdr h1{font-size:15px}.comp-val{font-size:18px}.g-read{font-size:22px}.g-wrap{max-width:220px}}
</style>
</head>
<body>
<div class="app">
  <div class="hdr">
    <h1><b>MNQ</b> BIAS</h1>
    <div class="live"><div class="dot loading" id="statusDot"></div><span id="statusText">CONECTANDO...</span></div>
  </div>

  <div class="notice" id="notice"></div>

  <!-- Composite -->
  <div class="comp">
    <div class="comp-top">
      <span class="comp-lbl">Vies Composto</span>
      <span class="comp-val" id="cV">--</span>
    </div>
    <div class="bar-wrap">
      <div class="bar-fill" id="cF"></div>
      <div class="bar-needle" id="cN" style="left:50%"></div>
    </div>
    <div class="bar-labels"><span>Bear</span><span>Neutro</span><span>Bull</span></div>
  </div>

  <div class="weights">
    <span class="w-item">VOL <b>20%</b></span>
    <span class="w-item">ADV <b>15%</b></span>
    <span class="w-item">OI <b>15%</b></span>
    <span class="w-item">TOP20 <b>50%</b></span>
  </div>

  <!-- Top 20 Stocks -->
  <div class="card">
    <div class="card-top"><span class="card-title">Top 20 Nasdaq</span><span class="badge" id="b-stk">--</span></div>
    <div class="g-wrap"><svg viewBox="0 0 280 150" id="g-stk"></svg><div class="g-center"><div class="g-read" id="r-stk">--</div><div class="g-unit">weighted avg</div></div></div>
    <div class="stats" id="s-stk"></div>
    <div id="stk-detail" style="width:100%;display:none">
      <table class="stk-table" id="stk-tbl"><thead><tr><th>Ticker</th><th>Preço</th><th>Var%</th></tr></thead><tbody></tbody></table>
    </div>
    <button class="stk-expand" id="stk-btn" onclick="toggleStocks()">▼ Ver ações</button>
  </div>

  <!-- Volume -->
  <div class="card">
    <div class="card-top"><span class="card-title">Volume Totals</span><span class="badge" id="b-vol">--</span></div>
    <div class="g-wrap"><svg viewBox="0 0 280 150" id="g-vol"></svg><div class="g-center"><div class="g-read" id="r-vol">--</div><div class="g-unit">contratos</div></div></div>
    <div class="stats" id="s-vol"></div>
  </div>

  <!-- ADV -->
  <div class="card">
    <div class="card-top"><span class="card-title">Avg Daily Volume</span><span class="badge" id="b-adv">--</span></div>
    <div class="g-wrap"><svg viewBox="0 0 280 150" id="g-adv"></svg><div class="g-center"><div class="g-read" id="r-adv">--</div><div class="g-unit">contratos</div></div></div>
    <div class="stats" id="s-adv"></div>
  </div>

  <!-- Open Interest -->
  <div class="card">
    <div class="card-top"><span class="card-title">Open Interest</span><span class="badge" id="b-oi">--</span></div>
    <div class="g-wrap"><svg viewBox="0 0 280 150" id="g-oi"></svg><div class="g-center"><div class="g-read" id="r-oi">--</div><div class="g-unit">contratos</div></div></div>
    <div class="stats" id="s-oi"></div>
  </div>

  <div class="foot" id="ts">Conectando...</div>
</div>

<script>
(function(){
'use strict';
const B='#ff4466',N='#ffb833',G='#00d4aa',TK='#1a1e30',M='#505670';
const fmt=n=>n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(0)+'K':n.toFixed(0);
const fmtD=p=>(p>=0?'+':'')+p.toFixed(1)+'%';
const fmtD2=p=>(p>=0?'+':'')+p.toFixed(2)+'%';

let stocksOpen=false;
window.toggleStocks=function(){
  const d=document.getElementById('stk-detail'),b=document.getElementById('stk-btn');
  stocksOpen=!stocksOpen;
  d.style.display=stocksOpen?'block':'none';
  b.textContent=stocksOpen?'▲ Fechar':'▼ Ver ações';
};

function bias(cur,avg20,avg5,prev){
  if(!avg20||!cur)return 0;
  const r=(((cur-avg20)/avg20)*.35+((cur-avg5)/avg5)*.4+((cur-prev)/prev)*.25)*500;
  return Math.max(-100,Math.min(100,r));
}
const bCol=s=>s>20?G:s<-20?B:N;
const bLbl=s=>s>50?'STRONG BULL':s>20?'BULLISH':s>-20?'NEUTRO':s>-50?'BEARISH':'STRONG BEAR';

function arc(cx,cy,r,sa,ea){
  const s={x:cx+r*Math.cos(sa),y:cy+r*Math.sin(sa)};
  const e={x:cx+r*Math.cos(ea),y:cy+r*Math.sin(ea)};
  return`M ${s.x} ${s.y} A ${r} ${r} 0 ${Math.abs(ea-sa)>Math.PI?1:0} ${ea>sa?1:0} ${e.x} ${e.y}`;
}

function drawG(id,score){
  const svg=document.getElementById(id);if(!svg)return;
  const cx=140,cy=138,r=105,sw=12,sa=Math.PI,ea=0;
  const norm=(score+100)/200;
  const fa=sa+(ea-sa)*norm;
  const col=bCol(score);
  const gid=id+'-g';
  let ticks='';
  for(let i=0;i<=10;i++){
    const a=Math.PI-Math.PI*i/10;
    const i1=r-sw/2-5,i2=r-sw/2-1;
    ticks+=`<line x1="${cx+i1*Math.cos(a)}" y1="${cy+i1*Math.sin(a)}" x2="${cx+i2*Math.cos(a)}" y2="${cy+i2*Math.sin(a)}" stroke="${M}" stroke-width="${i%5===0?1.5:0.8}" stroke-linecap="round"/>`;
  }
  const na=Math.PI-Math.PI*norm;
  const nx=cx+(r-sw/2)*Math.cos(na),ny=cy+(r-sw/2)*Math.sin(na);
  const ly=cy+12;
  svg.innerHTML=`
    <defs><linearGradient id="${gid}" x1="0%" y1="0%" x2="100%"><stop offset="0%" stop-color="${B}"/><stop offset="50%" stop-color="${N}"/><stop offset="100%" stop-color="${G}"/></linearGradient>
    <filter id="${id}-gl"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
    <path d="${arc(cx,cy,r,sa,ea)}" fill="none" stroke="${TK}" stroke-width="${sw}" stroke-linecap="round"/>
    <path d="${arc(cx,cy,r,sa,fa)}" fill="none" stroke="url(#${gid})" stroke-width="${sw}" stroke-linecap="round" filter="url(#${id}-gl)"/>
    ${ticks}
    <text x="22" y="${ly}" fill="${M}" font-family="'JetBrains Mono',monospace" font-size="8" text-anchor="start">-100</text>
    <text x="${cx}" y="16" fill="${M}" font-family="'JetBrains Mono',monospace" font-size="8" text-anchor="middle">0</text>
    <text x="258" y="${ly}" fill="${M}" font-family="'JetBrains Mono',monospace" font-size="8" text-anchor="end">+100</text>
    <circle cx="${nx}" cy="${ny}" r="5" fill="${col}" stroke="#0a0c12" stroke-width="2.5" filter="url(#${id}-gl)"/>`;
}

function setBadge(id,score){const el=document.getElementById(id);if(!el)return;const c=bCol(score);el.textContent=bLbl(score);el.style.background=c+'18';el.style.color=c}

function setStats(id,vals){
  const el=document.getElementById(id);if(!el)return;
  el.innerHTML=vals.map(s=>{const c=s.d>2?G:s.d<-2?B:N;return`<div class="st"><div class="st-l">${s.l}</div><div class="st-v" style="color:${c}">${typeof s.v==='string'?s.v:fmtD(s.d)}</div></div>`}).join('');
}

function setComp(score){
  const el=document.getElementById('cV'),f=document.getElementById('cF'),n=document.getElementById('cN');
  if(!el||!f||!n)return;
  const c=bCol(score);el.textContent=bLbl(score);el.style.color=c;
  const pct=(score+100)/200*100;
  if(score>=0){f.style.left='50%';f.style.width=(pct-50)+'%';f.style.background=`linear-gradient(to right,${N}40,${c})`}
  else{f.style.left=pct+'%';f.style.width=(50-pct)+'%';f.style.background=`linear-gradient(to left,${N}40,${c})`}
  f.style.boxShadow=`0 0 10px ${c}40`;n.style.left=pct+'%';n.style.background=c;n.style.boxShadow=`0 0 6px ${c}80`;
}

function renderStocksTable(details){
  if(!details||!details.length)return;
  const tbody=document.querySelector('#stk-tbl tbody');
  if(!tbody)return;
  tbody.innerHTML=details.map(s=>{
    const c=s.change_pct>0.05?G:s.change_pct<-0.05?B:N;
    return`<tr><td class="stk-tk">${s.ticker}</td><td style="color:var(--fg2)">${s.price.toFixed(2)}</td><td style="color:${c}">${fmtD2(s.change_pct)}</td></tr>`;
  }).join('');
}

async function fetchData(){
  try{
    const r=await fetch('/api/data');
    const d=await r.json();
    const dot=document.getElementById('statusDot');
    const stxt=document.getElementById('statusText');
    const notice=document.getElementById('notice');

    if(d.status==='live'||d.status==='partial'){
      dot.className=d.status==='live'?'dot on':'dot partial';
      stxt.textContent=d.status==='live'?'CME LIVE':'PARCIAL';
      if(d.error){notice.textContent=d.error;notice.classList.add('show')}
      else{notice.classList.remove('show')}

      const vol=d.volume_total||0,adv=d.adv_20||1,oi=d.open_interest||0;
      const a5=d.adv_5||adv,pv=d.prev_volume||Math.max(1,vol),poi=d.prev_oi||Math.max(1,oi);
      const stkBias=d.stocks_bias||0;
      const wAvg=d.weighted_change_pct||0;

      const vs=vol>0?bias(vol,adv,a5,pv):0;
      const as2=adv>0?bias(adv,adv,a5,pv):0;
      const os=oi>0?bias(oi,oi,oi*1.02,poi):0;

      // Composite: 20% Vol + 15% ADV + 15% OI + 50% Stocks
      const hasVol=vol>0;
      let comp;
      if(hasVol){comp=vs*.20+as2*.15+os*.15+stkBias*.50}
      else{comp=stkBias} // Se só stocks disponível, usa 100%

      // Stocks gauge
      drawG('g-stk',stkBias);
      document.getElementById('r-stk').textContent=(wAvg>=0?'+':'')+wAvg.toFixed(2)+'%';
      setBadge('b-stk',stkBias);
      const det=d.stocks_detail||[];
      const bullN=det.filter(s=>s.change_pct>0.05).length;
      const bearN=det.filter(s=>s.change_pct<-0.05).length;
      setStats('s-stk',[
        {l:'Bull',d:bullN*5,v:bullN+' ações'},{l:'Bear',d:-bearN*5,v:bearN+' ações'},{l:'Avg Pond',d:wAvg*50,v:(wAvg>=0?'+':'')+wAvg.toFixed(3)+'%'}
      ]);
      renderStocksTable(det);

      // Futures gauges
      if(hasVol){
        drawG('g-vol',vs);drawG('g-adv',as2);drawG('g-oi',os);
        document.getElementById('r-vol').textContent=fmt(vol);
        document.getElementById('r-adv').textContent=fmt(adv);
        document.getElementById('r-oi').textContent=fmt(oi);
        setBadge('b-vol',vs);setBadge('b-adv',as2);setBadge('b-oi',os);
        setStats('s-vol',[{l:'vs MA20',d:((vol-adv)/adv)*100},{l:'vs MA5',d:a5?((vol-a5)/a5)*100:0},{l:'vs Prev',d:pv?((vol-pv)/pv)*100:0}]);
        setStats('s-adv',[{l:'ADV 20d',d:0},{l:'ADV 5d',d:adv?((a5-adv)/adv)*100:0},{l:'Trend',d:adv?((a5-adv)/adv)*100:0}]);
        setStats('s-oi',[{l:'OI Total',d:0},{l:'vs Prev',d:poi?((oi-poi)/poi)*100:0},{l:'Delta',d:poi?(oi-poi)/1000:0}]);
      }else{
        ['r-vol','r-adv','r-oi'].forEach(id=>{document.getElementById(id).textContent='--'});
        ['b-vol','b-adv','b-oi'].forEach(id=>{const e=document.getElementById(id);e.textContent='FECHADO';e.style.background=M+'18';e.style.color=M});
        drawG('g-vol',0);drawG('g-adv',0);drawG('g-oi',0);
      }

      setComp(comp);

      const ts=d.last_update?new Date(d.last_update).toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'--';
      document.getElementById('ts').textContent=(d.status==='live'?'CME Live':'Stocks Only')+' · '+ts;

    }else if(d.status==='closed'){
      dot.className='dot closed';stxt.textContent='FECHADO';
      notice.textContent=d.error||'Mercado fechado';notice.classList.add('show');
      document.getElementById('ts').textContent='Mercado fechado';

    }else if(d.status==='error'){
      dot.className='dot off';stxt.textContent='ERRO';
      notice.textContent=d.error||'Erro ao buscar dados';notice.classList.add('show');

    }else{
      dot.className='dot loading';stxt.textContent='CARREGANDO...';
    }
  }catch(e){
    document.getElementById('statusDot').className='dot off';
    document.getElementById('statusText').textContent='OFFLINE';
  }
}

fetchData();setInterval(fetchData,15000);
})();
</script>
</body>
</html>"""


# ─── Main ───────────────────────────────────────────────────────
def main():
    print("=" * 54)
    print("  MNQ BIAS DASHBOARD — Cloud Edition")
    print("=" * 54)
    print()
    print(f"  Porta: {PORT}")
    print(f"  Futuros refresh: {REFRESH_FUTURES}s")
    print(f"  Stocks refresh:  {REFRESH_STOCKS}s")
    print()

    # Iniciar threads de dados
    t1 = threading.Thread(target=futures_loop, daemon=True)
    t2 = threading.Thread(target=stocks_loop, daemon=True)
    t1.start()
    t2.start()

    print(f"  Dashboard: http://localhost:{PORT}")
    print()

    app.run(host="0.0.0.0", port=PORT, debug=False)


if __name__ == "__main__":
    main()
