#!/bin/bash
# Kubernetes MCP Server - Install Script
# Run this once to set up the environment

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

echo "==> Installing Kubernetes MCP Server..."
echo "    Directory: $SCRIPT_DIR"

# Check prerequisites
for cmd in uv kubectl aws python3; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "WARNING: '$cmd' not found in PATH – some features may not work"
  else
    echo "    [OK] $cmd found: $(command -v $cmd)"
  fi
done

# Check ~/.kube/config
if [ -f "$HOME/.kube/config" ]; then
  echo "    [OK] ~/.kube/config found"
else
  echo "    WARNING: ~/.kube/config not found – create it with: aws eks update-kubeconfig ..."
fi

# Install Python dependencies
echo ""
echo "==> Installing Python dependencies..."
cd "$SCRIPT_DIR"
uv sync

echo ""
echo "==> Testing server import..."
uv run python -c "import mcp; import kubernetes; import boto3; print('[OK] All dependencies importable')"

# Claude Desktop config
echo ""
echo "==> Updating Claude Desktop config..."
echo "    Config path: $CONFIG_PATH"

if [ ! -f "$CONFIG_PATH" ]; then
  echo "    Creating new config file..."
  mkdir -p "$(dirname "$CONFIG_PATH")"
  echo '{"mcpServers":{}}' > "$CONFIG_PATH"
fi

# Inject the MCP entry using Python (portable JSON merge)
python3 - <<PYEOF
import json, sys, os

config_path = os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")
script_dir  = "$SCRIPT_DIR"

with open(config_path) as f:
    cfg = json.load(f)

cfg.setdefault("mcpServers", {})
cfg["mcpServers"]["kubernetes"] = {
    "command": "uv",
    "args": ["--directory", script_dir, "run", "python", "server.py"],
    "disabled": False,
    "alwaysAllow": []
}

with open(config_path, "w") as f:
    json.dump(cfg, f, indent=2)

print(f"    [OK] kubernetes MCP server registered in {config_path}")
PYEOF

echo ""
echo "==> Done! Restart Claude Desktop to activate the Kubernetes MCP server."
echo ""
echo "    Available tool categories:"
echo "      k8s_*    – Core Kubernetes operations (list, describe, delete, apply, logs, etc.)"
echo "      eks_*    – AWS EKS specific (version check, health, nodegroups, addons, etc.)"
