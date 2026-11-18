"""
models.py
=========
Data classes for the Chronicle RWA Engine.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MarketParams:
    """Immutable parameters defining a Morpho market."""
    market_id: str
    chain_id: int
    lltv: float                     # liquidation LTV as a ratio (e.g., 0.945)
    loan_asset_symbol: str
    loan_asset_address: str
    loan_asset_decimals: int
    collateral_asset_symbol: str
    collateral_asset_address: str
    collateral_asset_decimals: int
    oracle_address: str
    oracle_type: Optional[str] = None
    irm_address: Optional[str] = None
    collateral_price_usd: float = 0.0
    loan_price_usd: float = 0.0


@dataclass
class MarketState:
    """Live snapshot of market-level metrics."""
    supply_assets: float
    supply_assets_usd: float
    borrow_assets: float
    borrow_assets_usd: float
    collateral_assets: float
    collateral_assets_usd: float
    liquidity_assets: float
    liquidity_assets_usd: float
    utilization: float
    borrow_apy: float
    supply_apy: float
    fee: float


@dataclass
class Position:
    """A single user's position in a market."""
    user_address: str
    collateral: float               # raw collateral amount (token units)
    collateral_usd: float
    borrow_assets: float            # raw borrow amount (token units)
    borrow_assets_usd: float
    supply_assets: float
    supply_assets_usd: float
    # Computed fields
    current_ltv: float = 0.0
    distance_from_lltv: float = 0.0 # absolute ratio distance
    distance_pct: float = 0.0       # price move % needed to trigger liquidation
    liquidation_price_ratio: float = 0.0
    is_borrower: bool = False


@dataclass
class LiquidationBucket:
    """Aggregated liquidation data at a specific price level."""
    price_change_pct: float
    position_count: int = 0
    total_collateral_usd: float = 0.0
    total_debt_usd: float = 0.0
    cumulative_count: int = 0
    cumulative_collateral_usd: float = 0.0
    cumulative_debt_usd: float = 0.0


@dataclass
class SettlementPayload:
    """Output payload when liquidations are flagged.

    Analogous to what RedStone Settle's Auctioneer Module broadcasts to
    solvers — adapted here for Chronicle's architecture.
    """
    timestamp: str
    market_id: str
    chain_id: int
    collateral_asset: str
    loan_asset: str
    lltv: float
    oracle_current_price: float
    next_poke_price: float
    price_change_pct: float
    liquidation_triggered: bool
    total_positions_at_risk: int
    total_collateral_at_risk_usd: float
    total_debt_at_risk_usd: float
    positions: list = field(default_factory=list)
