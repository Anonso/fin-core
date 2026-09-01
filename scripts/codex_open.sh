#!/bin/bash

set -euo pipefail

WORKSPACE="/home/ypk/fin-core"
KEY_FILE="/tmp/open.txt"
# 项目自定义模型目录（2026-09-01）：deepseek-v4-pro 声明 supports_search_tool=false，
# 使 zhipu-web MCP（webSearchPrime/webReader）直接注入会话；原生 hosted search
# 由 provider 能力独立控制，不受影响，仍作兜底。勿改回全局目录。
MODEL_CATALOG="/home/ypk/fin-core/.codex/models.json"
CODEX_BINARY="$(command -v codex || true)"

if [[ -z "$CODEX_BINARY" || ! -x "$CODEX_BINARY" ]]; then
    printf 'codex-open: native Codex is unavailable\n' >&2
    exit 78
fi
if [[ -L "$KEY_FILE" || ! -f "$KEY_FILE" || ! -r "$KEY_FILE" ]]; then
    printf 'codex-open: OpenCode Go key file /tmp/open.txt is unavailable\n' >&2
    exit 78
fi
if [[ "$(stat -c '%u:%a:%h' "$KEY_FILE")" != "$(id -u):600:1" ]]; then
    printf 'codex-open: key file /tmp/open.txt must be owner-only (0600)\n' >&2
    exit 78
fi
if [[ ! -r "$MODEL_CATALOG" ]]; then
    printf 'codex-open: Codex model catalog is unavailable\n' >&2
    exit 78
fi

OPENCODE_GO_KEY_VALUE="$(tr -d '[:space:]' < "$KEY_FILE")" || {
    printf 'codex-open: OpenCode Go key read failed\n' >&2
    exit 78
}
if [[ -z "$OPENCODE_GO_KEY_VALUE" ]]; then
    printf 'codex-open: OpenCode Go key is empty\n' >&2
    exit 78
fi
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
