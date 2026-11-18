# Chronicle RWA Engine v0.2

**Proactive liquidation detection and settlement infrastructure for tokenized real-world asset positions in Morpho lending markets.**

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  MODULE 1: Market Data Retrieval Layer                      │
│  Morpho GraphQL API → market params, state, all positions   │
└──────────────────────────┬──────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  MODULE 2: Liquidation Risk Analysis                        │
│  For each borrower:                                         │
│    • Current LTV = debt_usd / collateral_usd                │
│    • Distance from LLTV (absolute + as % price move)        │
│    • Liquidation price = price × (current_ltv / lltv)       │
│  Bucket positions by price drop % → chart                   │
└──────────────────────────┬──────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  MODULE 3: Next Price Simulation                            │
│  Simulates the next Chronicle "poke" value                  │
│  (random in dev; Chronicle Agent Network in production)     │
└──────────────────────────┬──────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  MODULE 4: Liquidation Flagging & Payload                   │
│  Recompute all LTVs at new price                            │
│  If any cross LLTV → flag = True, generate payload          │
│  Payload: positions, debt, collateral, shortfall             │
└─────────────────────────────────────────────────────────────┘
```

## Quickstart

```bash
# Install dependencies
pip install -r chronicle_rwa_engine/requirements.txt

# Run against the default market (0x4440abd9…)
python -m chronicle_rwa_engine

# Run against a specific market
python -m chronicle_rwa_engine --market 0x4440abd9eff38ebf76b8f64c074682acef8c89e77555075478a7582883206604 --chain 1

# Custom price shock range (simulate a -5% to +3% move)
python -m chronicle_rwa_engine --shock-min -5 --shock-max 3

# Output to a specific directory
python -m chronicle_rwa_engine --output ./results
```

## Outputs

1. **`chronicle_rwa_engine_output.html`** — Interactive Plotly dashboard:
   - Market parameters and state
   - Liquidation volume distribution chart (bar + cumulative)
   - Simulated next poke price marker
   - Full settlement payload

2. **`settlement_payload.json`** — Machine-readable payload:
   ```json
   {
     "timestamp": "2026-05-06T...",
     "market_id": "0x4440abd9...",
     "liquidation_triggered": true,
     "total_positions_at_risk": 3,
     "total_debt_at_risk_usd": 1250000.00,
     "positions": [
       {
         "user": "0xabc...",
         "current_ltv": 0.912,
         "new_ltv": 0.965,
         "debt_usd": 500000.00,
         "shortfall_usd": 12500.00
       }
     ]
   }
   ```

## API Reference

The engine uses the Morpho GraphQL API (`https://api.morpho.org/graphql`).  
Full docs: https://docs.morpho.org/tools/offchain/api/get-started/

### Queries Used

| Query | Purpose |
|-------|---------|
| `marketById(marketId, chainId)` | Full market params, oracle, state, vaults |
| `marketPositions(where: {marketUniqueKey_in})` | All positions, paginated |
| `assetByAddress(address, chainId)` | Asset USD prices |
| `transactions(where: {type_in: [MarketLiquidation]})` | Historical liquidations |

## Target Market

Default: `0x4440abd9eff38ebf76b8f64c074682acef8c89e77555075478a7582883206604` on Ethereum (chain 1).

## Python API

```python
from chronicle_rwa_engine import run, MorphoClient

# Full pipeline
payload = run(market_id="0x4440abd9...", chain_id=1)

# Just fetch market data
client = MorphoClient()
market = client.get_market("0x4440abd9...", chain_id=1)
positions = client.get_market_positions("0x4440abd9...")
```

## Production Integration

In production, Module 3 (price simulation) would be replaced with the actual
Chronicle Agent Network attestation pipeline:

1. Chronicle validators independently fetch the asset price
2. Each validator signs with Schnorr multi-sig
3. The aggregated attested value is the next "poke" to the on-chain oracle
4. This value feeds directly into Module 4 for real-time liquidation detection

The settlement payload output is designed to be consumed by an Auctioneer. 
