"""
CLI entry point: python -m chronicle_rwa_engine

Usage:
    python -m chronicle_rwa_engine
    python -m chronicle_rwa_engine --market 0x4440abd9eff38ebf76b8f64c074682acef8c89e77555075478a7582883206604
    python -m chronicle_rwa_engine --market 0x... --chain 8453 --output ./results
    python -m chronicle_rwa_engine --shock-min -5 --shock-max 3
"""

import argparse
import sys

from .engine import run, DEFAULT_MARKET_ID, DEFAULT_CHAIN_ID


def main():
    parser = argparse.ArgumentParser(
        description="Chronicle RWA Engine — Proactive Liquidation Detection"
    )
    parser.add_argument(
        "--market", "-m",
        default=DEFAULT_MARKET_ID,
        help=f"Morpho market ID (default: {DEFAULT_MARKET_ID[:20]}…)",
    )
    parser.add_argument(
        "--chain", "-c",
        type=int,
        default=DEFAULT_CHAIN_ID,
        help=f"Chain ID (default: {DEFAULT_CHAIN_ID})",
    )
    parser.add_argument(
        "--output", "-o",
        default=".",
        help="Output directory for chart and payload (default: current dir)",
    )
    parser.add_argument(
        "--shock-min",
        type=float,
        default=-8.0,
        help="Min price shock %% for simulation (default: -8.0)",
    )
    parser.add_argument(
        "--shock-max",
        type=float,
        default=8.0,
        help="Max price shock %% for simulation (default: 8.0)",
    )

    args = parser.parse_args()

    payload = run(
        market_id=args.market,
        chain_id=args.chain,
        output_dir=args.output,
        shock_range=(args.shock_min, args.shock_max),
    )

    if payload is None:
        sys.exit(1)

    sys.exit(0 if not payload.liquidation_triggered else 2)


if __name__ == "__main__":
    main()
