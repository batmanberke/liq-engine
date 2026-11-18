"""
dashboard.py
============
Local web dashboard for the Chronicle RWA Engine.

Run:
    python -m chronicle_rwa_engine.dashboard
    python -m chronicle_rwa_engine.dashboard --port 8080
    python -m chronicle_rwa_engine.dashboard --market 0x... --chain 8453

Opens in your browser at http://localhost:5000
"""

import argparse
import json
import threading
import webbrowser
import random
from dataclasses import asdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from .morpho_client import MorphoClient, CHAIN_NAMES
from .models import MarketParams, MarketState
from .analysis import LiquidationAnalyzer, ChroniclePokeSimulator, LiquidationFlagger
from .liquidation import get_eligible_liquidators, run_auction, LIQUIDATOR_REGISTRY, MORPHO_BLUE_ADDRESS
from .engine import _parse_lltv, _safe_float, DEFAULT_MARKET_ID, DEFAULT_CHAIN_ID


# ═══════════════════════════════════════════════════════════════════════════════
# DATA PIPELINE — runs the full engine and returns structured JSON
# ═══════════════════════════════════════════════════════════════════════════════

def run_pipeline(market_id: str, chain_id: int, shock_min: float, shock_max: float) -> dict:
    """Run the full engine pipeline and return all intermediate data as a dict."""

    client = MorphoClient()

    # ── Module 1: Market Data ──────────────────────────────────────────────
    if chain_id is None:
        chain_id = client.detect_chain(market_id)
        if chain_id is None:
            return {"error": "Market not found on any supported chain."}

    try:
        raw = client.get_market(market_id, chain_id)
    except Exception as e:
        # Auto-detect fallback
        detected = client.detect_chain(market_id)
        if detected:
            chain_id = detected
            raw = client.get_market(market_id, chain_id)
        else:
            return {"error": f"Market not found: {e}"}

    m = raw.get("marketById")
    if m is None:
        return {"error": f"Market not found on chain {chain_id}."}

    oracle_info = m.get("oracle") or {}
    loan = m["loanAsset"]
    coll = m["collateralAsset"]
    lltv = _parse_lltv(m["lltv"])

    collateral_price = _safe_float(
        coll.get("price", {}).get("usd") if coll.get("price") else None
    )
    loan_price = _safe_float(
        loan.get("price", {}).get("usd") if loan.get("price") else None
    )

    params = MarketParams(
        market_id=m["marketId"], chain_id=chain_id, lltv=lltv,
        loan_asset_symbol=loan["symbol"], loan_asset_address=loan["address"],
        loan_asset_decimals=int(loan["decimals"]),
        collateral_asset_symbol=coll["symbol"],
        collateral_asset_address=coll["address"],
        collateral_asset_decimals=int(coll["decimals"]),
        oracle_address=oracle_info.get("address", "N/A"),
        oracle_type=oracle_info.get("type"),
        irm_address=m.get("irmAddress"),
        collateral_price_usd=collateral_price, loan_price_usd=loan_price,
    )

    s = m["state"]

    # Debug: print raw API values to terminal for diagnosis
    loan_decimals = int(loan["decimals"])
    coll_decimals = int(coll["decimals"])
    print(f"  [debug] Raw API state values:")
    for k in ["supplyAssets","supplyAssetsUsd","borrowAssets","borrowAssetsUsd",
              "collateralAssets","collateralAssetsUsd","liquidityAssets","liquidityAssetsUsd"]:
        print(f"    {k}: {s.get(k)}")
    print(f"    loan decimals: {loan_decimals}, collateral decimals: {coll_decimals}")

    # Morpho API returns raw asset amounts as uint256 strings.
    # The *Usd fields are already human-readable floats.
    # The raw asset fields need dividing by 10^decimals.
    def scale_loan(raw):
        return _safe_float(raw) / (10 ** loan_decimals)

    def scale_coll(raw):
        return _safe_float(raw) / (10 ** coll_decimals)

    # Check if USD values look like they need scaling.
    # If supplyAssetsUsd is way larger than expected (> 1e12) it's raw.
    raw_supply_usd = _safe_float(s.get("supplyAssetsUsd"))
    raw_supply = _safe_float(s.get("supplyAssets"))

    # Heuristic: if USD value > 1e12, it's likely unscaled raw value
    # In that case, compute USD from scaled assets * price
    if raw_supply_usd > 1e12 and loan_price > 0:
        # USD fields are raw — compute from scaled assets
        print(f"  [debug] USD fields appear raw, computing from assets × price")
        supply_usd = scale_loan(s.get("supplyAssets")) * loan_price
        borrow_usd = scale_loan(s.get("borrowAssets")) * loan_price
        collateral_usd = scale_coll(s.get("collateralAssets")) * collateral_price
        liquidity_usd = scale_loan(s.get("liquidityAssets")) * loan_price
    else:
        supply_usd = raw_supply_usd
        borrow_usd = _safe_float(s.get("borrowAssetsUsd"))
        collateral_usd = _safe_float(s.get("collateralAssetsUsd"))
        liquidity_usd = _safe_float(s.get("liquidityAssetsUsd"))

    state = MarketState(
        supply_assets=scale_loan(s.get("supplyAssets")),
        supply_assets_usd=supply_usd,
        borrow_assets=scale_loan(s.get("borrowAssets")),
        borrow_assets_usd=borrow_usd,
        collateral_assets=scale_coll(s.get("collateralAssets")),
        collateral_assets_usd=collateral_usd,
        liquidity_assets=scale_loan(s.get("liquidityAssets")),
        liquidity_assets_usd=liquidity_usd,
        utilization=_safe_float(s.get("utilization")),
        borrow_apy=_safe_float(s.get("borrowApy")),
        supply_apy=_safe_float(s.get("supplyApy")),
        fee=_safe_float(s.get("fee")),
    )

    vaults = m.get("supplyingVaults") or []
    warnings = m.get("warnings") or []

    # Reallocatable liquidity from Public Allocator (raw uint256)
    raw_reallocatable = _safe_float(m.get("reallocatableLiquidityAssets"))
    reallocatable_assets = raw_reallocatable / (10 ** loan_decimals) if raw_reallocatable > 1e10 else raw_reallocatable
    reallocatable_usd = reallocatable_assets * loan_price if loan_price > 0 else 0

    # Morpho "Total Market Size" = supply (from market) + reallocatable from vaults
    # Morpho "Total Liquidity" = liquidity + reallocatable
    total_market_size_assets = state.supply_assets + reallocatable_assets
    total_market_size_usd = state.supply_assets_usd + reallocatable_usd
    total_liquidity_assets = state.liquidity_assets + reallocatable_assets
    total_liquidity_usd = state.liquidity_assets_usd + reallocatable_usd

    print(f"  [debug] Reallocatable: {reallocatable_assets:,.2f} ({loan['symbol']}) = ${reallocatable_usd:,.2f}")
    print(f"  [debug] Total Market Size: ${total_market_size_usd:,.2f}")
    print(f"  [debug] Total Liquidity:   ${total_liquidity_usd:,.2f}")

    if collateral_price > 0 and loan_price > 0:
        effective_price = collateral_price / loan_price
    elif collateral_price > 0:
        effective_price = collateral_price
    else:
        effective_price = 1.0

    # Positions
    raw_positions = client.get_market_positions(market_id)

    # ── Module 2: Analysis ─────────────────────────────────────────────────
    analyzer = LiquidationAnalyzer(params)
    positions = analyzer.analyze_positions(raw_positions)
    borrowers = [p for p in positions if p.is_borrower]
    by_risk = sorted(borrowers, key=lambda p: p.distance_pct)
    buckets = analyzer.build_liquidation_buckets(positions)

    # ── Module 3: Next price simulation ────────────────────────────────────
    simulator = ChroniclePokeSimulator(effective_price, (shock_min, shock_max))
    next_price, pct_change = simulator.generate_next_poke()

    # ── Module 4: Liquidation flagging ─────────────────────────────────────
    flagger = LiquidationFlagger(params, positions, effective_price)
    payload = flagger.evaluate(next_price, pct_change)

    # ── Assemble response ──────────────────────────────────────────────────
    return {
        "market": {
            "market_id": params.market_id,
            "chain_id": chain_id,
            "chain_name": CHAIN_NAMES.get(chain_id, "Unknown"),
            "pair": f"{params.collateral_asset_symbol} / {params.loan_asset_symbol}",
            "collateral_symbol": params.collateral_asset_symbol,
            "loan_symbol": params.loan_asset_symbol,
            "lltv": lltv,
            "oracle_address": params.oracle_address,
            "oracle_type": params.oracle_type or "unknown",
            "irm_address": params.irm_address,
            "collateral_price_usd": collateral_price,
            "loan_price_usd": loan_price,
            "effective_price": effective_price,
            "total_market_size_usd": total_market_size_usd,
            "total_market_size_assets": total_market_size_assets,
            "total_liquidity_usd": total_liquidity_usd,
            "total_liquidity_assets": total_liquidity_assets,
            "reallocatable_usd": reallocatable_usd,
        },
        "state": asdict(state),
        "vaults": vaults,
        "warnings": warnings,
        "positions": {
            "total_count": len(raw_positions),
            "borrower_count": len(borrowers),
            "ltv_min": min((p.current_ltv for p in borrowers), default=0),
            "ltv_max": max((p.current_ltv for p in borrowers), default=0),
            "distance_min": min((p.distance_pct for p in borrowers), default=0),
            "distance_max": max((p.distance_pct for p in borrowers), default=0),
            "underwater_count": sum(1 for p in borrowers if p.distance_pct <= 0),
            "underwater_debt_usd": sum(p.borrow_assets_usd for p in borrowers if p.distance_pct <= 0),
            "top_riskiest": [
                {
                    "address": p.user_address,
                    "current_ltv": round(p.current_ltv, 6),
                    "lltv": round(params.lltv, 6),
                    "health_factor": round((params.lltv / p.current_ltv), 4) if p.current_ltv > 0 else 999,
                    "distance_pct": round(p.distance_pct, 4),
                    "liquidation_price_ratio": round(p.liquidation_price_ratio, 6),
                    "liquidation_price_usd": round(collateral_price * p.liquidation_price_ratio, 4) if collateral_price > 0 else 0,
                    "debt_usd": round(p.borrow_assets_usd, 2),
                    "debt_assets": round(p.borrow_assets, 6),
                    "collateral_usd": round(p.collateral_usd, 2),
                    "collateral_assets": round(p.collateral, 6),
                    "supply_usd": round(p.supply_assets_usd, 2),
                    "supply_assets": round(p.supply_assets, 6),
                    "max_borrowable_usd": round(p.collateral_usd * params.lltv, 2),
                    "remaining_borrow_capacity_usd": round(p.collateral_usd * params.lltv - p.borrow_assets_usd, 2),
                }
                for p in by_risk[:30]
            ],
            "all_borrowers": [
                {
                    "address": p.user_address,
                    "current_ltv": round(p.current_ltv, 6),
                    "health_factor": round((params.lltv / p.current_ltv), 4) if p.current_ltv > 0 else 999,
                    "distance_pct": round(p.distance_pct, 4),
                    "debt_usd": round(p.borrow_assets_usd, 2),
                    "collateral_usd": round(p.collateral_usd, 2),
                }
                for p in by_risk
            ],
        },
        "buckets": [
            {
                "price_change_pct": round(b.price_change_pct, 2),
                "count": b.position_count,
                "debt_usd": round(b.total_debt_usd, 2),
                "collateral_usd": round(b.total_collateral_usd, 2),
                "cum_count": b.cumulative_count,
                "cum_debt_usd": round(b.cumulative_debt_usd, 2),
            }
            for b in buckets if b.price_change_pct >= -30
        ],
        "simulation": {
            "current_price": effective_price,
            "next_price": next_price,
            "pct_change": round(pct_change, 4),
            "shock_min": shock_min,
            "shock_max": shock_max,
        },
        "payload": asdict(payload),
        "liquidators": _build_liquidator_data(params, payload),
    }


def _build_liquidator_data(params, payload):
    """Build Steps 5 & 6 data: liquidator eligibility + auction results."""
    from dataclasses import asdict as _asdict

    eligible, ineligible = get_eligible_liquidators(params.collateral_asset_symbol)

    # Build registry view
    registry = []
    for liq in LIQUIDATOR_REGISTRY:
        is_eligible = liq in eligible
        registry.append({
            "name": liq.name,
            "address": liq.address,
            "entity_type": liq.entity_type,
            "kyc_status": liq.kyc_status,
            "whitelisted_assets": liq.whitelisted_assets,
            "jurisdiction": liq.jurisdiction,
            "capital_available_usd": liq.capital_available_usd,
            "contact": liq.contact,
            "registered_since": liq.registered_since,
            "liquidations_executed": liq.liquidations_executed,
            "total_volume_usd": liq.total_volume_usd,
            "eligible": is_eligible,
            "reason": _eligibility_reason(liq, params.collateral_asset_symbol),
        })

    # Run auction if there are liquidatable positions
    auctions = []
    if payload.liquidation_triggered and payload.positions and eligible:
        auction_results = run_auction(
            liquidatable_positions=payload.positions,
            eligible_liquidators=eligible,
            collateral_symbol=params.collateral_asset_symbol,
            loan_symbol=params.loan_asset_symbol,
            oracle_price=params.collateral_price_usd,
            lltv=params.lltv,
            market_id=params.market_id,
            chain_id=params.chain_id,
            collateral_address=params.collateral_asset_address,
            loan_address=params.loan_asset_address,
            oracle_address=params.oracle_address,
            irm_address=params.irm_address or "0x0000000000000000000000000000000000000000",
        )
        auctions = auction_results  # already plain dicts now

    return {
        "collateral_symbol": params.collateral_asset_symbol,
        "morpho_address": MORPHO_BLUE_ADDRESS,
        "eligible_count": len(eligible),
        "ineligible_count": len(ineligible),
        "registry": registry,
        "auctions": auctions,
        "auction_count": len(auctions),
    }


def _eligibility_reason(liq, collateral_symbol):
    if liq.kyc_status != "Verified":
        return "KYC not verified (status: " + liq.kyc_status + ")"
    if collateral_symbol not in liq.whitelisted_assets:
        return "Not whitelisted for " + collateral_symbol
    return "KYC verified + whitelisted for " + collateral_symbol


# ═══════════════════════════════════════════════════════════════════════════════
# HTML DASHBOARD TEMPLATE
# ═══════════════════════════════════════════════════════════════════════════════

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Chronicle RWA Engine — Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=DM+Sans:wght@400;500;600;700&display=swap');

:root {
  --bg: #080c14; --surface: #0f1629; --surface2: #151d33;
  --border: #1c2744; --border-l: #253052;
  --text: #e2e8f0; --muted: #7b8bab; --dim: #4a5779;
  --accent: #38bdf8; --blue: #3b82f6; --purple: #8b5cf6;
  --danger: #ef4444; --danger-bg: #ef44441a; --danger-border: #ef444444;
  --success: #22c55e; --success-bg: #22c55e1a; --success-border: #22c55e44;
  --warn: #f59e0b; --warn-bg: #f59e0b1a;
  --mono: 'JetBrains Mono', monospace;
  --sans: 'DM Sans', system-ui, sans-serif;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:var(--sans); background:var(--bg); color:var(--text); min-height:100vh; }

.loading-overlay {
  position:fixed; inset:0; background:var(--bg); z-index:9999;
  display:flex; flex-direction:column; align-items:center; justify-content:center;
  transition: opacity 0.4s;
}
.loading-overlay.hidden { opacity:0; pointer-events:none; }
.spinner { width:48px; height:48px; border:3px solid var(--border);
  border-top-color:var(--blue); border-radius:50%; animation:spin 0.8s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
.loading-overlay p { margin-top:16px; color:var(--muted); font-size:0.9rem; }

.app { max-width:1400px; margin:0 auto; padding:20px 24px 40px; }

/* Header */
.header { display:flex; justify-content:space-between; align-items:center;
  padding:16px 0 20px; border-bottom:1px solid var(--border); margin-bottom:24px; }
.header h1 { font-size:1.4rem; font-weight:700;
  background:linear-gradient(135deg,var(--blue),var(--purple));
  -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
.header .sub { color:var(--muted); font-size:0.78rem; margin-top:3px; }

.controls { display:flex; gap:8px; align-items:center; }
.controls input { background:var(--surface); border:1px solid var(--border);
  color:var(--text); font-family:var(--mono); font-size:0.75rem;
  padding:7px 10px; border-radius:6px; width:90px; }
.controls input:focus { outline:none; border-color:var(--blue); }
.btn { background:linear-gradient(135deg,var(--blue),var(--purple));
  color:#fff; border:none; padding:8px 18px; border-radius:7px;
  font-family:var(--sans); font-size:0.8rem; font-weight:600;
  cursor:pointer; transition:opacity 0.2s; }
.btn:hover { opacity:0.85; }
.btn:disabled { opacity:0.4; cursor:not-allowed; }
.btn-sm { padding:6px 12px; font-size:0.72rem; }

/* Status badge */
.status-badge { display:inline-flex; align-items:center; gap:7px;
  padding:7px 14px; border-radius:7px; font-family:var(--mono);
  font-size:0.75rem; font-weight:700; }
.status-badge .dot { width:8px; height:8px; border-radius:50%;
  animation:pulse 1.5s ease-in-out infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
.status-triggered { background:var(--danger-bg); color:var(--danger); border:1px solid var(--danger-border); }
.status-triggered .dot { background:var(--danger); }
.status-safe { background:var(--success-bg); color:var(--success); border:1px solid var(--success-border); }
.status-safe .dot { background:var(--success); }

/* Step sections */
.step { background:var(--surface); border:1px solid var(--border);
  border-radius:12px; padding:20px 24px; margin-bottom:16px; }
.step-header { display:flex; align-items:center; gap:10px; margin-bottom:16px; }
.step-num { background:var(--surface2); border:1px solid var(--border-l);
  color:var(--blue); font-family:var(--mono); font-size:0.7rem; font-weight:700;
  width:28px; height:28px; border-radius:7px;
  display:flex; align-items:center; justify-content:center; }
.step-title { font-size:0.95rem; font-weight:600; }
.step-sub { color:var(--muted); font-size:0.75rem; margin-left:auto; }

/* Metric grid */
.metrics { display:grid; grid-template-columns:repeat(auto-fill, minmax(160px,1fr)); gap:10px; }
.mc { background:var(--surface2); border:1px solid var(--border);
  border-radius:8px; padding:12px 14px; }
.mc .label { font-size:0.65rem; text-transform:uppercase; letter-spacing:0.7px;
  color:var(--muted); margin-bottom:4px; }
.mc .val { font-family:var(--mono); font-size:1rem; font-weight:700; }
.mc .detail { font-size:0.7rem; color:var(--dim); margin-top:2px; }

/* Tables */
.tbl-wrap { overflow-x:auto; margin-top:12px; }
table { width:100%; border-collapse:collapse; font-size:0.76rem; }
th { text-align:left; color:var(--muted); font-weight:600; font-size:0.68rem;
  text-transform:uppercase; letter-spacing:0.5px; padding:8px 10px;
  border-bottom:1px solid var(--border); }
td { padding:7px 10px; border-bottom:1px solid var(--border);
  font-family:var(--mono); font-size:0.74rem; }
tr:hover td { background:var(--surface2); }
.addr { color:var(--blue); }
.danger { color:var(--danger); }
.warn { color:var(--warn); }
.ok { color:var(--success); }

/* Chart */
.chart-wrap { margin-top:12px; border-radius:10px; overflow:hidden; }

/* Payload */
.payload-pre { background:var(--bg); border:1px solid var(--border);
  border-radius:8px; padding:14px; overflow:auto; max-height:350px;
  font-family:var(--mono); font-size:0.68rem; line-height:1.6;
  color:var(--text); margin-top:10px; }

/* Vault chips */
.vault-chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
.chip { background:var(--surface2); border:1px solid var(--border);
  padding:4px 10px; border-radius:6px; font-size:0.72rem; color:var(--muted); }

.footer { text-align:center; color:var(--dim); font-size:0.68rem; margin-top:24px; }

/* Two-col layout */
.cols { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
@media (max-width:900px) { .cols { grid-template-columns:1fr; } }
</style>
</head>
<body>

<div class="loading-overlay" id="loader">
  <div class="spinner"></div>
  <p>Pulling live data from Morpho API…</p>
</div>

<div class="app" id="app" style="display:none;">

  <!-- HEADER -->
  <div class="header">
    <div>
      <h1>Chronicle RWA Engine</h1>
      <div class="sub" id="headerSub">Loading…</div>
    </div>
    <div style="display:flex;align-items:center;gap:12px;">
      <div id="statusBadge"></div>
      <div class="controls">
        <input type="number" id="shockMin" step="0.5" title="Min shock %">
        <input type="number" id="shockMax" step="0.5" title="Max shock %">
        <button class="btn" id="rerunBtn" onclick="rerun()">Re-simulate</button>
      </div>
    </div>
  </div>

  <!-- STEP 1: MARKET DATA -->
  <div class="step" id="step1">
    <div class="step-header">
      <div class="step-num">1</div>
      <div class="step-title">Market Data Retrieval Layer</div>
      <div class="step-sub">Morpho GraphQL API → market params, state, positions</div>
    </div>
    <div class="metrics" id="marketMetrics"></div>
    <div id="vaultChips"></div>
    <div id="warningsDiv"></div>
  </div>

  <!-- STEP 2: RISK ANALYSIS -->
  <div class="step" id="step2">
    <div class="step-header">
      <div class="step-num">2</div>
      <div class="step-title">Liquidation Risk Analysis</div>
      <div class="step-sub">LLTV distance & liquidation price for every position</div>
    </div>
    <div class="metrics" id="riskMetrics"></div>
    <div style="margin-top:14px;">
      <div style="font-size:0.8rem;font-weight:600;margin-bottom:8px;">All Borrower Positions <span style="color:var(--dim);font-weight:400;font-size:0.72rem;">(click row to expand)</span></div>
      <div class="tbl-wrap" id="riskTable"></div>
    </div>
    <div class="cols" style="margin-top:16px;">
      <div>
        <div style="font-size:0.8rem;font-weight:600;margin-bottom:8px;">LTV Distribution</div>
        <div class="chart-wrap" id="ltvChart"></div>
      </div>
      <div>
        <div style="font-size:0.8rem;font-weight:600;margin-bottom:8px;">Liquidation Volume by Price Change</div>
        <div class="chart-wrap" id="bucketChart"></div>
      </div>
    </div>
  </div>

  <!-- STEP 3: PRICE SIMULATION -->
  <div class="step" id="step3">
    <div class="step-header">
      <div class="step-num">3</div>
      <div class="step-title">Next Price Simulation</div>
      <div class="step-sub">Chronicle Agent Network mock — Schnorr-signed poke</div>
    </div>
    <div class="metrics" id="simMetrics"></div>
  </div>

  <!-- STEP 4: LIQUIDATION FLAG -->
  <div class="step" id="step4">
    <div class="step-header">
      <div class="step-num">4</div>
      <div class="step-title">Liquidation Flagging &amp; Payload</div>
      <div class="step-sub">Evaluate positions at new price → settlement payload</div>
    </div>
    <div class="metrics" id="flagMetrics"></div>
    <div id="atRiskTable" style="margin-top:12px;"></div>
    <div style="margin-top:14px;">
      <div style="font-size:0.8rem;font-weight:600;margin-bottom:6px;">Settlement Payload (Auctioneer Output)</div>
      <pre class="payload-pre" id="payloadPre"></pre>
    </div>
  </div>

  <!-- STEP 5: LIQUIDATOR ONBOARDING -->
  <div class="step" id="step5">
    <div class="step-header">
      <div class="step-num">5</div>
      <div class="step-title">Liquidator Onboarding &amp; Eligibility</div>
      <div class="step-sub">KYC-verified, RWA-whitelisted liquidator registry</div>
    </div>
    <div class="metrics" id="liqMetrics"></div>
    <div style="margin-top:14px;">
      <div style="font-size:0.8rem;font-weight:600;margin-bottom:8px;">Liquidator Registry</div>
      <div class="tbl-wrap" id="liqRegistry"></div>
    </div>
  </div>

  <!-- STEP 6: AUCTION & EXECUTION -->
  <div class="step" id="step6">
    <div class="step-header">
      <div class="step-num">6</div>
      <div class="step-title">Auction &amp; Execution</div>
      <div class="step-sub">Sealed-bid auction among eligible liquidators → Morpho liquidate() calldata</div>
    </div>
    <div class="metrics" id="auctionMetrics"></div>
    <div id="auctionResults" style="margin-top:14px;"></div>
  </div>

  <div class="footer">Chronicle RWA Engine v0.3 — Powered by Chronicle Proof of Assets × Morpho Blue</div>

</div>

<script>
const API = '/api/run';
let DATA = null;

// --- Helpers ---
const $ = id => document.getElementById(id);
const fmt = (n, d=2) => n == null ? '—' : Number(n).toLocaleString(undefined, {minimumFractionDigits:d, maximumFractionDigits:d});
const fmtUsd = n => n == null ? '—' : '$' + fmt(n);
const fmtPct = (n, d=2) => n == null ? '—' : fmt(n,d) + '%';
const shortAddr = a => a ? a.slice(0,10) + '…' + a.slice(-6) : '—';
const riskClass = dist => dist <= 0 ? 'danger' : dist < 2 ? 'warn' : 'ok';

function metricCard(label, val, detail='') {
  return `<div class="mc"><div class="label">${label}</div><div class="val">${val}</div>${detail ? `<div class="detail">${detail}</div>` : ''}</div>`;
}

// --- Fetch & Render ---
async function fetchData(shockMin, shockMax) {
  $('loader').classList.remove('hidden');
  $('app').style.display = 'none';
  $('rerunBtn').disabled = true;

  const params = new URLSearchParams({shock_min: shockMin, shock_max: shockMax});
  const resp = await fetch(`${API}?${params}`);
  DATA = await resp.json();

  $('loader').classList.add('hidden');
  $('app').style.display = 'block';
  $('rerunBtn').disabled = false;

  if (DATA.error) {
    $('app').innerHTML = `<div class="step"><p style="color:var(--danger)">${DATA.error}</p></div>`;
    return;
  }
  render();
}

function render() {
  const d = DATA;
  const mk = d.market, st = d.state, pos = d.positions, sim = d.simulation, pl = d.payload;

  // Header
  $('headerSub').textContent = `${mk.pair} on ${mk.chain_name} (${mk.chain_id}) — Market ${mk.market_id.slice(0,14)}…${mk.market_id.slice(-6)}`;

  // Status badge
  const triggered = pl.liquidation_triggered;
  $('statusBadge').innerHTML = `<div class="status-badge ${triggered ? 'status-triggered' : 'status-safe'}"><span class="dot"></span>${triggered ? 'LIQUIDATIONS TRIGGERED' : 'NO LIQUIDATIONS'}</div>`;

  // Shock inputs
  $('shockMin').value = sim.shock_min;
  $('shockMax').value = sim.shock_max;

  // ── Step 1: Market Data ──
  $('marketMetrics').innerHTML = [
    metricCard('Market', mk.pair, `Chain ${mk.chain_id} (${mk.chain_name})`),
    metricCard('LLTV', fmtPct(mk.lltv*100), 'Liquidation threshold'),
    metricCard('Total Market Size', fmtUsd(mk.total_market_size_usd), `${fmt(mk.total_market_size_assets,2)} ${mk.loan_symbol}`),
    metricCard('Total Liquidity', fmtUsd(mk.total_liquidity_usd), `${fmt(mk.total_liquidity_assets,2)} ${mk.loan_symbol}`),
    metricCard('Supply (market)', fmtUsd(st.supply_assets_usd), `Util: ${fmtPct(st.utilization*100,1)}`),
    metricCard('Borrow', fmtUsd(st.borrow_assets_usd), `Fee: ${fmtPct(st.fee*100,1)}`),
    metricCard('Collateral', fmtUsd(st.collateral_assets_usd)),
    metricCard('Reallocatable', fmtUsd(mk.reallocatable_usd), 'From Public Allocator'),
    metricCard('Borrow APY', fmtPct(st.borrow_apy*100, 3)),
    metricCard('Supply APY', fmtPct(st.supply_apy*100, 3)),
    metricCard(mk.collateral_symbol + ' Price', fmtUsd(mk.collateral_price_usd)),
    metricCard(mk.loan_symbol + ' Price', fmtUsd(mk.loan_price_usd)),
    metricCard('Oracle Ratio', fmt(mk.effective_price, 6), `${mk.collateral_symbol}/${mk.loan_symbol}`),
    metricCard('Oracle Type', mk.oracle_type, shortAddr(mk.oracle_address)),
  ].join('');

  // Vaults
  if (d.vaults.length) {
    $('vaultChips').innerHTML = `<div style="font-size:0.75rem;color:var(--muted);margin-top:12px;">Supplying Vaults (${d.vaults.length}):</div><div class="vault-chips">${d.vaults.map(v => `<span class="chip">${v.name} (${v.symbol})</span>`).join('')}</div>`;
  }

  // Warnings
  if (d.warnings.length) {
    $('warningsDiv').innerHTML = `<div style="margin-top:10px;padding:8px 12px;background:var(--warn-bg);border:1px solid #f59e0b44;border-radius:7px;font-size:0.75rem;color:var(--warn);">${d.warnings.map(w => `[${w.level}] ${w.type}`).join(', ')}</div>`;
  }

  // ── Step 2: Risk Analysis ──
  const avgLtv = pos.all_borrowers.length > 0
    ? pos.all_borrowers.reduce((s,b) => s+b.current_ltv, 0) / pos.all_borrowers.length : 0;
  const totalDebt = pos.all_borrowers.reduce((s,b) => s+b.debt_usd, 0);
  const totalColl = pos.all_borrowers.reduce((s,b) => s+b.collateral_usd, 0);

  $('riskMetrics').innerHTML = [
    metricCard('Total Positions', pos.total_count),
    metricCard('Active Borrowers', pos.borrower_count),
    metricCard('Total Debt', fmtUsd(totalDebt)),
    metricCard('Total Collateral', fmtUsd(totalColl)),
    metricCard('Avg LTV', fmtPct(avgLtv*100, 2)),
    metricCard('LTV Range', `${fmt(pos.ltv_min,4)} — ${fmt(pos.ltv_max,4)}`),
    metricCard('Distance Range', `${fmt(pos.distance_min,2)}% — ${fmt(pos.distance_max,2)}%`),
    metricCard('Underwater', `<span class="${pos.underwater_count > 0 ? 'danger' : 'ok'}">${pos.underwater_count}</span>`, pos.underwater_count > 0 ? fmtUsd(pos.underwater_debt_usd) + ' debt' : 'All healthy'),
  ].join('');

  // ── Position detail table ──
  const cSym = mk.collateral_symbol;
  const lSym = mk.loan_symbol;

  let tbl = `<table><thead><tr>
    <th></th>
    <th>Address</th>
    <th style="text-align:right">Health Factor</th>
    <th style="text-align:right">LTV</th>
    <th style="text-align:right">Dist to Liq</th>
    <th style="text-align:right">Debt (${lSym})</th>
    <th style="text-align:right">Debt (USD)</th>
    <th style="text-align:right">Collateral (${cSym})</th>
    <th style="text-align:right">Collateral (USD)</th>
    <th style="text-align:right">Liq. Price (${cSym})</th>
    <th style="text-align:right">Capacity Left</th>
  </tr></thead><tbody>`;

  for (let i = 0; i < pos.top_riskiest.length; i++) {
    const p = pos.top_riskiest[i];
    const cls = riskClass(p.distance_pct);
    const hfCls = p.health_factor < 1 ? 'danger' : p.health_factor < 1.05 ? 'warn' : p.health_factor < 1.2 ? 'warn' : 'ok';
    const hfBar = Math.min(p.health_factor / 2 * 100, 100);
    const hfColor = p.health_factor < 1 ? 'var(--danger)' : p.health_factor < 1.1 ? 'var(--warn)' : 'var(--success)';
    const capCls = p.remaining_borrow_capacity_usd < 0 ? 'danger' : 'ok';
    const rowId = 'detail_' + i;

    tbl += '<tr style="cursor:pointer" onclick="var el=document.getElementById(\'' + rowId + '\');el.style.display=el.style.display===\'none\'?\'table-row\':\'none\'">';
    tbl += '<td style="color:var(--dim);font-size:0.65rem;">▶</td>';
    tbl += '<td class="addr">' + shortAddr(p.address) + '</td>';
    tbl += '<td style="text-align:right"><div style="display:flex;align-items:center;gap:6px;justify-content:flex-end">';
    tbl += '<div style="width:40px;height:5px;background:var(--border);border-radius:3px;overflow:hidden">';
    tbl += '<div style="width:' + hfBar + '%;height:100%;background:' + hfColor + ';border-radius:3px"></div></div>';
    tbl += '<span class="' + hfCls + '">' + fmt(p.health_factor,3) + '</span></div></td>';
    tbl += '<td style="text-align:right" class="' + cls + '">' + fmtPct(p.current_ltv*100, 2) + '</td>';
    tbl += '<td style="text-align:right" class="' + cls + '">' + fmt(p.distance_pct,2) + '%</td>';
    tbl += '<td style="text-align:right">' + fmt(p.debt_assets,4) + '</td>';
    tbl += '<td style="text-align:right">' + fmtUsd(p.debt_usd) + '</td>';
    tbl += '<td style="text-align:right">' + fmt(p.collateral_assets,4) + '</td>';
    tbl += '<td style="text-align:right">' + fmtUsd(p.collateral_usd) + '</td>';
    tbl += '<td style="text-align:right">' + fmtUsd(p.liquidation_price_usd) + '</td>';
    tbl += '<td style="text-align:right" class="' + capCls + '">' + fmtUsd(p.remaining_borrow_capacity_usd) + '</td>';
    tbl += '</tr>';

    // Expandable detail row
    let detail = '<tr id="' + rowId + '" style="display:none"><td colspan="11" style="padding:12px 16px;background:var(--surface2);border-radius:6px;">';
    detail += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;font-size:0.72rem;">';
    detail += '<div><span style="color:var(--muted)">Full address:</span><br><span class="addr" style="word-break:break-all">' + p.address + '</span></div>';
    detail += '<div><span style="color:var(--muted)">Health Factor:</span><br><span class="' + hfCls + '" style="font-size:1rem;font-weight:700">' + fmt(p.health_factor,4) + '</span></div>';
    detail += '<div><span style="color:var(--muted)">Current LTV / LLTV:</span><br>' + fmtPct(p.current_ltv*100,3) + ' / ' + fmtPct(p.lltv*100,2) + '</div>';
    detail += '<div><span style="color:var(--muted)">Distance to liquidation:</span><br><span class="' + cls + '">' + fmt(p.distance_pct,3) + '% price drop</span></div>';
    detail += '<div><span style="color:var(--muted)">Liquidation price (' + cSym + '):</span><br>' + fmtUsd(p.liquidation_price_usd) + ' <span style="color:var(--dim)">(current: ' + fmtUsd(mk.collateral_price_usd) + ')</span></div>';
    detail += '<div><span style="color:var(--muted)">Liq price ratio:</span><br>' + fmt(p.liquidation_price_ratio,6) + '</div>';
    detail += '<div><span style="color:var(--muted)">Debt:</span><br>' + fmt(p.debt_assets,6) + ' ' + lSym + ' (' + fmtUsd(p.debt_usd) + ')</div>';
    detail += '<div><span style="color:var(--muted)">Collateral:</span><br>' + fmt(p.collateral_assets,6) + ' ' + cSym + ' (' + fmtUsd(p.collateral_usd) + ')</div>';
    detail += '<div><span style="color:var(--muted)">Max borrowable at LLTV:</span><br>' + fmtUsd(p.max_borrowable_usd) + '</div>';
    detail += '<div><span style="color:var(--muted)">Remaining borrow capacity:</span><br><span class="' + capCls + '">' + fmtUsd(p.remaining_borrow_capacity_usd) + '</span></div>';
    if (p.supply_usd > 0) {
      detail += '<div><span style="color:var(--muted)">Supply:</span><br>' + fmt(p.supply_assets,6) + ' ' + lSym + ' (' + fmtUsd(p.supply_usd) + ')</div>';
    }
    detail += '</div></td></tr>';
    tbl += detail;
  }
  tbl += '</tbody></table>';
  $('riskTable').innerHTML = tbl;

  // LTV histogram
  try {
    const ltvs = pos.all_borrowers.map(b => b.current_ltv);
    Plotly.newPlot('ltvChart', [{
      x: ltvs, type: 'histogram', nbinsx: 40,
      marker: { color: '#3b82f680', line: { color: '#3b82f6', width: 1 } },
    }], {
      paper_bgcolor:'#0f1629', plot_bgcolor:'#0f1629',
      font: {family:'DM Sans', color:'#7b8bab', size:10},
      xaxis: {title:'LTV', gridcolor:'#1c2744'},
      yaxis: {title:'Count', gridcolor:'#1c2744'},
      shapes: [{type:'line', x0:mk.lltv, x1:mk.lltv, y0:0, y1:1, yref:'paper',
        line:{color:'#ef4444', width:2, dash:'dot'}}],
      annotations: [{x:mk.lltv, y:1.03, yref:'paper', text:'LLTV', showarrow:false,
        font:{size:10, color:'#ef4444', family:'JetBrains Mono'}}],
      margin:{l:45,r:20,t:25,b:40}, height:250,
    }, {responsive:true, displayModeBar:false});
  } catch(e) { console.error('LTV chart error:', e); }

  // Bucket chart
  try {
    const bk = d.buckets;
    const pokeLabel = 'Poke ' + (sim.pct_change > 0 ? '+' : '') + fmt(sim.pct_change,2) + '%';
    const pokeX = fmt(sim.pct_change,1) + '%';
    Plotly.newPlot('bucketChart', [
      {
        x: bk.map(b => b.price_change_pct + '%'), y: bk.map(b => b.debt_usd),
        type:'bar', name:'Debt at level ($)',
        marker:{color: bk.map(b => b.price_change_pct >= 0 ? 'rgba(239,68,68,0.8)' : b.price_change_pct > -5 ? 'rgba(245,158,11,0.7)' : 'rgba(59,130,246,0.55)')},
        hovertemplate:'%{x}<br>Positions: %{customdata}<br>Debt: $%{y:,.0f}<extra></extra>',
        customdata: bk.map(b => b.count),
      },
      {
        x: bk.map(b => b.price_change_pct + '%'), y: bk.map(b => b.cum_debt_usd),
        type:'scatter', mode:'lines', name:'Cumulative debt ($)',
        line:{color:'#8b5cf6', width:2.5, shape:'spline'},
        yaxis:'y2',
        hovertemplate:'%{x}<br>Cum. debt: $%{y:,.0f}<extra></extra>',
      }
    ], {
      paper_bgcolor:'#0f1629', plot_bgcolor:'#0f1629',
      font:{family:'DM Sans', color:'#7b8bab', size:10},
      xaxis:{title:'Collateral Price Change', gridcolor:'#1c2744', tickangle:-45, tickfont:{size:8}},
      yaxis:{title:'Debt at level (USD)', gridcolor:'#1c2744', tickformat:'$,.0f'},
      yaxis2:{title:'Cumulative (USD)', overlaying:'y', side:'right', tickformat:'$,.0f', gridcolor:'transparent'},
      shapes:[{type:'line', x0:pokeX, x1:pokeX, y0:0, y1:1, xref:'x', yref:'paper',
        line:{color:'#ef4444', width:2, dash:'dot'}}],
      annotations:[{x:pokeX, y:1.03, xref:'x', yref:'paper', text:pokeLabel,
        showarrow:false, font:{size:9, color:'#ef4444', family:'JetBrains Mono'},
        bgcolor:'#ef444420', bordercolor:'#ef444466', borderwidth:1, borderpad:3}],
      legend:{x:0.01, y:0.98, bgcolor:'#0f162999', bordercolor:'#1c2744', font:{size:9}},
      margin:{l:70, r:70, t:30, b:70}, height:320, bargap:0.12,
    }, {responsive:true, displayModeBar:false});
  } catch(e) { console.error('Bucket chart error:', e); }

  // ── Step 3: Simulation ──
  try {
    const pColor = sim.pct_change < 0 ? 'var(--danger)' : 'var(--success)';
    $('simMetrics').innerHTML = [
      metricCard('Current Oracle Ratio', fmt(sim.current_price, 6)),
      metricCard('Simulated Next Poke', '<span style="color:' + pColor + '">' + fmt(sim.next_price, 6) + '</span>', (sim.pct_change > 0 ? '+' : '') + fmt(sim.pct_change,4) + '% price move'),
      metricCard('Shock Range', sim.shock_min + '% to ' + sim.shock_max + '%', 'Configurable above'),
      metricCard('Source', 'Random (mock)', 'Production: Chronicle Agents'),
    ].join('');
  } catch(e) { console.error('Step 3 error:', e); }

  // ── Step 4: Flagging ──
  try {
    $('flagMetrics').innerHTML = [
      metricCard('Triggered', '<span class="' + (triggered ? 'danger' : 'ok') + '">' + triggered + '</span>'),
      metricCard('Positions at Risk', '<span class="' + (pl.total_positions_at_risk > 0 ? 'danger' : 'ok') + '">' + pl.total_positions_at_risk + '</span>'),
      metricCard('Collateral at Risk', fmtUsd(pl.total_collateral_at_risk_usd)),
      metricCard('Debt at Risk', fmtUsd(pl.total_debt_at_risk_usd)),
    ].join('');

    // At-risk positions table
    if (pl.positions.length > 0) {
      let rt = '<div style="font-size:0.8rem;font-weight:600;margin-bottom:6px;color:var(--danger);">Positions Flagged for Liquidation</div>';
      rt += '<table><thead><tr><th>Address</th><th style="text-align:right">Current LTV</th><th style="text-align:right">New LTV</th><th style="text-align:right">Debt</th><th style="text-align:right">Collateral (new)</th><th style="text-align:right">Shortfall</th></tr></thead><tbody>';
      for (const p of pl.positions) {
        rt += '<tr><td class="addr">' + shortAddr(p.user) + '</td>';
        rt += '<td style="text-align:right">' + fmt(p.current_ltv,5) + '</td>';
        rt += '<td style="text-align:right" class="danger">' + fmt(p.new_ltv,5) + '</td>';
        rt += '<td style="text-align:right">' + fmtUsd(p.debt_usd) + '</td>';
        rt += '<td style="text-align:right">' + fmtUsd(p.collateral_usd_at_new_price) + '</td>';
        rt += '<td style="text-align:right" class="danger">' + fmtUsd(p.shortfall_usd) + '</td></tr>';
      }
      rt += '</tbody></table>';
      $('atRiskTable').innerHTML = rt;
    } else {
      $('atRiskTable').innerHTML = '<div style="color:var(--success);font-size:0.8rem;margin-top:8px;">No positions would be liquidated at the simulated price.</div>';
    }

    // Payload JSON
    $('payloadPre').textContent = JSON.stringify(pl, null, 2);
  } catch(e) { console.error('Step 4 error:', e); }

  // ── Step 5: Liquidator Registry ──
  try {
    const lq = d.liquidators;
    $('liqMetrics').innerHTML = [
      metricCard('Collateral Asset', lq.collateral_symbol, 'RWA requiring KYC to hold'),
      metricCard('Registered Liquidators', lq.registry.length),
      metricCard('Eligible for ' + lq.collateral_symbol, '<span class="' + (lq.eligible_count > 0 ? 'ok' : 'danger') + '">' + lq.eligible_count + '</span>', 'KYC verified + whitelisted'),
      metricCard('Ineligible', lq.ineligible_count, 'Missing KYC or whitelist'),
    ].join('');

    var ltbl = '<table><thead><tr>';
    ltbl += '<th>Liquidator</th><th>Type</th><th style="text-align:center">Status</th>';
    ltbl += '<th>Whitelisted Assets</th><th>Jurisdiction</th>';
    ltbl += '<th style="text-align:right">Capital</th>';
    ltbl += '<th style="text-align:right">Executions</th>';
    ltbl += '<th style="text-align:right">Volume</th>';
    ltbl += '<th style="text-align:center">Eligible</th>';
    ltbl += '</tr></thead><tbody>';

    for (var li = 0; li < lq.registry.length; li++) {
      var l = lq.registry[li];
      var eligCls = l.eligible ? 'ok' : 'danger';
      var eligIcon = l.eligible ? '✓' : '✗';
      var rowBg = l.eligible ? 'background:rgba(34,197,94,0.04);' : '';
      var kycBadge = l.kyc_status === 'Verified'
        ? '<span style="color:var(--success);font-size:0.7rem;background:var(--success-bg);padding:2px 8px;border-radius:4px;">Verified</span>'
        : '<span style="color:var(--warn);font-size:0.7rem;background:var(--warn-bg);padding:2px 8px;border-radius:4px;">' + l.kyc_status + '</span>';

      ltbl += '<tr style="' + rowBg + '">';
      ltbl += '<td><strong>' + l.name + '</strong><br><span style="color:var(--dim);font-size:0.65rem;">' + shortAddr(l.address) + '</span></td>';
      ltbl += '<td>' + l.entity_type + '</td>';
      ltbl += '<td style="text-align:center">' + kycBadge + '</td>';
      ltbl += '<td style="font-size:0.7rem;">' + l.whitelisted_assets.join(', ') + '</td>';
      ltbl += '<td style="font-size:0.72rem;">' + l.jurisdiction + '</td>';
      ltbl += '<td style="text-align:right">' + fmtUsd(l.capital_available_usd) + '</td>';
      ltbl += '<td style="text-align:right">' + l.liquidations_executed.toLocaleString() + '</td>';
      ltbl += '<td style="text-align:right">' + fmtUsd(l.total_volume_usd) + '</td>';
      ltbl += '<td style="text-align:center"><span class="' + eligCls + '" style="font-weight:700">' + eligIcon + '</span><br><span style="color:var(--dim);font-size:0.62rem;">' + l.reason + '</span></td>';
      ltbl += '</tr>';
    }
    ltbl += '</tbody></table>';
    $('liqRegistry').innerHTML = ltbl;
  } catch(e) { console.error('Step 5 error:', e); }

  // ── Step 6: Auction & Execution ──
  try {
    var lq = d.liquidators;
    var auctions = lq.auctions || [];

    if (!d.payload.liquidation_triggered) {
      $('auctionMetrics').innerHTML = metricCard('Status', '<span class="ok">No liquidations triggered</span>', 'Re-simulate with a larger negative shock to trigger auctions');
      $('auctionResults').innerHTML = '';
    } else if (auctions.length === 0) {
      $('auctionMetrics').innerHTML = metricCard('Status', '<span class="danger">Liquidations triggered but no eligible liquidators</span>', 'No KYC-verified liquidators whitelisted for ' + lq.collateral_symbol);
      $('auctionResults').innerHTML = '';
    } else {
      $('auctionMetrics').innerHTML = [
        metricCard('Auctions Run', auctions.length),
        metricCard('Status', '<span class="ok">Completed</span>', 'Sealed-bid, lowest discount wins'),
        metricCard('Eligible Bidders', lq.eligible_count),
        metricCard('Morpho Contract', shortAddr(lq.morpho_address), 'Target for liquidate() calls'),
      ].join('');

      var ahtml = '';
      for (var ai = 0; ai < auctions.length; ai++) {
        var auc = auctions[ai];
        var ph = auc.phases;
        var win = auc.winner;

        ahtml += '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:18px;margin-bottom:16px;">';

        // Auction header
        ahtml += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--border);">';
        ahtml += '<div><span style="font-size:0.9rem;font-weight:700;">Auction #' + auc.auction_id.slice(0,10) + '</span>';
        ahtml += '<span style="color:var(--dim);font-size:0.72rem;margin-left:10px;">Borrower: <span class="addr">' + shortAddr(auc.borrower) + '</span></span></div>';
        ahtml += '<div style="font-size:0.75rem;">Debt: <strong class="danger">' + fmtUsd(auc.debt_usd) + '</strong> &nbsp;|&nbsp; Collateral: <strong>' + fmtUsd(auc.collateral_usd) + '</strong></div>';
        ahtml += '</div>';

        // ── Phase 0: Broadcast ──
        var bc = ph.broadcast;
        ahtml += '<div style="margin-bottom:14px;">';
        ahtml += '<div style="font-size:0.75rem;font-weight:700;color:var(--blue);margin-bottom:6px;">Phase 0 — Broadcast to Liquidator Network</div>';
        ahtml += '<div style="font-size:0.7rem;color:var(--muted);margin-bottom:6px;">Auctioneer broadcasts liquidation opportunity to all registered eligible liquidators:</div>';
        ahtml += '<pre class="payload-pre" style="max-height:180px;">' + JSON.stringify(bc, null, 2) + '</pre>';
        ahtml += '</div>';

        // ── Phase 1: Eligibility ──
        ahtml += '<div style="margin-bottom:14px;">';
        ahtml += '<div style="font-size:0.75rem;font-weight:700;color:var(--blue);margin-bottom:6px;">Phase 1 — Eligibility Evaluation</div>';
        ahtml += '<div style="font-size:0.7rem;color:var(--muted);margin-bottom:6px;">Each liquidator checked against KYC status, asset whitelist, and capital requirements:</div>';
        ahtml += '<table><thead><tr><th>Liquidator</th><th style="text-align:center">KYC</th><th style="text-align:center">Whitelisted</th><th style="text-align:center">Capital OK</th><th style="text-align:right">Capital</th><th style="text-align:right">Min Required</th><th style="text-align:center">Can Participate</th><th>Reason</th></tr></thead><tbody>';
        for (var ei = 0; ei < ph.eligibility.length; ei++) {
          var ec = ph.eligibility[ei];
          var rpBg = ec.can_participate ? 'background:rgba(34,197,94,0.04);' : '';
          ahtml += '<tr style="' + rpBg + '">';
          ahtml += '<td><strong>' + ec.name + '</strong></td>';
          ahtml += '<td style="text-align:center" class="' + (ec.kyc_verified ? 'ok' : 'danger') + '">' + (ec.kyc_verified ? '✓' : '✗') + '</td>';
          ahtml += '<td style="text-align:center" class="' + (ec.asset_whitelisted ? 'ok' : 'danger') + '">' + (ec.asset_whitelisted ? '✓' : '✗') + '</td>';
          ahtml += '<td style="text-align:center" class="' + (ec.capital_sufficient ? 'ok' : 'warn') + '">' + (ec.capital_sufficient ? '✓' : '⚠') + '</td>';
          ahtml += '<td style="text-align:right">' + fmtUsd(ec.capital_available_usd) + '</td>';
          ahtml += '<td style="text-align:right">' + fmtUsd(ec.min_capital_required_usd) + '</td>';
          ahtml += '<td style="text-align:center;font-weight:700" class="' + (ec.can_participate ? 'ok' : 'danger') + '">' + (ec.can_participate ? '✓ YES' : '✗ NO') + '</td>';
          ahtml += '<td style="font-size:0.68rem;color:var(--dim);">' + ec.reason + '</td>';
          ahtml += '</tr>';
        }
        ahtml += '</tbody></table>';
        ahtml += '</div>';

        // ── Phase 2: Commit ──
        ahtml += '<div style="margin-bottom:14px;">';
        ahtml += '<div style="font-size:0.75rem;font-weight:700;color:var(--blue);margin-bottom:6px;">Phase 2 — Sealed Bid Commitments</div>';
        ahtml += '<div style="font-size:0.7rem;color:var(--muted);margin-bottom:6px;">Eligible liquidators submit cryptographic commitments (hash of bid + nonce). Bid values are hidden:</div>';
        ahtml += '<table><thead><tr><th>Liquidator</th><th>Commitment Hash</th><th>Timestamp</th></tr></thead><tbody>';
        for (var ci = 0; ci < ph.commit.length; ci++) {
          var cm = ph.commit[ci];
          ahtml += '<tr><td><strong>' + cm.liquidator_name + '</strong></td>';
          ahtml += '<td style="font-family:var(--mono);font-size:0.65rem;color:var(--accent);">' + cm.commitment_hash + '</td>';
          ahtml += '<td style="font-size:0.68rem;color:var(--dim);">' + cm.timestamp + '</td></tr>';
        }
        ahtml += '</tbody></table>';
        ahtml += '</div>';

        // ── Phase 3: Reveal ──
        ahtml += '<div style="margin-bottom:14px;">';
        ahtml += '<div style="font-size:0.75rem;font-weight:700;color:var(--blue);margin-bottom:6px;">Phase 3 — Bid Reveal &amp; Verification</div>';
        ahtml += '<div style="font-size:0.7rem;color:var(--muted);margin-bottom:6px;">Liquidators reveal their bids with nonces. Commitment hashes are verified against revealed values:</div>';
        for (var ri = 0; ri < ph.reveal.length; ri++) {
          var rv = ph.reveal[ri];
          ahtml += '<div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:8px;">';
          ahtml += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">';
          ahtml += '<strong>' + rv.liquidator_name + '</strong>';
          ahtml += '<span class="' + (rv.valid ? 'ok' : 'danger') + '" style="font-size:0.72rem;font-weight:700;">' + (rv.valid ? '✓ VERIFIED' : '✗ INVALID') + '</span>';
          ahtml += '</div>';
          ahtml += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:6px;font-size:0.7rem;">';
          ahtml += '<div><span style="color:var(--muted)">Discount:</span><br><strong>' + rv.discount_bps + ' bps (' + rv.discount_pct + '%)</strong></div>';
          ahtml += '<div><span style="color:var(--muted)">Max Repay:</span><br>' + fmtUsd(rv.max_repay_usd) + '</div>';
          ahtml += '<div><span style="color:var(--muted)">Nonce:</span><br><span style="font-family:var(--mono);font-size:0.62rem;">' + rv.nonce + '</span></div>';
          ahtml += '<div><span style="color:var(--muted)">Est. Profit:</span><br>' + fmtUsd(rv.evaluation.estimated_profit_usd) + '</div>';
          ahtml += '<div><span style="color:var(--muted)">Capital Allocated:</span><br>' + rv.evaluation.capital_allocated_pct + '% of available</div>';
          ahtml += '<div><span style="color:var(--muted)">Est. Hold Period:</span><br>' + rv.evaluation.estimated_hold_period_days + ' days</div>';
          ahtml += '</div>';
          ahtml += '<div style="margin-top:6px;font-size:0.62rem;color:var(--dim);font-family:var(--mono);">commit: ' + rv.commitment_hash.slice(0,32) + '...<br>verify: ' + rv.verification_hash.slice(0,32) + '...</div>';
          ahtml += '</div>';
        }
        ahtml += '</div>';

        // ── Phase 4: Ranking & Winner ──
        ahtml += '<div style="margin-bottom:14px;">';
        ahtml += '<div style="font-size:0.75rem;font-weight:700;color:var(--blue);margin-bottom:6px;">Phase 4 — Ranking &amp; Winner Selection</div>';
        ahtml += '<div style="font-size:0.7rem;color:var(--muted);margin-bottom:6px;">Bids ranked by discount ascending (lowest = best for borrower). Ties broken by max repay:</div>';
        ahtml += '<table><thead><tr><th>Rank</th><th>Liquidator</th><th style="text-align:right">Discount</th><th style="text-align:right">Max Repay</th><th style="text-align:right">Est. Profit</th><th style="text-align:center">Result</th></tr></thead><tbody>';
        for (var rki = 0; rki < ph.ranking.length; rki++) {
          var rk = ph.ranking[rki];
          var isW = rk.is_winner;
          var rwBg = isW ? 'background:rgba(34,197,94,0.1);' : '';
          var wBadge = isW ? ' <span style="background:var(--success);color:#fff;font-size:0.6rem;padding:2px 8px;border-radius:4px;margin-left:6px;">WINNER</span>' : '';
          ahtml += '<tr style="' + rwBg + '">';
          ahtml += '<td style="font-weight:700;color:' + (isW ? 'var(--success)' : 'var(--dim)') + ';">#' + rk.rank + '</td>';
          ahtml += '<td><strong>' + rk.liquidator_name + '</strong>' + wBadge + '</td>';
          ahtml += '<td style="text-align:right">' + rk.discount_bps + ' bps (' + rk.discount_pct + '%)</td>';
          ahtml += '<td style="text-align:right">' + fmtUsd(rk.max_repay_usd) + '</td>';
          ahtml += '<td style="text-align:right">' + fmtUsd(rk.evaluation.estimated_profit_usd) + '</td>';
          ahtml += '<td style="text-align:center">' + (isW ? '<span class="ok" style="font-weight:700;">Awarded</span>' : '<span style="color:var(--dim);">Outbid</span>') + '</td>';
          ahtml += '</tr>';
        }
        ahtml += '</tbody></table>';
        ahtml += '</div>';

        // ── Phase 5: Execution ──
        if (auc.execution) {
          var ex = auc.execution;
          ahtml += '<div style="margin-bottom:6px;">';
          ahtml += '<div style="font-size:0.75rem;font-weight:700;color:var(--success);margin-bottom:6px;">Phase 5 — Morpho liquidate() Execution Payload</div>';
          ahtml += '<div style="font-size:0.7rem;color:var(--muted);margin-bottom:8px;">Winner receives the following calldata to submit on-chain. Single atomic transaction on Morpho Blue:</div>';
          ahtml += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px;font-size:0.72rem;margin-bottom:10px;">';
          ahtml += '<div><span style="color:var(--muted)">Target Contract:</span><br><span style="font-family:var(--mono);font-size:0.68rem;">' + ex.to + '</span></div>';
          ahtml += '<div><span style="color:var(--muted)">Function:</span><br><span style="font-family:var(--mono);font-size:0.65rem;">liquidate(MarketParams,address,uint256,uint256,bytes)</span></div>';
          ahtml += '<div><span style="color:var(--muted)">Selector:</span><br><span style="font-family:var(--mono);">' + ex.selector + '</span></div>';
          ahtml += '<div><span style="color:var(--muted)">Winner / Liquidator:</span><br>' + win.name + '<br><span style="font-family:var(--mono);font-size:0.65rem;">' + win.address + '</span></div>';
          ahtml += '<div><span style="color:var(--muted)">Borrower:</span><br><span style="font-family:var(--mono);font-size:0.65rem;">' + ex.params.borrower + '</span></div>';
          ahtml += '<div><span style="color:var(--muted)">Est. Repay (debt covered):</span><br><strong>' + fmtUsd(ex.estimated_repay_usd) + '</strong></div>';
          ahtml += '<div><span style="color:var(--muted)">Est. Seize (incl LIF bonus):</span><br><strong>' + fmtUsd(ex.estimated_seize_usd) + '</strong></div>';
          ahtml += '<div><span style="color:var(--muted)">Liquidation Incentive Factor:</span><br><strong>' + fmt(ex.liquidation_incentive_factor, 4) + 'x</strong></div>';
          ahtml += '<div><span style="color:var(--muted)">Winner Discount:</span><br>' + ex.discount_bps + ' bps</div>';
          ahtml += '<div><span style="color:var(--muted)">Close Factor:</span><br>100% (full debt)</div>';
          ahtml += '<div><span style="color:var(--muted)">Note:</span><br><span style="font-size:0.68rem;">' + ex.note + '</span></div>';
          ahtml += '</div>';
          ahtml += '<div style="font-size:0.7rem;font-weight:600;color:var(--muted);margin-bottom:4px;">Full Transaction Params:</div>';
          ahtml += '<pre class="payload-pre" style="max-height:250px;">' + JSON.stringify(ex.params, null, 2) + '</pre>';
          ahtml += '</div>';
        }

        ahtml += '</div>';  // close auction card
      }
      $('auctionResults').innerHTML = ahtml;
    }
  } catch(e) { console.error('Step 6 error:', e); }
}

function rerun() {
  const sMin = parseFloat($('shockMin').value) || -8;
  const sMax = parseFloat($('shockMax').value) || 8;
  fetchData(sMin, sMax);
}

// Initial load
fetchData(INITIAL_SHOCK_MIN, INITIAL_SHOCK_MAX);
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP SERVER
# ═══════════════════════════════════════════════════════════════════════════════

class DashboardHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler: serves the dashboard HTML and the API endpoint."""

    market_id = DEFAULT_MARKET_ID
    chain_id = DEFAULT_CHAIN_ID
    shock_min = -8.0
    shock_max = 8.0

    def log_message(self, format, *args):
        # Quiet logging
        pass

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/" or parsed.path == "":
            # Serve dashboard HTML with initial config injected
            html = DASHBOARD_HTML.replace(
                "INITIAL_SHOCK_MIN", str(self.shock_min)
            ).replace(
                "INITIAL_SHOCK_MAX", str(self.shock_max)
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

        elif parsed.path == "/api/run":
            qs = parse_qs(parsed.query)
            s_min = float(qs.get("shock_min", [self.shock_min])[0])
            s_max = float(qs.get("shock_max", [self.shock_max])[0])

            try:
                result = run_pipeline(self.market_id, self.chain_id, s_min, s_max)
            except Exception as e:
                result = {"error": str(e)}

            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()


def main():
    parser = argparse.ArgumentParser(description="Chronicle RWA Engine Dashboard")
    parser.add_argument("--market", "-m", default=DEFAULT_MARKET_ID)
    parser.add_argument("--chain", "-c", type=int, default=DEFAULT_CHAIN_ID)
    parser.add_argument("--port", "-p", type=int, default=8050)
    parser.add_argument("--shock-min", type=float, default=-8.0)
    parser.add_argument("--shock-max", type=float, default=8.0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    DashboardHandler.market_id = args.market
    DashboardHandler.chain_id = args.chain
    DashboardHandler.shock_min = args.shock_min
    DashboardHandler.shock_max = args.shock_max

    server = HTTPServer(("0.0.0.0", args.port), DashboardHandler)

    url = f"http://127.0.0.1:{args.port}"
    print(f"\n  Chronicle RWA Engine Dashboard")
    print(f"  ──────────────────────────────")
    print(f"  Market: {args.market[:18]}…{args.market[-8:]}")
    print(f"  Chain:  {args.chain} ({CHAIN_NAMES.get(args.chain, '?')})")
    print(f"  URL:    {url}")
    print(f"  Press Ctrl+C to stop.\n")

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
