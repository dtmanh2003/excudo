# Creates the Excudo shortcuts (with the Excudo logo) for this computer.
# Run once after downloading the project, through セットアップ.bat. Run it again only if the
# project folder is moved or renamed: a shortcut stores the full path of this computer.
#
#   -Destination <folder>   where to create the shortcuts (default: the user's Desktop)
param([string]$Destination = [Environment]::GetFolderPath("Desktop"))

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$icon = Join-Path $root "excudo\app\ui\excudo.ico"
$shell = New-Object -ComObject WScript.Shell

function New-ExcudoShortcut([string]$Name, [string]$Target, [string]$Arguments, [string]$WorkingDirectory, [string]$Description, [string]$Icon) {
    $path = Join-Path $Destination "$Name.lnk"
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $Target
    $link.Arguments = $Arguments
    $link.WorkingDirectory = $WorkingDirectory
    $link.IconLocation = "$Icon,0"
    $link.Description = $Description
    $link.Save()
    Write-Host "  作成しました: $path"
}

foreach ($required in @($icon, (Join-Path $root "Excudo.vbs"))) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "見つかりません: $required （プロジェクトのフォルダーで実行してください）"
    }
}
if (-not (Test-Path -LiteralPath $Destination)) {
    throw "作成先のフォルダーがありません: $Destination"
}

Write-Host "Excudo のショートカットを作成します: $Destination"
# The desktop app, opened without a console window (the same as Excudo.bat).
# Receipts (Keichan) are tools inside this app, so no separate shortcut is needed.
New-ExcudoShortcut "Excudo" (Join-Path $env:WINDIR "System32\wscript.exe") ('"' + (Join-Path $root "Excudo.vbs") + '"') $root "Excudo（デスクトップアプリ）" $icon
Write-Host "完了しました。"
