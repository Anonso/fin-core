#!/usr/bin/env bash
# 外部审视评审者入口（设计门/吓人 diff/外援三触发共用）—— D-045
# 评审者链：cmd（Command Code · deepseek-v4-pro，主）→ glm（codex-glm·glm-5.3，替补）。
# 调用语法（跨评审者稳定，翻译层按 profile 吸收/拒绝，详见 docs/design/d045-*.md）：
#   codex-open exec [--skip-git-repo-check] [-C <path>] "<packet>"   # 无头评审
#   codex-open "<prompt>"                                            # TTY 交互
#   stdin 无参或 '-' 传 prompt；非 TTY 自动补 exec；首参 exec|e|review 同义
# cmd profile：吸收 --sandbox/-C/--skip-git-repo-check；权限放大旗标与未识别
#   旗标 fail-closed（exit 78）。glm profile：codex 参数原样透传（现行为）。
# fallback：主评审者 precheck 失败或运行非零 → stdout 横幅 + fallback.tsv 落账
#   → 替补重发同参。换主评审者改 DEFAULT_PROFILE 一行。
# tsv 行语义 = fallback 事件（非最终结论；双挂时 glm 的失败 rc 见 stderr）。
# cmd 翻译层：-C 仅吸收 WORKSPACE；--sandbox 仅吸收 read-only 值；其余见 D-045。

set -euo pipefail

WORKSPACE="/home/ypk/fin-core"
DEFAULT_PROFILE="${GATE_PROFILE:-cmd}"   # 环境覆盖：GATE_PROFILE=glm（测试/运维用）

CMD_BIN="$(command -v cmd || true)"
CMD_MODEL="deepseek/deepseek-v4-pro"
CMD_VERSION_PIN="1.49.1"

CODEX_GLM_AUTH_FILE="/home/ypk/fin-data/codex-routes/codex-glm/auth.json"
# 模型目录：复用问询链 codex-glm 路由的目录（glm-5.3 带 instructions_template
# 与 effort 枚举 low/high/max，2026-09-04 验证）。
MODEL_CATALOG="/home/ypk/fin-data/codex-routes/codex-glm/models.json"
CODEX_BINARY="$(command -v codex || true)"
JQ_BINARY="$(command -v jq || true)"

FALLBACK_TSV="${XDG_STATE_HOME:-$HOME/.local/state}/fin-analyse/design-gate/fallback.tsv"
DIE_FLAG_RE='^(--yolo|--dangerously-skip-permissions|--tools-all|--tools-enable|--permission-mode)(=.*)?$'

die78() { printf 'codex-open: %s\n' "$*" >&2; exit 78; }

# —— profile precheck（两个评审者启动前都查，替补不可用要提前暴露）——
precheck_cmd() {
    [[ -n "$CMD_BIN" && -x "$CMD_BIN" ]] || return 1
    local v
    v="$("$CMD_BIN" --version 2>/dev/null | head -1)" || return 1
    [[ "$v" == "$CMD_VERSION_PIN" ]] || return 1   # 版本钉定：闭源客户端升级先落替补
    local st
    st="$("$CMD_BIN" status 2>/dev/null)" || return 1
    printf '%s' "$st" | grep -qi "authenticated" || return 1
    return 0
}

precheck_glm() {
    [[ -n "$CODEX_BINARY" && -x "$CODEX_BINARY" ]] || return 1
    [[ -n "$JQ_BINARY" && -x "$JQ_BINARY" ]] || return 1
    if [[ -L "$CODEX_GLM_AUTH_FILE" || ! -f "$CODEX_GLM_AUTH_FILE" ]]; then
        return 1
    fi
    if [[ "$(stat -c '%u:%a:%h' "$CODEX_GLM_AUTH_FILE")" != "$(id -u):600:1" ]]; then
        return 1
    fi
    [[ -r "$MODEL_CATALOG" ]] || return 1
    "$JQ_BINARY" -er '.glm_api_key | strings | select(length > 0)' \
        "$CODEX_GLM_AUTH_FILE" >/dev/null || return 1
    return 0
}

export_cmd_glm_key() {
    CODEX_GLM_KEY_VALUE="$($JQ_BINARY -er \
        '.glm_api_key | strings | select(length > 0)' \
        "$CODEX_GLM_AUTH_FILE")" || die78 "codex-glm credential is invalid"
    export CODEX_GLM_API_KEY="$CODEX_GLM_KEY_VALUE"
    unset CODEX_GLM_KEY_VALUE
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
    [[ $PRIMARY == cmd ]] && echo "$CMD_MODEL" || echo "glm-5.3"
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
            export_cmd_glm_key
            exec "$CODEX_BINARY" \
                -c model_provider=codex_glm_review \
                -c 'model_providers.codex_glm_review.name=GLM Review' \
                -c model_providers.codex_glm_review.base_url=https://open.bigmodel.cn/api/v1 \
                -c model_providers.codex_glm_review.env_key=CODEX_GLM_API_KEY \
                -c model_providers.codex_glm_review.wire_api=responses \
                -c "model_catalog_json=${MODEL_CATALOG}" \
                -c model_reasoning_effort=max \
                -m glm-5.3 \
                --sandbox read-only \
                "$@"
        fi
        die78 "两个评审者都不可用（cmd: $PRE_CMD / glm: $PRE_GLM）"
    else
        [[ $PRE_GLM == ok ]] || die78 "glm precheck 失败（$PRE_GLM）"
        export_cmd_glm_key
        exec "$CODEX_BINARY" \
            -c model_provider=codex_glm_review \
            -c 'model_providers.codex_glm_review.name=GLM Review' \
            -c model_providers.codex_glm_review.base_url=https://open.bigmodel.cn/api/v1 \
            -c model_providers.codex_glm_review.env_key=CODEX_GLM_API_KEY \
            -c model_providers.codex_glm_review.wire_api=responses \
            -c "model_catalog_json=${MODEL_CATALOG}" \
            -c model_reasoning_effort=max \
            -m glm-5.3 \
            --sandbox read-only \
            "$@"
    fi
fi

# —— 无头评审：主评审者输出先捕获，成功才透传；失败丢弃半份输出（留存追溯）
#    并落 fallback.tsv 后以替补重发同参 ——
run_cmd_capture() {
    local out="$1"
    shift
    translate_cmd "$@"
    if [[ -n $STDIN_NULL ]]; then
        "$CMD_BIN" --skip-onboarding --no-auto-update --no-session -p --effort max \
            -m "$CMD_MODEL" "${CMD_ARGS[@]}" < "$STDIN_NULL" > "$out"
    else
        "$CMD_BIN" --skip-onboarding --no-auto-update --no-session -p --effort max \
            -m "$CMD_MODEL" "${CMD_ARGS[@]}" > "$out"
    fi
}

run_glm_capture() {
    local out="$1"
    shift
    export_cmd_glm_key
    if [[ -n $STDIN_NULL ]]; then
        "$CODEX_BINARY" exec \
            -c model_provider=codex_glm_review \
            -c 'model_providers.codex_glm_review.name=GLM Review' \
            -c model_providers.codex_glm_review.base_url=https://open.bigmodel.cn/api/v1 \
            -c model_providers.codex_glm_review.env_key=CODEX_GLM_API_KEY \
            -c model_providers.codex_glm_review.wire_api=responses \
            -c "model_catalog_json=${MODEL_CATALOG}" \
            -c model_reasoning_effort=max \
            -m glm-5.3 \
            --sandbox read-only \
            "$@" < "$STDIN_NULL" > "$out"
    else
        "$CODEX_BINARY" exec \
            -c model_provider=codex_glm_review \
            -c 'model_providers.codex_glm_review.name=GLM Review' \
            -c model_providers.codex_glm_review.base_url=https://open.bigmodel.cn/api/v1 \
            -c model_providers.codex_glm_review.env_key=CODEX_GLM_API_KEY \
            -c model_providers.codex_glm_review.wire_api=responses \
            -c "model_catalog_json=${MODEL_CATALOG}" \
            -c model_reasoning_effort=max \
            -m glm-5.3 \
            --sandbox read-only \
            "$@" > "$out"
    fi
}

PRIMARY_NAME="$DEFAULT_PROFILE"
SECONDARY_NAME="$SECONDARY"
TMP_PRIMARY="$(mktemp /tmp/codex-open-primary.XXXXXX)"
trap 'rm -f "$TMP_PRIMARY" "$TMP_PRIMARY.secondary" 2>/dev/null' EXIT

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
