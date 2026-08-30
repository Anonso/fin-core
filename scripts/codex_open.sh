#!/bin/bash

set -euo pipefail

WORKSPACE="/home/ypk/fin-core"
AUTH_FILE="${XDG_DATA_HOME:-${HOME:?}/.local/share}/opencode/auth.json"
MODEL_CATALOG="${HOME:?}/.codex/models.json"
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
if [[ -L "$AUTH_FILE" || ! -f "$AUTH_FILE" ]]; then
    printf 'codex-open: OpenCode Go credential file is unavailable\n' >&2
    exit 78
fi
if [[ "$(stat -c '%u:%a:%h' "$AUTH_FILE")" != "$(id -u):600:1" ]]; then
    printf 'codex-open: credential file must be owner-only\n' >&2
    exit 78
fi
if [[ ! -r "$MODEL_CATALOG" ]]; then
    printf 'codex-open: Codex model catalog is unavailable\n' >&2
    exit 78
fi

OPENCODE_GO_KEY_VALUE="$($JQ_BINARY -er \
    '.["opencode-go"] | select(.type == "api") | .key | strings | select(length > 0)' \
    "$AUTH_FILE")" || {
    printf 'codex-open: OpenCode Go credential is invalid\n' >&2
    exit 78
}
export OPENCODE_GO_API_KEY="$OPENCODE_GO_KEY_VALUE"
unset OPENCODE_GO_KEY_VALUE

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
        -c model_provider=opencode_go \
        -c 'model_providers.opencode_go.name=OpenCode Go' \
        -c model_providers.opencode_go.base_url=https://opencode.ai/zen/go/v1 \
        -c model_providers.opencode_go.env_key=OPENCODE_GO_API_KEY \
        -c model_providers.opencode_go.wire_api=responses \
        -c "model_catalog_json=${MODEL_CATALOG}" \
        -c model_reasoning_effort=max \
        -m deepseek-v4-pro \
        --sandbox read-only \
        "$@"
}
if [[ -n $STDIN_NULL ]]; then
    run_codex "$@" < /dev/null
else
    run_codex "$@"
fi
