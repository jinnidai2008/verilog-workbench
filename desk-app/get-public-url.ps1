# 从 cloudflared 的日志里取出公网地址。
#
# 为什么不写在 .bat 里：批处理的转义规则（^|、" 的嵌套、% 展开）会把
# PowerShell 的管道和正则弄得面目全非 —— 实测被这个坑了好几轮。
# 单独放一个 .ps1 就完全没有转义问题。
#
# 两个实测踩到的坑，都写在这里免得再犯：
#   1) cloudflared 一边跑一边**锁着**日志文件，所以绝不能用
#      [IO.File]::ReadAllLines（会抛"being used by another process"）。
#      必须用 FileStream + FileShare.ReadWrite 打开。
#   2) 日志是 JSON，地址藏在 message 字段里还带框线装饰：
#      {"level":"info","time":"...","message":"|  https://xxx.trycloudflare.com  |"}
#      所以先按 JSON 解析，失败就直接在整行里正则。

param(
    [Parameter(Mandatory = $true)][string]$LogFile
)

$ErrorActionPreference = 'SilentlyContinue'

if (-not (Test-Path -LiteralPath $LogFile)) { exit 0 }

# 共享读取：cloudflared 还在往这个文件里写
$text = ''
try {
    $fs = [System.IO.File]::Open($LogFile,
                                 [System.IO.FileMode]::Open,
                                 [System.IO.FileAccess]::Read,
                                 [System.IO.FileShare]::ReadWrite)
    $sr = New-Object System.IO.StreamReader($fs)
    $text = $sr.ReadToEnd()
    $sr.Close()
    $fs.Close()
} catch {
    exit 0
}
if (-not $text) { exit 0 }

$url = ''
foreach ($line in ($text -split "`r?`n")) {
    if ($line -notmatch 'trycloudflare\.com') { continue }
    $msg = $line
    try {
        $o = $line | ConvertFrom-Json
        if ($o -and $o.message) { $msg = [string]$o.message }
    } catch { }
    $m = [regex]::Match($msg, 'https://[A-Za-z0-9\-]+\.trycloudflare\.com')
    if ($m.Success) { $url = $m.Value; break }
}

if ($url) { Write-Output $url }
exit 0
