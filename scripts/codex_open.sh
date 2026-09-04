#!/bin/bash

set -euo pipefail

WORKSPACE="/home/ypk/fin-core"
CODEX_GLM_AUTH_FILE="/home/ypk/fin-data/codex-routes/codex-glm/auth.json"
# 模型目录：复用问询链 codex-glm 路由的目录（glm-5.3 带 instructions_template
# 与 effort 枚举 low/high/max，2026-09-04 验证）。
MODEL_CATALOG="/home/ypk/fin-data/codex-routes/codex-glm/models.json"
CODEX_BINARY="$(command -v codex || true)"
JQ_BINARY="$(command -v jq || true)"

if [[ -z "$CODEX_BINARY" || ! -x "$CODEX_BINARY" ]]; then
    printf 'codex-open: native Codex is unavailable\n' >&2
    exit 78
fi
if [[ -z "$JQ_BINARY" || ! -x "$JQ_BINARY" ]]; then
    printf 'codex-open: jq is unavailable\n' >&2
    exit 78
fi
if [[ -L "$CODEX_GLM_AUTH_FILE" || ! -f "$CODEX_GLM_AUTH_FILE" ]]; then
    printf 'codex-open: codex-glm credential file is unavailable\n' >&2
    exit 78
fi
if [[ "$(stat -c '%u:%a:%h' "$CODEX_GLM_AUTH_FILE")" != "$(id -u):600:1" ]]; then
    printf 'codex-open: credential file must be owner-only\n' >&2
    exit 78
fi
if [[ ! -r "$MODEL_CATALOG" ]]; then
    printf 'codex-open: Codex model catalog is unavailable\n' >&2
    exit 78
fi

CODEX_GLM_KEY_VALUE="$($JQ_BINARY -er \
    '.glm_api_key | strings | select(length > 0)' \
    "$CODEX_GLM_AUTH_FILE")" || {
    printf 'codex-open: codex-glm credential is invalid\n' >&2
    exit 78
}
export CODEX_GLM_API_KEY="$CODEX_GLM_KEY_VALUE"
unset CODEX_GLM_KEY_VALUE

# 非 TTY 调用方（agent 会话/管道）没有交互式 TUI，缺 exec 子命令会直接
# "Error: stdin is not a terminal" 退出——自动补 exec，人在终端手跑不受影响。
# 注意：STDIN_NULL 必须按原始首参判定（在 exec 前置之前），否则补上的
# exec 子命令会让 `-`/无参的 stdin 传 prompt 用法被误判为位置传参。
FIRST_ARG="${1:-}"
if [[ ! -t 0 && $FIRST_ARG != exec && $FIRST_ARG != e && $FIRST_ARG != review ]]; then
    set -- exec "$@"
fi
# 非 TTY 且 prompt 走位置参数时，stdin 往往是永不 EOF 的管道（agent 后台
# shell），codex exec 会永久阻塞在 "Reading additional input from stdin..."
# ——显式喂 EOF。显式用 stdin 传 prompt（无参数或 -）的用法不受影响；
# `-` 可能不在首参（如 `exec --json --output-last-message F -`），
# 故任一参数出现 `-` 即视为 stdin 传 prompt，必须透传。
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

cd "$WORKSPACE"
run_codex() {
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
}
if [[ -n $STDIN_NULL ]]; then
    run_codex "$@" < /dev/null
else
    run_codex "$@"
fi
