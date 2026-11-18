"""
engine.py
=========
Main orchestrator for the Chronicle RWA Engine.
Ties together all 4 modules into a single pipeline.
"""

import json
import sys
from dataclasses import asdict

from .morpho_client import MorphoClient
from .models import MarketParams, MarketState
from .analysis import LiquidationAnalyzer, ChroniclePokeSimulator, LiquidationFlagger
from .chart import generate_chart


# ═══════════════════════════════════════════════════════════════════════════════
# DEFAULTS
# ═══════════════════════════════════════════════════════════════════════════════

# Your target market — the one you specified
DEFAULT_MARKET_ID = "0x4440abd9eff38ebf76b8f64c074682acef8c89e77555075478a7582883206604"
DEFAULT_CHAIN_ID = 8453  # Base

# Simulated price shock range (± %). In production → Chronicle Agent Network.
PRICE_SHOCK_RANGE_PCT = (-8.0, 8.0)


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_lltv(raw) -> float:
    """Morpho returns LLTV as a raw uint256 string (18 decimals)."""
    if isinstance(raw, str) and len(raw) > 6:
        return int(raw) / 1e18
    return float(raw)


def _safe_float(val, default=0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

def run(
    market_id: str = DEFAULT_MARKET_ID,
    chain_id: int = DEFAULT_CHAIN_ID,
    output_dir: str = ".",
    shock_range: tuple = PRICE_SHOCK_RANGE_PCT,
):
    """Execute the full Chronicle RWA Engine pipeline.

    Returns the SettlementPayload (also written to JSON).
    """

    sep = "═" * 72
    print(f"\n{sep}")
    print("  CHRONICLE RWA ENGINE v0.2")
    print("  Proactive Liquidation Detection & Settlement Infrastructure")
    print(f"{sep}\n")

    client = MorphoClient()

    # ── MODULE 1: Market Data Retrieval ────────────────────────────────────
    print("▸ MODULE 1 — Market Data Retrieval Layer")
    print(f"  Market ID: {market_id[:18]}…{market_id[-8:]}")

    # Auto-detect chain if needed
    if chain_id is None:
        print(f"  Chain ID:  auto-detecting...")
        from .morpho_client import CHAIN_NAMES
        chain_id = client.detect_chain(market_id)
        if chain_id is None:
            print("  ✗ Market not found on any supported chain.")
            print("    Supported chains: " + ", ".join(
                f"{v} ({k})" for k, v in CHAIN_NAMES.items()
            ))
            return None
        print(f"  Chain ID:  {chain_id} ({CHAIN_NAMES.get(chain_id, 'unknown')})")
    else:
        print(f"  Chain ID:  {chain_id}")

    print()

    raw = client.get_market(market_id, chain_id)
    m = raw.get("marketById")

    if m is None:
        # Try auto-detect before giving up
        from .morpho_client import CHAIN_NAMES
        print(f"  ⚠ Market not found on chain {chain_id}. Auto-detecting...")
        detected = client.detect_chain(market_id)
        if detected and detected != chain_id:
            chain_id = detected
            print(f"  ✓ Found on chain {chain_id} ({CHAIN_NAMES.get(chain_id, 'unknown')})")
            raw = client.get_market(market_id, chain_id)
            m = raw.get("marketById")

    if m is None:
        print("  ✗ Market not found. Verify the market ID.")
        print("    Tip: check https://app.morpho.org/markets for active markets.")
        return None

    # Parse parameters
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
        market_id=m["marketId"],
        chain_id=chain_id,
        lltv=lltv,
        loan_asset_symbol=loan["symbol"],
        loan_asset_address=loan["address"],
        loan_asset_decimals=int(loan["decimals"]),
        collateral_asset_symbol=coll["symbol"],
        collateral_asset_address=coll["address"],
        collateral_asset_decimals=int(coll["decimals"]),
        oracle_address=oracle_info.get("address", "N/A"),
        oracle_type=oracle_info.get("type"),
        irm_address=m.get("irmAddress"),
        collateral_price_usd=collateral_price,
        loan_price_usd=loan_price,
    )

    # Parse state — Morpho API returns raw uint256 for asset amounts
    s = m["state"]
    loan_decimals = params.loan_asset_decimals
    coll_decimals = params.collateral_asset_decimals

    def scale_loan(raw):
        return _safe_float(raw) / (10 ** loan_decimals)

    def scale_coll(raw):
        return _safe_float(raw) / (10 ** coll_decimals)

    raw_supply_usd = _safe_float(s.get("supplyAssetsUsd"))
    if raw_supply_usd > 1e12 and loan_price > 0:
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

    # Print market summary
    print(f"  ✓ Market: {params.collateral_asset_symbol} / {params.loan_asset_symbol}")
    print(f"    LLTV:            {lltv:.6f} ({lltv*100:.2f}%)")
    print(f"    Oracle:          {params.oracle_address}")
    print(f"    Oracle type:     {params.oracle_type or 'unknown'}")
    print(f"    IRM:             {params.irm_address or 'N/A'}")
    print(f"    Supply:          ${state.supply_assets_usd:>14,.2f}")
    print(f"    Borrow:          ${state.borrow_assets_usd:>14,.2f}")
    print(f"    Collateral:      ${state.collateral_assets_usd:>14,.2f}")
    print(f"    Liquidity:       ${state.liquidity_assets_usd:>14,.2f}")
    print(f"    Utilization:     {state.utilization*100:.2f}%")
    print(f"    Borrow APY:      {state.borrow_apy*100:.4f}%")
    print(f"    Supply APY:      {state.supply_apy*100:.4f}%")
    print(f"    Fee:             {state.fee*100:.2f}%")

    # Vaults
    vaults = m.get("supplyingVaults") or []
    if vaults:
        print(f"\n    Supplying Vaults ({len(vaults)}):")
        for v in vaults[:8]:
            print(f"      • {v['name']} ({v['symbol']})")

    # Warnings
    warnings = m.get("warnings") or []
    if warnings:
        print(f"\n    ⚠ Market Warnings:")
        for w in warnings:
            print(f"      [{w.get('level', '?')}] {w.get('type', '?')}")

    # Asset prices
    print(f"\n    Asset Prices (from Morpho API):")
    print(f"      {params.collateral_asset_symbol}: ${collateral_price:,.6f}")
    print(f"      {params.loan_asset_symbol}: ${loan_price:,.6f}")

    # Effective price = collateral/loan ratio
    if collateral_price > 0 and loan_price > 0:
        effective_price = collateral_price / loan_price
    elif collateral_price > 0:
        effective_price = collateral_price
    else:
        effective_price = 1.0
    print(f"      Effective ratio: {effective_price:,.8f}")

    # Fetch positions
    print(f"\n  Fetching positions...")
    raw_positions = client.get_market_positions(market_id)
    print(f"    Retrieved: {len(raw_positions)} positions")

    # ── MODULE 2: Liquidation Risk Analysis ────────────────────────────────
    print(f"\n▸ MODULE 2 — Liquidation Risk Analysis")

    analyzer = LiquidationAnalyzer(params)
    positions = analyzer.analyze_positions(raw_positions)
    borrowers = [p for p in positions if p.is_borrower]
    print(f"  Active borrowers: {len(borrowers)}")

    if borrowers:
        ltv_values = [p.current_ltv for p in borrowers]
        distances = [p.distance_pct for p in borrowers]
        print(f"  LTV range:         {min(ltv_values):.6f} — {max(ltv_values):.6f}")
        print(f"  Distance to LLTV:  {min(distances):+.2f}% — {max(distances):+.2f}%")

        underwater = [p for p in borrowers if p.distance_pct <= 0]
        if underwater:
            print(f"  ⚠ {len(underwater)} position(s) already at or past LLTV!")
            uw_debt = sum(p.borrow_assets_usd for p in underwater)
            print(f"    Total underwater debt: ${uw_debt:,.2f}")

        # Top riskiest
        by_risk = sorted(borrowers, key=lambda p: p.distance_pct)
        print(f"\n  Top riskiest positions:")
        header = f"  {'Address':<44} {'LTV':>10} {'Dist%':>9} {'Debt USD':>15} {'Collat USD':>15}"
        print(header)
        print(f"  {'─'*44} {'─'*10} {'─'*9} {'─'*15} {'─'*15}")
        for p in by_risk[:10]:
            addr = p.user_address[:8] + "…" + p.user_address[-6:]
            print(
                f"  {addr:<44} {p.current_ltv:>10.6f} {p.distance_pct:>+8.2f}% "
                f"${p.borrow_assets_usd:>14,.2f} ${p.collateral_usd:>14,.2f}"
            )

    buckets = analyzer.build_liquidation_buckets(positions)

    # ── MODULE 3: Next Price Simulation ────────────────────────────────────
    print(f"\n▸ MODULE 3 — Next Price Simulation (Chronicle Agent Network Mock)")

    simulator = ChroniclePokeSimulator(effective_price, shock_range)
    next_price, pct_change = simulator.generate_next_poke()
    print(f"  Current oracle ratio: {effective_price:,.8f}")
    print(f"  Simulated next poke:  {next_price:,.8f} ({pct_change:+.4f}%)")

    # ── MODULE 4: Liquidation Flagging ─────────────────────────────────────
    print(f"\n▸ MODULE 4 — Liquidation Flagging & Payload Generation")

    flagger = LiquidationFlagger(params, positions, effective_price)
    payload = flagger.evaluate(next_price, pct_change)

    if payload.liquidation_triggered:
        print(f"  🚨 LIQUIDATION TRIGGERED = True")
        print(f"     Positions at risk:  {payload.total_positions_at_risk}")
        print(f"     Collateral at risk: ${payload.total_collateral_at_risk_usd:,.2f}")
        print(f"     Debt at risk:       ${payload.total_debt_at_risk_usd:,.2f}")
        print(f"\n     Affected positions:")
        for lp in payload.positions[:10]:
            print(
                f"       {lp['user'][:12]}… "
                f"LTV {lp['current_ltv']:.4f} → {lp['new_ltv']:.4f}  "
                f"debt ${lp['debt_usd']:,.2f}  shortfall ${lp['shortfall_usd']:,.2f}"
            )
    else:
        print(f"  ✅ LIQUIDATION TRIGGERED = False")
        print(f"     No positions would be liquidated at the simulated price.")

    # ── Generate outputs ───────────────────────────────────────────────────
    print(f"\n▸ Generating outputs...")

    market_state_summary = {
        "supply_usd": state.supply_assets_usd,
        "borrow_usd": state.borrow_assets_usd,
        "collateral_usd": state.collateral_assets_usd,
        "utilization": state.utilization,
    }

    chart_path = generate_chart(
        buckets=buckets,
        market_params=params,
        effective_price=effective_price,
        next_price=next_price,
        pct_change=pct_change,
        payload=payload,
        borrower_count=len(borrowers),
        market_state_summary=market_state_summary,
        output_path=f"{output_dir}/chronicle_rwa_engine_output.html",
    )
    print(f"  ✓ Chart:   {chart_path}")

    payload_path = f"{output_dir}/settlement_payload.json"
    with open(payload_path, "w") as f:
        json.dump(asdict(payload), f, indent=2)
    print(f"  ✓ Payload: {payload_path}")

    print(f"\n{sep}")
    print("  PIPELINE COMPLETE")
    print(f"{sep}\n")

    return payload
