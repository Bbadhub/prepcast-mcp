# {{PROJECT_NAME}}

{{PROJECT_DESCRIPTION}}

## What You Get

- MCP server (`SSE` + `stdio`)
- Tool registry (`tools/`)
- Billing middleware (`billing.py`)
- API keys + free tier + rate limits
- Stripe usage metering + checkout endpoint
- Affiliate signup + referral commission tracking
- Landing page (`site/index.html`)
- Consumer web panel (no MCP setup required)
- Deploy scripts (`docker-compose.yml`, `deploy.sh`)
- Competitor/pricing strategy toolkit (`strategy/`)
- Prompt pack for install/sales/public messaging (`prompts/`)

## Local Setup

```bash
cp .env.example .env
./deploy.sh setup
./deploy.sh start
```

Health:

```bash
curl http://localhost:{{SERVER_PORT}}/health
```

## Billing Endpoints

- `POST /billing/keys` create API key
- `GET /billing/usage?api_key=...` user usage summary
- `GET /billing/activity?api_key=...` recent calls
- `GET /billing/metrics?admin_key=...` admin usage/revenue totals
- `POST /billing/checkout` Stripe checkout session
- `POST /billing/webhook` Stripe webhook handler

## Deploy

### Docker Compose

```bash
./deploy.sh start
```

### Competitive Pricing Analysis

```bash
./deploy.sh benchmark
```

This generates:
- `reports/competitive_report.md`
- `site/public-comparison.json`
- `site/pricing-recommendation.json`

### Fly.io

```bash
./deploy.sh fly
```

### Railway

```bash
./deploy.sh railway
```

## Connect from Claude

### Remote SSE

Point Claude MCP config to:

```text
https://your-domain.example/sse
```

Include `X-API-Key: <user_api_key>` in client headers if billing is enabled.

### Local stdio

```bash
python server.py --stdio
```

## Customize

1. Replace `tools/example.py` with your product tools.
2. Update per-tool pricing in `billing.py`.
3. Configure affiliate defaults in `.env`.
4. Edit landing page copy and consumer panel in `site/index.html`.
5. Set Stripe env vars in `.env`.
6. Update `prompts/` files for support and sales assistant behavior.
