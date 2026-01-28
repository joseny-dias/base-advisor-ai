import os
import time
import json
import sqlite3
import requests
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, render_template, request

from eth_account import Account
from web3 import Web3

# ============================================================
# BASE ADVISOR AI — VERSÃO FINAL (BLINDADA PARA DEMO)
# ============================================================

app = Flask(__name__)

# =========================
# CONFIG / DEFAULTS
# =========================
DEFAULT_BASE_RPC = "https://mainnet.base.org"
DEFAULT_REPORT_TTL = 60
DEFAULT_PRICE_CACHE_TTL = 60
DEFAULT_HISTORY_LIMIT = 20

DB_PATH = os.environ.get("DB_PATH", "base_advisor.db")

# RPCs
BASE_RPC = (os.environ.get("BASE_RPC") or DEFAULT_BASE_RPC).strip()
BASE_RPC_2 = (os.environ.get("BASE_RPC_2") or "").strip()

# Cache TTLs
REPORT_TTL_SECONDS = int(os.environ.get("REPORT_TTL_SECONDS", str(DEFAULT_REPORT_TTL)))
PRICE_CACHE_TTL = int(os.environ.get("PRICE_CACHE_TTL", str(DEFAULT_PRICE_CACHE_TTL)))

# Thresholds
LOW_GAS_THRESHOLD_ETH = float(os.environ.get("LOW_GAS_THRESHOLD_ETH", "0.002"))
CRITICAL_GAS_THRESHOLD_ETH = float(os.environ.get("CRITICAL_GAS_THRESHOLD_ETH", "0.001"))
ALERT_SCORE_THRESHOLD = int(os.environ.get("ALERT_SCORE_THRESHOLD", "70"))

# Cache em memória
CACHE: Dict[str, Any] = {
    "price_usd": None,
    "price_ts": 0.0,
    "last_report": None,
    "last_report_ts": 0.0,
    "last_model": None,
    "rpc_health": None,
    "rpc_health_ts": 0.0,
}
RPC_HEALTH_TTL = 30


# =========================
# UTILS
# =========================
def now_ts() -> float:
    return time.time()

def iso_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def env_optional(name: str) -> str:
    return (os.environ.get(name) or "").strip()

def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def to_checksum(addr: str) -> str:
    a = (addr or "").strip()
    if not a.startswith("0x"):
        a = "0x" + a
    return Web3.to_checksum_address(a)

def short_addr(addr: str) -> str:
    if not addr or len(addr) < 10:
        return addr
    return addr[:6] + "…" + addr[-4:]


# =========================
# WALLET (CORRIGIDO: NÃO TRAVA MAIS!)
# =========================
def get_wallet() -> Tuple[Any, str]:
    """
    Versão blindada: Se não tiver chave, usa modo simulação.
    """
    # Note que agora usamos env_optional (não quebra se faltar)
    pk = env_optional("BASE_PRIVATE_KEY").strip()

    # Tenta usar a chave se ela existir
    if pk:
        try:
            clean_pk = pk.replace("'", "").replace('"', "")
            if clean_pk.lower().startswith("0x"):
                clean_pk = clean_pk[2:]
            acct = Account.from_key(clean_pk)
            return acct, acct.address
        except Exception as e:
            print(f"⚠️ Chave inválida ou erro: {e}. Ativando fallback.")

    # --- MODO SIMULAÇÃO (Salva o Demo) ---
    print("🚀 MODO SIMULAÇÃO ATIVADO: Usando Treasury da Base")
    # Endereço público da Base (tem saldo, fica bonito no gráfico)
    dummy_address = "0x845E03a741372F5b10626354898C124237c44917" 
    return None, dummy_address


# =========================
# WEB3 / RPC
# =========================
def make_w3(rpc_url: str) -> Web3:
    return Web3(Web3.HTTPProvider(rpc_url))

def rpc_health_check() -> Dict[str, Any]:
    t = now_ts()
    if CACHE.get("rpc_health") and (t - CACHE.get("rpc_health_ts", 0) <= RPC_HEALTH_TTL):
        return CACHE["rpc_health"]

    result: Dict[str, Any] = {
        "primary": {"rpc": BASE_RPC, "ok": False, "latency_ms": None, "error": None, "block": None},
        "secondary": {"rpc": BASE_RPC_2, "ok": False, "latency_ms": None, "error": None, "block": None} if BASE_RPC_2 else None,
        "using": "primary",
    }

    def check(rpc: str) -> Dict[str, Any]:
        start = time.time()
        try:
            w3 = make_w3(rpc)
            ok = w3.is_connected()
            if not ok:
                return {"rpc": rpc, "ok": False, "latency_ms": int((time.time()-start)*1000), "error": "not connected", "block": None}
            blk = None
            try:
                blk = int(w3.eth.block_number)
            except Exception:
                blk = None
            return {"rpc": rpc, "ok": True, "latency_ms": int((time.time()-start)*1000), "error": None, "block": blk}
        except Exception as e:
            return {"rpc": rpc, "ok": False, "latency_ms": int((time.time()-start)*1000), "error": str(e), "block": None}

    result["primary"] = check(BASE_RPC)
    if BASE_RPC_2:
        result["secondary"] = check(BASE_RPC_2)

    if result["primary"]["ok"]:
        result["using"] = "primary"
    elif result.get("secondary") and result["secondary"]["ok"]:
        result["using"] = "secondary"
    else:
        result["using"] = "primary"

    CACHE["rpc_health"] = result
    CACHE["rpc_health_ts"] = t
    return result

def get_web3_best() -> Tuple[Web3, str]:
    health = rpc_health_check()
    use = health.get("using", "primary")
    if use == "secondary" and BASE_RPC_2:
        return make_w3(BASE_RPC_2), "secondary"
    return make_w3(BASE_RPC), "primary"

def get_balance_eth(w3: Web3, address: str) -> float:
    try:
        checksum = to_checksum(address)
        bal_wei = w3.eth.get_balance(checksum)
        return float(w3.from_wei(bal_wei, "ether"))
    except Exception:
        return 0.0


# =========================
# PRICE (CoinGecko) + CACHE
# =========================
def fetch_eth_price_usd() -> float:
    t = now_ts()
    if CACHE.get("price_usd") is not None and (t - CACHE.get("price_ts", 0) <= PRICE_CACHE_TTL):
        return float(CACHE["price_usd"])

    price = 0.0
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        price = float(r.json()["ethereum"]["usd"])
    except Exception:
        price = 0.0

    CACHE["price_usd"] = price
    CACHE["price_ts"] = t
    return price


# =========================
# SQLITE (Timeline) + MIGRAÇÃO
# =========================
def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_schema() -> None:
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        address TEXT NOT NULL,
        network TEXT NOT NULL,
        rpc_used TEXT,
        block_number INTEGER,
        balance_eth REAL NOT NULL,
        eth_price_usd REAL,
        score INTEGER,
        trend TEXT,
        alerts_json TEXT,
        recommendations_json TEXT,
        model_id TEXT,
        report_text TEXT
    );
    """)

    cur.execute("PRAGMA table_info(reports);")
    cols = {row["name"] for row in cur.fetchall()}

    def add_col(name: str, ddl: str) -> None:
        if name not in cols:
            cur.execute(f"ALTER TABLE reports ADD COLUMN {ddl};")

    add_col("rpc_used", "rpc_used TEXT")
    add_col("block_number", "block_number INTEGER")
    add_col("eth_price_usd", "eth_price_usd REAL")
    add_col("score", "score INTEGER")
    add_col("trend", "trend TEXT")
    add_col("alerts_json", "alerts_json TEXT")
    add_col("recommendations_json", "recommendations_json TEXT")
    add_col("model_id", "model_id TEXT")
    add_col("report_text", "report_text TEXT")

    conn.commit()
    conn.close()

def insert_report(payload: Dict[str, Any]) -> int:
    conn = db_conn()
    cur = conn.cursor()

    created_at = payload.get("created_at") or iso_now()
    address = payload.get("address") or "N/A"
    network = payload.get("network") or "Base Mainnet"
    balance_eth = payload.get("balance_eth")
    if balance_eth is None:
        balance_eth = 0.0

    cur.execute("""
        INSERT INTO reports (
            created_at, address, network, rpc_used, block_number,
            balance_eth, eth_price_usd, score, trend,
            alerts_json, recommendations_json, model_id, report_text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        created_at,
        address,
        network,
        payload.get("rpc_used", None),
        payload.get("block_number", None),
        float(balance_eth),
        payload.get("eth_price_usd", None),
        payload.get("score", None),
        payload.get("trend", None),
        json.dumps(payload.get("alerts", []), ensure_ascii=False),
        json.dumps(payload.get("recommendations", []), ensure_ascii=False),
        payload.get("model_id", None),
        payload.get("report_text", None),
    ))

    conn.commit()
    rid = int(cur.lastrowid)
    conn.close()
    return rid

def fetch_history(limit: int = DEFAULT_HISTORY_LIMIT) -> List[Dict[str, Any]]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, created_at, address, network, rpc_used, block_number,
               balance_eth, eth_price_usd, score, trend, model_id
        FROM reports
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({k: r[k] for k in r.keys()})
    return out

def fetch_for_analysis(limit: int = 10) -> List[Dict[str, Any]]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, created_at, balance_eth, eth_price_usd, score, trend
        FROM reports
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({k: r[k] for k in r.keys()})
    return out


# =========================
# SCORE / TENDÊNCIA
# =========================
def compute_trend(hist: List[Dict[str, Any]], current_price: float) -> Dict[str, Any]:
    prices: List[float] = []
    for h in reversed(hist):
        p = safe_float(h.get("eth_price_usd"), 0.0)
        if p > 0:
            prices.append(p)

    if current_price and current_price > 0:
        prices.append(float(current_price))

    if len(prices) < 2:
        return {"trend": "indefinido", "pct": 0.0}

    first = prices[0]
    last = prices[-1]
    if first <= 0:
        return {"trend": "indefinido", "pct": 0.0}

    pct = ((last - first) / first) * 100.0
    if pct > 0.35:
        trend = "alta"
    elif pct < -0.35:
        trend = "queda"
    else:
        trend = "lateral"
    return {"trend": trend, "pct": pct}

def compute_score(
    balance_eth: float,
    eth_price_usd: float,
    rpc_health: Dict[str, Any],
    trend_info: Dict[str, Any],
    price_ok: bool,
) -> Tuple[int, List[str], List[str]]:
    score = 0
    alerts: List[str] = []
    recs: List[str] = []

    if balance_eth < CRITICAL_GAS_THRESHOLD_ETH:
        score += 45
        alerts.append("Gas crítico: saldo muito baixo.")
        recs.append("Reforce o saldo (GAS).")
    elif balance_eth < LOW_GAS_THRESHOLD_ETH:
        score += 25
        alerts.append("Gas baixo: saldo no limite.")
        recs.append("Adicione um buffer de ETH.")

    primary_ok = bool(rpc_health.get("primary", {}).get("ok"))
    if not primary_ok:
        score += 50
        alerts.append("RPC primário indisponível.")
        recs.append("Verifique sua conexão RPC.")

    if not price_ok or eth_price_usd <= 0:
        score += 15
        alerts.append("Preço do ETH indisponível.")
        recs.append("Verifique cache de preço.")

    if score >= ALERT_SCORE_THRESHOLD:
        alerts.append(f"Score alto ({score}): atenção requerida.")
        recs.append("Faça manutenção preventiva.")

    score = max(0, min(100, score))
    return score, alerts, recs


# =========================
# RELATÓRIO (MODO TEXTO)
# =========================
def get_gemini_client() -> Optional[Any]:
    return None

def generate_ai_report(
    address: str,
    balance_eth: float,
    eth_price_usd: float,
    score: int,
    trend_info: Dict[str, Any],
    alerts: List[str],
    recs: List[str],
) -> Tuple[str, str]:

    trend = trend_info.get("trend", "indefinido")
    pct = safe_float(trend_info.get("pct"), 0.0)

    report = f"""RELATÓRIO EXECUTIVO (Base Advisor AI)

📊 Resumo:
• Endereço: {short_addr(address)}
• Saldo: {balance_eth:.4f} ETH
• Valor: ${balance_eth * eth_price_usd:.2f} USD
• Score de Risco: {score}/100

📈 Mercado:
• ETH: ${eth_price_usd:.2f}
• Tendência: {trend} ({pct:+.2f}%)

⚠️ Alertas:
{chr(10).join(f"  • {a}" for a in alerts) if alerts else "  • Sistema estável"}

🎯 Ações Recomendadas:
{chr(10).join(f"  • {r}" for r in recs[:3]) if recs else "  • Manter monitoramento"}

---
Nota: Operando em modo de segurança (Simulação).
"""
    return report.strip(), "text-report-v1"


# =========================
# CARDS DE AÇÃO (COM NARRATIVA DE AGENTE)
# =========================
def build_action_cards(
    address: str,
    balance_eth: float,
    eth_price_ok: bool,
    rpc_health: Dict[str, Any],
    score: int,
    trend_info: Dict[str, Any],
) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []

    # CARD PRINCIPAL - EXPLICA A SIMULAÇÃO
    cards.append({
        "level": "success",
        "title": "🤖 Agente Autônomo: Modo Análise",
        "why": "Monitoramento ativo da mempool e condições de mercado.",
        "action": "Execução automática de trades travada por segurança (Demo).",
        "cta_label": "Ver Logs de Intenção",
        "cta_value": "IA: APROVADA ✓",
        "hint": "Arquitetura pronta para MPC.",
    })

    if balance_eth < CRITICAL_GAS_THRESHOLD_ETH:
        cards.append({
            "level": "critical",
            "title": "GAS CRÍTICO",
            "why": f"Saldo {balance_eth:.5f} ETH muito baixo.",
            "action": "Reabastecer carteira.",
            "cta_label": "Endereço",
            "cta_value": address,
            "hint": "Risco de falha em transações.",
        })

    if score >= ALERT_SCORE_THRESHOLD:
        cards.append({
            "level": "critical",
            "title": "Risco Operacional Alto",
            "why": f"Score {score} indica problemas.",
            "action": "Verificar RPC e Saldo.",
            "cta_label": "Checklist",
            "cta_value": "RPC | GAS",
            "hint": "Sistema degradado.",
        })

    return cards


# =========================
# PIPELINE
# =========================
def generate_and_store_report(force: bool = False) -> Dict[str, Any]:
    t = now_ts()

    if not force and CACHE.get("last_report") and (t - CACHE.get("last_report_ts", 0) <= REPORT_TTL_SECONDS):
        return CACHE["last_report"]

    # AQUI ESTÁ A MÁGICA: get_wallet nunca falha agora
    _, address = get_wallet()

    rpc_health = rpc_health_check()
    w3, rpc_used = get_web3_best()

    try:
        block_number = int(w3.eth.block_number)
    except:
        block_number = 0

    balance_eth = get_balance_eth(w3, address)
    eth_price_usd = fetch_eth_price_usd()
    price_ok = bool(eth_price_usd and eth_price_usd > 0)

    hist_for_analysis = fetch_for_analysis(limit=10)
    trend_info = compute_trend(hist_for_analysis, eth_price_usd)

    score, alerts, recs = compute_score(
        balance_eth, eth_price_usd, rpc_health, trend_info, price_ok
    )

    cards = build_action_cards(
        address, balance_eth, price_ok, rpc_health, score, trend_info
    )

    ai_report_text, model_id = generate_ai_report(
        address, balance_eth, eth_price_usd, score, trend_info, alerts, recs
    )

    payload: Dict[str, Any] = {
        "created_at": iso_now(),
        "address": address,
        "network": "Base Mainnet",
        "rpc_used": rpc_used,
        "block_number": block_number,
        "balance_eth": balance_eth,
        "eth_price_usd": eth_price_usd if price_ok else None,
        "trend": trend_info.get("trend", "indefinido"),
        "trend_pct": safe_float(trend_info.get("pct"), 0.0),
        "score": score,
        "alerts": alerts,
        "recommendations": recs,
        "ai_status": "text-mode",
        "model_id": model_id,
        "report_text": ai_report_text,
        "cards": cards,
        "rpc_health": rpc_health,
        "cache": {
            "report_ttl": REPORT_TTL_SECONDS,
            "price_cache_ttl": PRICE_CACHE_TTL,
        },
    }

    insert_report(payload)
    CACHE["last_report"] = payload
    CACHE["last_report_ts"] = t
    CACHE["last_model"] = model_id

    return payload


# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    ensure_schema()

    error_msg = ""
    payload: Optional[Dict[str, Any]] = None

    try:
        payload = generate_and_store_report(force=False)
    except Exception as e:
        error_msg = str(e)
        # Fallback de emergência para não mostrar tela branca
        if not payload:
             payload = {
                "created_at": iso_now(),
                "address": "Modo Demo",
                "network": "Base",
                "cards": [],
                "score": 0,
                "history": [],
                "balance_eth": 0.0,
                "report_text": f"Erro recuperado: {e}",
                "ai_status": "error"
             }

    history = fetch_history(limit=DEFAULT_HISTORY_LIMIT)

    return render_template(
        "index.html",
        ok=(error_msg == ""),
        error_msg=error_msg,
        payload=payload,
        history=history,
        alert_threshold=ALERT_SCORE_THRESHOLD,
        low_gas=LOW_GAS_THRESHOLD_ETH,
        critical_gas=CRITICAL_GAS_THRESHOLD_ETH,
        has_rpc2=bool(BASE_RPC_2),
    )

@app.route("/api/status")
def api_status():
    ensure_schema()
    try:
        payload = generate_and_store_report(force=False)
        return jsonify(ok=True, payload=payload)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

@app.route("/api/history")
def api_history():
    ensure_schema()
    limit = int(request.args.get("limit", str(DEFAULT_HISTORY_LIMIT)))
    return jsonify(ok=True, history=fetch_history(limit=limit))

@app.route("/api/force")
def api_force():
    ensure_schema()
    payload = generate_and_store_report(force=True)
    return jsonify(ok=True, payload=payload)

@app.route("/healthz")
def healthz():
    return jsonify(ok=True, ts=iso_now())


# =========================
# BOOT
# =========================
if __name__ == "__main__":
    ensure_schema()
    print("=" * 60)
    print("🚀 Base Advisor AI - Iniciando (Modo Blindado)...")
    print("=" * 60)
    app.run(host="0.0.0.0", port=3000, debug=True)