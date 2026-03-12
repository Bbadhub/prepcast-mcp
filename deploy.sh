#!/usr/bin/env bash
# ===========================================================================
# Deploy & manage script for the MCP SaaS server.
#
# Usage:
#   ./deploy.sh setup       - Install dependencies and create .env
#   ./deploy.sh start       - Start the server (Docker Compose)
#   ./deploy.sh stop        - Stop the server
#   ./deploy.sh restart     - Restart the server
#   ./deploy.sh logs        - Tail server logs
#   ./deploy.sh status      - Show running containers & health
#   ./deploy.sh billing     - Show billing metrics
#   ./deploy.sh create-key  - Create a new API key
#   ./deploy.sh benchmark   - Generate competitor + pricing analysis outputs
#   ./deploy.sh fly         - Deploy to Fly.io
#   ./deploy.sh railway     - Deploy to Railway
# ===========================================================================

set -euo pipefail

PORT="${SERVER_PORT:-{{SERVER_PORT}}}"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_setup() {
    info "Setting up project..."

    # Create .env from example if it doesn't exist
    if [ ! -f "$PROJECT_DIR/.env" ]; then
        if [ -f "$PROJECT_DIR/.env.example" ]; then
            cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
            ok "Created .env from .env.example - edit it with your values"
        else
            warn ".env.example not found"
        fi
    else
        ok ".env already exists"
    fi

    # Install Python dependencies
    if command -v pip &> /dev/null; then
        info "Installing Python dependencies..."
        pip install -r "$PROJECT_DIR/requirements.txt"
        ok "Dependencies installed"
    else
        warn "pip not found - install dependencies manually"
    fi

    # Create data directory
    mkdir -p "$PROJECT_DIR/data/usage"
    ok "Data directory created"

    ok "Setup complete. Edit .env then run: ./deploy.sh start"
}

cmd_start() {
    info "Starting server on port $PORT..."
    cd "$PROJECT_DIR"
    docker compose up -d --build
    ok "Server started. Health: http://localhost:$PORT/health"
}

cmd_stop() {
    info "Stopping server..."
    cd "$PROJECT_DIR"
    docker compose down
    ok "Server stopped"
}

cmd_restart() {
    info "Restarting server..."
    cd "$PROJECT_DIR"
    docker compose restart
    ok "Server restarted"
}

cmd_logs() {
    cd "$PROJECT_DIR"
    docker compose logs -f --tail=100
}

cmd_status() {
    info "Container status:"
    cd "$PROJECT_DIR"
    docker compose ps

    echo ""
    info "Health check:"
    curl -s "http://localhost:$PORT/health" 2>/dev/null | python3 -m json.tool 2>/dev/null || err "Server not responding"
}

cmd_billing() {
    info "Billing metrics:"
    ADMIN_KEY="${BILLING_ADMIN_KEY:-}"
    curl -s "http://localhost:$PORT/billing/metrics?admin_key=$ADMIN_KEY" 2>/dev/null | python3 -m json.tool 2>/dev/null || err "Could not fetch metrics"
}

cmd_create_key() {
    local user_id="${1:-}"
    local tier="${2:-free}"

    if [ -z "$user_id" ]; then
        read -p "Enter user_id: " user_id
    fi

    if [ -z "$user_id" ]; then
        err "user_id is required"
        exit 1
    fi

    info "Creating API key for user: $user_id (tier: $tier)"
    curl -s -X POST "http://localhost:$PORT/billing/keys" \
        -H "Content-Type: application/json" \
        -d "{\"user_id\": \"$user_id\", \"tier\": \"$tier\"}" \
        | python3 -m json.tool 2>/dev/null || err "Failed to create key"
}

cmd_benchmark() {
    info "Generating competitor and pricing analysis outputs..."
    cd "$PROJECT_DIR"
    python strategy/competitor_analysis.py \
        --competitors strategy/competitors.example.json \
        --policy strategy/pricing_policy.example.json \
        --output-root .
    ok "Generated reports/competitive_report.md and site/public-comparison.json"
}

cmd_fly() {
    info "Deploying to Fly.io..."

    if ! command -v fly &> /dev/null; then
        err "Fly CLI not installed. Install: https://fly.io/docs/hands-on/install-flyctl/"
        exit 1
    fi

    cd "$PROJECT_DIR"

    # Initialize if no fly.toml
    if [ ! -f "fly.toml" ]; then
        info "Initializing Fly app..."
        fly launch --no-deploy
    fi

    # Set secrets from .env
    if [ -f ".env" ]; then
        info "Setting secrets from .env..."
        fly secrets import < .env
    fi

    # Deploy
    fly deploy
    ok "Deployed to Fly.io"
    fly status
}

cmd_railway() {
    info "Deploying to Railway..."

    if ! command -v railway &> /dev/null; then
        err "Railway CLI not installed. Install: https://docs.railway.app/develop/cli"
        exit 1
    fi

    cd "$PROJECT_DIR"

    # Initialize if needed
    if [ ! -f "railway.json" ]; then
        info "Initializing Railway project..."
        railway init
    fi

    # Deploy
    railway up
    ok "Deployed to Railway"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

case "${1:-help}" in
    setup)      cmd_setup ;;
    start)      cmd_start ;;
    stop)       cmd_stop ;;
    restart)    cmd_restart ;;
    logs)       cmd_logs ;;
    status)     cmd_status ;;
    billing)    cmd_billing ;;
    create-key) cmd_create_key "${2:-}" "${3:-free}" ;;
    benchmark)  cmd_benchmark ;;
    fly)        cmd_fly ;;
    railway)    cmd_railway ;;
    help|*)
        echo "Usage: $0 <command>"
        echo ""
        echo "Commands:"
        echo "  setup        Install dependencies and create .env"
        echo "  start        Start the server (Docker Compose)"
        echo "  stop         Stop the server"
        echo "  restart      Restart the server"
        echo "  logs         Tail server logs"
        echo "  status       Show running containers & health"
        echo "  billing      Show billing metrics"
        echo "  create-key   Create a new API key (usage: create-key <user_id> [tier])"
        echo "  benchmark    Generate competitor and pricing analysis outputs"
        echo "  fly          Deploy to Fly.io"
        echo "  railway      Deploy to Railway"
        ;;
esac
