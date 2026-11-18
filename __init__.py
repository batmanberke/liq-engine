"""
Chronicle RWA Engine
====================
Proactive liquidation detection and settlement infrastructure for
tokenized real-world asset positions in Morpho lending markets.

Built on Chronicle Protocol's Proof of Assets (PoA) oracle architecture.
Inspired by RedStone Settle's auction-based liquidation model.

Usage:
    from chronicle_rwa_engine import run
    payload = run(market_id="0x4440abd9...", chain_id=8453)

Or from CLI:
    python -m chronicle_rwa_engine
    python -m chronicle_rwa_engine.dashboard          # web dashboard
"""

from .engine import run
from .morpho_client import MorphoClient
from .models import (
    MarketParams, MarketState, Position,
    LiquidationBucket, SettlementPayload,
)
from .analysis import (
    LiquidationAnalyzer, ChroniclePokeSimulator, LiquidationFlagger,
)

__version__ = "0.2.0"
__all__ = [
    "run",
    "MorphoClient",
    "MarketParams", "MarketState", "Position",
    "LiquidationBucket", "SettlementPayload",
    "LiquidationAnalyzer", "ChroniclePokeSimulator", "LiquidationFlagger",
]
