#!/bin/bash
set -e

echo "=== flagyard-watcher — Install ==="
echo ""

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$HOME/.claude/skills"

echo "[1/3] Checking flagyard-submit skill (required dependency)..."
if [ -f "$SKILL_DIR/flagyard-submit/scripts/flagyard_lib.py" ]; then
    echo "  Found at $SKILL_DIR/flagyard-submit"
else
    echo "  WARNING: flagyard-submit skill not found at $SKILL_DIR/flagyard-submit/"
    echo "  Install it: https://github.com/RusiruSadathana/flagyard-submit"
fi

echo "[2/3] Checking t3-manage skill (required to spawn T3 threads)..."
if [ -f "$SKILL_DIR/t3-manage/scripts/t3_manage.py" ]; then
    echo "  Found at $SKILL_DIR/t3-manage"
else
    echo "  WARNING: t3-manage skill not found at $SKILL_DIR/t3-manage/"
    echo "  See the t3-manage skill docs to install it."
fi

echo "[3/3] Installing as a Claude Code skill..."
mkdir -p "$SKILL_DIR/flagyard-watcher"
if [ -L "$SKILL_DIR/flagyard-watcher/scripts" ] || [ -d "$SKILL_DIR/flagyard-watcher/scripts" ]; then
    rm -rf "$SKILL_DIR/flagyard-watcher/scripts"
fi
ln -sf "$REPO_DIR/scripts" "$SKILL_DIR/flagyard-watcher/scripts"
cp "$REPO_DIR/SKILL.md" "$SKILL_DIR/flagyard-watcher/SKILL.md"

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Quick start:"
echo "  python3 $REPO_DIR/scripts/flagyard_watcher.py --event-id <uuid> --once --dry-run"
