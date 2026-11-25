# Compatibility for existing local shortcuts; Python loads launcher settings.
param([ValidateSet("excudo", "keichan", "ink")][string]$App = "excudo")
Set-Location -LiteralPath $PSScriptRoot
$frontend = if ($App -eq "ink") { "ink" } else { "studio" }
uv run --locked --extra desktop --extra keichan --extra studio --extra browser python -m excudo.launcher --app $frontend
exit $LASTEXITCODE
