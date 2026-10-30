# Run one complete reading-action chain over the two public texts in examples/reading.
#
#   powershell -ExecutionPolicy Bypass -File scripts\run_reading_demo.ps1 [WORKSPACE]
#
# WORKSPACE is a scratch folder (default: a new temporary directory). The materials are
# downloaded there, the run is written to WORKSPACE\reading\<run-id>\, and nothing is
# copied back into the repository. The script prints every command it runs, the run ID,
# and the node files the engine actually wrote.
#
# Prerequisites: Node.js >= 22.19.0, the reading engine built once
#   npm --prefix packages/reading install; npm --prefix packages/reading run build
# and a text model reachable through MISAKA's settings (or --model below). If MISAKA is
# not on PATH, set MISAKA_READING_BIN to the command that starts it.
#
# MISAKA_READING_RUNTIME points at an already-built dist\cli.js; the source checkout
# finds its own. MISAKA_READING_NODE points at a node binary that is not on PATH.

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Workspace = ""
)

$ErrorActionPreference = "Stop"
# MISAKA prints UTF-8; Windows PowerShell decodes native output with the console code page
# unless told otherwise, which turns a Chinese run id into mojibake ConvertFrom-Json rejects.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
# The other direction: text piped into a native command is encoded with $OutputEncoding,
# which is ASCII in Windows PowerShell, so a Chinese message would reach the chat as "?".
$OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Split-Path -Parent $PSScriptRoot

if (-not $Workspace) {
    $Workspace = Join-Path ([System.IO.Path]::GetTempPath()) ("misaka-reading-demo-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
}
New-Item -ItemType Directory -Force -Path $Workspace | Out-Null
$Workspace = (Resolve-Path -LiteralPath $Workspace).Path

# The command that starts MISAKA, as an argv prefix. A bare executable plus arguments,
# so the same prefix can be used with or without a pipe into its stdin.
if ($env:MISAKA_READING_BIN) {
    $MisakaCommand = @($env:MISAKA_READING_BIN)
} elseif (Test-Path -LiteralPath (Join-Path $RepoRoot ".venv\Scripts\misaka.exe")) {
    $MisakaCommand = @((Join-Path $RepoRoot ".venv\Scripts\misaka.exe"))
} elseif (Get-Command uv -ErrorAction SilentlyContinue) {
    $MisakaCommand = @("uv", "run", "--project", $RepoRoot, "misaka")
} else {
    $MisakaCommand = @("misaka")
}
$MisakaExe = $MisakaCommand[0]
$MisakaPrefix = @()
if ($MisakaCommand.Count -gt 1) { $MisakaPrefix = $MisakaCommand[1..($MisakaCommand.Count - 1)] }

function Invoke-Misaka {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & $MisakaExe @MisakaPrefix @Arguments
}

function Show-Command {
    param([string]$Text)
    Write-Host ("   $ " + $Text)
}

function Write-Step {
    param([string]$Text)
    Write-Host ""
    Write-Host ("== " + $Text)
}

function Send-Chat {
    # Feed one message to `misaka read chat` and let the input end, so the turn runs and the
    # session closes. An explicit /stop in the same pipe can arrive before the turn is
    # streaming, and stop means stop: end of input is the honest way to end a scripted turn.
    param([string]$RunId, [string]$Message)

    $inputFile = Join-Path $Workspace (".chat-" + [guid]::NewGuid().ToString("N").Substring(0, 8) + ".txt")
    Set-Content -LiteralPath $inputFile -Value $Message -Encoding UTF8
    try {
        Get-Content -LiteralPath $inputFile -Raw -Encoding utf8 | & $MisakaExe @MisakaPrefix read chat $RunId --workspace $Workspace
    } finally {
        Remove-Item -LiteralPath $inputFile -Force
    }
    if ($LASTEXITCODE -ne 0) { throw "read chat failed with exit code $LASTEXITCODE" }
}

$Luxun = "https://zh.wikisource.org/wiki/%E5%A8%9C%E6%8B%89%E8%B5%B0%E5%BE%8C%E6%80%8E%E6%A8%A3"
$Ibsen = "https://www.gutenberg.org/files/2542/2542-0.txt"
$Question = "个人出走与经济条件如何共同构成自由问题"

Write-Step ("workspace: " + $Workspace)

# 1. Open: index/download both texts and write the run. No model is called here; N0 is
#    the reader's own question.
Show-Command ("misaka read open <luxun> <ibsen> --question '...' --intent '...' --workspace '" + $Workspace + "' --json")
$open = Invoke-Misaka read open $Luxun $Ibsen `
    --question $Question `
    --intent "把两篇文本放在一起读，最后回到最初的问题。" `
    --workspace $Workspace --json
if ($LASTEXITCODE -ne 0) { throw "read open failed with exit code $LASTEXITCODE" }
$open = $open | ConvertFrom-Json

$Run = $open.runId
$RootNode = $open.currentNodeId
$RunDir = $open.runDirectory
Write-Host ("   run: " + $Run)
Write-Host ("   entry node: " + $RootNode)
Write-Host ("   directory: " + $RunDir)

# 2. Reconstruct: rebuild the lecture's question and its turns in the entry node.
Show-Command ("misaka read act " + $Run + " reconstruct --node " + $RootNode + " --focus '论证的转折' --workspace '" + $Workspace + "' --json")
$act = Invoke-Misaka read act $Run reconstruct --node $RootNode `
    --focus "论证的转折" --workspace $Workspace --json
if ($LASTEXITCODE -ne 0) { throw "read act failed with exit code $LASTEXITCODE" }
Write-Host ("   wrote: " + ($act | ConvertFrom-Json).bodyPath)

# 3. Fork: two lenses, run in parallel as independent reading lines.
Show-Command ("misaka read fork " + $Run + " --node " + $RootNode + " --lens '主体性与自我理解' --lens '经济条件与社会关系' --workspace '" + $Workspace + "' --json")
$fork = Invoke-Misaka read fork $Run --node $RootNode `
    --lens "主体性与自我理解" --lens "经济条件与社会关系" `
    --workspace $Workspace --json
if ($LASTEXITCODE -ne 0) { throw "read fork failed with exit code $LASTEXITCODE" }
$ForkIds = @(($fork | ConvertFrom-Json).nodeIds)
Write-Host ("   lines: " + ($ForkIds -join " "))

# 4. Compare: one node with every chosen line as a parent.
Show-Command ("misaka read compare " + $Run + " " + ($ForkIds -join " ") + " --question '...' --workspace '" + $Workspace + "' --json")
$compareArgs = @("read", "compare", $Run) + $ForkIds + @(
    "--question", "两条路线给出的自由是怎样的，它们在哪里分岔？",
    "--workspace", $Workspace, "--json"
)
$cmp = Invoke-Misaka @compareArgs
if ($LASTEXITCODE -ne 0) { throw "read compare failed with exit code $LASTEXITCODE" }
$cmp = $cmp | ConvertFrom-Json
Write-Host ("   wrote: " + $cmp.bodyPath)

# 5. Reread: the entry node's material, read again with the new understanding.
Show-Command ("misaka read reread " + $Run + " --node " + $RootNode + " --using " + $cmp.nodeId + " --focus '结尾' --workspace '" + $Workspace + "' --json")
$reread = Invoke-Misaka read reread $Run --node $RootNode --using $cmp.nodeId `
    --focus "结尾" --workspace $Workspace --json
if ($LASTEXITCODE -ne 0) { throw "read reread failed with exit code $LASTEXITCODE" }
Write-Host ("   wrote: " + ($reread | ConvertFrom-Json).bodyPath)

# 6. Graph: the map and every node body path, read back from disk.
Write-Step ("graph (read back from " + (Join-Path $RunDir "graph.json") + ")")
Show-Command ("misaka read graph " + $Run + " --workspace '" + $Workspace + "' --json")
$graph = Invoke-Misaka read graph $Run --workspace $Workspace --json
if ($LASTEXITCODE -ne 0) { throw "read graph failed with exit code $LASTEXITCODE" }
$graph = $graph | ConvertFrom-Json
Write-Host ("graph: " + $graph.graphMarkdownPath)
Write-Host ("map:   " + $graph.readingPath)
foreach ($node in $graph.nodes) {
    $lens = if ($node.lens) { " [" + $node.lens + "]" } else { "" }
    $actions = if ($node.actions) { $node.actions -join ", " } else { "unread" }
    Write-Host ("  " + $node.id + $lens + "  " + $actions + "  " + (Join-Path $graph.runDirectory $node.bodyPath))
}

# 7. Resume: talk with the leader, then reopen the run in a new process and talk again.
#    Both turns use the leader session saved under sessions\.
Write-Step "chat: one leader turn, then end of input"
Show-Command ("misaka read chat " + $Run + "   <<< '用两三句话说明这次阅读的两条线'")
Send-Chat -RunId $Run -Message "用两三句话说明这次阅读的两条线分别读到了什么。"

Write-Step "resume: a new process reopens the run and its leader session"
Show-Command ("misaka read graph " + $Run + " --workspace '" + $Workspace + "' --json   (read-only)")
$againRaw = Invoke-Misaka read graph $Run --workspace $Workspace --json
if ($LASTEXITCODE -ne 0) { throw "read graph failed with exit code $LASTEXITCODE" }
$again = $againRaw | ConvertFrom-Json
$before = $graph.nodes | ForEach-Object { $_.id + " " + $_.bodyPath }
$after = $again.nodes | ForEach-Object { $_.id + " " + $_.bodyPath }
if (($before -join "|") -ne ($after -join "|")) {
    throw ("graph changed after reopening it: " + ($before -join ", ") + " != " + ($after -join ", "))
}
Write-Host ("   " + $again.nodes.Count + " nodes, unchanged: " + (($again.nodes | ForEach-Object { $_.id }) -join ", "))

Show-Command ("session files recorded in " + (Join-Path $RunDir "graph.json"))
$saved = Get-Content -LiteralPath (Join-Path $RunDir "graph.json") -Raw -Encoding utf8 | ConvertFrom-Json
if (-not $saved.sessions) { throw "no sessions recorded: the leader conversation was not saved" }
foreach ($record in $saved.sessions) {
    $where = if ($record.nodeId) { $record.nodeId } elseif ($record.lineId) { $record.lineId } else { "-" }
    $exists = Test-Path -LiteralPath $record.sessionFile
    Write-Host ("   " + $record.role.PadRight(8) + $where.PadRight(6) + $record.sessionFile + "  saved=" + $exists)
    if (-not $exists) { throw ("session file missing: " + $record.sessionFile) }
}

Show-Command ("misaka read chat " + $Run + "   <<< '刚才两条线，哪一条更接近你的判断？'")
Send-Chat -RunId $Run -Message "刚才那两条线，哪一条更接近你的判断？"

Write-Step "done"
Write-Host ("run:    " + $Run)
Write-Host ("folder: " + $RunDir)
Write-Host ("Open " + (Join-Path $RunDir "reading.md") + " and " + (Join-Path $RunDir "nodes") + " to read what was actually written.")
