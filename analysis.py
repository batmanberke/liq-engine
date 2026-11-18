"""
analysis.py
===========
Modules 2–4 of the Chronicle RWA Engine:
  2. Liquidation Risk Analysis   — LLTV distance, liquidation prices, bucketing
  3. Next Price Simulation       — Chronicle Agent Network poke mock
  4. Liquidation Flagging        — evaluate positions & generate settlement payload
"""

import random
from datetime import datetime, timezone
from .models import (
    MarketParams, Position, LiquidationBucket, SettlementPayload
)


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 2 — LIQUIDATION RISK ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

class LiquidationAnalyzer:
    """Computes LLTV distances, liquidation prices, and buckets positions
    by the price drop % at which they become liquidatable.

    Morpho health model:
        LTV = borrowValue / collateralValue
        Position is liquidatable when LTV >= LLTV
        Health Factor = (collateralValue * LLTV) / borrowValue

    The oracle price determines collateralValue relative to loanValue.
    A drop in collateral/loan price ratio increases LTV.
    """

    def __init__(self, market_params: MarketParams):
        self.params = market_params
        self.lltv = market_params.lltv

    def analyze_positions(self, raw_positions: list[dict]) -> list[Position]:
        """Enrich raw API positions with liquidation risk metrics.

        For each borrower, we compute:
        - current_ltv:           borrowUsd / collateralUsd
        - distance_from_lltv:    lltv - current_ltv (positive = healthy)
        - liquidation_price_ratio: current_ltv / lltv
            → the fraction of current price at which liquidation triggers
        - distance_pct:          (1 - liquidation_price_ratio) * 100
            → price must drop this % to trigger liquidation
            → negative means already liquidatable
        """
        coll_decimals = self.params.collateral_asset_decimals
        loan_decimals = self.params.loan_asset_decimals
        coll_price = self.params.collateral_price_usd
        loan_price = self.params.loan_price_usd

        positions = []
        for raw in raw_positions:
            state = raw["state"]

            # Raw amounts from API — need decimal scaling
            raw_borrow = float(state.get("borrowAssets") or 0)
            raw_collateral = float(state.get("collateral") or 0)
            raw_supply = float(state.get("supplyAssets") or 0)

            # USD values from API
            raw_borrow_usd = float(state.get("borrowAssetsUsd") or 0)
            raw_collateral_usd = float(state.get("collateralUsd") or 0)
            raw_supply_usd = float(state.get("supplyAssetsUsd") or 0)

            # Scale raw amounts to human-readable
            borrow_scaled = raw_borrow / (10 ** loan_decimals) if raw_borrow > 1e10 else raw_borrow
            collateral_scaled = raw_collateral / (10 ** coll_decimals) if raw_collateral > 1e10 else raw_collateral
            supply_scaled = raw_supply / (10 ** loan_decimals) if raw_supply > 1e10 else raw_supply

            # Determine if USD values need scaling
            # If raw USD >> scaled * price, they're unscaled
            if raw_borrow_usd > 1e10 and loan_price > 0:
                borrow_usd = borrow_scaled * loan_price
            else:
                borrow_usd = raw_borrow_usd

            if raw_collateral_usd > 1e10 and coll_price > 0:
                collateral_usd = collateral_scaled * coll_price
            else:
                collateral_usd = raw_collateral_usd

            if raw_supply_usd > 1e10 and loan_price > 0:
                supply_usd = supply_scaled * loan_price
            else:
                supply_usd = raw_supply_usd

            pos = Position(
                user_address=raw["user"]["address"],
                collateral=collateral_scaled,
                collateral_usd=collateral_usd,
                borrow_assets=borrow_scaled,
                borrow_assets_usd=borrow_usd,
                supply_assets=supply_scaled,
                supply_assets_usd=supply_usd,
                is_borrower=(borrow_usd > 0 and collateral_usd > 0),
            )

            if pos.is_borrower:
                pos.current_ltv = borrow_usd / collateral_usd

                # Distance from LLTV
                pos.distance_from_lltv = self.lltv - pos.current_ltv

                # Price ratio at which liquidation fires
                if self.lltv > 0:
                    pos.liquidation_price_ratio = pos.current_ltv / self.lltv
                    pos.distance_pct = (1.0 - pos.liquidation_price_ratio) * 100.0

            positions.append(pos)

        return positions

    def build_liquidation_buckets(
        self,
        positions: list[Position],
        bucket_size_pct: float = 0.5,
        max_drop_pct: float = 50.0,
    ) -> list[LiquidationBucket]:
        """Bucket borrowers by the collateral price drop % that triggers
        their liquidation. Includes a 0% bucket for already-underwater."""

        borrowers = [p for p in positions if p.is_borrower]

        n_buckets = int(max_drop_pct / bucket_size_pct) + 1
        buckets: list[LiquidationBucket] = []
        for i in range(n_buckets):
            drop_pct = -i * bucket_size_pct
            buckets.append(LiquidationBucket(price_change_pct=drop_pct))

        for pos in borrowers:
            drop = pos.distance_pct
            if drop <= 0:
                idx = 0  # already liquidatable
            else:
                idx = min(int(drop / bucket_size_pct), n_buckets - 1)

            buckets[idx].position_count += 1
            buckets[idx].total_collateral_usd += pos.collateral_usd
            buckets[idx].total_debt_usd += pos.borrow_assets_usd

        # Cumulative from current price downward
        cum_count = 0
        cum_collateral = 0.0
        cum_debt = 0.0
        for b in buckets:
            cum_count += b.position_count
            cum_collateral += b.total_collateral_usd
            cum_debt += b.total_debt_usd
            b.cumulative_count = cum_count
            b.cumulative_collateral_usd = cum_collateral
            b.cumulative_debt_usd = cum_debt

        return buckets


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 3 — NEXT PRICE SIMULATION (Chronicle Agent Network Mock)
# ═══════════════════════════════════════════════════════════════════════════════

class ChroniclePokeSimulator:
    """Simulates the next price poked on-chain by Chronicle validators.

    In production:
        - Chronicle validators independently fetch the asset price
        - Each signs the value using Schnorr multi-sig
        - The aggregated attested value is poked to the on-chain oracle
        - This is the "poke" that updates collateral/loan pricing

    Here we simulate with a random perturbation within a configurable range.
    """

    def __init__(self, current_price: float,
                 shock_range_pct: tuple[float, float] = (-8.0, 8.0)):
        self.current_price = current_price
        self.shock_range = shock_range_pct

    def generate_next_poke(self) -> tuple[float, float]:
        """Returns (next_price, pct_change)."""
        pct_change = random.uniform(*self.shock_range)
        next_price = self.current_price * (1.0 + pct_change / 100.0)
        return next_price, pct_change


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 4 — LIQUIDATION FLAGGING & PAYLOAD GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

class LiquidationFlagger:
    """Evaluates whether the next poke price triggers liquidations and
    generates a settlement payload for the Chronicle RWA Auctioneer."""

    def __init__(self, market_params: MarketParams, positions: list[Position],
                 current_price: float):
        self.params = market_params
        self.positions = positions
        self.current_price = current_price

    def evaluate(self, next_price: float, pct_change: float) -> SettlementPayload:
        """Recompute each borrower's LTV at the new price.

        The oracle price is a ratio (collateral/loan). When this drops,
        collateral value decreases → LTV rises → liquidation risk increases.
        """
        at_risk = []

        for pos in self.positions:
            if not pos.is_borrower:
                continue

            # Collateral USD scales linearly with oracle price ratio
            price_ratio = next_price / self.current_price
            new_collateral_usd = pos.collateral_usd * price_ratio

            if new_collateral_usd <= 0:
                new_ltv = float("inf")
            else:
                new_ltv = pos.borrow_assets_usd / new_collateral_usd

            if new_ltv >= self.params.lltv:
                shortfall = pos.borrow_assets_usd - (
                    new_collateral_usd * self.params.lltv
                )
                at_risk.append({
                    "user": pos.user_address,
                    "current_ltv": round(pos.current_ltv, 6),
                    "new_ltv": round(new_ltv, 6),
                    "lltv": round(self.params.lltv, 6),
                    "collateral_usd_current": round(pos.collateral_usd, 2),
                    "collateral_usd_at_new_price": round(new_collateral_usd, 2),
                    "debt_usd": round(pos.borrow_assets_usd, 2),
                    "shortfall_usd": round(max(shortfall, 0), 2),
                })

        total_collateral = sum(p["collateral_usd_current"] for p in at_risk)
        total_debt = sum(p["debt_usd"] for p in at_risk)

        return SettlementPayload(
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_id=self.params.market_id,
            chain_id=self.params.chain_id,
            collateral_asset=self.params.collateral_asset_symbol,
            loan_asset=self.params.loan_asset_symbol,
            lltv=round(self.params.lltv, 6),
            oracle_current_price=round(self.current_price, 8),
            next_poke_price=round(next_price, 8),
            price_change_pct=round(pct_change, 4),
            liquidation_triggered=len(at_risk) > 0,
            total_positions_at_risk=len(at_risk),
            total_collateral_at_risk_usd=round(total_collateral, 2),
            total_debt_at_risk_usd=round(total_debt, 2),
            positions=at_risk,
        )
