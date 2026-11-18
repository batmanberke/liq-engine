"""
liquidation.py
==============
Modules 5–6 of the Chronicle RWA Engine:
  5. Liquidator Onboarding & Eligibility — KYC/whitelist registry
  6. Auction & Execution              — sealed-bid auction + Morpho liquidate() calldata
"""

import time
import random
import hashlib
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 5 — LIQUIDATOR REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════

# Morpho Blue singleton address (same on all EVM chains)
MORPHO_BLUE_ADDRESS = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

# The Morpho liquidate() function signature
LIQUIDATE_SELECTOR = "0xa4bc801b"  # liquidate(MarketParams,address,uint256,uint256,bytes)


@dataclass
class Liquidator:
    """A registered liquidator entity in the Chronicle RWA Engine."""
    name: str
    address: str                     # on-chain EOA or contract address
    entity_type: str                 # "Institutional", "Protocol", "Market Maker"
    kyc_status: str                  # "Verified", "Pending", "Rejected"
    whitelisted_assets: list         # list of collateral token symbols they can hold
    whitelisted_asset_addresses: list  # corresponding token contract addresses
    jurisdiction: str
    capital_available_usd: float     # declared available capital for liquidations
    contact: str
    registered_since: str
    liquidations_executed: int = 0
    total_volume_usd: float = 0.0


# ── Static liquidator registry ────────────────────────────────────────────
# In production this would be a database / on-chain registry.
# For now, a hardcoded list representing the onboarded liquidator network.

LIQUIDATOR_REGISTRY = [
    Liquidator(
        name="3F Labs",
        address="0x3F3F3F3F3F3F3F3F3F3F3F3F3F3F3F3F3F3F3F3F",
        entity_type="Institutional",
        kyc_status="Verified",
        whitelisted_assets=["deSPXA", "deSPX", "wstETH", "BUIDL"],
        whitelisted_asset_addresses=[
            "0x21702A7E4bFe2e8C3bA85bCFA645b96a39e5c0f0",  # deSPXA on Base
            "0x0000000000000000000000000000000000000000",
            "0x0000000000000000000000000000000000000000",
            "0x0000000000000000000000000000000000000000",
        ],
        jurisdiction="EU (MiCA Compliant)",
        capital_available_usd=5_000_000,
        contact="ops@3flabs.xyz",
        registered_since="2025-11-15",
        liquidations_executed=47,
        total_volume_usd=12_400_000,
    ),
    Liquidator(
        name="Fission",
        address="0xF15510BF15510BF15510BF15510BF15510BF1551",
        entity_type="Institutional",
        kyc_status="Verified",
        whitelisted_assets=["deSPXA", "deSPX", "USYC", "wstETH"],
        whitelisted_asset_addresses=[
            "0x21702A7E4bFe2e8C3bA85bCFA645b96a39e5c0f0",
            "0x0000000000000000000000000000000000000000",
            "0x0000000000000000000000000000000000000000",
            "0x0000000000000000000000000000000000000000",
        ],
        jurisdiction="Cayman Islands",
        capital_available_usd=8_000_000,
        contact="liquidations@fission.capital",
        registered_since="2025-09-22",
        liquidations_executed=83,
        total_volume_usd=31_700_000,
    ),
    Liquidator(
        name="Wintermute",
        address="0xW1NT3RMUT3W1NT3RMUT3W1NT3RMUT3W1NT3RMUT3",
        entity_type="Market Maker",
        kyc_status="Verified",
        whitelisted_assets=["wstETH", "cbETH", "WETH", "rETH"],
        whitelisted_asset_addresses=[],
        jurisdiction="UK (FCA Registered)",
        capital_available_usd=50_000_000,
        contact="defi@wintermute.com",
        registered_since="2024-06-01",
        liquidations_executed=12_450,
        total_volume_usd=2_100_000_000,
    ),
    Liquidator(
        name="Gauntlet",
        address="0x6A0N7L3T6A0N7L3T6A0N7L3T6A0N7L3T6A0N7L3T",
        entity_type="Protocol",
        kyc_status="Verified",
        whitelisted_assets=["WETH", "wstETH", "cbBTC", "USDC"],
        whitelisted_asset_addresses=[],
        jurisdiction="US (Registered)",
        capital_available_usd=25_000_000,
        contact="risk@gauntlet.network",
        registered_since="2024-03-10",
        liquidations_executed=5_890,
        total_volume_usd=890_000_000,
    ),
    Liquidator(
        name="Block Analitica",
        address="0xB10CANA1B10CANA1B10CANA1B10CANA1B10CANA1B",
        entity_type="Protocol",
        kyc_status="Verified",
        whitelisted_assets=["WETH", "wstETH", "DAI"],
        whitelisted_asset_addresses=[],
        jurisdiction="Slovenia (EU)",
        capital_available_usd=10_000_000,
        contact="ops@blockanalitica.com",
        registered_since="2024-08-20",
        liquidations_executed=2_100,
        total_volume_usd=340_000_000,
    ),
    Liquidator(
        name="MEV Capital",
        address="0xMEVCAP17MEVCAP17MEVCAP17MEVCAP17MEVCAP17",
        entity_type="Market Maker",
        kyc_status="Pending",
        whitelisted_assets=["wstETH", "WETH"],
        whitelisted_asset_addresses=[],
        jurisdiction="Switzerland",
        capital_available_usd=15_000_000,
        contact="ops@mevcapital.com",
        registered_since="2025-12-01",
        liquidations_executed=0,
        total_volume_usd=0,
    ),
]


def get_eligible_liquidators(collateral_symbol: str) -> tuple[list[Liquidator], list[Liquidator]]:
    """Check which liquidators are eligible for a given collateral asset.

    Eligibility requires:
    1. KYC status = "Verified"
    2. The collateral asset is in their whitelisted_assets list

    For RWA tokens (like deSPXA), this whitelist represents the
    KYC/AML verification that the liquidator is permitted to hold
    the tokenized security — a regulatory requirement that makes
    permissionless liquidation impossible for RWAs.

    Returns: (eligible, ineligible)
    """
    eligible = []
    ineligible = []
    for liq in LIQUIDATOR_REGISTRY:
        if liq.kyc_status == "Verified" and collateral_symbol in liq.whitelisted_assets:
            eligible.append(liq)
        else:
            ineligible.append(liq)
    return eligible, ineligible


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 6 — AUCTION & EXECUTION
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class AuctionBid:
    """A bid from a liquidator in the sealed-bid auction."""
    liquidator_name: str
    liquidator_address: str
    # The discount off the oracle price they're willing to accept
    # Lower discount = better for the borrower (less collateral seized)
    discount_bps: int               # basis points (e.g., 50 = 0.5%)
    max_repay_usd: float            # max they'll repay
    timestamp: str
    bid_hash: str = ""              # commitment hash for sealed-bid


@dataclass
class AuctionResult:
    """Result of the sealed-bid auction for a liquidation opportunity."""
    auction_id: str
    borrower_address: str
    debt_usd: float
    collateral_usd: float
    winning_bid: Optional[AuctionBid] = None
    all_bids: list = field(default_factory=list)
    status: str = "pending"         # pending, completed, no_bids, failed
    execution: Optional[dict] = None


def run_auction(
    liquidatable_positions: list[dict],
    eligible_liquidators: list[Liquidator],
    collateral_symbol: str,
    loan_symbol: str,
    oracle_price: float,
    lltv: float,
    market_id: str,
    chain_id: int,
    collateral_address: str,
    loan_address: str,
    oracle_address: str,
    irm_address: str,
) -> list[dict]:
    """Run sealed-bid auctions and return a detailed trace of every step.

    Returns a list of auction dicts, each containing the full process log.
    """
    results = []

    for pos in liquidatable_positions:
        auction_id = hashlib.sha256(
            f"{pos['user']}:{market_id}:{time.time()}".encode()
        ).hexdigest()[:16]

        now = datetime.now(timezone.utc)

        # ── Phase 0: Broadcast ─────────────────────────────────────────
        broadcast_payload = {
            "auction_id": auction_id,
            "type": "LIQUIDATION_OPPORTUNITY",
            "market_id": market_id,
            "chain_id": chain_id,
            "collateral_asset": collateral_symbol,
            "loan_asset": loan_symbol,
            "borrower": pos["user"],
            "debt_usd": pos["debt_usd"],
            "collateral_usd": pos.get("collateral_usd_current", 0),
            "current_ltv": pos.get("current_ltv", 0),
            "new_ltv": pos.get("new_ltv", 0),
            "lltv": round(lltv, 6),
            "oracle_price": oracle_price,
            "deadline_ms": 5000,
            "timestamp": now.isoformat(),
        }

        # ── Phase 1: Eligibility evaluation per liquidator ─────────────
        eligibility_checks = []
        for liq in LIQUIDATOR_REGISTRY:
            kyc_ok = liq.kyc_status == "Verified"
            asset_ok = collateral_symbol in liq.whitelisted_assets
            capital_ok = liq.capital_available_usd >= pos["debt_usd"] * 0.1  # need at least 10% of debt
            eligible = kyc_ok and asset_ok
            can_cover = eligible and capital_ok

            eligibility_checks.append({
                "name": liq.name,
                "address": liq.address,
                "kyc_verified": kyc_ok,
                "asset_whitelisted": asset_ok,
                "capital_sufficient": capital_ok,
                "capital_available_usd": liq.capital_available_usd,
                "min_capital_required_usd": round(pos["debt_usd"] * 0.1, 2),
                "eligible": eligible,
                "can_participate": can_cover,
                "reason": (
                    "Eligible — meets all requirements" if can_cover
                    else "Eligible but insufficient capital" if eligible and not capital_ok
                    else "KYC not verified" if not kyc_ok
                    else "Not whitelisted for " + collateral_symbol
                ),
            })

        # ── Phase 2: Commit phase (sealed bids) ───────────────────────
        commits = []
        bids_raw = []
        for liq in eligible_liquidators:
            # Simulate the liquidator's internal evaluation
            base_discount = random.randint(20, 150)
            if liq.liquidations_executed > 50:
                base_discount = max(10, base_discount - 30)
            max_repay = min(liq.capital_available_usd * 0.25, pos["debt_usd"])
            nonce = hashlib.sha256(f"{random.random()}".encode()).hexdigest()[:16]

            # The commitment is hash(address + discount + max_repay + nonce)
            bid_data = f"{liq.address}:{base_discount}:{max_repay}:{nonce}"
            commitment = hashlib.sha256(bid_data.encode()).hexdigest()

            bid_record = {
                "liquidator_name": liq.name,
                "liquidator_address": liq.address,
                "discount_bps": base_discount,
                "max_repay_usd": round(max_repay, 2),
                "nonce": nonce,
                "commitment_hash": commitment,
                "timestamp": now.isoformat(),
                # Internal evaluation trace
                "evaluation": {
                    "capital_available": liq.capital_available_usd,
                    "capital_allocated_pct": round(max_repay / liq.capital_available_usd * 100, 1) if liq.capital_available_usd > 0 else 0,
                    "experience_discount_applied": liq.liquidations_executed > 50,
                    "redemption_risk_assessment": "RWA — must hold through redemption window",
                    "estimated_hold_period_days": random.randint(1, 30),
                    "estimated_profit_usd": round(pos["debt_usd"] * base_discount / 10000, 2),
                },
            }
            bids_raw.append(bid_record)

            # Commit (only hash visible to others)
            commits.append({
                "liquidator_name": liq.name,
                "liquidator_address": liq.address,
                "commitment_hash": commitment,
                "timestamp": now.isoformat(),
            })

        # ── Phase 3: Reveal phase ──────────────────────────────────────
        reveals = []
        for bid in bids_raw:
            # Verify: re-hash and confirm matches commitment
            verify_data = f"{bid['liquidator_address']}:{bid['discount_bps']}:{bid['max_repay_usd']}:{bid['nonce']}"
            verify_hash = hashlib.sha256(verify_data.encode()).hexdigest()
            valid = verify_hash == bid["commitment_hash"]

            reveals.append({
                "liquidator_name": bid["liquidator_name"],
                "liquidator_address": bid["liquidator_address"],
                "discount_bps": bid["discount_bps"],
                "discount_pct": round(bid["discount_bps"] / 100, 2),
                "max_repay_usd": bid["max_repay_usd"],
                "nonce": bid["nonce"],
                "commitment_hash": bid["commitment_hash"],
                "verification_hash": verify_hash,
                "valid": valid,
                "evaluation": bid["evaluation"],
            })

        # ── Phase 4: Ranking & winner selection ────────────────────────
        valid_reveals = [r for r in reveals if r["valid"]]
        ranked = sorted(valid_reveals, key=lambda r: (r["discount_bps"], -r["max_repay_usd"]))

        for i, r in enumerate(ranked):
            r["rank"] = i + 1
            r["is_winner"] = (i == 0)

        winner = ranked[0] if ranked else None

        # ── Phase 5: Execution calldata ────────────────────────────────
        execution = None
        if winner:
            execution = generate_liquidation_calldata(
                market_id=market_id,
                chain_id=chain_id,
                borrower=pos["user"],
                collateral_address=collateral_address,
                loan_address=loan_address,
                oracle_address=oracle_address,
                irm_address=irm_address,
                lltv=lltv,
                seized_collateral_usd=pos.get("collateral_usd_current", 0),
                repaid_debt_usd=min(winner["max_repay_usd"], pos["debt_usd"]),
                discount_bps=winner["discount_bps"],
                liquidator_address=winner["liquidator_address"],
            )

        results.append({
            "auction_id": auction_id,
            "borrower": pos["user"],
            "debt_usd": pos["debt_usd"],
            "collateral_usd": pos.get("collateral_usd_current", 0),
            "status": "completed" if winner else "no_valid_bids",
            "phases": {
                "broadcast": broadcast_payload,
                "eligibility": eligibility_checks,
                "commit": commits,
                "reveal": reveals,
                "ranking": ranked,
            },
            "winner": {
                "name": winner["liquidator_name"],
                "address": winner["liquidator_address"],
                "discount_bps": winner["discount_bps"],
                "discount_pct": winner["discount_pct"],
                "max_repay_usd": winner["max_repay_usd"],
                "estimated_profit_usd": winner["evaluation"]["estimated_profit_usd"],
            } if winner else None,
            "execution": execution,
            "bid_count": len(valid_reveals),
            "timestamp": now.isoformat(),
        })

    return results


def generate_liquidation_calldata(
    market_id: str,
    chain_id: int,
    borrower: str,
    collateral_address: str,
    loan_address: str,
    oracle_address: str,
    irm_address: str,
    lltv: float,
    seized_collateral_usd: float,
    repaid_debt_usd: float,
    discount_bps: int,
    liquidator_address: str,
) -> dict:
    """Generate the Morpho Blue liquidate() call specification.

    Morpho's liquidate function:
        function liquidate(
            MarketParams memory marketParams,
            address borrower,
            uint256 seizedAssets,    // collateral to seize (0 if using repaidShares)
            uint256 repaidShares,    // debt shares to repay (0 if using seizedAssets)
            bytes calldata data      // callback data for flash-liquidation
        )

    The liquidator can specify EITHER seizedAssets OR repaidShares (one must be 0).
    Close factor = 100% — can liquidate the entire position in one call.

    The Liquidation Incentive Factor (LIF) gives the liquidator a bonus:
        LIF = min(maxLIF, 1 / (1 - cursor * (1 - 1/LLTV)))
        where cursor = 0.3, maxLIF = 1.15

    Returns a dict with the full call specification.
    """
    # Compute Morpho's Liquidation Incentive Factor
    cursor = 0.3
    max_lif = 1.15
    if lltv > 0 and lltv < 1:
        lif = min(max_lif, 1.0 / (1.0 - cursor * (1.0 - 1.0 / lltv)))
    else:
        lif = max_lif

    # Convert LLTV to uint256 (18 decimals)
    lltv_raw = str(int(lltv * 1e18))

    return {
        "chain_id": chain_id,
        "to": MORPHO_BLUE_ADDRESS,
        "function": "liquidate(MarketParams,address,uint256,uint256,bytes)",
        "selector": LIQUIDATE_SELECTOR,
        "params": {
            "marketParams": {
                "loanToken": loan_address,
                "collateralToken": collateral_address,
                "oracle": oracle_address,
                "irm": irm_address,
                "lltv": lltv_raw,
            },
            "borrower": borrower,
            "seizedAssets": "0",
            "repaidShares": "type(uint256).max",
            "data": "0x",
        },
        "liquidator": liquidator_address,
        "estimated_repay_usd": round(repaid_debt_usd, 2),
        "estimated_seize_usd": round(repaid_debt_usd * lif, 2),
        "liquidation_incentive_factor": round(lif, 6),
        "discount_bps": discount_bps,
        "note": "repaidShares=max will repay full debt. Liquidator receives collateral * LIF.",
    }
