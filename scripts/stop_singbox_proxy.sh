#!/usr/bin/env bash
set -uo pipefail

PROXY_DIR="${RUNNER_TEMP:-/tmp}/checkin-proxy"
PID_FILE="${PROXY_DIR}/singbox.pid"

if [[ -f "${PID_FILE}" ]]; then
	echo "🛑 停止 sing-box 代理（PID $(cat "${PID_FILE}")）"
	kill "$(cat "${PID_FILE}")" 2>/dev/null || true
	rm -f "${PID_FILE}"
	echo "✅ 代理已停止"
else
	echo "ℹ️  未发现运行中的代理，无需停止"
fi
