"""
morpho_client.py
================
Morpho GraphQL API client — pulls real market data, positions, and asset prices.

API Reference: https://docs.morpho.org/tools/offchain/api/morpho/
Endpoint:      https://api.morpho.org/graphql
Rate limit:    5,000 requests / 5 minutes
"""

import json
import time
import requests


MORPHO_API_URL = "https://api.morpho.org/graphql"

# All chains supported by Morpho API (from docs.morpho.org)
SUPPORTED_CHAIN_IDS = [1, 8453, 42161, 10, 137, 130, 999, 747474, 143, 988]
CHAIN_NAMES = {
    1: "Ethereum", 8453: "Base", 42161: "Arbitrum", 10: "OP Mainnet",
    137: "Polygon", 130: "Unichain", 999: "HyperEVM", 747474: "Katana",
    143: "Monad", 988: "Stable",
}


class MorphoClient:
    """Thin GraphQL client for the Morpho API."""

    def __init__(self, endpoint: str = MORPHO_API_URL):
        self.endpoint = endpoint
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _query(self, query: str, variables: dict = None) -> dict:
        """Execute a GraphQL query against the Morpho API."""
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        resp = self.session.post(self.endpoint, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if "errors" in data:
            raise RuntimeError(
                f"Morpho GraphQL errors:\n{json.dumps(data['errors'], indent=2)}"
            )
        return data["data"]

    def _query_safe(self, query: str, variables: dict = None):
        """Like _query but returns None on errors instead of raising."""
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        try:
            resp = self.session.post(self.endpoint, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                return None
            return data["data"]
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 0. Auto-detect which chain a market lives on
    # ------------------------------------------------------------------
    def detect_chain(self, market_id: str) -> int | None:
        """Try all supported chains to find where this market exists.

        Returns the chain_id, or None if not found on any chain.
        """
        query = """
        query MarketById($marketId: String!, $chainId: Int!) {
          marketById(marketId: $marketId, chainId: $chainId) {
            marketId
          }
        }
        """
        for cid in SUPPORTED_CHAIN_IDS:
            result = self._query_safe(query, {"marketId": market_id, "chainId": cid})
            if result and result.get("marketById"):
                return cid
        return None

    # ------------------------------------------------------------------
    # 1. Market parameters + state by marketId
    #    Uses: marketById(marketId, chainId) as per Morpho docs
    # ------------------------------------------------------------------
    def get_market(self, market_id: str, chain_id: int = 1) -> dict:
        """Fetch full market parameters, oracle info, state, and supplying vaults.

        Args:
            market_id: The 0x-prefixed bytes32 unique market key
            chain_id:  Chain ID (1=Ethereum, 8453=Base, etc.)
        """
        query = """
        query MarketById($marketId: String!, $chainId: Int!) {
          marketById(marketId: $marketId, chainId: $chainId) {
            marketId
            lltv
            irmAddress
            creationBlockNumber
            oracle {
              address
              type
              data {
                ... on MorphoChainlinkOracleData {
                  baseFeedOne { address }
                  baseFeedTwo { address }
                  quoteFeedOne { address }
                  quoteFeedTwo { address }
                  scaleFactor
                }
                ... on MorphoChainlinkOracleV2Data {
                  baseFeedOne { address }
                  baseFeedTwo { address }
                  quoteFeedOne { address }
                  quoteFeedTwo { address }
                  scaleFactor
                }
              }
            }
            loanAsset {
              address
              symbol
              decimals
              price { usd }
            }
            collateralAsset {
              address
              symbol
              decimals
              price { usd }
            }
            state {
              supplyAssets
              supplyAssetsUsd
              borrowAssets
              borrowAssetsUsd
              collateralAssets
              collateralAssetsUsd
              liquidityAssets
              liquidityAssetsUsd
              utilization
              borrowApy
              supplyApy
              fee
            }
            reallocatableLiquidityAssets
            supplyingVaults {
              address
              name
              symbol
            }
            warnings {
              type
              level
            }
          }
        }
        """
        return self._query(query, {"marketId": market_id, "chainId": chain_id})

    # ------------------------------------------------------------------
    # 2. All borrower positions in a market (paginated)
    #    Uses: marketPositions(where: {marketUniqueKey_in: [...]})
    # ------------------------------------------------------------------
    def get_market_positions(self, market_unique_key: str,
                             first: int = 100, skip: int = 0) -> list:
        """Paginate through all positions in a market.

        Returns all positions (including supply-only) — filtering for
        borrowers is done downstream.
        """
        all_positions = []
        while True:
            query = """
            query MarketPositions($key: [String!]!, $first: Int!, $skip: Int!) {
              marketPositions(
                first: $first
                skip: $skip
                orderBy: BorrowShares
                orderDirection: Desc
                where: { marketUniqueKey_in: $key }
              ) {
                pageInfo { count countTotal }
                items {
                  user { address }
                  state {
                    collateral
                    collateralUsd
                    borrowAssets
                    borrowAssetsUsd
                    supplyAssets
                    supplyAssetsUsd
                    borrowShares
                    supplyShares
                  }
                }
              }
            }
            """
            data = self._query(query, {
                "key": [market_unique_key],
                "first": first,
                "skip": skip,
            })

            items = data["marketPositions"]["items"]
            page_info = data["marketPositions"]["pageInfo"]
            all_positions.extend(items)

            total = page_info.get("countTotal", 0)
            if len(all_positions) >= total or len(items) < first:
                break
            skip += first
            time.sleep(0.1)  # respect rate limits

        return all_positions

    # ------------------------------------------------------------------
    # 3. Asset price lookup
    #    Uses: assetByAddress(address, chainId)
    # ------------------------------------------------------------------
    def get_asset_price(self, address: str, chain_id: int = 1) -> float:
        """Get current USD price for an asset."""
        query = """
        query AssetPrice($address: String!, $chainId: Int!) {
          assetByAddress(address: $address, chainId: $chainId) {
            symbol
            price { usd }
          }
        }
        """
        data = self._query(query, {"address": address, "chainId": chain_id})
        asset = data.get("assetByAddress")
        if asset and asset.get("price"):
            return float(asset["price"]["usd"])
        return 0.0

    # ------------------------------------------------------------------
    # 4. Historical liquidation transactions for a market
    # ------------------------------------------------------------------
    def get_market_liquidations(self, market_unique_key: str, first: int = 50) -> list:
        """Fetch recent liquidation events for a market."""
        query = """
        query Liquidations($keys: [String!]!, $first: Int!) {
          transactions(
            first: $first
            where: {
              marketUniqueKey_in: $keys
              type_in: [MarketLiquidation]
            }
          ) {
            items {
              blockNumber
              hash
              timestamp
              user { address }
              data {
                ... on MarketLiquidationTransactionData {
                  seizedAssets
                  repaidAssets
                  seizedAssetsUsd
                  repaidAssetsUsd
                  badDebtAssetsUsd
                  liquidator
                }
              }
            }
          }
        }
        """
        data = self._query(query, {"keys": [market_unique_key], "first": first})
        return data.get("transactions", {}).get("items", [])
