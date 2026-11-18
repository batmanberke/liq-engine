"""
chart.py
========
Generates a self-contained HTML dashboard with Plotly chart showing
liquidation volume distribution by collateral price change percentage.
"""

import json
from datetime import datetime, timezone
from dataclasses import asdict

from .models import MarketParams, LiquidationBucket, SettlementPayload


def generate_chart(
    buckets: list[LiquidationBucket],
    market_params: MarketParams,
    effective_price: float,
    next_price: float,
    pct_change: float,
    payload: SettlementPayload,
    borrower_count: int,
    market_state_summary: dict,
    output_path: str = "chronicle_rwa_engine_output.html",
) -> str:
    """Generate the full HTML dashboard and write to output_path."""

    meaningful = [b for b in buckets if b.price_change_pct >= -30]

    x_labels = [f"{b.price_change_pct:.1f}%" for b in meaningful]
    bar_counts = [b.position_count for b in meaningful]
    bar_debt = [round(b.total_debt_usd, 2) for b in meaningful]
    cum_counts = [b.cumulative_count for b in meaningful]
    cum_debt = [round(b.cumulative_debt_usd, 2) for b in meaningful]

    poke_marker_x = f"{pct_change:.1f}%"
    lltv_pct = market_params.lltv * 100

    flag_color = "#e74c3c" if payload.liquidation_triggered else "#27ae60"
    flag_label = (
        "LIQUIDATIONS TRIGGERED" if payload.liquidation_triggered
        else "NO LIQUIDATIONS"
    )

    ms = market_state_summary
    pair = f"{market_params.collateral_asset_symbol} / {market_params.loan_asset_symbol}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Chronicle RWA Engine — {pair}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=DM+Sans:wght@400;500;600;700&display=swap');

  :root {{
    --bg: #0a0e17; --surface: #111827; --border: #1e293b;
    --text: #e2e8f0; --muted: #94a3b8;
    --accent: #38bdf8; --danger: #ef4444; --success: #22c55e; --warn: #f59e0b;
    --chronicle-blue: #3b82f6; --chronicle-purple: #8b5cf6;
  }}

  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'DM Sans', sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh; padding: 24px;
  }}

  .header {{
    display: flex; justify-content: space-between; align-items: flex-start;
    margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border);
  }}
  .header h1 {{
    font-size: 1.5rem; font-weight: 700;
    background: linear-gradient(135deg, var(--chronicle-blue), var(--chronicle-purple));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }}
  .header .sub {{ color: var(--muted); font-size: 0.82rem; margin-top: 4px; }}
  .flag-badge {{
    display: inline-flex; align-items: center; gap: 8px;
    padding: 8px 16px; border-radius: 8px;
    font-family: 'JetBrains Mono', monospace; font-size: 0.78rem; font-weight: 700;
    background: {flag_color}18; color: {flag_color}; border: 1px solid {flag_color}44;
  }}
  .flag-dot {{
    width: 8px; height: 8px; border-radius: 50%; background: {flag_color};
    animation: pulse 1.5s ease-in-out infinite;
  }}
  @keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:0.4; }} }}

  .metrics {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 10px; margin-bottom: 20px;
  }}
  .mc {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 14px;
  }}
  .mc .label {{
    font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.8px;
    color: var(--muted); margin-bottom: 5px;
  }}
  .mc .value {{
    font-family: 'JetBrains Mono', monospace; font-size: 1.1rem; font-weight: 700;
  }}
  .mc .detail {{ font-size: 0.72rem; color: var(--muted); margin-top: 3px; }}

  #chart {{ width: 100%; border-radius: 12px; overflow: hidden; }}

  .payload-section {{
    margin-top: 20px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 10px; padding: 18px;
  }}
  .payload-section h3 {{
    font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.8px;
    color: var(--chronicle-blue); margin-bottom: 10px;
  }}
  .payload-pre {{
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    padding: 14px; overflow-x: auto;
    font-family: 'JetBrains Mono', monospace; font-size: 0.7rem; line-height: 1.6;
    color: var(--text); max-height: 400px; overflow-y: auto;
  }}

  .footer {{
    margin-top: 16px; text-align: center; color: var(--muted); font-size: 0.7rem;
  }}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>Chronicle RWA Engine</h1>
    <div class="sub">
      Proactive Liquidation Detection — Market: {pair}
      &bull; Chain {market_params.chain_id}
      &bull; ID: {market_params.market_id[:14]}…{market_params.market_id[-8:]}
    </div>
  </div>
  <div class="flag-badge"><span class="flag-dot"></span>{flag_label}</div>
</div>

<div class="metrics">
  <div class="mc">
    <div class="label">Market</div>
    <div class="value">{pair}</div>
    <div class="detail">Chain {market_params.chain_id}</div>
  </div>
  <div class="mc">
    <div class="label">LLTV</div>
    <div class="value">{lltv_pct:.2f}%</div>
    <div class="detail">Liquidation threshold</div>
  </div>
  <div class="mc">
    <div class="label">Total Supply</div>
    <div class="value">${ms.get('supply_usd', 0):,.0f}</div>
    <div class="detail">Utilization: {ms.get('utilization', 0)*100:.1f}%</div>
  </div>
  <div class="mc">
    <div class="label">Total Borrow</div>
    <div class="value">${ms.get('borrow_usd', 0):,.0f}</div>
    <div class="detail">{borrower_count} borrowers</div>
  </div>
  <div class="mc">
    <div class="label">Collateral Price</div>
    <div class="value">${market_params.collateral_price_usd:,.4f}</div>
    <div class="detail">{market_params.collateral_asset_symbol}</div>
  </div>
  <div class="mc">
    <div class="label">Oracle Price Ratio</div>
    <div class="value">{effective_price:,.6f}</div>
    <div class="detail">{market_params.collateral_asset_symbol}/{market_params.loan_asset_symbol}</div>
  </div>
  <div class="mc">
    <div class="label">Next Poke (simulated)</div>
    <div class="value">{next_price:,.6f}</div>
    <div class="detail" style="color: {'var(--danger)' if pct_change < 0 else 'var(--success)'}">{pct_change:+.2f}%</div>
  </div>
  <div class="mc">
    <div class="label">Positions at Risk</div>
    <div class="value" style="color: {'var(--danger)' if payload.total_positions_at_risk > 0 else 'var(--success)'}">{payload.total_positions_at_risk}</div>
    <div class="detail">${payload.total_debt_at_risk_usd:,.2f} debt</div>
  </div>
</div>

<div id="chart"></div>

<div class="payload-section">
  <h3>Settlement Payload — Chronicle Auctioneer Output</h3>
  <pre class="payload-pre">{json.dumps(asdict(payload), indent=2)}</pre>
</div>

<div class="footer">
  Chronicle RWA Engine v0.2 &mdash; Powered by Chronicle Proof of Assets &amp; Morpho Blue
  &mdash; {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}
</div>

<script>
const xLabels = {json.dumps(x_labels)};
const barDebt  = {json.dumps(bar_debt)};
const barCnt   = {json.dumps(bar_counts)};
const cumDebt  = {json.dumps(cum_debt)};
const cumCnt   = {json.dumps(cum_counts)};

const traceBar = {{
  x: xLabels, y: barDebt, type: 'bar', name: 'Debt at price level ($)',
  marker: {{
    color: barDebt.map((_, i) => {{
      const p = parseFloat(xLabels[i]);
      if (p >= 0) return 'rgba(239,68,68,0.85)';
      if (p > -5) return 'rgba(245,158,11,0.75)';
      return 'rgba(59,130,246,0.6)';
    }}),
    line: {{ color: 'rgba(255,255,255,0.08)', width: 0.5 }}
  }},
  hovertemplate: 'Price %{{x}}<br>Positions: %{{customdata[0]}}<br>Debt: $%{{y:,.0f}}<extra></extra>',
  customdata: barCnt.map(c => [c]),
  yaxis: 'y'
}};

const traceCum = {{
  x: xLabels, y: cumDebt, type: 'scatter', mode: 'lines',
  name: 'Cumulative debt ($)',
  line: {{ color: '#8b5cf6', width: 2.5, shape: 'spline' }},
  hovertemplate: 'Price %{{x}}<br>Cum. positions: %{{customdata[0]}}<br>Cum. debt: $%{{y:,.0f}}<extra></extra>',
  customdata: cumCnt.map(c => [c]),
  yaxis: 'y2'
}};

const layout = {{
  paper_bgcolor: '#111827', plot_bgcolor: '#111827',
  font: {{ family: 'DM Sans, sans-serif', color: '#e2e8f0', size: 11 }},
  title: {{
    text: 'Liquidation Volume Distribution by Collateral Price Change',
    font: {{ size: 14, color: '#94a3b8' }}, x: 0.01, xanchor: 'left'
  }},
  xaxis: {{
    title: 'Collateral Price Change (%)',
    gridcolor: '#1e293b', zerolinecolor: '#334155',
    tickangle: -45, tickfont: {{ size: 9 }}
  }},
  yaxis: {{
    title: 'Debt at price level (USD)',
    gridcolor: '#1e293b', zerolinecolor: '#334155', tickformat: '$,.0f'
  }},
  yaxis2: {{
    title: 'Cumulative debt (USD)', overlaying: 'y', side: 'right',
    gridcolor: 'transparent', tickformat: '$,.0f'
  }},
  shapes: [{{
    type: 'line',
    x0: '{poke_marker_x}', x1: '{poke_marker_x}',
    y0: 0, y1: 1, xref: 'x', yref: 'paper',
    line: {{ color: '#ef4444', width: 2, dash: 'dot' }}
  }}],
  annotations: [{{
    x: '{poke_marker_x}', y: 1.02, xref: 'x', yref: 'paper',
    text: 'Next Poke ({pct_change:+.2f}%)', showarrow: false,
    font: {{ size: 10, color: '#ef4444', family: 'JetBrains Mono' }},
    bgcolor: '#ef444420', bordercolor: '#ef444466', borderwidth: 1, borderpad: 4
  }}],
  legend: {{
    x: 0.01, y: 0.98, bgcolor: '#11182799',
    bordercolor: '#1e293b', borderwidth: 1, font: {{ size: 10 }}
  }},
  margin: {{ l: 80, r: 80, t: 50, b: 80 }}, bargap: 0.15
}};

Plotly.newPlot('chart', [traceBar, traceCum], layout, {{
  responsive: true, displayModeBar: false
}});
</script>
</body>
</html>"""

    with open(output_path, "w") as f:
        f.write(html)

    return output_path
