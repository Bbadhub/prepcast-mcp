# Strategy Toolkit

This folder helps you price competitively without racing to the bottom.

## Run analysis

```bash
python strategy/competitor_analysis.py \
  --competitors strategy/competitors.example.json \
  --policy strategy/pricing_policy.example.json \
  --output-root .
```

## Outputs

- `reports/competitive_report.md`: internal strategy report
- `site/public-comparison.json`: public-safe comparison data for landing page
- `site/pricing-recommendation.json`: suggested pricing band and call limits

## Secret Sauce Guardrail

Use the output for:
- feature positioning
- pricing rationale
- outcome-level comparisons

Do not publish:
- proprietary scoring formulas
- internal prompt chains
- model routing and ranking internals
