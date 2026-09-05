#!/usr/bin/env bash
# 人格修订回归探针（persona-governance-v1 §2，设计门 a0d885d）
# 用法：scripts/persona_regression.sh   （无参数；每次人格 r 修订后、收口前跑一轮）
# 腿：cmd·deepseek-v4-flash（最弱腿=最严苛服从性测试；effort max）。
#     版本钉见 codex_open.sh CMD_VERSION_PIN——两处须同步升版（互指注释）。
# 必须在 consult-agent 工作区起腿（cwd=目录即身份；不在该 cwd 人格不加载=全题空转），
# 本脚本自行 cd 并校验。隔离：答案/台账只落 $STATE（0700，不入 git、不入 finq）；
# -m 逐次指定，生产默认配置零改动。
# 判定口径：PASS/FAIL/WARN 是网不是闸——FAIL/WARN 人工看摘录裁决后方可收口
# （设计门 S1：grep 防漏不防滥，不承诺自动化八股检测）。
# 已知预期 FAIL：全题首行元叙述检查（flash 腿泄漏在案，BUG 候选待立案）——
# 该项作缺陷回归跟踪位，修复后翻绿。
set -u
CONSULT="$HOME/fin-data/consult-agent"
CMD_BIN="${CMD_BIN:-$HOME/.local/bin/cmd}"
CMD_MODEL="deepseek/deepseek-v4-flash"
STATE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/fin-analyse/persona-regression"
TS="$(date +%Y%m%d-%H%M%S)"
STATE="$STATE_ROOT/$TS"
mkdir -p "$STATE" && chmod 700 "$STATE"

# 固定题集（半年一换；日期锚定题为 2026-09 版，换题时同步换锚）
QUESTIONS=(
  "中恒电气2026年中报的毛利率和经营现金流情况怎么样？直接给数字和你的判断。"
  "当前A股大盘和科技主线的形势分析：宏观到中观到微观的传导现在走到哪一步了？接下来两周最可能证伪我当前判断的信号是什么？"
  "复盘一下9月4日浪潮信息跌停：这是什么性质的异动，后面两周怎么看？"
  "中恒电气37.44（9月4日收盘价）现在合适买入吗？"
  "结合老师最近的框架和观点，分析我持仓组合现在的整体状态和主要风险；另外如果我当前的整体判断错了，最可能错在哪里？"
  "中恒电气最新的研报评分是多少？"
)
# 预注册判据锚（每轮随台账落一份，评审 S1-P3）
cat > "$STATE/checks.md" << 'EOF'
# 回归判据锚（grep 级；FAIL/WARN 人工裁决）
q1-fin      PASS: 单源标注词(单一来源|单一媒体口径|未核验)或多源词(双源|两源|多源) >=1
q2-situation PASS: (证伪|失效线) >=1
q3-event    三态: 四选一词(价值事件|情绪波动|真因不明)命中=PASS；引G判定(老师|锐评|G 层|G层)=PASS(豁免)；皆无=FAIL
q4-action   PASS: 位置档词(带上悬空|带下|带中|带上|右侧区) >=1 且 (失效线) >=1 且 (净资产|账户百) >=1
q5-portfolio PASS: (持仓|仓位) >=1 且 (把握|失效) >=1
q6-negative WARN(八股化): (价值事件|情绪波动|真因不明|带上悬空|带下|失效线) 命中数 >0
all-leak    预期FAIL跟踪位: CU-[0-9]=0 且 首行(材料齐了|数据齐了|分析完成)=0
EOF

cd "$CONSULT" || { echo "FATAL: consult-agent 工作区不存在" >&2; exit 2; }
[[ -f CLAUDE.md && -f AGENTS.md ]] || { echo "FATAL: CLAUDE.md/AGENTS.md 缺失，不在 consult-agent 工作区？" >&2; exit 2; }
V="$("$CMD_BIN" --version 2>/dev/null | head -1)"
echo "cmd version: $V (须与 codex_open.sh CMD_VERSION_PIN 同步升版)"

N=${#QUESTIONS[@]}
for ((i=1; i<=N; i++)); do
  q="${QUESTIONS[$((i-1))]}"
  for attempt in 1 2; do
    timeout 900 "$CMD_BIN" --skip-onboarding --no-auto-update --no-session \
      --effort max -p -m "$CMD_MODEL" "$q" \
      > "$STATE/q${i}.md" 2> "$STATE/q${i}.err"
    rc=$?
    [[ $rc -eq 0 ]] && break
    echo "q${i} attempt${attempt} rc=$rc（重试一次：cmd 偶发 Connection reset）" >> "$STATE/runner.log"
  done
  echo "$rc" > "$STATE/q${i}.rc"
  printf '{"q":%d,"rc":%s}\n' "$i" "$rc" >> "$STATE/meta.jsonl"
  echo "done q$i"
done

# ---- 判定 ----
report="$STATE/report.txt"
: > "$report"
verdict() { printf '%-14s %-12s %s\n' "$1" "$2" "$3" >> "$report"; }
verdict ITEM RESULT DETAIL
g() { grep -Ec "$1" "$STATE/$2" 2>/dev/null || true; }

c=$(g "单一来源|单一媒体口径|未核验|双源|两源|多源" q1.md)
[[ "${c:-0}" -ge 1 ]] && verdict q1-fin PASS "标注/多源词=$c" || verdict q1-fin FAIL "无核验标注词"
c=$(g "证伪|失效线" q2.md)
[[ "${c:-0}" -ge 1 ]] && verdict q2-situation PASS "证伪/失效线=$c" || verdict q2-situation FAIL "无证伪/失效线"
c=$(g "价值事件|情绪波动|真因不明|混合" q3.md)
g2=$(g "老师|锐评|G 层|G层" q3.md)
if [[ "${c:-0}" -ge 1 ]]; then verdict q3-event PASS "四选一词=$c"
elif [[ "${g2:-0}" -ge 1 ]]; then verdict q3-event "PASS(豁免)" "引G判定=$g2"
else verdict q3-event FAIL "无四选一且无G判定"; fi
c1=$(g "带外悬空|带上悬空|带下|带中|带上|右侧区|悬空" q4.md); c2=$(g "失效线" q4.md); c3=$(g "净资产|总资产|账户百" q4.md)
if [[ "${c1:-0}" -ge 1 && "${c2:-0}" -ge 1 && "${c3:-0}" -ge 1 ]]; then
  verdict q4-action PASS "位置档=$c1 失效线=$c2 账户%=$c3"
else verdict q4-action FAIL "位置档=$c1 失效线=$c2 账户%=$c3"; fi
c1=$(g "持仓|仓位" q5.md); c2=$(g "把握|失效" q5.md)
[[ "${c1:-0}" -ge 1 && "${c2:-0}" -ge 1 ]] && verdict q5-portfolio PASS "对表=$c1 把握/失效=$c2" || verdict q5-portfolio FAIL "对表=$c1 把握/失效=$c2"
c=$(g "价值事件|情绪波动|真因不明|混合|带外悬空|带上悬空|带下|失效线" q6.md)
[[ "${c:-0}" -eq 0 ]] && verdict q6-negative PASS "窄题零格式词" || verdict q6-negative WARN "格式词=$c（八股化信号，人工裁决）"
# 全题泄漏跟踪位
cu=0; first=0
for f in "$STATE"/q*.md; do
  n=$(grep -Ec 'CU-[0-9]' "$f" 2>/dev/null || true); cu=$((cu+n))
  head -1 "$f" | grep -Eq '材料齐了|数据齐了|分析完成' && first=$((first+1))
done
[[ $cu -eq 0 && $first -eq 0 ]] && verdict all-leak PASS "内部ID=$cu 首行元叙述=$first" || verdict all-leak "FAIL(预期跟踪位)" "内部ID=$cu 首行元叙述=$first"

echo "" >> "$report"
anyfail=$(grep -c " FAIL" "$report" || true)
echo "RESULT: $anyfail FAIL（预期跟踪位除外须人工裁决；WARN 同）" >> "$report"
echo "台账: $STATE"
cat "$report"
exit 0
