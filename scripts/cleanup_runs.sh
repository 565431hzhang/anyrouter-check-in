#!/usr/bin/env bash
# 清理历史：删除除「当前这次运行」以外的所有 workflow 运行记录与 artifacts。
#
# - 当前运行正在进行，GitHub 不允许删除，且它就是最新的一次，因此保留它和它的 artifacts
# - 只清理 runs 与 artifacts，不动 Actions 缓存（余额历史、浏览器 profile 存在缓存里）
# - 仍在排队 / 进行中的其他运行无法删除，会跳过
# - 清理失败只警告，永远以 0 退出，不影响签到结果
#
# 环境变量（Actions 自带）: GH_TOKEN、GITHUB_REPOSITORY、GITHUB_RUN_ID
# 需要 job 权限: actions: write

set -uo pipefail

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🧹 清理历史运行与 Artifacts"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [[ -z "${GH_TOKEN:-}" || -z "${GITHUB_REPOSITORY:-}" || -z "${GITHUB_RUN_ID:-}" ]]; then
	echo "⚠️  缺少 GH_TOKEN / GITHUB_REPOSITORY / GITHUB_RUN_ID，跳过清理"
	exit 0
fi

REPO="${GITHUB_REPOSITORY}"
CUR="${GITHUB_RUN_ID}"
echo "📌 保留当前运行 #${CUR}"

delete_all() { # $1=名称  $2=API 路径前缀  后续参数: ID 列表
	local label="$1" prefix="$2" id ok=0 bad=0
	shift 2
	for id in "$@"; do
		if gh api -X DELETE "${prefix}/${id}" >/dev/null 2>&1; then
			ok=$((ok + 1))
		else
			bad=$((bad + 1))
		fi
	done
	echo "   ${label}：已删除 ${ok} 个$([[ ${bad} -gt 0 ]] && echo "，失败 ${bad} 个")"
}

# ── Artifacts（先删：包含仍在进行中的其他运行产生的）──
if ART_IDS="$(gh api --paginate "repos/${REPO}/actions/artifacts?per_page=100" \
	--jq ".artifacts[] | select(.workflow_run.id != ${CUR}) | .id" 2>&1)"; then
	mapfile -t ARTS <<<"${ART_IDS}"
	[[ -z "${ARTS[0]:-}" ]] && ARTS=()
	echo "📦 Artifacts：待清理 ${#ARTS[@]} 个"
	((${#ARTS[@]})) && delete_all "Artifacts" "repos/${REPO}/actions/artifacts" "${ARTS[@]}"
else
	echo "⚠️  获取 Artifacts 列表失败，已跳过：$(echo "${ART_IDS}" | head -n 1)"
fi

# ── 历史运行记录（仅已完成的；包含所有 workflow）──
if RUN_IDS="$(gh api --paginate "repos/${REPO}/actions/runs?per_page=100" \
	--jq ".workflow_runs[] | select(.id != ${CUR}) | select(.status == \"completed\") | .id" 2>&1)"; then
	mapfile -t RUNS <<<"${RUN_IDS}"
	[[ -z "${RUNS[0]:-}" ]] && RUNS=()
	echo "🗂️  历史运行：待清理 ${#RUNS[@]} 条"
	((${#RUNS[@]})) && delete_all "运行记录" "repos/${REPO}/actions/runs" "${RUNS[@]}"
else
	echo "⚠️  获取运行列表失败，已跳过：$(echo "${RUN_IDS}" | head -n 1)"
fi

echo "✅ 清理完成（缓存未改动）"
exit 0
