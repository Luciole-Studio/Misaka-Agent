#!/bin/sh
# 金标回归：模块自检 + 工具自检 + 架构边界 + 全树编译 + 端到端干跑 + 金标问题集。零 LLM 调用（除非 --live）。
# 每次改架构必跑；红了就是退化，不许"看着还行"放行。
cd "$(dirname "$0")/.." || exit 1
pass=0; fail=0; failed=""

run() {  # run <名字> <命令...>
  name="$1"; shift
  if out=$("$@" 2>&1); then
    pass=$((pass + 1)); printf "  ✅ %s\n" "$name"
  else
    fail=$((fail + 1)); failed="$failed\n    - $name: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
    printf "  ❌ %s\n" "$name"
  fi
}

echo "── 模块自检（17）──"
for m in misaka/extensions/board/db.py misaka/extensions/board/validate.py \
         misaka/research/kernel/canon.py misaka/research/kernel/saturation.py \
         misaka/research/kernel/tms.py misaka/research/kernel/verdict.py \
         misaka/research/kernel/frontier.py misaka/research/kernel/evidence.py \
         misaka/research/kernel/cdcl.py misaka/research/kernel/audit.py \
         misaka/research/kernel/precedent.py misaka/orchestration/budget.py \
         misaka/orchestration/skill_sandbox.py misaka/config/profiles.py \
         misaka/research/kernel/spike.py misaka/research/kernel/rounds.py \
         misaka/extensions/board/todo.py misaka/orchestration/moa.py \
         misaka/orchestration/lcm/tokens.py misaka/orchestration/lcm/search_query.py \
         misaka/orchestration/lcm/store.py misaka/orchestration/lcm/dag.py \
         misaka/orchestration/lcm/fresh_tail.py misaka/orchestration/lcm/escalation.py \
         misaka/orchestration/lcm/compactor.py misaka/orchestration/lcm/maintenance.py \
         misaka/orchestration/skills_guard.py misaka/orchestration/skill_layers.py \
         misaka/orchestration/skill_preprocessing.py misaka/orchestration/learn_prompt.py \
         misaka/research/basemap.py misaka/research/indexer/index.py misaka/research/indexer/workspace.py; do
  run "$(basename "$m")" .venv/bin/python "$m"
done

echo "── 工具自检（项目 venv）──"
run "subagent合同" .venv/bin/python -m pytest -q tests/contract/test_subagent_contract.py tests/contract/test_agent_definitions.py tests/contract/test_subagent_roles.py
run "板工具执行面" .venv/bin/python -m pytest -q tests/contract/test_board_tools.py
run "思辨红队" .venv/bin/python -m pytest -q tests/contract/test_critic.py
run "深研工具" .venv/bin/python -m pytest -q tests/contract/test_research_tools.py
run "深研驱动器" .venv/bin/python -m pytest -q tests/contract/test_research_loop.py
run "可观测性" .venv/bin/python -m pytest -q tests/contract/test_observability.py
run "微观代办工具" .venv/bin/python -m pytest -q tests/contract/test_todo_tools.py
run "面板召唤树" .venv/bin/python -m pytest -q tests/unit/test_tree_summon.py
run "心虚点回流" .venv/bin/python -m pytest -q tests/contract/test_harvest_uncertain.py
run "共同魂" .venv/bin/python -m pytest -q tests/contract/test_shared_soul.py
run "MoA扇出" .venv/bin/python -m pytest -q tests/contract/test_moa_fanout.py
run "LCM引擎接缝" .venv/bin/python -m pytest -q tests/contract/test_lcm_engine.py
run "skill显式调用" .venv/bin/python -m pytest -q tests/contract/test_skill_invoke.py
run "协力者工具执行面" .venv/bin/python -m pytest -q tests/contract/test_ally_tools.py
run "Sister控制面" .venv/bin/python -m pytest -q tests/contract/test_sister_contract.py
run "Sister持久生命周期" .venv/bin/python -m pytest -q tests/integration/test_sister_durable.py
run "subagent权限策略" .venv/bin/python -m pytest -q tests/contract/test_subagent_policy.py
run "subagent钩子与进程组" .venv/bin/python -m pytest -q tests/integration/test_subagent_hooks.py
run "subagent运行时边角" .venv/bin/python -m pytest -q tests/integration/test_subagent_runtime_edges.py
run "Sister属主围栏" .venv/bin/python -m pytest -q tests/integration/test_sister_owner_fence.py
run "课题维度" .venv/bin/python -m pytest -q tests/integration/test_project_dimension.py
run "课题目录" .venv/bin/python misaka/extensions/board/project.py
run "校准账" .venv/bin/python misaka/research/kernel/calibration.py
run "假说综合" .venv/bin/python misaka/research/kernel/synthesize.py
run "轮次预算" .venv/bin/python -m pytest -q tests/unit/test_token_budget.py
run "SQLite并发" .venv/bin/python -m pytest -q tests/integration/test_board_db_threading.py
run "网络守护" .venv/bin/python -m pytest -q tests/integration/test_net_daemon.py
run "面板端到端" .venv/bin/python -m pytest -q tests/integration/test_panel.py
run "面板状态栏" .venv/bin/python -m misaka.cli.panel
run "herdr布局移植" .venv/bin/python -m misaka.cli.herdr_ui
run "客户端版本闸" .venv/bin/python -m pytest -q tests/unit/test_net_client.py
run "交卷边界" .venv/bin/python -m pytest -q tests/unit/test_report_validation.py
run "会话现场连续性" .venv/bin/python -m pytest -q tests/unit/test_session_continuity.py
run "验收子进程" .venv/bin/python -m pytest -q tests/unit/test_judge_child.py
run "内联扩展命名" .venv/bin/python -m pytest -q tests/unit/test_inline_extensions.py
run "事件压缩合法性" .venv/bin/python -m pytest -q tests/unit/test_compact_event.py
run "扩展总线泄漏" .venv/bin/python -m pytest -q tests/unit/test_event_bus_leak.py
run "LaTeX渲染" .venv/bin/python misaka/tui/latex.py
run "Markdown数学" .venv/bin/python -m pytest -q tests/unit/test_markdown_latex.py
run "架构边界" .venv/bin/python -m pytest -q tests/architecture/test_import_boundaries.py
run "agents名册" .venv/bin/python misaka/extensions/subagent/agents.py
run "切人命令" .venv/bin/python misaka/extensions/switch.py
run "名册命令" .venv/bin/python misaka/extensions/roster.py
run "消息层" .venv/bin/python -m misaka.extensions.messages
run "协力者执行" .venv/bin/python -m misaka.extensions.ally.runner
run "协力者送信" .venv/bin/python -m misaka.extensions.ally.tell
run "主题体检" .venv/bin/python -m misaka.modes.interactive.theme.check
run "MCP接入" .venv/bin/python misaka/extensions/mcp.py

echo "── 编译（全树）──"
run "py_compile(全包)" sh -c '.venv/bin/python -c "import compileall,sys; sys.exit(0 if compileall.compile_dir(\"misaka\", quiet=2) else 1)"'

echo "── CLI 冒烟 ──"
run "misaka board" .venv/bin/misaka board
run "misaka lcm" env MISAKA_LCM_DB=/tmp/misaka-regression-lcm-smoke.db .venv/bin/misaka lcm doctor
run "terminal_colors" .venv/bin/python misaka/tui/terminal_colors.py

echo "── 端到端干跑（临时库，不碰真板）──"
run "端到端" .venv/bin/python eval/e2e_dry.py

echo "── 金标问题集 ──"
run "golden" .venv/bin/python eval/golden.py

echo
if [ "$fail" -eq 0 ]; then
  printf "回归通过：%d/%d ✅\n" "$pass" "$pass"
else
  printf "回归失败：%d 过 / %d 挂%b\n" "$pass" "$fail" "$failed"
  exit 1
fi
