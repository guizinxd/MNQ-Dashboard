#!/usr/bin/env python3
"""
MNQ Bias Dashboard — Cloud Edition v3 (Premium UI)
====================================================
Dados reais via Yahoo Finance Chart API (sem lib yfinance).
4 métricas: Volume, ADV, Open Interest, Top 20 Stocks.
Deploy: Render (free tier) ou roda local.

USO LOCAL:
  pip install flask requests
  python mnq_dashboard.py

DEPLOY (Render):
  - Cria Web Service apontando pro repo
  - Start command: python mnq_dashboard.py
"""

import json
import os
import threading
import time
import requests
from datetime import datetime, timezone

try:
    from flask import Flask, jsonify, Response
except ImportError:
    print("Instale: pip install flask")
    exit(1)

# ─── Config ─────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", 8050))
REFRESH_STOCKS = 300   # top 20 a cada 5 min
REFRESH_FUTURES = 180  # futuros a cada 3 min

# Top 20 Nasdaq-100 por peso (aprox.)
NASDAQ_TOP20 = {
    "AAPL": 8.9, "MSFT": 8.1, "NVDA": 7.8, "AMZN": 5.4, "META": 5.0,
    "AVGO": 4.8, "GOOGL": 2.8, "GOOG": 2.6, "TSLA": 3.2, "COST": 2.6,
    "NFLX": 2.3, "AMD": 2.0, "ADBE": 1.7, "QCOM": 1.6, "TMUS": 1.5,
    "LIN": 1.4, "ISRG": 1.3, "INTU": 1.3, "AMAT": 1.2, "BKNG": 1.2,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
    "Origin": "https://finance.yahoo.com",
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

# ─── Yahoo Finance Chart API (sem yfinance lib) ─────────────────

def yahoo_chart(ticker, range_str="1mo", interval="1d"):
    """Busca dados via Yahoo Finance Chart API v8 diretamente."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {
        "range": range_str,
        "interval": interval,
        "includePrePost": "false",
        "events": "",
    }
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None
        return result[0]
    except Exception as e:
        print(f"  [{now()}] yahoo_chart({ticker}): {e}")
        return None


def yahoo_multi_quotes(tickers):
    """Busca cotações de múltiplos tickers de uma vez via Yahoo Finance."""
    symbols = ",".join(tickers)
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    params = {"symbols": symbols}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data.get("quoteResponse", {}).get("result", [])
    except Exception:
        # v7 pode precisar de crumb, tentar um por um via chart API
        return None


# ─── Fetchers ───────────────────────────────────────────────────

def yahoo_quote(ticker):
    """Busca quote individual via Yahoo Finance v7 — retorna dict com OI, volume etc."""
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    params = {"symbols": ticker}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        results = data.get("quoteResponse", {}).get("result", [])
        return results[0] if results else None
    except Exception as e:
        print(f"  [{now()}] yahoo_quote({ticker}): {e}")
        return None


def fetch_futures():
    """Busca Volume e OI do MNQ/NQ via Yahoo Finance."""
    global data_cache
    try:
        print(f"  [{now()}] Buscando futuros MNQ...")

        # ── 1) Chart API para histórico de volume (ADV) ──
        chart = yahoo_chart("MNQ=F", "1mo", "1d")
        ticker_used = "MNQ=F"
        if not chart:
            print(f"  [{now()}] MNQ vazio, tentando NQ=F...")
            chart = yahoo_chart("NQ=F", "1mo", "1d")
            ticker_used = "NQ=F"

        if not chart:
            data_cache["error"] = "Sem dados de futuros (mercado fechado?)"
            if data_cache["status"] == "loading":
                data_cache["status"] = "closed"
            print(f"  [{now()}] Sem dados de futuros")
            return

        indicators = chart.get("indicators", {})
        quotes = indicators.get("quote", [{}])[0]
        volumes = quotes.get("volume", [])

        # Filtrar None
        valid_vols = [v for v in volumes if v is not None and v > 0]

        if not valid_vols:
            data_cache["error"] = "Volume zerado (mercado fechado?)"
            if data_cache["status"] == "loading":
                data_cache["status"] = "closed"
            print(f"  [{now()}] Volume zerado")
            return

        vol_today = int(valid_vols[-1])
        adv_20 = int(sum(valid_vols[-20:]) / len(valid_vols[-20:])) if valid_vols else 0
        adv_5 = int(sum(valid_vols[-5:]) / len(valid_vols[-5:])) if len(valid_vols) >= 2 else adv_20
        prev_vol = int(valid_vols[-2]) if len(valid_vols) >= 2 else vol_today

        # ── 2) Quote API para Open Interest REAL ──
        oi = 0
        prev_oi = 0
        quote = yahoo_quote(ticker_used)
        if quote:
            oi = int(quote.get("openInterest", 0) or 0)
            print(f"  [{now()}] OI via quote API ({ticker_used}): {fmt(oi)}")

        # Fallback: tentar o outro ticker se OI veio zero
        if oi == 0 and ticker_used == "MNQ=F":
            quote2 = yahoo_quote("NQ=F")
            if quote2:
                oi = int(quote2.get("openInterest", 0) or 0)
                print(f"  [{now()}] OI via quote API (NQ=F fallback): {fmt(oi)}")

        # prev_oi: guardar o último OI conhecido para comparação
        if oi > 0:
            prev_oi = data_cache.get("prev_oi_real", oi)
            data_cache["prev_oi_real"] = oi
        else:
            prev_oi = data_cache.get("prev_oi_real", 0)
            print(f"  [{now()}] OI indisponivel no momento (mercado fechado?)")

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
            print(f"  [{now()}] Volume zerado (mercado fechado?)")

    except Exception as e:
        data_cache["error"] = str(e)
        print(f"  [{now()}] Erro futuros: {e}")


def fetch_stocks():
    """Busca top 20 ações do Nasdaq-100 via Yahoo Finance."""
    global data_cache
    try:
        print(f"  [{now()}] Buscando top 20 acoes Nasdaq...")

        # Tentar batch quote primeiro
        tickers = list(NASDAQ_TOP20.keys())
        quotes = yahoo_multi_quotes(tickers)

        details = []
        total_weight = 0
        weighted_sum = 0

        if quotes:
            # Método rápido: batch quotes
            for q in quotes:
                ticker = q.get("symbol", "")
                weight = NASDAQ_TOP20.get(ticker, 0)
                if weight == 0:
                    continue

                price = q.get("regularMarketPrice", 0)
                prev_close = q.get("regularMarketPreviousClose", 0)

                if not price or not prev_close or prev_close <= 0:
                    continue

                pct_change = ((price - prev_close) / prev_close) * 100

                details.append({
                    "ticker": ticker,
                    "price": round(price, 2),
                    "change_pct": round(pct_change, 2),
                    "weight": weight,
                })

                weighted_sum += pct_change * weight
                total_weight += weight

        else:
            # Fallback: buscar um por um via chart API
            print(f"  [{now()}] Batch falhou, buscando individualmente...")
            for ticker, weight in NASDAQ_TOP20.items():
                chart = yahoo_chart(ticker, "5d", "1d")
                if not chart:
                    continue

                quotes_data = chart.get("indicators", {}).get("quote", [{}])[0]
                closes = [c for c in quotes_data.get("close", []) if c is not None]

                if len(closes) < 2:
                    continue

                current = closes[-1]
                previous = closes[-2]

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

                # Pequeno delay entre requests individuais
                time.sleep(0.3)

        if total_weight > 0:
            weighted_avg = weighted_sum / total_weight
            # Escala: ±2% no dia = ±100 no bias
            stocks_bias = max(-100, min(100, weighted_avg * 50))

            details.sort(key=lambda x: x["weight"], reverse=True)

            data_cache.update({
                "stocks_bias": round(stocks_bias, 1),
                "stocks_detail": details,
                "weighted_change_pct": round(weighted_avg, 3),
            })

            bull_count = sum(1 for d in details if d["change_pct"] > 0)
            bear_count = sum(1 for d in details if d["change_pct"] < 0)
            print(f"  [{now()}] Acoes OK — Bias: {stocks_bias:+.1f} | Avg: {weighted_avg:+.3f}% | {bull_count}up {bear_count}dn")

            if data_cache["status"] in ("loading", "closed"):
                data_cache["status"] = "partial"
                data_cache["error"] = None
                data_cache["last_update"] = datetime.now(timezone.utc).isoformat()
        else:
            print(f"  [{now()}] Sem dados de acoes")

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
    # Esperar 5s pro server subir antes de começar
    time.sleep(5)
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


# ─── Dashboard HTML — Premium UI v3 ────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#070a12">
<title>MNQ Bias</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap">
<style>
:root{
  --bg:#070a12;--card:rgba(255,255,255,0.028);--card-bd:rgba(255,255,255,0.06);
  --card-h:rgba(255,255,255,0.045);--trk:rgba(255,255,255,0.04);
  --t1:#edf0f7;--t2:#8891a8;--t3:#4a5168;
  --bull:#00d4aa;--bear:#ff4466;--neut:#ffb833;
  --bull-bg:rgba(0,212,170,0.08);--bear-bg:rgba(255,68,102,0.08);--neut-bg:rgba(255,184,51,0.08);
  --mono:'JetBrains Mono',monospace;--sans:'Inter',system-ui,-apple-system,sans-serif;
  --ease:cubic-bezier(0.22,1,0.36,1);--ease2:cubic-bezier(0.4,0,0.2,1)
}
*{box-sizing:border-box;margin:0;padding:0}
html{background:var(--bg);height:100%}
body{background:var(--bg);color:var(--t1);font-family:var(--sans);min-height:100%;
  padding:env(safe-area-inset-top,0) max(16px,env(safe-area-inset-right,0)) env(safe-area-inset-bottom,0) max(16px,env(safe-area-inset-left,0));
  -webkit-font-smoothing:antialiased;-webkit-text-size-adjust:100%;overscroll-behavior-y:contain;
  -webkit-user-select:none;user-select:none;-webkit-tap-highlight-color:transparent}
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:radial-gradient(ellipse 70% 50% at 10% 15%,rgba(0,212,170,0.025),transparent 65%),
  radial-gradient(ellipse 60% 50% at 90% 85%,rgba(255,68,102,0.02),transparent 65%)}
.app{position:relative;z-index:1;max-width:420px;margin:0 auto;display:flex;flex-direction:column;gap:12px;padding:14px 0 36px}

/* Header */
.hdr{display:flex;align-items:center;justify-content:space-between;padding:0 2px 8px}
.logo{font:700 19px/1 var(--mono);letter-spacing:1.5px}
.logo span{color:var(--bull)}
.status{display:flex;align-items:center;gap:6px;font:600 10px/1 var(--mono);color:var(--t2);letter-spacing:.8px;text-transform:uppercase}
.dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.dot.live{background:var(--bull);box-shadow:0 0 8px rgba(0,212,170,.5);animation:pulse 2s ease-in-out infinite}
.dot.loading{background:var(--neut);animation:blink 1.5s ease infinite}
.dot.partial{background:var(--neut);animation:pulse 2.5s ease-in-out infinite}
.dot.closed{background:var(--t3)}
.dot.off{background:var(--bear)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.15}}

/* Notice */
.notice{background:rgba(255,184,51,.04);border:1px solid rgba(255,184,51,.12);border-radius:12px;
  padding:10px 16px;font:500 11px/1.5 var(--sans);color:var(--neut);text-align:center;display:none}
.notice.show{display:block}

/* Card base */
.card{background:var(--card);backdrop-filter:blur(40px);-webkit-backdrop-filter:blur(40px);
  border:1px solid var(--card-bd);border-radius:18px;padding:16px;
  opacity:0;transform:translateY(14px);animation:fadeUp .55s var(--ease2) forwards;
  transition:background .25s}
.card:active{background:var(--card-h)}
@supports not(backdrop-filter:blur(1px)){.card{background:rgba(12,15,26,.92)}}
.c1{animation-delay:.04s}.c2{animation-delay:.08s}.c3{animation-delay:.12s}
.c4{animation-delay:.16s}.c5{animation-delay:.2s}.c6{animation-delay:.24s}
@keyframes fadeUp{to{opacity:1;transform:none}}

/* Card head */
.card-h{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.card-t{font:600 11px/1 var(--mono);text-transform:uppercase;letter-spacing:.9px;color:var(--t2)}
.badge{font:700 9px/1 var(--mono);padding:4px 9px;border-radius:6px;letter-spacing:.5px;white-space:nowrap}

/* Gauge */
.g-wrap{display:flex;flex-direction:column;align-items:center}
.g-svg{width:100%;max-width:250px;display:block;overflow:visible}
[id^="gf-"]{transition:stroke-dashoffset 1.6s var(--ease)}
.g-center{text-align:center;margin-top:-28px;position:relative}
.g-val{font:700 28px/1 var(--mono);font-variant-numeric:tabular-nums;transition:color .6s}
.g-unit{font:500 8.5px/1 var(--mono);color:var(--t3);text-transform:uppercase;letter-spacing:1.2px;margin-top:3px}

/* Stats row */
.stats{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-top:12px}
.st{text-align:center;padding:8px 4px;background:rgba(255,255,255,.018);border-radius:8px}
.st-l{font:500 7.5px/1 var(--mono);color:var(--t3);text-transform:uppercase;letter-spacing:.6px}
.st-v{font:600 12px/1 var(--mono);margin-top:4px;font-variant-numeric:tabular-nums;transition:color .5s}

/* Composite hero */
.comp-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.comp-lbl{font:500 10px/1 var(--mono);text-transform:uppercase;letter-spacing:1px;color:var(--t2)}
.comp-val{font:700 26px/1 var(--mono);transition:color .8s}
.comp-track{position:relative;height:8px;background:var(--trk);border-radius:4px;overflow:visible}
.comp-fill{position:absolute;top:0;height:100%;border-radius:4px;transition:all 1.6s var(--ease)}
.comp-needle{position:absolute;top:-7px;width:4px;height:22px;border-radius:2px;transform:translateX(-50%);transition:all 1.6s var(--ease)}
.comp-mid{position:absolute;left:50%;top:-2px;width:1px;height:12px;background:rgba(255,255,255,.07);transform:translateX(-50%)}
.comp-labels{display:flex;justify-content:space-between;margin-top:7px;font:500 9px/1 var(--mono);color:var(--t3);letter-spacing:.6px;text-transform:uppercase}

/* Weights */
.weights{display:flex;justify-content:center;gap:10px;flex-wrap:wrap;padding:0 2px}
.w{font:500 9px/1 var(--mono);color:var(--t3);letter-spacing:.4px}
.w b{color:var(--t2);font-weight:600}

/* Stocks detail */
.stk-wrap{max-height:0;overflow:hidden;transition:max-height .45s var(--ease2)}
.stk-wrap.open{max-height:700px}
.stk-tbl{width:100%;border-collapse:collapse;margin-top:8px}
.stk-tbl th{font:500 7.5px/1 var(--mono);color:var(--t3);text-transform:uppercase;letter-spacing:.5px;
  text-align:left;padding:5px 6px;border-bottom:1px solid var(--card-bd)}
.stk-tbl th:last-child,.stk-tbl td:last-child{text-align:right}
.stk-tbl th:nth-child(2),.stk-tbl td:nth-child(2){text-align:center}
.stk-tbl th:nth-child(3),.stk-tbl td:nth-child(3){text-align:right}
.stk-tbl td{font:500 10px/1 var(--mono);padding:5px 6px;transition:background .2s}
.stk-tbl tbody tr{border-bottom:1px solid rgba(255,255,255,.02)}
.stk-tbl tbody tr:active{background:rgba(255,255,255,.03)}
.stk-tk{color:var(--t1);font-weight:600}
.stk-w{color:var(--t3);font-size:9px}
.stk-btn{width:100%;background:rgba(255,255,255,.025);border:1px solid var(--card-bd);border-radius:10px;
  color:var(--t2);font:500 10px/1 var(--mono);padding:10px;margin-top:10px;cursor:pointer;
  letter-spacing:.6px;text-transform:uppercase;transition:all .2s;display:flex;align-items:center;justify-content:center;gap:6px}
.stk-btn:active{background:rgba(255,255,255,.05);transform:scale(.98)}
.stk-btn .arrow{display:inline-block;transition:transform .3s var(--ease2);font-size:8px}
.stk-btn.open .arrow{transform:rotate(180deg)}

/* Footer */
.foot{text-align:center;font:500 9px/1 var(--mono);color:var(--t3);padding:6px 0 0;cursor:pointer;
  transition:color .2s}
.foot:active{color:var(--t2)}

/* Responsive */
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
@media(max-width:360px){.logo{font-size:16px}.comp-val{font-size:22px}.g-val{font-size:24px}.g-svg{max-width:210px}}
@media(min-width:768px){.app{max-width:460px;gap:14px;padding:20px 0 40px}.card{border-radius:20px;padding:20px}}
</style>
</head>
<body>
<div class="app">
  <header class="hdr">
    <div class="logo"><span>MNQ</span> BIAS</div>
    <div class="status"><div class="dot loading" id="dot"></div><span id="stxt">CONECTANDO</span></div>
  </header>

  <div class="notice" id="notice"></div>

  <div class="card c1">
    <div class="comp-head">
      <span class="comp-lbl">Vies Composto</span>
      <span class="comp-val" id="cV">--</span>
    </div>
    <div class="comp-track">
      <div class="comp-fill" id="cF"></div>
      <div class="comp-mid"></div>
      <div class="comp-needle" id="cN" style="left:50%"></div>
    </div>
    <div class="comp-labels"><span>Bear</span><span>Neutro</span><span>Bull</span></div>
  </div>

  <div class="weights">
    <span class="w">VOL <b>20%</b></span>
    <span class="w">ADV <b>15%</b></span>
    <span class="w">OI <b>15%</b></span>
    <span class="w">TOP20 <b>50%</b></span>
  </div>

  <div class="card c2">
    <div class="card-h"><span class="card-t">Top 20 Nasdaq</span><span class="badge" id="b-stk">--</span></div>
    <div class="g-wrap">
      <svg class="g-svg" viewBox="0 0 200 120" id="g-stk"></svg>
      <div class="g-center"><div class="g-val" id="r-stk">--</div><div class="g-unit">weighted avg</div></div>
    </div>
    <div class="stats" id="s-stk"></div>
    <div class="stk-wrap" id="stk-wrap">
      <table class="stk-tbl" id="stk-tbl"><thead><tr><th>Ticker</th><th>Peso</th><th>Preco</th><th>Var%</th></tr></thead><tbody></tbody></table>
    </div>
    <button class="stk-btn" id="stk-btn" onclick="toggleStocks()"><span class="arrow">&#9660;</span> Ver acoes</button>
  </div>

  <div class="card c3">
    <div class="card-h"><span class="card-t">Volume Totals</span><span class="badge" id="b-vol">--</span></div>
    <div class="g-wrap">
      <svg class="g-svg" viewBox="0 0 200 120" id="g-vol"></svg>
      <div class="g-center"><div class="g-val" id="r-vol">--</div><div class="g-unit">contratos</div></div>
    </div>
    <div class="stats" id="s-vol"></div>
  </div>

  <div class="card c4">
    <div class="card-h"><span class="card-t">Avg Daily Volume</span><span class="badge" id="b-adv">--</span></div>
    <div class="g-wrap">
      <svg class="g-svg" viewBox="0 0 200 120" id="g-adv"></svg>
      <div class="g-center"><div class="g-val" id="r-adv">--</div><div class="g-unit">contratos</div></div>
    </div>
    <div class="stats" id="s-adv"></div>
  </div>

  <div class="card c5">
    <div class="card-h"><span class="card-t">Open Interest</span><span class="badge" id="b-oi">--</span></div>
    <div class="g-wrap">
      <svg class="g-svg" viewBox="0 0 200 120" id="g-oi"></svg>
      <div class="g-center"><div class="g-val" id="r-oi">--</div><div class="g-unit">contratos</div></div>
    </div>
    <div class="stats" id="s-oi"></div>
  </div>

  <footer class="foot" id="ts" onclick="refresh()">Conectando...</footer>
</div>
<script>
(function(){
'use strict';
var B='#ff4466',N='#ffb833',G='#00d4aa',TK='rgba(255,255,255,0.04)',M='#4a5168';
var CX=100,CY=110,R=80,SW=14,ARCLEN=Math.PI*R;
var ARC='M '+(CX-R)+' '+CY+' A '+R+' '+R+' 0 0 1 '+(CX+R)+' '+CY;
var stocksOpen=false,refreshing=false;

function fmt(n){return n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(0)+'K':n.toFixed(0)}
function fmtP(p){return(p>=0?'+':'')+p.toFixed(1)+'%'}
function fmtP2(p){return(p>=0?'+':'')+p.toFixed(2)+'%'}
function bCol(s){return s>20?G:s<-20?B:N}
function bLbl(s){return s>50?'STRONG BULL':s>20?'BULLISH':s>-20?'NEUTRO':s>-50?'BEARISH':'STRONG BEAR'}
function bias(cur,a20,a5,prev){if(!a20||!cur)return 0;var r=(((cur-a20)/a20)*.35+((cur-a5)/a5)*.4+((cur-prev)/prev)*.25)*500;return Math.max(-100,Math.min(100,r))}

/* Gauge init */
function initGauge(id){
  var svg=document.getElementById('g-'+id);if(!svg)return;
  var gid='gg-'+id,fid='gl-'+id;
  var ticks='';
  for(var i=0;i<=20;i++){
    var n=i/20,a=Math.PI*(1-n);
    var ri=R-SW/2-7,ro=R-SW/2-2,major=i%5===0;
    if(major)ri=R-SW/2-9;
    var sw=major?1.2:.5;
    var op=major?.12:.05;
    ticks+='<line x1="'+(CX+ri*Math.cos(a))+'" y1="'+(CY-ri*Math.sin(a))+'" x2="'+(CX+ro*Math.cos(a))+'" y2="'+(CY-ro*Math.sin(a))+'" stroke="rgba(255,255,255,'+op+')" stroke-width="'+sw+'" stroke-linecap="round"/>';
  }
  svg.innerHTML='<defs><linearGradient id="'+gid+'" x1="0%" y1="0%" x2="100%"><stop offset="0%" stop-color="'+B+'"/><stop offset="45%" stop-color="'+N+'"/><stop offset="100%" stop-color="'+G+'"/></linearGradient><filter id="'+fid+'"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs><path d="'+ARC+'" fill="none" stroke="'+TK+'" stroke-width="'+SW+'" stroke-linecap="round"/><path id="gf-'+id+'" d="'+ARC+'" fill="none" stroke="url(#'+gid+')" stroke-width="'+SW+'" stroke-linecap="round" style="stroke-dasharray:'+ARCLEN+';stroke-dashoffset:'+ARCLEN/2+'"/>'+ticks+'<circle id="gn-'+id+'" cx="'+CX+'" cy="'+(CY-R)+'" r="5.5" fill="'+N+'" stroke="#070a12" stroke-width="2.5" filter="url(#'+fid+')"/><text x="14" y="'+(CY+13)+'" fill="rgba(255,255,255,.12)" font-family="JetBrains Mono,monospace" font-size="7">-100</text><text x="'+CX+'" y="10" fill="rgba(255,255,255,.12)" font-family="JetBrains Mono,monospace" font-size="7" text-anchor="middle">0</text><text x="186" y="'+(CY+13)+'" fill="rgba(255,255,255,.12)" font-family="JetBrains Mono,monospace" font-size="7" text-anchor="end">+100</text>';
}

function updateGauge(id,score){
  var norm=(score+100)/200;
  var fill=document.getElementById('gf-'+id);
  var needle=document.getElementById('gn-'+id);
  if(!fill||!needle)return;
  fill.style.strokeDashoffset=ARCLEN*(1-norm);
  var angle=Math.PI*(1-norm);
  var nx=CX+R*Math.cos(angle),ny=CY-R*Math.sin(angle);
  needle.setAttribute('cx',nx);needle.setAttribute('cy',ny);
  var col=bCol(score);
  needle.setAttribute('fill',col);
}

function setBadge(id,score){
  var el=document.getElementById(id);if(!el)return;
  var c=bCol(score);el.textContent=bLbl(score);
  el.style.background=c+'14';el.style.color=c;el.style.boxShadow='0 0 12px '+c+'10';
}

function setStats(id,vals){
  var el=document.getElementById(id);if(!el)return;
  el.innerHTML=vals.map(function(s){
    var c=s.d>2?G:s.d<-2?B:N;
    return'<div class="st"><div class="st-l">'+s.l+'</div><div class="st-v" style="color:'+c+'">'+(typeof s.v==='string'?s.v:fmtP(s.d))+'</div></div>';
  }).join('');
}

function setComp(score){
  var el=document.getElementById('cV'),f=document.getElementById('cF'),n=document.getElementById('cN');
  if(!el||!f||!n)return;
  var c=bCol(score);el.textContent=bLbl(score);el.style.color=c;
  var pct=(score+100)/200*100;
  if(score>=0){f.style.left='50%';f.style.width=(pct-50)+'%';f.style.background='linear-gradient(90deg,'+N+'40,'+c+')'}
  else{f.style.left=pct+'%';f.style.width=(50-pct)+'%';f.style.background='linear-gradient(270deg,'+N+'40,'+c+')'}
  f.style.boxShadow='0 0 14px '+c+'30';
  n.style.left=pct+'%';n.style.background=c;n.style.boxShadow='0 0 8px '+c+'80';
}

function renderStocks(details){
  if(!details||!details.length)return;
  var tbody=document.querySelector('#stk-tbl tbody');if(!tbody)return;
  tbody.innerHTML=details.map(function(s){
    var c=s.change_pct>0.05?G:s.change_pct<-0.05?B:N;
    return'<tr><td class="stk-tk">'+s.ticker+'</td><td class="stk-w">'+s.weight.toFixed(1)+'%</td><td style="color:var(--t2)">'+s.price.toFixed(2)+'</td><td style="color:'+c+'">'+fmtP2(s.change_pct)+'</td></tr>';
  }).join('');
}

/* Number animation */
function animNum(el,to,format){
  var from=parseFloat(el.dataset.v||'0');
  if(Math.abs(from-to)<0.001){el.textContent=format(to);return}
  el.dataset.v=to;
  var dur=900,start=performance.now();
  (function step(t){
    var p=Math.min((t-start)/dur,1);
    var e=1-Math.pow(1-p,3);
    el.textContent=format(from+(to-from)*e);
    if(p<1)requestAnimationFrame(step);
  })(performance.now());
}

window.toggleStocks=function(){
  stocksOpen=!stocksOpen;
  document.getElementById('stk-wrap').classList.toggle('open',stocksOpen);
  var btn=document.getElementById('stk-btn');
  btn.classList.toggle('open',stocksOpen);
  btn.innerHTML=stocksOpen?'<span class="arrow">&#9660;</span> Fechar':'<span class="arrow">&#9660;</span> Ver acoes';
};

window.refresh=function(){
  if(refreshing)return;refreshing=true;
  var ts=document.getElementById('ts');ts.textContent='Atualizando...';ts.style.color='var(--neut)';
  fetchData().then(function(){ts.style.color='';refreshing=false});
};

/* Init gauges */
['stk','vol','adv','oi'].forEach(initGauge);

async function fetchData(){
  try{
    var r=await fetch('/api/data');var d=await r.json();
    var dot=document.getElementById('dot'),stxt=document.getElementById('stxt'),notice=document.getElementById('notice');

    if(d.status==='live'||d.status==='partial'){
      dot.className=d.status==='live'?'dot live':'dot partial';
      stxt.textContent=d.status==='live'?'CME LIVE':'PARCIAL';
      if(d.error){notice.textContent=d.error;notice.classList.add('show')}else{notice.classList.remove('show')}

      var vol=d.volume_total||0,adv=d.adv_20||1,oi=d.open_interest||0;
      var a5=d.adv_5||adv,pv=d.prev_volume||Math.max(1,vol),poi=d.prev_oi||Math.max(1,oi);
      var stkBias=d.stocks_bias||0,wAvg=d.weighted_change_pct||0;

      var vs=vol>0?bias(vol,adv,a5,pv):0;
      var as2=adv>0?bias(adv,adv,a5,pv):0;
      var os=oi>0?bias(oi,oi,oi*1.02,poi):0;
      var hasVol=vol>0;
      var comp=hasVol?vs*.20+as2*.15+os*.15+stkBias*.50:stkBias;

      /* Stocks */
      updateGauge('stk',stkBias);
      animNum(document.getElementById('r-stk'),wAvg,function(v){return(v>=0?'+':'')+v.toFixed(2)+'%'});
      document.getElementById('r-stk').style.color=bCol(stkBias);
      setBadge('b-stk',stkBias);
      var det=d.stocks_detail||[];
      var bullN=det.filter(function(s){return s.change_pct>0.05}).length;
      var bearN=det.filter(function(s){return s.change_pct<-0.05}).length;
      setStats('s-stk',[{l:'Bull',d:bullN*5,v:bullN+' acoes'},{l:'Bear',d:-bearN*5,v:bearN+' acoes'},{l:'Avg Pond',d:wAvg*50,v:(wAvg>=0?'+':'')+wAvg.toFixed(3)+'%'}]);
      renderStocks(det);

      if(hasVol){
        /* Volume */
        updateGauge('vol',vs);
        animNum(document.getElementById('r-vol'),vol,fmt);
        document.getElementById('r-vol').style.color=bCol(vs);
        setBadge('b-vol',vs);
        setStats('s-vol',[{l:'vs MA20',d:((vol-adv)/adv)*100},{l:'vs MA5',d:a5?((vol-a5)/a5)*100:0},{l:'vs Prev',d:pv?((vol-pv)/pv)*100:0}]);

        /* ADV */
        updateGauge('adv',as2);
        animNum(document.getElementById('r-adv'),adv,fmt);
        document.getElementById('r-adv').style.color=bCol(as2);
        setBadge('b-adv',as2);
        setStats('s-adv',[{l:'ADV 20d',d:0},{l:'ADV 5d',d:adv?((a5-adv)/adv)*100:0},{l:'Trend',d:adv?((a5-adv)/adv)*100:0}]);

        /* OI */
        updateGauge('oi',os);
        animNum(document.getElementById('r-oi'),oi,fmt);
        document.getElementById('r-oi').style.color=bCol(os);
        setBadge('b-oi',os);
        setStats('s-oi',[{l:'OI Total',d:0},{l:'vs Prev',d:poi?((oi-poi)/poi)*100:0},{l:'Delta',d:poi?(oi-poi)/1000:0}]);
      }else{
        ['vol','adv','oi'].forEach(function(id){
          updateGauge(id,0);
          document.getElementById('r-'+id).textContent='--';
          document.getElementById('r-'+id).style.color='var(--t3)';
          var be=document.getElementById('b-'+id);be.textContent='FECHADO';be.style.background='rgba(74,81,104,.12)';be.style.color=M;be.style.boxShadow='none';
        });
      }
      setComp(comp);
      var ts=d.last_update?new Date(d.last_update).toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'--';
      document.getElementById('ts').textContent=(hasVol?'CME Live':'Stocks Only')+' · '+ts+' · toque p/ atualizar';

    }else if(d.status==='closed'){
      dot.className='dot closed';stxt.textContent='FECHADO';
      notice.textContent=d.error||'Mercado fechado';notice.classList.add('show');
      document.getElementById('ts').textContent='Mercado fechado';
    }else if(d.status==='error'){
      dot.className='dot off';stxt.textContent='ERRO';
      notice.textContent=d.error||'Erro ao buscar dados';notice.classList.add('show');
    }else{
      dot.className='dot loading';stxt.textContent='CARREGANDO';
    }
  }catch(e){
    document.getElementById('dot').className='dot off';
    document.getElementById('stxt').textContent='OFFLINE';
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
    print("  MNQ BIAS DASHBOARD — Cloud Edition v3")
    print("=" * 54)
    print()
    print(f"  Porta: {PORT}")
    print(f"  Futuros refresh: {REFRESH_FUTURES}s")
    print(f"  Stocks refresh:  {REFRESH_STOCKS}s")
    print()

    t1 = threading.Thread(target=futures_loop, daemon=True)
    t2 = threading.Thread(target=stocks_loop, daemon=True)
    t1.start()
    t2.start()

    print(f"  Dashboard: http://localhost:{PORT}")
    print()

    app.run(host="0.0.0.0", port=PORT, debug=False)


if __name__ == "__main__":
    main()
