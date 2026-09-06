#!/usr/bin/env bash
# 外部审视评审者入口（设计门/吓人 diff/外援三触发共用）—— D-045
# 评审者链：cmd（Command Code · deepseek-v4-pro，主）→ glm（zcode 无头·glm-5.3，替补）。
# 调用语法（跨评审者稳定，翻译层按 profile 吸收/拒绝，详见 docs/design/d045-*.md）：
#   codex-open exec [--skip-git-repo-check] [-C <path>] "<packet>"   # 无头评审
#   codex-open "<prompt>"                                            # TTY 交互
#   stdin 无参或 '-' 传 prompt；非 TTY 自动补 exec；首参 exec|e|review 同义
# cmd profile：吸收 --sandbox/-C/--skip-git-repo-check；权限放大旗标与未识别
#   旗标 fail-closed（exit 78）。glm profile：zcode 无头（owner 2026-09-06 拍板
#   换替——codex-glm 路由退役后 GLM 无头统一走 zcode；harness 本体保留）。
# fallback：主评审者 precheck 失败或运行非零 → stdout 横幅 + fallback.tsv 落账
#   → 替补重发同参。换主评审者改 DEFAULT_PROFILE 一行。
# tsv 行语义 = fallback 事件（非最终结论；双挂时 glm 的失败 rc 见 stderr）。
# cmd 翻译层：-C 仅吸收 WORKSPACE；--sandbox 仅吸收 read-only 值；其余见 D-045。

set -euo pipefail

WORKSPACE="/home/ypk/fin-core"
DEFAULT_PROFILE="${GATE_PROFILE:-cmd}"   # 环境覆盖：GATE_PROFILE=glm（测试/运维用）

CMD_BIN="$(command -v cmd || true)"
# CMD_MODEL/ZCODE_MODEL 自连接池解析（pool_harness_model，见下）——池禁/悬空/池
# 不可读时为空串，对应 precheck 失败走 fallback/双挂（fail-closed，不猜模型）。
# 版本钉单源=finqa_nodes.yaml 顶层 cmd_version_pin(2026-09-06 收口,双记账废止);
# 读不到 fail-closed——版本钉是安全闸(闭源客户端升级先落替补),不许空值放行。
CMD_VERSION_PIN="$(sed -n 's/^cmd_version_pin:[[:space:]]*"\{0,1\}\([^"[:space:]#]*\)"\{0,1\}.*/\1/p' \
    "$WORKSPACE/config/finqa_nodes.yaml")"
[[ -n "$CMD_VERSION_PIN" ]] || { printf 'codex-open: %s\n' \
    "cmd_version_pin missing/unreadable in config/finqa_nodes.yaml" >&2; exit 78; }

# glm 替补 = zcode 无头·glm-5.3（模型单旋钮 ~/.zcode/cli/config.json，与问询链
# zcode 腿共用；harness 本体 codex/codex-glm 资产保留不删，仅退出评审链）。
# 连接池分层（docs/design/llm-pool-layering.md）：评审者=池条目别名
# （commandcode-pro / zcode），模型与开关自 llm.yaml 解析；池禁/悬空/yaml 异常
# 一律视为该评审者不可用（fail-closed，走 fallback 或双挂 rc 78）。
ZCODE_BINARY="$(command -v zcode || true)"
ZCODE_CONFIG="$HOME/.zcode/cli/config.json"
JQ_BINARY="$(command -v jq || true)"
FIN_PY="$WORKSPACE/.venv/bin/python"

pool_harness_model() {  # $1=别名 → stdout=该连接 model（仅启用时）；非零=禁用/悬空/池不可读
    "$FIN_PY" -c '
import os, sys, yaml
from pathlib import Path
alias = sys.argv[1]
p = Path(os.environ.get("FINQA_POOL_FILE") or "/home/ypk/fin-core/config/llm.yaml")
d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
e = (d.get("models") or {}).get(alias) or {}
if e.get("type") != "harness":
    raise SystemExit(3)
if e.get("managed", "pool") == "pool" and e.get("enabled") is not True:
    raise SystemExit(3)
print(e.get("model") or "")
' "$1" 2>/dev/null
}

CMD_MODEL="$(pool_harness_model commandcode-pro)" || CMD_MODEL=""
ZCODE_MODEL="$(pool_harness_model zcode)" || ZCODE_MODEL=""

FALLBACK_TSV="${XDG_STATE_HOME:-$HOME/.local/state}/fin-analyse/design-gate/fallback.tsv"
DIE_FLAG_RE='^(--yolo|--dangerously-skip-permissions|--tools-all|--tools-enable|--permission-mode)(=.*)?$'

die78() { printf 'codex-open: %s\n' "$*" >&2; exit 78; }

# —— profile precheck（两个评审者启动前都查，替补不可用要提前暴露）——
precheck_cmd() {
    [[ -n "$CMD_BIN" && -x "$CMD_BIN" ]] || return 1
    [[ -n "$CMD_MODEL" ]] || return 1   # 池禁/悬空/池不可读 → 评审者不可用（传导）
    local v
    v="$("$CMD_BIN" --version 2>/dev/null | head -1)" || return 1
    [[ "$v" == "$CMD_VERSION_PIN" ]] || return 1   # 版本钉定：闭源客户端升级先落替补
    local st
    st="$("$CMD_BIN" status 2>/dev/null)" || return 1
    printf '%s' "$st" | grep -qi "authenticated" || return 1
    return 0
}

precheck_glm() {
    [[ -n "$ZCODE_BINARY" && -x "$ZCODE_BINARY" ]] || return 1
    [[ -n "$JQ_BINARY" && -x "$JQ_BINARY" ]] || return 1
    [[ -n "$ZCODE_MODEL" ]] || return 1   # 池禁/悬空/池不可读 → 替补不可用（fail-closed）
    # 模型旋钮防漂移：zcode 配置须与池条目一致（与问询链 zcode 腿共用单旋钮）
    "$JQ_BINARY" -er --arg m "$ZCODE_MODEL" '.model == $m' "$ZCODE_CONFIG" >/dev/null || return 1
    return 0
}

# —— cmd 翻译层（只对 cmd profile；glm 原样透传）——
# 吸收：exec|e|review 首参、--sandbox <v>、-C <path>、--skip-git-repo-check
# 拒绝：权限放大旗标、其余未识别 -- 旗标（fail-closed）；'-' stdin 标记保留
translate_cmd() {
    CMD_ARGS=()
    local a skip_next=0 skip_flag=""
    for a in "$@"; do
        if [[ $skip_next -eq 1 ]]; then
            skip_next=0
            case "$skip_flag" in
                -C) [[ "$a" == "$WORKSPACE" ]] || die78 "cmd profile 的 -C 仅支持 $WORKSPACE（收到: $a）" ;;
                --sandbox) [[ "$a" == "read-only" ]] || die78 "cmd profile 仅吸收 --sandbox read-only（收到: $a）" ;;
            esac
            continue
        fi
        case "$a" in
            exec|e|review) ;;
            --sandbox)
                skip_next=1; skip_flag="--sandbox" ;;
            -C)
                skip_next=1; skip_flag="-C" ;;
            --skip-git-repo-check) ;;
            -) CMD_ARGS+=("$a") ;;
            -*) die78 "cmd profile 未识别旗标（fail-closed，不猜译）: $a" ;;
            *) CMD_ARGS+=("$a") ;;
        esac
    done
}

note() { printf 'codex-open: %s\n' "$*" >&2; }

note_fallback() {  # $1=primary $2=final $3=rc $4=stage
    printf 'codex-open: REVIEWER FALLBACK %s → %s（%s 失败 rc=%s @%s）\n' "$1" "$2" "$1" "$3" "$4" >&2
    mkdir -p "$(dirname "$FALLBACK_TSV")"
    chmod 700 "$(dirname "$FALLBACK_TSV")" 2>/dev/null || true
    printf '%s\t%s\t%s\t%s\t%s\n' "$(date +%s)" "$1" "$2" "$3" "$4" >> "$FALLBACK_TSV"
    chmod 600 "$FALLBACK_TSV" 2>/dev/null || true
}

# —— 非 TTY 自动补 exec / STDIN_NULL（原逻辑保留，两 profile 共用）——
FIRST_ARG="${1:-}"
if [[ ! -t 0 && $FIRST_ARG != exec && $FIRST_ARG != e && $FIRST_ARG != review ]]; then
    set -- exec "$@"
fi
STDIN_NULL=""
if [[ ! -t 0 && -n $FIRST_ARG && $FIRST_ARG != "-" ]]; then
    STDIN_NULL=/dev/null
    for a in "$@"; do
        if [[ $a == "-" ]]; then
            STDIN_NULL=""
            break
        fi
    done
fi
HEADLESS=0
if [[ ${1:-} == exec || ${1:-} == e || ${1:-} == review ]]; then
    HEADLESS=1
    shift
elif [[ ! -t 0 ]]; then
    HEADLESS=1
fi

PRE_CMD=ok
PRE_GLM=ok
if ! precheck_cmd; then PRE_CMD="precheck 失败"; fi
if ! precheck_glm; then PRE_GLM="precheck 失败"; fi

cd "$WORKSPACE"

PRIMARY="$DEFAULT_PROFILE"
if [[ $PRIMARY == cmd ]]; then SECONDARY=glm; else SECONDARY=cmd; fi

note "reviewer=${PRIMARY} ($(
    [[ $PRIMARY == cmd ]] && echo "${CMD_MODEL:-池未解析}" || echo "${ZCODE_MODEL:-池未解析}"
)) fallback=${SECONDARY}"

# —— TTY 交互：仅 precheck 阶段可 fallback，运行期不劫持 TUI ——
if [[ $HEADLESS -eq 0 ]]; then
    if [[ $PRIMARY == cmd ]]; then
        if [[ $PRE_CMD == ok ]]; then
            translate_cmd "$@"
            exec "$CMD_BIN" --skip-onboarding --no-auto-update --permission-mode plan \
                --effort max -m "$CMD_MODEL" "${CMD_ARGS[@]}"
        fi
        if [[ $PRE_GLM == ok ]]; then
            note_fallback "cmd" "glm" "pre" "tui"
            exec "$ZCODE_BINARY" "$@"
        fi
        die78 "两个评审者都不可用（cmd: $PRE_CMD / glm: $PRE_GLM）"
    else
        [[ $PRE_GLM == ok ]] || die78 "glm precheck 失败（$PRE_GLM）"
        exec "$ZCODE_BINARY" "$@"
    fi
fi

# —— 无头评审：主评审者输出先捕获，成功才透传；失败丢弃半份输出（留存追溯）
#    并落 fallback.tsv 后以替补重发同参 ——
# BUG-055：cmd -p 在工具权限被拒时以 rc=0/subtype=success 结束（result 行
# stopReason=permission_denied），stdout 止于工具调用前；rc 不可信。cmd 路径
# 固定 --output-format json，只认 stopReason=end_turn 且 finalText 非空，
# 透传面=finalText；否则按失败处理走替补。glm 路径（codex exec）文本输出不变。
run_cmd_capture() {
    local out="$1"
    shift
    translate_cmd "$@"
    local cli_rc=0
    if [[ -n $STDIN_NULL ]]; then
        "$CMD_BIN" --skip-onboarding --no-auto-update --no-session -p \
            --output-format json --effort max \
            -m "$CMD_MODEL" "${CMD_ARGS[@]}" < "$STDIN_NULL" > "$out" || cli_rc=$?
    else
        "$CMD_BIN" --skip-onboarding --no-auto-update --no-session -p \
            --output-format json --effort max \
            -m "$CMD_MODEL" "${CMD_ARGS[@]}" > "$out" || cli_rc=$?
    fi
    # 完整性判定先于 rc 分支：end_turn 且 finalText 非空才算成功；rc≠0 或
    # permission_denied/截断/坏输出一律走失败路径（含 rc=0 的假成功形态）。
    if [[ $cli_rc -eq 0 ]] \
        && "$JQ_BINARY" -ers \
            '[.[] | select(.type=="result")][-1] // empty
             | select(.stopReason=="end_turn" and (.finalText|length>0))
             | .finalText' \
            "$out" > "${out}.text"; then
        mv "${out}.text" "$out"
        return 0
    fi
    # 失败路径：提取 result 行 finalText 供 stderr 追溯，避免 NDJSON 原样灌入
    # stderr（大评审可达数 MB）；提取失败留原始尾部 2000B。
    if ! "$JQ_BINARY" -ers \
        '[.[] | select(.type=="result")][-1] // empty | .finalText // ""' \
        "$out" 2>/dev/null > "${out}.text"; then
        tail -c 2000 "$out" > "${out}.text"
    fi
    mv "${out}.text" "$out"
    return $(( cli_rc != 0 ? cli_rc : 3 ))
}

run_glm_capture() {
    local out="$1"
    shift
    # zcode 凭据注入（与 finqa_chain.py _launch_env 同源同语义：llm.env GLM_API_KEY
    # → ZHIPU_API_KEY；缺失时 zcode 自己失败，走 fail-visible）
    local glm_key
    glm_key="$(grep -E '^GLM_API_KEY=' "$HOME/.config/fin-analyse/llm.env" | cut -d= -f2-)" || true
    [[ -n "$glm_key" ]] && export ZHIPU_API_KEY="$glm_key"
    unset glm_key
    # zcode 无头：提示词只收参数（无 stdin 提示词形态，2026-09-06 实测）——
    # '-' stdin 形态先缓冲为单参数；args 形态原样透传。
    local pkt=""
    if [[ -z $STDIN_NULL ]]; then
        pkt=$(cat)
    fi
    if [[ -n $STDIN_NULL ]]; then
        "$ZCODE_BINARY" -p "$@" < "$STDIN_NULL" > "$out"
    else
        "$ZCODE_BINARY" -p "$pkt" > "$out"
    fi
}

PRIMARY_NAME="$DEFAULT_PROFILE"
SECONDARY_NAME="$SECONDARY"
TMP_PRIMARY="$(mktemp /tmp/codex-open-primary.XXXXXX)"
trap 'rm -f "$TMP_PRIMARY" "$TMP_PRIMARY.secondary" "$TMP_PRIMARY.text" 2>/dev/null' EXIT

PRIMARY_RUNNER="run_${PRIMARY_NAME}_capture"
SECONDARY_RUNNER="run_${SECONDARY_NAME}_capture"

note "headless launch: $PRIMARY_NAME"
if [[ $PRE_CMD == ok ]]; then
    if "$PRIMARY_RUNNER" "$TMP_PRIMARY" "$@"; then
        cat "$TMP_PRIMARY"
        exit 0
    else
        RC_PRIMARY=$?
    fi
else
    RC_PRIMARY="pre"
fi

if [[ $RC_PRIMARY != "pre" ]]; then
    # 主评审者失败：半份输出转 stderr 留存（前缀化，不混入正式产物）
    {
        printf -- '----- [%s partial, rc=%s] -----\n' "$PRIMARY_NAME" "$RC_PRIMARY"
        sed 's/^/[primary] /' "$TMP_PRIMARY"
    } >&2
fi

if [[ $PRE_GLM != ok ]]; then
    die78 "主评审者不可用（cmd: $PRE_CMD）且 glm precheck 失败（$PRE_GLM），无替补可用"
fi

note_fallback "$PRIMARY_NAME" "glm" "$RC_PRIMARY" "run"
printf '⚠ REVIEWER FALLBACK: %s → glm（主评审者 rc=%s，半份输出已转 stderr）\n' \
    "$PRIMARY_NAME" "$RC_PRIMARY"

if run_glm_capture "$TMP_PRIMARY.secondary" "$@"; then
    cat "$TMP_PRIMARY.secondary"
    exit 0
else
    RC_SECONDARY=$?
fi
if [[ $RC_SECONDARY != "pre" ]]; then
    {
        printf -- '----- [glm partial, rc=%s] -----\n' "$RC_SECONDARY"
        sed 's/^/[secondary] /' "$TMP_PRIMARY.secondary"
    } >&2
fi
die78 "两个评审者都失败（$PRIMARY_NAME rc=$RC_PRIMARY / glm rc=$RC_SECONDARY）"
