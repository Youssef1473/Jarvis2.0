$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyExe = Join-Path $scriptDir ".venv\Scripts\python.exe"
if (-not (Test-Path $pyExe)) {
    $parentPy = Join-Path $scriptDir "..\.venv\Scripts\python.exe"
    if (Test-Path $parentPy) {
        $pyExe = $parentPy
    } else {
        $pyExe = "python"
    }
}
& $pyExe "$scriptDir\run.py" @args
