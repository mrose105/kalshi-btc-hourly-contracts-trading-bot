#!/usr/bin/env bash
set -euo pipefail

# Source this file after exporting your local credentials:
#
#   export KALSHI_API_KEY_ID="your-key-id"
#   export KALSHI_PRIVATE_KEY_PATH="$HOME/.kalshi-key.pem"
#   source setup_kalshi.sh
#
# Keep the private key outside this repository. Do not paste private key
# material into this file.

if [[ -z "${KALSHI_API_KEY_ID:-}" ]]; then
  echo "Missing KALSHI_API_KEY_ID"
  return 1 2>/dev/null || exit 1
fi

if [[ -z "${KALSHI_PRIVATE_KEY_PATH:-}" ]]; then
  echo "Missing KALSHI_PRIVATE_KEY_PATH"
  return 1 2>/dev/null || exit 1
fi

if [[ ! -f "$KALSHI_PRIVATE_KEY_PATH" ]]; then
  echo "Private key file not found: $KALSHI_PRIVATE_KEY_PATH"
  return 1 2>/dev/null || exit 1
fi

chmod 600 "$KALSHI_PRIVATE_KEY_PATH"

echo "KALSHI_API_KEY_ID is set"
echo "KALSHI_PRIVATE_KEY_PATH=$KALSHI_PRIVATE_KEY_PATH"
echo
echo "Run live BTC bot:"
echo "  caffeinate -dimsu python3 -m kalshi_btc_bot.app"
