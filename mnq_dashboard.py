#!/usr/bin/env python3
"""
MNQ Bias Dashboard — Cloud Edition v8 (Divergence Alerts + Notifications)
=======================================================================
Dados reais via Yahoo Finance Chart API (sem lib yfinance).
5 métricas: Volume (confirmador de direção), ADV, OI (NQ + histórico), Options P/C (QQQ multi-exp), Top 20 Stocks.
Interatividade: 3D tilt suave, ripple touch, sparkline OI com tooltip, favicon custom.
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

# JSONBin.io — persistência do histórico OI (gratuito, 10K req/mês)
JSONBIN_KEY = os.environ.get("JSONBIN_KEY", "")
JSONBIN_ID = os.environ.get("JSONBIN_ID", "")

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
    # Preço MNQ para volume confirmador
    "mnq_price_change_pct": 0,
    "mnq_price": 0,
    "volume_bias": 0,  # volume confirmado pela direção do preço
    "stocks_bias": 0,
    "stocks_detail": [],
    "weighted_change_pct": 0,
    # Options data (QQQ) — acumulado multi-vencimento
    "options_pc_ratio": 0,
    "options_call_vol": 0,
    "options_put_vol": 0,
    "options_total_vol": 0,
    "options_call_oi": 0,
    "options_put_oi": 0,
    "options_bias": 0,
    "options_expirations": 0,  # qtd de vencimentos agregados
    # Histórico OI (últimos 15 dias úteis)
    "oi_history": [],  # [{date, oi, price, trend}]
    "oi_trend": "neutro",  # montando / desmontando / neutro
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
    """Busca Volume + Preço (MNQ) e OI (NQ contrato cheio) via Yahoo Finance.
    Volume agora é confirmador: a direção do preço define se volume alto é bull ou bear."""
    global data_cache
    try:
        print(f"  [{now()}] Buscando futuros MNQ/NQ...")

        # ── 1) Chart API para histórico de volume + preço MNQ ──
        chart = yahoo_chart("MNQ=F", "1mo", "1d")
        if not chart:
            print(f"  [{now()}] MNQ vazio, tentando NQ=F...")
            chart = yahoo_chart("NQ=F", "1mo", "1d")

        if not chart:
            data_cache["error"] = "Sem dados de futuros (mercado fechado?)"
            if data_cache["status"] == "loading":
                data_cache["status"] = "closed"
            print(f"  [{now()}] Sem dados de futuros")
            return

        indicators = chart.get("indicators", {})
        quotes = indicators.get("quote", [{}])[0]
        volumes = quotes.get("volume", [])
        closes = quotes.get("close", [])

        # Filtrar None
        valid_vols = [v for v in volumes if v is not None and v > 0]
        valid_closes = [c for c in closes if c is not None]

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

        # ── Preço: variação % do dia para confirmação de direção ──
        mnq_price = valid_closes[-1] if valid_closes else 0
        mnq_prev_close = valid_closes[-2] if len(valid_closes) >= 2 else mnq_price
        price_change_pct = ((mnq_price - mnq_prev_close) / mnq_prev_close * 100) if mnq_prev_close > 0 else 0

        # ── Volume como confirmador de direção ──
        # Magnitude: quão acima/abaixo da média está o volume (0..1 scale)
        vol_magnitude = 0
        if adv_20 > 0:
            vol_ratio = vol_today / adv_20
            # vol_ratio 1.0 = na média, 1.5 = 50% acima, 0.5 = 50% abaixo
            vol_magnitude = min(1.0, max(0, (vol_ratio - 0.5) / 1.0))  # 0.5x=0, 1.5x=1

        # Direção: preço define o sinal, volume define a intensidade
        # price_change_pct * fator de escala * vol_magnitude
        # ±1% no preço com volume alto = ±100 bias
        if abs(price_change_pct) > 0.01:
            direction = 1 if price_change_pct > 0 else -1
            intensity = min(abs(price_change_pct), 2.0) / 2.0  # cap em 2%
            vol_bias = direction * intensity * vol_magnitude * 100
        else:
            vol_bias = 0  # preço flat = volume neutro

        vol_bias = max(-100, min(100, vol_bias))

        # ── 2) Quote API para OI — NQ=F (contrato cheio) como primário ──
        oi = 0
        prev_oi = 0
        quote = yahoo_quote("NQ=F")
        if quote:
            oi = int(quote.get("openInterest", 0) or 0)
            print(f"  [{now()}] OI via quote API (NQ=F): {fmt(oi)}")

        # Fallback: tentar MNQ=F se OI veio zero
        if oi == 0:
            quote2 = yahoo_quote("MNQ=F")
            if quote2:
                oi = int(quote2.get("openInterest", 0) or 0)
                print(f"  [{now()}] OI via quote API (MNQ=F fallback): {fmt(oi)}")

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
            "mnq_price": round(mnq_price, 2),
            "mnq_price_change_pct": round(price_change_pct, 3),
            "volume_bias": round(vol_bias, 1),
        })

        # ── Histórico OI (acumular snapshots diários, manter 15 dias) ──
        if oi > 0:
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            oi_hist = data_cache.get("oi_history", [])
            # Atualizar ou adicionar entrada do dia
            found = False
            for entry in oi_hist:
                if entry["date"] == today_str:
                    entry["oi"] = oi
                    entry["price"] = round(mnq_price, 0)
                    found = True
                    break
            if not found:
                oi_hist.append({"date": today_str, "oi": oi, "price": round(mnq_price, 0)})
                # Novo dia → salvar no JSONBin pra persistir
                threading.Thread(target=jsonbin_save, daemon=True).start()
            # Manter apenas últimos 15 registros
            if len(oi_hist) > 15:
                oi_hist = oi_hist[-15:]
            # Determinar tendência (3+ dias)
            oi_trend = "neutro"
            if len(oi_hist) >= 2:
                recent = [e["oi"] for e in oi_hist[-3:]] if len(oi_hist) >= 3 else [e["oi"] for e in oi_hist]
                if all(recent[i] > recent[i-1] for i in range(1, len(recent))):
                    oi_trend = "montando"  # OI crescente = montando posições
                elif all(recent[i] < recent[i-1] for i in range(1, len(recent))):
                    oi_trend = "desmontando"  # OI decrescente = desmontando posições
            data_cache["oi_history"] = oi_hist
            data_cache["oi_trend"] = oi_trend

        if vol_today > 0:
            data_cache["status"] = "live"
            data_cache["error"] = None
            print(f"  [{now()}] Futuros OK — Vol: {fmt(vol_today)} | ADV20: {fmt(adv_20)} | OI(NQ): {fmt(oi)} | Preco: {mnq_price:.0f} ({price_change_pct:+.2f}%) | VolBias: {vol_bias:+.1f} | OI Trend: {data_cache.get('oi_trend','?')}")
        else:
            if data_cache.get("stocks_bias", 0) != 0:
                data_cache["status"] = "partial"
            print(f"  [{now()}] Volume zerado (mercado fechado?)")

    except Exception as e:
        data_cache["error"] = str(e)
        print(f"  [{now()}] Erro futuros: {e}")


def fetch_options():
    """Busca dados de opções do QQQ acumulado em múltiplos vencimentos via Yahoo Finance."""
    global data_cache
    try:
        print(f"  [{now()}] Buscando opcoes QQQ (multi-vencimento)...")

        # 1) Primeiro request: pega lista de vencimentos disponíveis + primeiro vencimento
        url = "https://query1.finance.yahoo.com/v7/finance/options/QQQ"
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()

        result = data.get("optionChain", {}).get("result", [])
        if not result:
            print(f"  [{now()}] Sem dados de opcoes QQQ")
            return

        # Lista de todos os vencimentos disponíveis (epoch timestamps)
        all_expirations = result[0].get("expirationDates", [])
        if not all_expirations:
            print(f"  [{now()}] Sem datas de vencimento")
            return

        # Pegar os 4 primeiros vencimentos (mais próximos)
        exp_to_fetch = all_expirations[:4]
        num_exp = len(exp_to_fetch)
        print(f"  [{now()}] {len(all_expirations)} vencimentos disponiveis, buscando {num_exp} mais proximos")

        # Acumuladores
        total_call_vol = 0
        total_put_vol = 0
        total_call_oi = 0
        total_put_oi = 0

        # O primeiro vencimento já veio no request inicial
        first_options = result[0].get("options", [])
        if first_options:
            chain = first_options[0]
            calls = chain.get("calls", [])
            puts = chain.get("puts", [])
            total_call_vol += sum(c.get("volume", 0) or 0 for c in calls)
            total_put_vol += sum(p.get("volume", 0) or 0 for p in puts)
            total_call_oi += sum(c.get("openInterest", 0) or 0 for c in calls)
            total_put_oi += sum(p.get("openInterest", 0) or 0 for p in puts)

        # 2) Buscar vencimentos adicionais (2º, 3º, 4º)
        for exp_date in exp_to_fetch[1:]:
            try:
                url2 = f"https://query1.finance.yahoo.com/v7/finance/options/QQQ?date={exp_date}"
                r2 = requests.get(url2, headers=HEADERS, timeout=15)
                r2.raise_for_status()
                d2 = r2.json()
                res2 = d2.get("optionChain", {}).get("result", [])
                if res2:
                    opts2 = res2[0].get("options", [])
                    if opts2:
                        c2 = opts2[0].get("calls", [])
                        p2 = opts2[0].get("puts", [])
                        total_call_vol += sum(c.get("volume", 0) or 0 for c in c2)
                        total_put_vol += sum(p.get("volume", 0) or 0 for p in p2)
                        total_call_oi += sum(c.get("openInterest", 0) or 0 for c in c2)
                        total_put_oi += sum(p.get("openInterest", 0) or 0 for p in p2)
                time.sleep(0.3)  # Rate limit
            except Exception as ex:
                print(f"  [{now()}] Erro ao buscar vencimento {exp_date}: {ex}")

        total_vol = total_call_vol + total_put_vol

        # Put/Call Ratio acumulado
        pc_ratio = (total_put_vol / total_call_vol) if total_call_vol > 0 else 0

        # Bias das opções (CONFIRMADOR intraday, não contrário):
        # P/C < 0.7 = muito mais calls que puts → sentimento bullish → bias +
        # 0.85-1.0 = equilibrado → neutro
        # P/C > 1.3 = muito mais puts que calls → medo/venda → bias -
        # Mais puts = mercado vendendo/hedging = bearish no curto prazo
        if pc_ratio > 0:
            opt_bias = max(-100, min(100, (0.9 - pc_ratio) / 0.6 * 100))
        else:
            opt_bias = 0

        data_cache.update({
            "options_pc_ratio": round(pc_ratio, 3),
            "options_call_vol": total_call_vol,
            "options_put_vol": total_put_vol,
            "options_total_vol": total_vol,
            "options_call_oi": total_call_oi,
            "options_put_oi": total_put_oi,
            "options_bias": round(opt_bias, 1),
            "options_expirations": num_exp,
        })

        print(f"  [{now()}] Opcoes OK ({num_exp} exp) — P/C: {pc_ratio:.3f} | Call Vol: {fmt(total_call_vol)} | Put Vol: {fmt(total_put_vol)} | Bias: {opt_bias:+.1f}")

    except Exception as e:
        print(f"  [{now()}] Erro opcoes: {e}")


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


# ─── JSONBin.io — Persistência OI ──────────────────────────────

def jsonbin_load():
    """Carrega histórico OI do JSONBin.io na inicialização."""
    global data_cache
    if not JSONBIN_KEY or not JSONBIN_ID:
        print(f"  [{now()}] JSONBin nao configurado — historico OI apenas em memoria")
        return
    try:
        url = f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}/latest"
        headers = {"X-Master-Key": JSONBIN_KEY, "X-Bin-Meta": "false"}
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        record = r.json()
        oi_hist = record.get("oi_history", [])
        if oi_hist:
            data_cache["oi_history"] = oi_hist
            print(f"  [{now()}] Historico OI carregado do JSONBin: {len(oi_hist)} dias")
        else:
            print(f"  [{now()}] JSONBin vazio, historico comeca do zero")
    except Exception as e:
        print(f"  [{now()}] Erro ao carregar JSONBin: {e}")


def jsonbin_save():
    """Salva histórico OI no JSONBin.io (chamado apenas quando novo dia é adicionado)."""
    if not JSONBIN_KEY or not JSONBIN_ID:
        return
    try:
        url = f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}"
        headers = {
            "X-Master-Key": JSONBIN_KEY,
            "Content-Type": "application/json",
        }
        payload = {"oi_history": data_cache.get("oi_history", [])}
        r = requests.put(url, headers=headers, json=payload, timeout=10)
        r.raise_for_status()
        print(f"  [{now()}] Historico OI salvo no JSONBin ({len(payload['oi_history'])} dias)")
    except Exception as e:
        print(f"  [{now()}] Erro ao salvar JSONBin: {e}")


def futures_loop():
    while True:
        fetch_futures()
        data_cache["last_update"] = datetime.now(timezone.utc).isoformat()
        time.sleep(REFRESH_FUTURES)


def stocks_loop():
    time.sleep(5)
    while True:
        fetch_stocks()
        time.sleep(REFRESH_STOCKS)


def options_loop():
    time.sleep(10)
    while True:
        fetch_options()
        time.sleep(REFRESH_STOCKS)  # Mesmo intervalo das stocks (5min)


# ─── Divergence Detection ──────────────────────────────────────

def compute_divergences():
    """Detecta divergências entre indicadores — sinais de possível reversão."""
    divs = []

    vol_bias = data_cache.get("volume_bias", 0)
    stk_bias = data_cache.get("stocks_bias", 0)
    opt_bias = data_cache.get("options_bias", 0)
    price_pct = data_cache.get("mnq_price_change_pct", 0)
    oi_trend = data_cache.get("oi_trend", "neutro")
    has_vol = data_cache.get("volume_total", 0) > 0
    has_opt = data_cache.get("options_total_vol", 0) > 0

    if not has_vol:
        return divs  # sem dados de volume, sem divergências confiáveis

    TH = 30   # bias forte (threshold)
    PP = 0.25  # preço mínimo (% move)

    # 1) Top20 subindo mas Volume confirma queda (ou vice-versa)
    if stk_bias > TH and vol_bias < -TH:
        divs.append({
            "id": "stk_bull_vol_bear",
            "title": "Stocks Bull x Volume Bear",
            "detail": f"Top20 subindo ({stk_bias:+.0f}) mas volume confirma queda ({vol_bias:+.0f})",
            "signal": "Alta sem convicção de volume — possível fraqueza",
            "dir": "bear",
        })
    elif stk_bias < -TH and vol_bias > TH:
        divs.append({
            "id": "stk_bear_vol_bull",
            "title": "Stocks Bear x Volume Bull",
            "detail": f"Top20 caindo ({stk_bias:+.0f}) mas volume confirma alta ({vol_bias:+.0f})",
            "signal": "Volume nao confirma queda — possível reversao altista",
            "dir": "bull",
        })

    # 2) Options vs Volume
    if has_opt:
        if opt_bias > TH and vol_bias < -TH:
            divs.append({
                "id": "opt_bull_vol_bear",
                "title": "Options Bull x Volume Bear",
                "detail": f"P/C otimista ({opt_bias:+.0f}) mas volume pressiona venda ({vol_bias:+.0f})",
                "signal": "Sentimento positivo vs fluxo negativo",
                "dir": "neutro",
            })
        elif opt_bias < -TH and vol_bias > TH:
            divs.append({
                "id": "opt_bear_vol_bull",
                "title": "Options Bear x Volume Bull",
                "detail": f"P/C pessimista ({opt_bias:+.0f}) mas volume confirma alta ({vol_bias:+.0f})",
                "signal": "Medo nas opcoes vs forca no volume — possível short squeeze",
                "dir": "bull",
            })

    # 3) OI Trend vs Preco
    if oi_trend == "montando" and price_pct < -PP:
        divs.append({
            "id": "oi_mont_price_dn",
            "title": "OI Montando + Preco Caindo",
            "detail": f"Posicoes crescendo mas MNQ {price_pct:+.2f}%",
            "signal": "Acumulo durante queda — possível reversao altista",
            "dir": "bull",
        })
    elif oi_trend == "desmontando" and price_pct > PP:
        divs.append({
            "id": "oi_desm_price_up",
            "title": "OI Desmontando + Preco Subindo",
            "detail": f"Posicoes diminuindo mas MNQ {price_pct:+.2f}%",
            "signal": "Desmonte durante alta — possível topo/exaustao",
            "dir": "bear",
        })

    # 4) Options muito bearish + Stocks fortes (ou vice-versa)
    if has_opt and opt_bias < -40 and stk_bias > 35:
        divs.append({
            "id": "opt_fear_stk_strong",
            "title": "Medo nas Opcoes + Stocks Fortes",
            "detail": f"P/C alto ({opt_bias:+.0f}) mas Top20 bullish ({stk_bias:+.0f})",
            "signal": "Hedge excessivo — mercado overprotected",
            "dir": "bull",
        })
    elif has_opt and opt_bias > 40 and stk_bias < -35:
        divs.append({
            "id": "opt_greed_stk_weak",
            "title": "Ganancia nas Opcoes + Stocks Fracas",
            "detail": f"P/C baixo ({opt_bias:+.0f}) mas Top20 bearish ({stk_bias:+.0f})",
            "signal": "Complacencia — risco de queda acentuada",
            "dir": "bear",
        })

    return divs


# ─── Flask App ──────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/api/data")
def api_data():
    d = dict(data_cache)
    d["divergences"] = compute_divergences()
    return jsonify(d)


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
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='6' fill='%23070a12'/%3E%3Cpath d='M6 22 L11 10 L16 18 L21 8 L26 16' fill='none' stroke='%2300d4aa' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3Ccircle cx='26' cy='16' r='2.5' fill='%2300d4aa'/%3E%3Ctext x='16' y='29' text-anchor='middle' fill='%238891a8' font-family='monospace' font-size='5' font-weight='700'%3EMNQ%3C/text%3E%3C/svg%3E">
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
  transition:background .25s,transform .15s ease-out,border-color .25s}
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

/* Divergence alerts */
.div-wrap{display:none;flex-direction:column;gap:8px}
.div-wrap.show{display:flex}
.div-head{display:flex;align-items:center;gap:6px;padding:0 2px}
.div-head-dot{width:6px;height:6px;border-radius:50%;background:var(--neut);animation:pulse 1.5s ease-in-out infinite}
.div-head-lbl{font:600 9px/1 var(--mono);color:var(--neut);text-transform:uppercase;letter-spacing:.8px}
.div-item{background:rgba(255,184,51,.03);border:1px solid rgba(255,184,51,.12);border-radius:14px;
  padding:12px 14px;animation:fadeUp .4s var(--ease2) forwards;opacity:0}
.div-item.bull{background:rgba(0,212,170,.03);border-color:rgba(0,212,170,.15)}
.div-item.bear{background:rgba(255,68,102,.03);border-color:rgba(255,68,102,.15)}
.div-title{font:600 11px/1.2 var(--mono);letter-spacing:.3px;margin-bottom:4px}
.div-item.bull .div-title{color:var(--bull)}
.div-item.bear .div-title{color:var(--bear)}
.div-item.neutro .div-title{color:var(--neut)}
.div-detail{font:400 10px/1.4 var(--sans);color:var(--t2);margin-bottom:3px}
.div-signal{font:500 10px/1.3 var(--sans);color:var(--t1)}
.div-notif-btn{align-self:flex-start;background:rgba(255,184,51,.06);border:1px solid rgba(255,184,51,.15);
  border-radius:8px;color:var(--neut);font:500 9px/1 var(--mono);padding:7px 12px;cursor:pointer;
  letter-spacing:.5px;text-transform:uppercase;transition:all .2s;display:none}
.div-notif-btn.show{display:block}
.div-notif-btn:active{background:rgba(255,184,51,.12);transform:scale(.97)}

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

/* Interactive cards — smooth 3D tilt */
.card{transform-style:preserve-3d;perspective:800px;cursor:pointer;
  transition:transform .25s cubic-bezier(0.22,1,0.36,1),background .25s,border-color .25s,box-shadow .3s}
.card:hover{background:var(--card-h);border-color:rgba(255,255,255,0.09);
  box-shadow:0 8px 32px rgba(0,0,0,.3),0 0 0 1px rgba(255,255,255,.04)}
.card-inner{transition:transform .15s ease-out}
.card.tilt .card-inner{will-change:transform}

/* Sparkline tooltip */
.spark-tip{position:absolute;pointer-events:none;opacity:0;transition:opacity .15s;
  background:rgba(10,14,24,.95);border:1px solid rgba(255,255,255,.1);border-radius:8px;
  padding:6px 10px;font:500 10px/1.3 var(--mono);color:var(--t1);z-index:10;
  white-space:nowrap;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
  box-shadow:0 4px 12px rgba(0,0,0,.5);transform:translateX(-50%) translateY(-100%)}
.spark-tip.show{opacity:1}
.spark-tip .tip-date{color:var(--t3);font-size:8px;letter-spacing:.5px}
.spark-tip .tip-val{font-size:11px;font-weight:600;margin-top:2px}
.spark-tip .tip-price{color:var(--t2);font-size:9px;margin-top:1px}
.spark-dot-hover{transition:r .15s ease,opacity .15s;cursor:crosshair}
.spark-line-hover{position:absolute;width:1px;background:rgba(255,255,255,.08);top:0;bottom:0;pointer-events:none;opacity:0;transition:opacity .15s}
.spark-container{position:relative}

/* Gauge glow on hover */
.g-svg{transition:filter .3s ease}
.card:hover .g-svg{filter:drop-shadow(0 0 12px rgba(255,255,255,.06))}
[id^="gn-"]{transition:r .3s ease,filter .3s ease}
.card:hover [id^="gn-"]{r:7;filter:url(#gl-stk) drop-shadow(0 0 6px currentColor)}

/* Touch ripple */
.ripple{position:absolute;border-radius:50%;background:rgba(255,255,255,.06);
  transform:scale(0);animation:rippleAnim .6s ease-out forwards;pointer-events:none}
@keyframes rippleAnim{to{transform:scale(4);opacity:0}}

/* OI History sparkline */
.oi-hist{margin-top:12px;padding-top:10px;border-top:1px solid var(--card-bd)}
.oi-hist-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.oi-hist-lbl{font:500 8px/1 var(--mono);color:var(--t3);text-transform:uppercase;letter-spacing:.7px}
.oi-trend{font:600 9px/1 var(--mono);letter-spacing:.5px;padding:3px 8px;border-radius:4px}
.oi-trend.montando{color:var(--bull);background:var(--bull-bg)}
.oi-trend.desmontando{color:var(--bear);background:var(--bear-bg)}
.oi-trend.neutro{color:var(--neut);background:var(--neut-bg)}
.spark-svg{width:100%;height:40px;display:block}
.oi-dates{display:flex;justify-content:space-between;margin-top:2px;font:400 7px/1 var(--mono);color:var(--t3)}

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
    <span class="w">ADV <b>10%</b></span>
    <span class="w">OI <b>10%</b></span>
    <span class="w">OPT <b>15%</b></span>
    <span class="w">TOP20 <b>45%</b></span>
  </div>

  <div class="div-wrap" id="div-wrap">
    <div class="div-head">
      <div class="div-head-dot"></div>
      <span class="div-head-lbl">Divergencias Detectadas</span>
    </div>
    <div id="div-items"></div>
    <button class="div-notif-btn" id="div-notif-btn" onclick="requestNotifPerm()">Ativar Notificacoes</button>
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
    <div class="card-h"><span class="card-t">Volume <span style="color:var(--t3);font-size:9px;font-weight:400">(confirmado)</span></span><span class="badge" id="b-vol">--</span></div>
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
    <div class="card-h"><span class="card-t">Open Interest <span style="color:var(--t3);font-size:9px;font-weight:400">(NQ)</span></span><span class="badge" id="b-oi">--</span></div>
    <div class="g-wrap">
      <svg class="g-svg" viewBox="0 0 200 120" id="g-oi"></svg>
      <div class="g-center"><div class="g-val" id="r-oi">--</div><div class="g-unit">contratos</div></div>
    </div>
    <div class="stats" id="s-oi"></div>
    <div class="oi-hist" id="oi-hist" style="display:none">
      <div class="oi-hist-head">
        <span class="oi-hist-lbl">Historico OI (dias)</span>
        <span class="oi-trend neutro" id="oi-trend-badge">--</span>
      </div>
      <div class="spark-container" id="spark-container">
        <svg class="spark-svg" id="oi-spark" viewBox="0 0 300 40" preserveAspectRatio="none"></svg>
        <div class="spark-tip" id="spark-tip"><div class="tip-date"></div><div class="tip-val"></div><div class="tip-price"></div></div>
      </div>
      <div class="oi-dates" id="oi-dates"></div>
    </div>
  </div>

  <div class="card c6">
    <div class="card-h"><span class="card-t">Options P/C <span style="color:var(--t3);font-size:9px;font-weight:400">(QQQ)</span></span><span class="badge" id="b-opt">--</span></div>
    <div class="g-wrap">
      <svg class="g-svg" viewBox="0 0 200 120" id="g-opt"></svg>
      <div class="g-center"><div class="g-val" id="r-opt">--</div><div class="g-unit">put/call ratio</div></div>
    </div>
    <div class="stats" id="s-opt"></div>
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
var lastDivIds=[],notifEnabled=false;

/* ── Notification system ── */
function checkNotifSupport(){return 'Notification' in window}
window.requestNotifPerm=function(){
  if(!checkNotifSupport())return;
  Notification.requestPermission().then(function(p){
    notifEnabled=p==='granted';
    var btn=document.getElementById('div-notif-btn');
    if(btn){btn.style.display=notifEnabled?'none':'block';
      if(notifEnabled)btn.textContent='Notificacoes ativas'}
  });
};
function sendNotif(title,body,dir){
  if(!notifEnabled||!checkNotifSupport())return;
  try{
    var icon='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="#070a12"/><text x="32" y="40" text-anchor="middle" fill="'+(dir==='bull'?'%2300d4aa':dir==='bear'?'%23ff4466':'%23ffb833')+'" font-family="monospace" font-size="28" font-weight="700">!</text></svg>');
    new Notification(title,{body:body,icon:icon,tag:'mnq-div-'+Date.now(),requireInteraction:false});
  }catch(e){}
}
function updateDivergences(divs){
  var wrap=document.getElementById('div-wrap');
  var items=document.getElementById('div-items');
  var btn=document.getElementById('div-notif-btn');
  if(!wrap||!items)return;
  if(!divs||!divs.length){
    wrap.classList.remove('show');lastDivIds=[];return;
  }
  wrap.classList.add('show');
  /* Render items */
  items.innerHTML=divs.map(function(d,i){
    return'<div class="div-item '+(d.dir||'neutro')+'" style="animation-delay:'+(.04*i)+'s">'
      +'<div class="div-title">'+d.title+'</div>'
      +'<div class="div-detail">'+d.detail+'</div>'
      +'<div class="div-signal">'+d.signal+'</div></div>';
  }).join('');
  /* Show notification button if not yet granted */
  if(btn){
    if(checkNotifSupport()&&Notification.permission==='default'){btn.classList.add('show')}
    else if(checkNotifSupport()&&Notification.permission==='granted'){notifEnabled=true;btn.classList.remove('show')}
    else{btn.classList.remove('show')}
  }
  /* Send notification for NEW divergences only */
  var newIds=divs.map(function(d){return d.id});
  var fresh=divs.filter(function(d){return lastDivIds.indexOf(d.id)===-1});
  if(fresh.length>0&&lastDivIds.length>0){
    /* Only notify after first load (avoid spam on page open) */
    fresh.forEach(function(d){
      sendNotif('MNQ Divergencia: '+d.title,d.detail+' — '+d.signal,d.dir);
    });
  }
  lastDivIds=newIds;
}

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

/* ── Interactive: smooth 3D tilt + ripple + glow ── */
function initInteractive(){
  document.querySelectorAll('.card').forEach(function(card){
    card.style.position='relative';card.style.overflow='hidden';
    /* Remove fadeUp animation after it completes so JS can control transform */
    card.addEventListener('animationend',function(){card.style.animation='none';card.style.opacity='1'});
    var targetX=0,targetY=0,curX=0,curY=0,raf=null;
    /* Smooth lerp animation for fluid tilt */
    function animate(){
      curX+=(targetX-curX)*.08;curY+=(targetY-curY)*.08;
      if(Math.abs(curX-targetX)<.01&&Math.abs(curY-targetY)<.01){curX=targetX;curY=targetY}
      card.style.transform='perspective(800px) rotateY('+curX+'deg) rotateX('+curY+'deg) scale('+(1+Math.abs(curX+curY)*.0005)+')';
      if(Math.abs(curX-targetX)>.01||Math.abs(curY-targetY)>.01){raf=requestAnimationFrame(animate)}else{raf=null}
    }
    function startAnim(){if(!raf)raf=requestAnimationFrame(animate)}
    /* 3D tilt on mouse — smooth follow */
    card.addEventListener('mousemove',function(e){
      var r=card.getBoundingClientRect();
      var x=(e.clientX-r.left)/r.width-.5;
      var y=(e.clientY-r.top)/r.height-.5;
      targetX=x*8;targetY=-y*8;startAnim();
    });
    card.addEventListener('mouseleave',function(){
      targetX=0;targetY=0;startAnim();
    });
    /* Touch tilt — smooth follow */
    card.addEventListener('touchmove',function(e){
      var t=e.touches[0],r=card.getBoundingClientRect();
      var x=(t.clientX-r.left)/r.width-.5;
      var y=(t.clientY-r.top)/r.height-.5;
      targetX=x*10;targetY=-y*10;startAnim();
    },{passive:true});
    card.addEventListener('touchend',function(){
      targetX=0;targetY=0;startAnim();
    });
    /* Ripple on tap */
    card.addEventListener('click',function(e){
      var r=card.getBoundingClientRect();
      var rip=document.createElement('span');
      rip.className='ripple';
      rip.style.left=(e.clientX-r.left)+'px';
      rip.style.top=(e.clientY-r.top)+'px';
      rip.style.width=rip.style.height=Math.max(r.width,r.height)+'px';
      card.appendChild(rip);
      setTimeout(function(){rip.remove()},600);
    });
  });
}

/* ── OI History sparkline (interactive with tooltip) ── */
var sparkData=[];/* stored for tooltip interaction */
function renderOiSparkline(history,trend){
  var wrap=document.getElementById('oi-hist');
  var svg=document.getElementById('oi-spark');
  var dates=document.getElementById('oi-dates');
  var badge=document.getElementById('oi-trend-badge');
  if(!wrap||!svg||!history||history.length<2){if(wrap)wrap.style.display='none';return}
  wrap.style.display='block';
  sparkData=history;
  /* Trend badge */
  var tLabels={montando:'MONTANDO POS.',desmontando:'DESMONTANDO',neutro:'ESTAVEL'};
  badge.textContent=tLabels[trend]||'ESTAVEL';
  badge.className='oi-trend '+(trend||'neutro');
  /* Draw sparkline */
  var vals=history.map(function(h){return h.oi});
  var mn=Math.min.apply(null,vals),mx=Math.max.apply(null,vals);
  var range=mx-mn||1;
  var w=300,h=40,pad=2;
  var pts=vals.map(function(v,i){
    var x=pad+i*(w-2*pad)/(vals.length-1);
    var y=h-pad-(v-mn)/range*(h-2*pad);
    return x+','+y;
  });
  /* Gradient: green if rising, red if falling */
  var lastDir=vals[vals.length-1]>=vals[vals.length-2]?'bull':'bear';
  var gc=lastDir==='bull'?G:B;
  var svgHtml='<defs><linearGradient id="sg" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="'+gc+'" stop-opacity=".3"/><stop offset="100%" stop-color="'+gc+'" stop-opacity="0"/></linearGradient></defs>'
    +'<polygon points="'+pad+','+h+' '+pts.join(' ')+' '+(w-pad)+','+h+'" fill="url(#sg)"/>'
    +'<polyline points="'+pts.join(' ')+'" fill="none" stroke="'+gc+'" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>';
  /* Dots — with hover class for interaction */
  vals.forEach(function(v,i){
    var x=pad+i*(w-2*pad)/(vals.length-1);
    var y=h-pad-(v-mn)/range*(h-2*pad);
    var isLast=i===vals.length-1;
    svgHtml+='<circle class="spark-dot-hover" data-idx="'+i+'" cx="'+x+'" cy="'+y+'" r="'+(isLast?'3.5':'2.5')+'" fill="'+gc+'" stroke="#070a12" stroke-width="1" style="cursor:crosshair"/>';
  });
  /* Invisible wider hit areas for easier touch/hover */
  vals.forEach(function(v,i){
    var x=pad+i*(w-2*pad)/(vals.length-1);
    var y=h-pad-(v-mn)/range*(h-2*pad);
    svgHtml+='<circle data-idx="'+i+'" cx="'+x+'" cy="'+y+'" r="10" fill="transparent" style="cursor:crosshair"/>';
  });
  svg.innerHTML=svgHtml;
  /* Date labels */
  if(history.length>0){
    var first=history[0].date.slice(5);
    var last=history[history.length-1].date.slice(5);
    dates.innerHTML='<span>'+first+'</span><span>'+history.length+' dias</span><span>'+last+'</span>';
  }
}
/* Sparkline tooltip interaction */
function initSparkTooltip(){
  var container=document.getElementById('spark-container');
  var svg=document.getElementById('oi-spark');
  var tip=document.getElementById('spark-tip');
  if(!container||!svg||!tip)return;
  function showTip(idx,clientX,clientY){
    if(!sparkData[idx])return;
    var d=sparkData[idx];
    var rect=container.getBoundingClientRect();
    var xPct=(clientX-rect.left)/rect.width;
    var tipX=xPct*100;
    /* Clamp so tooltip doesn't overflow */
    tipX=Math.max(15,Math.min(85,tipX));
    tip.style.left=tipX+'%';
    tip.style.bottom='calc(100% + 4px)';
    tip.querySelector('.tip-date').textContent=d.date||'';
    var oiFmt=d.oi>=1e6?(d.oi/1e6).toFixed(2)+'M':d.oi>=1e3?(d.oi/1e3).toFixed(1)+'K':d.oi;
    tip.querySelector('.tip-val').textContent='OI: '+oiFmt;
    if(d.price){tip.querySelector('.tip-price').textContent='Preco: '+d.price.toLocaleString('pt-BR')}else{tip.querySelector('.tip-price').textContent=''}
    tip.classList.add('show');
    /* Highlight the dot */
    svg.querySelectorAll('.spark-dot-hover').forEach(function(dot){
      dot.setAttribute('r',parseInt(dot.dataset.idx)===idx?'5':'2.5');
    });
  }
  function hideTip(){
    tip.classList.remove('show');
    svg.querySelectorAll('.spark-dot-hover').forEach(function(dot){
      var isLast=parseInt(dot.dataset.idx)===sparkData.length-1;
      dot.setAttribute('r',isLast?'3.5':'2.5');
    });
  }
  svg.addEventListener('mousemove',function(e){
    var t=e.target;if(!t.dataset||!t.dataset.idx){hideTip();return}
    showTip(parseInt(t.dataset.idx),e.clientX,e.clientY);
  });
  svg.addEventListener('mouseleave',hideTip);
  svg.addEventListener('touchstart',function(e){
    var t=e.target;if(!t.dataset||!t.dataset.idx)return;
    var touch=e.touches[0];
    showTip(parseInt(t.dataset.idx),touch.clientX,touch.clientY);
  },{passive:true});
  svg.addEventListener('touchend',function(){setTimeout(hideTip,1500)});
}
initSparkTooltip();

/* Init gauges */
['stk','vol','adv','oi','opt'].forEach(initGauge);
initInteractive();

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
      var optBias=d.options_bias||0,pcRatio=d.options_pc_ratio||0;
      var callVol=d.options_call_vol||0,putVol=d.options_put_vol||0,totalOptVol=d.options_total_vol||0;
      var callOi=d.options_call_oi||0,putOi=d.options_put_oi||0;

      var volBias=d.volume_bias||0;  /* volume confirmado pela direção do preço (backend) */
      var mnqPricePct=d.mnq_price_change_pct||0;
      var mnqPrice=d.mnq_price||0;
      var optExp=d.options_expirations||0;
      var vs=vol>0?volBias:0;  /* usa bias confirmado, não magnitude pura */
      var as2=adv>0?bias(adv,adv,a5,pv):0;
      var os=oi>0?bias(oi,oi,oi*1.02,poi):0;
      var hasVol=vol>0;
      var hasOpt=totalOptVol>0;
      /* Pesos v5: VOL 20%, ADV 10%, OI 10%, OPT 15%, TOP20 45% */
      var comp=hasVol?(vs*.20+as2*.10+os*.10+(hasOpt?optBias*.15:0)+stkBias*(hasOpt?.45:.65)):stkBias+(hasOpt?optBias*.15:0);

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
        /* Volume — confirmado pela direção do preço */
        updateGauge('vol',vs);
        animNum(document.getElementById('r-vol'),vol,fmt);
        document.getElementById('r-vol').style.color=bCol(vs);
        setBadge('b-vol',vs);
        var prDir=mnqPricePct>0.01?'+'+mnqPricePct.toFixed(2)+'%':mnqPricePct<-0.01?mnqPricePct.toFixed(2)+'%':'0.00%';
        setStats('s-vol',[{l:'MNQ',d:mnqPricePct*50,v:prDir},{l:'vs MA20',d:((vol-adv)/adv)*100},{l:'Confirm',d:vs,v:(vs>=0?'+':'')+vs.toFixed(0)}]);

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
        var oiTrend=d.oi_trend||'neutro';
        var trendD=oiTrend==='montando'?15:oiTrend==='desmontando'?-15:0;
        setStats('s-oi',[{l:'OI Total',d:0,v:fmt(oi)},{l:'vs Prev',d:poi?((oi-poi)/poi)*100:0},{l:'Trend',d:trendD,v:oiTrend.toUpperCase()}]);
        /* Sparkline histórico */
        renderOiSparkline(d.oi_history||[],oiTrend);
      }else{
        ['vol','adv','oi'].forEach(function(id){
          updateGauge(id,0);
          document.getElementById('r-'+id).textContent='--';
          document.getElementById('r-'+id).style.color='var(--t3)';
          var be=document.getElementById('b-'+id);be.textContent='FECHADO';be.style.background='rgba(74,81,104,.12)';be.style.color=M;be.style.boxShadow='none';
        });
      }

      /* Options — sempre renderiza se tiver dados */
      if(hasOpt){
        updateGauge('opt',optBias);
        animNum(document.getElementById('r-opt'),pcRatio,function(v){return v.toFixed(3)});
        document.getElementById('r-opt').style.color=bCol(optBias);
        setBadge('b-opt',optBias);
        setStats('s-opt',[{l:'Call Vol',d:optBias>0?10:-10,v:fmt(callVol)},{l:'Put Vol',d:optBias<0?-10:10,v:fmt(putVol)},{l:optExp+' Exp',d:0,v:fmt(totalOptVol)}]);
      }else{
        updateGauge('opt',0);
        document.getElementById('r-opt').textContent='--';
        document.getElementById('r-opt').style.color='var(--t3)';
        var bo=document.getElementById('b-opt');bo.textContent='S/ DADOS';bo.style.background='rgba(74,81,104,.12)';bo.style.color=M;bo.style.boxShadow='none';
      }

      setComp(comp);
      /* Divergence alerts */
      updateDivergences(d.divergences||[]);
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
    print("  MNQ BIAS DASHBOARD — Cloud Edition v8")
    print("  Divergence Alerts + Browser Notifications")
    print("=" * 54)
    print()
    print(f"  Porta: {PORT}")
    print(f"  Futuros refresh: {REFRESH_FUTURES}s")
    print(f"  Stocks refresh:  {REFRESH_STOCKS}s")
    print(f"  JSONBin:  {'Configurado' if JSONBIN_KEY and JSONBIN_ID else 'Nao configurado (historico em memoria)'}")
    print()

    # Carregar histórico OI persistido
    jsonbin_load()

    t1 = threading.Thread(target=futures_loop, daemon=True)
    t2 = threading.Thread(target=stocks_loop, daemon=True)
    t3 = threading.Thread(target=options_loop, daemon=True)
    t1.start()
    t2.start()
    t3.start()

    print(f"  Dashboard: http://localhost:{PORT}")
    print()

    app.run(host="0.0.0.0", port=PORT, debug=False)


if __name__ == "__main__":
    main()
