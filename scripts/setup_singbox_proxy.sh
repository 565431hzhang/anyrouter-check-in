#!/usr/bin/env bash
# 安装 sing-box → 根据 NODE_LINK 生成配置 → 启动本地代理 → 检测出口 IP。
# 任何一步失败都会以非 0 退出，workflow 随即中止，不会执行签到。
#
# 环境变量:
#   NODE_LINK         节点分享链接（GitHub Secret，必填）
#                     支持 hysteria2/hy2、vless(reality)、vmess、trojan、ss、tuic
#   SINGBOX_VERSION   sing-box 版本，默认 1.13.0
#   PROXY_PORT        本地 mixed 端口，默认 7890
#   PROXY_TEST_URL    连通性探测地址，默认 https://www.google.com/generate_204

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ───────────── 日志 ─────────────
log()     { echo "$*"; }
info()    { echo "ℹ️  $*"; }
ok()      { echo "✅ $*"; }
warn()    { echo "⚠️  $*"; }
section() { echo; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; echo "$*"; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }

# 失败即中止：停止已启动的代理并退出 1（后续签到步骤不会执行）
die() {
	echo "❌ $1"
	echo "🚫 代理未就绪，已中止，本次不执行签到"
	if [[ -f "${PROXY_DIR:-/nonexistent}/singbox.pid" ]]; then
		kill "$(cat "${PROXY_DIR}/singbox.pid")" 2>/dev/null || true
	fi
	if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
		printf "### ❌ 代理失败，已中止签到\n%s\n" "$1" >> "${GITHUB_STEP_SUMMARY}"
	fi
	exit 1
}

# 出口 IP 打码：122.122.33.44 → 122.122.*.*；IPv6 只保留前两段
mask_ip() {
	local ip="$1"
	if [[ "${ip}" =~ ^([0-9]+\.[0-9]+)\.[0-9]+\.[0-9]+$ ]]; then
		echo "${BASH_REMATCH[1]}.*.*"
	elif [[ "${ip}" == *:* ]]; then
		echo "${ip}" | awk -F: '{print $1":"$2":*:*"}'
	else
		echo "未知"
	fi
}

# 通过代理探测出口 IP，依次尝试多个服务，成功则输出纯 IP
probe_exit_ip() {
	local proxy_url="$1" svc ip
	for svc in "https://api.ipify.org" "https://ifconfig.me/ip" "https://api.ip.sb/ip" "https://icanhazip.com"; do
		ip="$(curl -fsS -x "${proxy_url}" --max-time 15 "${svc}" 2>/dev/null | tr -d '[:space:]')" || continue
		if [[ "${ip}" =~ ^[0-9a-fA-F:.]+$ ]]; then
			echo "${ip}"
			return 0
		fi
	done
	return 1
}

# ───────────── 开始 ─────────────
section "🛡️  代理配置（sing-box）"

if [[ -z "${NODE_LINK:-}" ]]; then
	die "未设置 NODE_LINK（请在 Settings → Environments → production → Secrets 中添加）"
fi
export NODE_LINK

PROXY_DIR="${RUNNER_TEMP:-/tmp}/checkin-proxy"
PROXY_PORT="${PROXY_PORT:-7890}"
PROXY_TEST_URL="${PROXY_TEST_URL:-https://www.google.com/generate_204}"
SINGBOX_VERSION="${SINGBOX_VERSION:-1.13.0}"
PROXY_URL="http://127.0.0.1:${PROXY_PORT}"
export PROXY_PORT

mkdir -p "${PROXY_DIR}" && cd "${PROXY_DIR}" || die "无法创建工作目录 ${PROXY_DIR}"

# ───────────── 1. 安装 sing-box ─────────────
section "📦 步骤 1/4：安装 sing-box v${SINGBOX_VERSION}"
PKG="sing-box-${SINGBOX_VERSION}-linux-amd64"
URL="https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/${PKG}.tar.gz"
info "下载 ${URL}"
curl --retry 3 --retry-delay 5 --retry-all-errors -fsSL -o "${PKG}.tar.gz" "${URL}" || die "sing-box 下载失败"
tar -xzf "${PKG}.tar.gz" || die "sing-box 解压失败"
SINGBOX_BIN="${PROXY_DIR}/${PKG}/sing-box"
chmod +x "${SINGBOX_BIN}"
ok "安装完成：$("${SINGBOX_BIN}" version 2>/dev/null | head -n 1)"

# ───────────── 2. 解析 NODE_LINK 并生成配置 ─────────────
section "🧩 步骤 2/4：解析节点并生成配置"
if ! python3 "${SCRIPT_DIR}/node_to_singbox.py" "${PROXY_DIR}/config.json"; then
	die "节点链接解析失败"
fi
"${SINGBOX_BIN}" check -c "${PROXY_DIR}/config.json" || die "sing-box 配置校验未通过"
ok "配置生成并校验通过"

# ───────────── 3. 启动 ─────────────
section "🚀 步骤 3/4：启动本地代理"
nohup "${SINGBOX_BIN}" run -c "${PROXY_DIR}/config.json" > singbox.log 2>&1 &
echo $! > singbox.pid

LISTENING=false
for _ in $(seq 1 20); do
	if (echo > "/dev/tcp/127.0.0.1/${PROXY_PORT}") 2>/dev/null; then
		LISTENING=true
		break
	fi
	kill -0 "$(cat singbox.pid)" 2>/dev/null || break
	sleep 0.5
done

if [[ "${LISTENING}" != "true" ]]; then
	tail -n 30 singbox.log || true
	die "sing-box 启动失败或端口未监听"
fi
ok "已监听 127.0.0.1:${PROXY_PORT}（PID $(cat singbox.pid)）"

# ───────────── 4. 连通性与出口 IP ─────────────
section "🌐 步骤 4/4：连通性与出口 IP 检测"
READY=false
for attempt in $(seq 1 6); do
	if curl -fsS -x "${PROXY_URL}" --max-time 15 "${PROXY_TEST_URL}" -o /dev/null 2>/dev/null; then
		READY=true
		break
	fi
	info "等待代理通路就绪（${attempt}/6）..."
	sleep 2
done

if [[ "${READY}" != "true" ]]; then
	tail -n 30 singbox.log || true
	warn "排查建议：① 节点是否在线 ② 服务器端口是否放行（hysteria2/tuic 需放行 UDP）③ 链接中的密码 / UUID / SNI 是否正确"
	die "代理连通性测试失败：${PROXY_TEST_URL}"
fi
ok "连通性测试通过（${PROXY_TEST_URL}）"

EXIT_IP="$(probe_exit_ip "${PROXY_URL}")" || EXIT_IP=""
[[ -n "${EXIT_IP}" ]] || die "无法获取出口 IP（所有探测服务均失败）"
MASKED_EXIT="$(mask_ip "${EXIT_IP}")"
log "🌍 当前出口IP：${MASKED_EXIT}"

# 与直连 IP 对比，确认代理确实生效（不打印直连 IP）
DIRECT_IP="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null | tr -d '[:space:]')" || DIRECT_IP=""
if [[ -n "${DIRECT_IP}" && "${DIRECT_IP}" == "${EXIT_IP}" ]]; then
	die "出口 IP 与直连 IP 相同，代理未生效"
elif [[ -n "${DIRECT_IP}" ]]; then
	ok "出口 IP 已与直连 IP 不同，代理生效"
fi

section "🎉 代理就绪"
log "🔌 本地代理：${PROXY_URL}（仅供签到脚本使用，不是全局代理）"
log "🌍 当前出口IP：${MASKED_EXIT}"

if [[ -n "${GITHUB_ENV:-}" ]]; then
	echo "CHECKIN_PROXY_URL=${PROXY_URL}" >> "${GITHUB_ENV}"
	echo "CHECKIN_PROXY_EXIT=${MASKED_EXIT}" >> "${GITHUB_ENV}"
fi
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
	{
		echo "### 🛡️ 代理状态"
		echo "- 🌍 当前出口IP：\`${MASKED_EXIT}\`"
		echo "- 📦 sing-box：v${SINGBOX_VERSION}"
	} >> "${GITHUB_STEP_SUMMARY}"
fi
