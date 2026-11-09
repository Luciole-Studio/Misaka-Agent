"""The bash cases of pi coding-agent's ``tools.test.ts``: non-zero exits are error results with
structured content, and structured content carries up to 1 MiB of output."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from misaka.core.tools.bash import createBashTool
from misaka.core.tools.render_utils import getTextOutput
from misaka.utils.shell import getShellConfig


def _bash_available() -> bool:
    try:
        getShellConfig(None)
    except Exception:  # noqa: BLE001 - no shell on this machine: the cases are skipped
        return False
    return True


pytestmark = pytest.mark.skipif(not _bash_available(), reason="bash is not available")


@pytest.fixture
def bash(tmp_path):
    return createBashTool(str(tmp_path))


async def test_reports_non_zero_exit_codes_as_error_results_with_structured_content(bash):
    result = await bash.execute("test-call-9", {"command": "echo out; exit 3"}, None, None)
    assert result.isError is True
    assert getTextOutput(result, False) == "out\n\n\nCommand exited with code 3"
    structured = dict(result.structuredContent)
    assert isinstance(structured.pop("wall_time_seconds"), (int, float))
    assert structured == {"output": "out\n", "truncated": False, "exit_code": 3}

    ok = await bash.execute("test-call-9b", {"command": "echo fine"}, None, None)
    assert ok.isError is None
    assert (ok.structuredContent["output"], ok.structuredContent["exit_code"]) == ("fine\n", 0)

    empty = await bash.execute("test-call-9c", {"command": "true"}, None, None)
    assert getTextOutput(empty, False) == "(no output)"
    assert (empty.structuredContent["output"], empty.structuredContent["truncated"]) == ("", False)


async def test_returns_up_to_1_mib_of_output_in_structured_content(bash):
    # 3000 lines exceed the model-facing 2000 line limit but not 1 MiB.
    medium = await bash.execute("test-call-9d", {"command": "seq 1 3000"}, None, None)
    assert "\n1\n2\n" not in getTextOutput(medium, False)
    assert medium.details.truncation.truncated is True
    assert medium.structuredContent["truncated"] is False
    assert medium.structuredContent["output"] == "".join(f"{i}\n" for i in range(1, 3001))

    # About 2 MB: keeps the first and last 512 KiB around an omission marker.
    large = await bash.execute("test-call-9e", {"command": "seq 1 300000"}, None, None)
    output = large.structuredContent
    assert output["truncated"] is True
    assert output["output"].startswith("1\n2\n3\n")
    assert output["output"].endswith("299999\n300000\n")
    assert "\n\n[... " in output["output"] and " bytes omitted ...]\n\n" in output["output"]
    assert len(output["output"].encode("utf-8")) < 1024 * 1024 + 100
    assert output["full_output_path"] == large.details.fullOutputPath
    assert Path(output["full_output_path"]).read_text(encoding="utf-8").endswith("300000\n")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_reports_signal_killed_commands_as_errors_while_preserving_partial_output(bash):
    result = await bash.execute("test-call-signal", {"command": "echo partial; kill -TERM $$"}, None, None)
    assert result.isError is True
    text = getTextOutput(result, False)
    assert text.startswith("partial")
    assert "Command exited with code" in text
    assert os.path.exists(os.devnull)
