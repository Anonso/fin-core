---
name: manage-zsxq-capture
description: Operate the ZSXQ six-slot capture topology — change a capture time point (改时点/换时间/调整爬取时间/采集调度) or run an urgent manual capture round (紧急爬取/手动抓一次/有紧急文章要爬). Covers the single-source constant, both derived surfaces (Windows Task + WSL poller timer), the owner-side Windows command, four-state summary watching, and poller wakeup. Not for capture-content bugs (see BUGS.md) or Windows Task creation from scratch.
---

# Manage ZSXQ Capture（时点变更 · 紧急手动抓取）

不变量与设计事实看 `docs/design/zsxq-capture.md`；本 skill 只管操作程序。改调度属家规 2
副作用类：本 skill 只在被 owner 明确要求时执行。

## 拓扑（动手前必读）

- **单一事实源**：`_EXPECTED_TIMES`（六时点，当前 08:45/12:20/14:40/15:30/18:00/20:20，
  以文件为准）在**本仓** `~/fin-core/scripts/zsxq_windows_incremental_scheduler.py`
  （2026-09-05 自老仓迁入，NOW #32；老仓副本自此只是历史回滚资产）。它同时派生三面：
  ① Windows Task `FIN-ZSXQ-Incremental` 触发器；② WSL poller timer 的 OnCalendar 窗口
  （每时点后 30 分钟）；③ `verify-task-xml` 拒漂移。**三面必须成套改，禁手改 timer 文件。**
- Windows Task 动作 = `powershell … -File C:\Users\22873\fin-zsxq-capture\run-capture-and-import.ps1`
  （抓取+发布 v4 summary 四态链）。WSL 消费 = `fin-zsxq-capture-poller.service`（oneshot，
  只在六窗口内醒；fin-core 钩子每次提交只重刷其 Description 的 SHA，不动 OnCalendar）。
- 变更备份惯例：`/mnt/c/Users/22873/fin-zsxq-capture/backup-YYYYMMDD/`（wrapper + cjs +
  当前任务 XML）。

## 操作 A：改时点（如 13:50→12:20，owner 2026-09-04 实战过一遍）

1. **改事实源并提交本仓**：`_EXPECTED_TIMES` 里改那一行；`git -C ~/fin-core commit`。
2. **重渲染 WSL timer**（用 fin-core 当前 HEAD）：
   ```bash
   cd ~/fin-core && python3 scripts/zsxq_windows_incremental_scheduler.py \
     render-poller-timer --release-sha $(git -C ~/fin-core rev-parse HEAD) \
     > /tmp/fin-render/poller.timer
   install -m 644 /tmp/fin-render/poller.timer ~/.config/systemd/user/fin-zsxq-capture-poller.timer
   systemctl --user daemon-reload && systemctl --user restart fin-zsxq-capture-poller.timer
   systemctl --user list-timers fin-zsxq-capture-poller.timer --no-pager   # 核对新窗口
   ```
3. **备份 + 生成 Windows 任务 XML**：
   ```bash
   cd /mnt/c/Users/22873/fin-zsxq-capture && mkdir -p backup-YYYYMMDD
   cp -p run-capture-and-import.ps1 capture-zsxq.cjs backup-YYYYMMDD/
   iconv -f UTF-16LE -t UTF-8 /mnt/c/Windows/System32/Tasks/FIN-ZSXQ-Incremental \
     > backup-YYYYMMDD/task-current.xml
   ```
   在副本上把旧时点 `StartBoundary` 替换为新时点（`T13:50:00`→`T12:20:00`），存成
   `task-<HHMM>.xml`，**UTF-16LE + BOM、声明保持 encoding="UTF-16"**（见坑 3）。
4. **Windows 侧重注册（owner 动作，WSL 侧无权）**：给 owner 一条命令贴进其
   PowerShell 窗口（普通窗口即可；被拒再管理员）：
   ```powershell
   Register-ScheduledTask -TaskName 'FIN-ZSXQ-Incremental' -Xml (Get-Content 'C:\Users\22873\fin-zsxq-capture\backup-YYYYMMDD\task-HHMM.xml' -Raw) -Force
   ```
   （`-Force` 必带，否则 0x800700b7 已存在被拒——2026-09-04 实证。）
5. **双端核验**：
   ```bash
   grep -o "T[0-9:]*:00+08:00" /mnt/c/Windows/System32/Tasks/FIN-ZSXQ-Incremental | sort
   cd ~/fin-core && python3 scripts/zsxq_windows_incremental_scheduler.py verify-task-xml \
     --task-xml /mnt/c/Windows/System32/Tasks/FIN-ZSXQ-Incremental \
     --wrapper-path 'C:\Users\22873\fin-zsxq-capture\run-capture-and-import.ps1' \
     --user-sid 'S-1-5-21-1547283755-2148356556-3188188356-1001'
   ```
   时点六元组对上即生效。verifier 若只报 `task_enabled_invalid`：schtasks/注册库通道会剥
   `<Enabled>` 元素（缺省=启用，功能无损）——用第 4 步的 Register 通道写入即过。
6. fin-core 设计页 `docs/design/zsxq-capture.md` 不变量 2 的时点清单同步 + commit。

## 操作 B：紧急手动抓取（与自动触发同构）

1. **触发**（二选一）：
   - 喊 owner 在 Windows 侧：`Start-ScheduledTask -TaskName 'FIN-ZSXQ-Incremental'`
   - CC 从 WSL 试（interop 通时）：`schtasks.exe /Run /TN "FIN-ZSXQ-Incremental"`
2. **盯 Windows 侧四态 summary**（runs 目录最新 runId）：
   ```bash
   ls -t /mnt/c/Users/22873/AppData/Local/fin-analyse/zsxq-scheduler/runs/ | head -1
   ```
   盯 `summary.json`：`capture_pending/75`（启动即发布的过渡态）→ 终态
   `capture_exit_code:0, capture_ready:true, artifact_published:true`（全程约 1–3 分钟）。
   summary 的 `trigger` 字段硬编码 "schedule"，不区分手动/自动——产物同构。
3. **唤醒 WSL 消费**（关键：窗口外的产物会躺到下个时点窗口）：
   ```bash
   systemctl --user start fin-zsxq-capture-poller.service   # 阻塞至完成，含深化最长 ~20min
   journalctl --user -u fin-zsxq-capture-poller.service --since "-5 min" --no-pager | tail
   ```
   `exit 2`（深读排空跨窗口）与 `75`（coalesced）是设计内收口，不是故障；判真故障看
   run payload 的 `status`/`changed_count`。
4. G manifest 由 capture 链 publish 自动重生，无需手动动作。

## 环境坑（全为 2026-09-04 实证）

1. **WSL→Windows interop 时好时坏**（vsock accept 110）：连续调用可能连环失败。姿势：
   探针循环 `until powershell.exe -NoProfile -Command "echo probe-ok" …; do sleep 15; done`
   通了再干正事；持续不通就把 Windows 侧命令交给 owner。
2. **WSL 侧无权写任务**：Set-ScheduledTask / schtasks /Create 均可能 0x80070005 拒绝
   访问——注册/改任务一律走 owner 的 PowerShell 窗口（owner 交互窗口可成功）。
3. **schtasks XML 编码**：导出注册库文件是 UTF-16LE+BOM；转 UTF-8 会先后炸
   `(1,2) 文档语法`（BOM 残留）和 `(1,40) 无法切换编码`（声明与字节不符）。最终可用
   形态 = UTF-16LE + BOM + 声明 UTF-16。PowerShell `Register-ScheduledTask -Xml` 对
   UTF-8/UTF-16 都吃得下，优先走它。
4. **schtasks 输出是 GBK**：中文乱码不影响功能，判读用英文关键词或先 iconv。
5. **UNC cwd 警告**：从 WSL 调 Windows exe 时 cwd 是 `\\wsl.localhost\...` 会告警，
   先 `cd /mnt/c/...` 再调。
