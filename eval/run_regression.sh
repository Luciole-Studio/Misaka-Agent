#!/bin/sh
# Golden regression: module self-checks + tool self-checks + import boundaries + whole-tree compile
# + end-to-end dry run + golden checks. No LLM calls (unless --live).
# Run after every structural change; red means a regression, do not ship on "looks fine".
cd "$(dirname "$0")/.." || exit 1
pass=0; fail=0; failed=""

run() { # run <name> <command...>
    name="$1"; shift
    if out=$("$@" 2>&1); then
        pass=$((pass + 1)); printf "  ✅ %s\n" "$name"
    else
        fail=$((fail + 1)); failed="$failed\n  - $name: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
        printf "  ❌ %s\n" "$name"
    fi
}

echo "── Module self-checks ──"
for m in misaka/platform/tasks.py misaka/platform/projects.py \
         misaka/platform/prompt_guard.py \
         misaka/network/validate.py \
         misaka/platform/budget.py \
         misaka/skills/sandbox.py misaka/config/profiles.py \
         misaka/network/todo.py misaka/extensions/moa/provider.py \
         misaka/extensions/lcm/tokens.py misaka/extensions/lcm/search_query.py \
         misaka/extensions/lcm/store.py misaka/extensions/lcm/dag.py \
         misaka/extensions/lcm/fresh_tail.py misaka/extensions/lcm/escalation.py \
         misaka/extensions/lcm/compactor.py misaka/extensions/lcm/maintenance.py \
         misaka/skills/guard.py misaka/skills/layers.py \
         misaka/skills/preprocessing.py misaka/skills/learn_prompt.py \
         misaka/research/basemap.py misaka/documents/index.py misaka/workspace.py \
         misaka/research/runs.py misaka/research/planner.py misaka/research/context.py \
         misaka/research/ledger.py misaka/research/workflow.py misaka/research/report.py \
         misaka/research/start.py eval/placebo.py; do
    if [ "$m" = "misaka/workspace.py" ]; then
        run "$(basename "$m")" .venv/bin/python -m misaka.workspace
    else
        run "$(basename "$m")" .venv/bin/python "$m"
    fi
done

echo "── Tool self-checks (project venv) ──"
run "subagent contracts" .venv/bin/python -m pytest -q tests/contract/test_subagent_contract.py tests/contract/test_agent_definitions.py tests/contract/test_subagent_roles.py
run "board tools" .venv/bin/python -m pytest -q tests/contract/test_board_tools.py
run "research critic" .venv/bin/python -m pytest -q tests/contract/test_critic.py
run "research tools" .venv/bin/python -m pytest -q tests/contract/test_research_tools.py
run "research workflow" .venv/bin/python -m pytest -q tests/contract/test_research_loop.py tests/contract/test_research_runs.py
run "observability" .venv/bin/python -m pytest -q tests/contract/test_observability.py
run "todo tools" .venv/bin/python -m pytest -q tests/contract/test_todo_tools.py
run "tree summon" .venv/bin/python -m pytest -q tests/unit/test_tree_summon.py
run "shared soul" .venv/bin/python -m pytest -q tests/contract/test_shared_soul.py
run "MoA one-shot + provider" .venv/bin/python -m pytest -q tests/contract/test_moa_oneshot.py tests/contract/test_moa_provider.py
run "LCM engine seams" .venv/bin/python -m pytest -q tests/contract/test_lcm_engine.py tests/contract/test_lcm_storage_retrieval.py
run "skill invoke" .venv/bin/python -m pytest -q tests/contract/test_skill_invoke.py
run "ally tools" .venv/bin/python -m pytest -q tests/contract/test_ally_tools.py
run "Sister control surface" .venv/bin/python -m pytest -q tests/contract/test_sister_contract.py
run "Sister durable lifecycle" .venv/bin/python -m pytest -q tests/integration/test_sister_durable.py
run "subagent permission policy" .venv/bin/python -m pytest -q tests/contract/test_subagent_policy.py
run "subagent hooks + process group" .venv/bin/python -m pytest -q tests/integration/test_subagent_hooks.py
run "subagent runtime edge cases" .venv/bin/python -m pytest -q tests/integration/test_subagent_runtime_edges.py
run "Sister owner fence" .venv/bin/python -m pytest -q tests/integration/test_sister_owner_fence.py
run "project dimension" .venv/bin/python -m pytest -q tests/integration/test_project_dimension.py
run "projects self-check" .venv/bin/python misaka/platform/projects.py
run "token budget" .venv/bin/python -m pytest -q tests/unit/test_token_budget.py
run "SQLite threading" .venv/bin/python -m pytest -q tests/integration/test_board_db_threading.py
run "net daemon" .venv/bin/python -m pytest -q tests/integration/test_net_daemon.py
run "panel end-to-end" .venv/bin/python -m pytest -q tests/integration/test_panel.py
run "panel status bar" .venv/bin/python -m misaka.cli.panel
run "herdr layout port" .venv/bin/python -m misaka.cli.herdr_ui
run "net client version gate" .venv/bin/python -m pytest -q tests/unit/test_net_client.py
run "report validation" .venv/bin/python -m pytest -q tests/unit/test_report_validation.py
run "session continuity" .venv/bin/python -m pytest -q tests/unit/test_session_continuity.py
run "judge child process" .venv/bin/python -m pytest -q tests/unit/test_judge_child.py
run "inline extension naming" .venv/bin/python -m pytest -q tests/unit/test_inline_extensions.py
run "compact event validity" .venv/bin/python -m pytest -q tests/unit/test_compact_event.py
run "extension event bus leak" .venv/bin/python -m pytest -q tests/unit/test_event_bus_leak.py
run "LaTeX render" .venv/bin/python misaka/tui/latex.py
run "Markdown math" .venv/bin/python -m pytest -q tests/unit/test_markdown_latex.py
run "import boundaries" .venv/bin/python -m pytest -q tests/architecture/test_import_boundaries.py
run "agents roster" .venv/bin/python misaka/extensions/subagent/agents.py
run "switch command" .venv/bin/python misaka/network/switch.py
run "roster command" .venv/bin/python misaka/network/roster.py
run "messages" .venv/bin/python -m misaka.network.messages
run "ally runner" .venv/bin/python -m misaka.extensions.ally.runner
run "ally tell" .venv/bin/python -m misaka.extensions.ally.tell
run "theme check" .venv/bin/python -m misaka.modes.interactive.theme.check
run "MCP access" .venv/bin/python misaka/extensions/mcp.py

echo "── Compile (whole tree) ──"
run "py_compile (whole package)" sh -c '.venv/bin/python -c "import compileall, sys; sys.exit(0 if compileall.compile_dir(\"misaka\", quiet=2) else 1)"'

echo "── CLI smoke ──"
run "misaka board" .venv/bin/misaka board
run "misaka lcm" env MISAKA_LCM_DB=/tmp/misaka-regression-lcm-smoke.db .venv/bin/misaka lcm doctor
run "terminal_colors" .venv/bin/python misaka/tui/terminal_colors.py

echo "── End-to-end dry run (temporary DB, nothing real is touched) ──"
run "e2e dry run" .venv/bin/python eval/e2e_dry.py

echo "── Golden checks ──"
run "golden" .venv/bin/python eval/golden.py

echo
if [ "$fail" -eq 0 ]; then
    printf "regression passed: %d/%d ✅\n" "$pass" "$pass"
else
    printf "regression failed: %d passed / %d failed%b\n" "$pass" "$fail" "$failed"
    exit 1
fi
