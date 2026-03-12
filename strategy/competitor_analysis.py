#!/usr/bin/env python3
"""
Competitor analysis and pricing recommendation engine.

Outputs:
1) reports/competitive_report.md (internal strategy brief)
2) site/public-comparison.json (safe marketing comparison table)
3) site/pricing-recommendation.json (recommended pricing bands)
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Dict, List


@dataclass
class PricePoint:
    pro_monthly_usd: float
    free_calls: int
    pro_calls: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate competitor and pricing analysis.")
    parser.add_argument(
        "--competitors",
        default="strategy/competitors.example.json",
        help="Path to competitor profile JSON.",
    )
    parser.add_argument(
        "--policy",
        default="strategy/pricing_policy.example.json",
        help="Path to pricing policy JSON.",
    )
    parser.add_argument(
        "--output-root",
        default=".",
        help="Project root where reports/ and site/ folders exist.",
    )
    return parser.parse_args()


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def weighted_score(scores: Dict[str, float], weights: Dict[str, float]) -> float:
    weighted_total = 0.0
    weight_sum = 0.0
    for key, weight in weights.items():
        value = float(scores.get(key, 0.0))
        weighted_total += value * weight
        weight_sum += weight
    if weight_sum == 0:
        return 0.0
    return round(weighted_total / weight_sum, 3)


def round_price(value: float) -> int:
    """
    Round to psychologically friendly integer ending in 9.
    Example: 103 -> 109, 148 -> 149, 196 -> 199.
    """
    if value <= 9:
        return 9
    rounded = int(round(value))
    base = rounded - (rounded % 10)
    candidate = base + 9
    if candidate < rounded:
        candidate += 10
    return candidate


def score_gap_labels(our_scores: Dict[str, float], competitors: List[Dict[str, Any]]) -> List[str]:
    if not competitors:
        return []
    categories = set(our_scores.keys())
    for c in competitors:
        categories.update((c.get("scores") or {}).keys())
    labels = []
    for category in sorted(categories):
        our = float(our_scores.get(category, 0.0))
        best_comp = max(float((c.get("scores") or {}).get(category, 0.0)) for c in competitors)
        if best_comp - our >= 0.75:
            labels.append(
                f"Improve {category.replace('_', ' ')} (best competitor leads by {best_comp - our:.2f} points)."
            )
    return labels


def generate_public_items(payload: Dict[str, Any], opportunities: List[str]) -> List[Dict[str, str]]:
    our = payload["our_product"]
    competitors = payload.get("competitors", [])
    items = [
        {
            "provider": our.get("name", "Our Product"),
            "positioning": our.get("positioning", "Outcome-focused MCP platform"),
            "public_strength": our.get("public_strength", "Strong execution and measurable outcomes."),
            "public_gap": opportunities[0] if opportunities else "Continue improving UX and ecosystem support.",
        }
    ]
    for comp in competitors:
        items.append(
            {
                "provider": comp.get("name", "Competitor"),
                "positioning": comp.get("positioning", "General AI tooling"),
                "public_strength": comp.get("public_strength", "Strong brand awareness."),
                "public_gap": comp.get("public_gap", "Less domain-specific depth."),
            }
        )
    return items


def main() -> int:
    args = parse_args()
    root = Path(args.output_root).resolve()
    competitors_path = Path(args.competitors).resolve()
    policy_path = Path(args.policy).resolve()

    payload = load_json(competitors_path)
    policy = load_json(policy_path)

    our = payload["our_product"]
    competitors = payload.get("competitors", [])
    weights = policy.get("weights", {})

    our_score = weighted_score(our.get("scores", {}), weights)
    competitor_scores = [
        weighted_score(c.get("scores", {}), weights)
        for c in competitors
    ]
    competitor_avg_score = round(sum(competitor_scores) / max(1, len(competitor_scores)), 3)

    # Pricing benchmark
    prices = [float(c.get("pricing", {}).get("pro_monthly_usd", 0)) for c in competitors]
    prices = [p for p in prices if p > 0]
    our_price = float(our.get("pricing", {}).get("pro_monthly_usd", 0))

    competitor_median_price = median(prices) if prices else max(49.0, our_price)
    competitor_p75 = sorted(prices)[int(math.floor(0.75 * (len(prices) - 1)))] if len(prices) > 1 else competitor_median_price

    score_advantage = our_score - competitor_avg_score
    if score_advantage >= 0.75:
        multiplier = policy.get("premium_multiplier_if_ahead", 1.15)
    elif score_advantage <= -0.50:
        multiplier = policy.get("discount_multiplier_if_behind", 0.90)
    else:
        multiplier = 1.0

    raw_target = competitor_median_price * multiplier
    floor_price = float(policy.get("min_pro_price_usd", 29))
    ceil_price = float(policy.get("max_pro_price_usd", max(999, competitor_p75 * 1.5)))
    recommended_pro_price = max(floor_price, min(ceil_price, round_price(raw_target)))

    # Call allocation guidance
    competitor_free_calls = [int(c.get("pricing", {}).get("free_calls", 0)) for c in competitors]
    competitor_pro_calls = [int(c.get("pricing", {}).get("pro_calls", 0)) for c in competitors]
    rec_free_calls = max(
        int(policy.get("min_free_calls", 50)),
        int(median(competitor_free_calls) if competitor_free_calls else 100),
    )
    rec_pro_calls = max(
        int(policy.get("min_pro_calls", 5000)),
        int((median(competitor_pro_calls) if competitor_pro_calls else 10000) * policy.get("pro_calls_multiplier", 1.1)),
    )

    opportunities = score_gap_labels(our.get("scores", {}), competitors)
    public_items = generate_public_items(payload, opportunities)

    reports_dir = root / "reports"
    site_dir = root / "site"
    reports_dir.mkdir(parents=True, exist_ok=True)
    site_dir.mkdir(parents=True, exist_ok=True)

    pricing_payload = {
        "our_current": our.get("pricing", {}),
        "benchmarks": {
            "competitor_median_pro_price_usd": competitor_median_price,
            "competitor_p75_pro_price_usd": competitor_p75,
            "our_score": our_score,
            "competitor_avg_score": competitor_avg_score,
            "score_advantage": round(score_advantage, 3),
        },
        "recommendation": {
            "pro_monthly_usd": recommended_pro_price,
            "free_calls": rec_free_calls,
            "pro_calls": rec_pro_calls,
            "rationale": (
                "Price from value/outcomes and market median. "
                "Do not underprice below your support + infra margin floor."
            ),
        },
    }

    report_md = f"""# Competitive Analysis Report

## Position
- Product: {our.get("name", "Our Product")}
- Weighted score: {our_score}
- Competitor avg score: {competitor_avg_score}
- Score advantage: {score_advantage:.3f}

## Pricing Benchmark
- Current pro price: ${our_price}/mo
- Competitor median pro price: ${competitor_median_price}/mo
- Competitor 75th percentile pro price: ${competitor_p75}/mo
- Recommended pro price: ${recommended_pro_price}/mo
- Recommended free calls: {rec_free_calls}
- Recommended pro calls: {rec_pro_calls}

## Actionable Improvement Areas
{"".join(f"- {item}\n" for item in opportunities) if opportunities else "- No major score gaps detected."}

## Public Messaging Guardrail
- Talk about outcomes, speed, reliability, compliance, and support.
- Do not disclose prompt engineering details, model routing logic, or proprietary ranking formulas.
- Publish only high-level comparisons and customer-visible capabilities.
"""

    (reports_dir / "competitive_report.md").write_text(report_md, encoding="utf-8")
    (site_dir / "public-comparison.json").write_text(
        json.dumps({"summary": "Outcome-level public comparison", "items": public_items}, indent=2),
        encoding="utf-8",
    )
    (site_dir / "pricing-recommendation.json").write_text(
        json.dumps(pricing_payload, indent=2),
        encoding="utf-8",
    )

    print("Generated:")
    print(f"- {reports_dir / 'competitive_report.md'}")
    print(f"- {site_dir / 'public-comparison.json'}")
    print(f"- {site_dir / 'pricing-recommendation.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
