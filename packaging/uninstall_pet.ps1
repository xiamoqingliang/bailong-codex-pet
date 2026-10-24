[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$DataRoot,
    [Parameter(Mandatory=$true)][ValidateSet('full','native')][string]$Edition,
    [string]$CodexHome,
    [string]$StartupDirectory,
    [switch]$CheckOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-LocalPath([string]$Path) {
    $absolute = [IO.Path]::GetFullPath($Path)
    if ($absolute.Length -gt [IO.Path]::GetPathRoot($absolute).Length) { $absolute = $absolute.TrimEnd('\') }
    if ($absolute.StartsWith('\\')) { throw '卸载只支持本机普通磁盘目录。' }
    $current = $absolute
    while ($current) {
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($null -ne $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw '卸载路径不能经过符号链接或目录联接；未删除文件。'
        }
        $parent = [IO.Directory]::GetParent($current)
        $current = if ($null -eq $parent) { $null } else { $parent.FullName }
    }
    return $absolute
}
function Same-Path([string]$First,[string]$Second) {
    if ([string]::IsNullOrWhiteSpace($First) -or [string]::IsNullOrWhiteSpace($Second)) { return $false }
    try { return [string]::Equals([IO.Path]::GetFullPath($First).TrimEnd('\'),[IO.Path]::GetFullPath($Second).TrimEnd('\'),[StringComparison]::OrdinalIgnoreCase) }
    catch { return $false }
}
function Read-Bytes([string]$Path,[long]$Limit=1048576) {
    $null=Assert-LocalPath $Path
    if (-not [IO.File]::Exists($Path)) {
        if ([IO.Directory]::Exists($Path)) { throw '卸载目标文件类型异常，未删除。' }
        return $null
    }
    if ((Get-Item -LiteralPath $Path).Length -gt $Limit) { throw '卸载目标文件大小异常，未删除。' }
    return ,([IO.File]::ReadAllBytes($Path))
}
function Same-Bytes($First,$Second) {
    if ($null -eq $First -or $null -eq $Second) { return $null -eq $First -and $null -eq $Second }
    return [Convert]::ToBase64String($First) -ceq [Convert]::ToBase64String($Second)
}
function Hash-Bytes([byte[]]$Bytes) {
    $hash=[Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash($Bytes))).Replace('-','').ToLowerInvariant() }
    finally { $hash.Dispose() }
}
function Atomic-Write([string]$Path,[byte[]]$Bytes) {
    $null=Assert-LocalPath $Path
    $parent=Split-Path -Parent $Path
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $temporary=Join-Path $parent ('.bailong-uninstall-'+[Guid]::NewGuid().ToString('N')+'.tmp')
    try {
        [IO.File]::WriteAllBytes($temporary,$Bytes)
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temporary,$Path,[NullString]::Value) }
        else { [IO.File]::Move($temporary,$Path) }
    } finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}
function Property($Object,[string]$Name) {
    if ($null -eq $Object) { return $null }
    $p=$Object.PSObject.Properties[$Name]
    if ($null -eq $p) { return $null }
    return $p.Value
}

if (-not ('BailongUninstallTomlV1' -as [type])) {
    Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Text;
using System.Text.RegularExpressions;
public static class BailongUninstallTomlV1 {
    public sealed class Value { public int Start, Length, LineStart, LineLength; public string Text, RemovalText; }
    public sealed class Info {
        public int InsertAt; public bool HasDesktop;
        public Dictionary<string, Value> Values = new Dictionary<string, Value>();
    }
    static Exception Unsupported() {
        return new InvalidOperationException("无法安全修改当前 TOML 写法；未修改文件。请使用普通 [desktop] 设置段。");
    }
    static string QuotedKey(string token) {
        string inner = token.Substring(1, token.Length - 2);
        if (token[0] == '\'') {
            if (inner.Contains("'")) throw Unsupported();
            return inner;
        }
        StringBuilder value = new StringBuilder();
        for (int i = 0; i < inner.Length; i++) {
            char c = inner[i];
            if (c == '"' || c < 32) throw Unsupported();
            if (c != '\\') { value.Append(c); continue; }
            if (++i >= inner.Length) throw Unsupported();
            c = inner[i];
            switch (c) {
                case '\\': value.Append('\\'); break;
                case '"': value.Append('"'); break;
                case 'b': value.Append('\b'); break;
                case 't': value.Append('\t'); break;
                case 'n': value.Append('\n'); break;
                case 'f': value.Append('\f'); break;
                case 'r': value.Append('\r'); break;
                case 'u': case 'U':
                    int count = c == 'u' ? 4 : 8;
                    if (i + count >= inner.Length) throw Unsupported();
                    string hex = inner.Substring(i + 1, count);
                    if (!Regex.IsMatch(hex, "^[0-9A-Fa-f]+$")) throw Unsupported();
                    uint code = Convert.ToUInt32(hex, 16);
                    if (code > 0x10FFFF || (code >= 0xD800 && code <= 0xDFFF)) throw Unsupported();
                    value.Append(Char.ConvertFromUtf32((int)code)); i += count; break;
                default: throw Unsupported();
            }
        }
        return value.ToString();
    }
    static List<string> Keys(string input) {
        List<string> keys = new List<string>(); int start = 0; char quote = '\0';
        for (int i = 0; i <= input.Length; i++) {
            char c = i == input.Length ? '\0' : input[i];
            if (quote != '\0') {
                if (c == '\0') throw Unsupported();
                if (quote == '"' && c == '\\') {
                    if (++i >= input.Length) throw Unsupported();
                    continue;
                }
                if (c == quote) quote = '\0';
            } else if (c == '"' || c == '\'') quote = c;
            else if (c == '.' || c == '\0') {
                string token = input.Substring(start, i - start).Trim();
                if (Regex.IsMatch(token, "^[A-Za-z0-9_-]+$")) keys.Add(token);
                else if (token.Length >= 2 &&
                    ((token[0] == '"' && token[token.Length - 1] == '"') ||
                     (token[0] == '\'' && token[token.Length - 1] == '\''))) {
                    keys.Add(QuotedKey(token));
                } else throw Unsupported();
                start = i + 1;
            }
        }
        return keys;
    }
    static int Assignment(string line) {
        char quote = '\0';
        for (int i = 0; i < line.Length; i++) {
            char c = line[i];
            if (quote != '\0') {
                if (quote == '"' && c == '\\') {
                    if (++i >= line.Length) throw Unsupported();
                    continue;
                }
                if (c == quote) quote = '\0';
            } else if (c == '\'' || c == '"') quote = c;
            else if (c == '=') return i;
            else if (c == '#') return -1;
        }
        return -1;
    }
    public static Info Inspect(string text) {
        Info result = new Info(); string section = ""; string mode = "";
        int depth = 0; bool indirectDesktop = false;
        foreach (Match lm in Regex.Matches(text, @"[^\r\n]+(?:\r\n|\r|\n|$)|(?:\r\n|\r|\n)")) {
            string raw = lm.Value; string line = raw.TrimEnd('\r', '\n'); string trimmed = line.Trim();
            bool neutral = mode == "" && depth == 0;
            if (neutral && trimmed.StartsWith("[")) {
                Match header = Regex.Match(trimmed, @"^(\[\[?)(.+?)(\]\]?)\s*(?:#.*)?$");
                if (!header.Success || header.Groups[1].Length != header.Groups[3].Length) throw Unsupported();
                List<string> keys = Keys(header.Groups[2].Value);
                if (section == "desktop") result.InsertAt = lm.Index;
                section = "other";
                if (keys[0] == "desktop") {
                    if (keys.Count != 1 || header.Groups[1].Length != 1) indirectDesktop = true;
                    else {
                        if (result.HasDesktop) throw Unsupported();
                        result.HasDesktop = true; section = "desktop";
                    }
                }
                continue;
            }
            if (neutral && trimmed != "" && !trimmed.StartsWith("#")) {
                int equals = Assignment(line);
                if (equals < 0) throw Unsupported();
                List<string> keys = Keys(line.Substring(0, equals));
                if (section == "" && keys[0] == "desktop") throw Unsupported();
                if (section == "desktop" &&
                    (keys[0] == "selected-avatar-id" || keys[0] == "avatar-overlay-pet-visible")) {
                    if (keys.Count != 1 || result.Values.ContainsKey(keys[0])) throw Unsupported();
                    string rhs = line.Substring(equals + 1);
                    Match value = Regex.Match(rhs, keys[0] == "selected-avatar-id"
                        ? "^\\s*(?<v>\"[^\"\\\\]*\"|'[^']*')\\s*(?:#.*)?$"
                        : "^\\s*(?<v>true|false)\\s*(?:#.*)?$");
                    if (!value.Success) throw Unsupported();
                    Group v = value.Groups["v"];
                    string trailing = rhs.Substring(v.Index + v.Length).TrimStart();
                    string removal = trailing.StartsWith("#") ? Regex.Match(line, @"^\s*").Value + trailing + raw.Substring(line.Length) : "";
                    result.Values.Add(keys[0], new Value { Start = lm.Index + equals + 1 + v.Index, Length = v.Length, Text = v.Value,
                        LineStart = lm.Index, LineLength = raw.Length, RemovalText = removal });
                }
            }
            // Track multiline strings/arrays so their content is never parsed as keys.
            for (int i = 0; i < line.Length; i++) {
                char c = line[i];
                if (mode == "mb" || mode == "ml") {
                    char q = mode == "mb" ? '"' : '\'';
                    if (mode == "mb" && c == '\\') { i++; continue; }
                    if (c == q && i + 2 < line.Length && line[i + 1] == q && line[i + 2] == q) { mode = ""; i += 2; }
                    continue;
                }
                if (mode == "b" || mode == "l") {
                    if (mode == "b" && c == '\\') { i++; continue; }
                    if (c == (mode == "b" ? '"' : '\'')) mode = "";
                    continue;
                }
                if (c == '#') break;
                if (c == '"' || c == '\'') {
                    bool multi = i + 2 < line.Length && line[i + 1] == c && line[i + 2] == c;
                    mode = c == '"' ? (multi ? "mb" : "b") : (multi ? "ml" : "l");
                    if (multi) i += 2;
                } else if (c == '[' || c == '{') depth++;
                else if (c == ']' || c == '}') { if (--depth < 0) throw Unsupported(); }
            }
            if (mode == "b" || mode == "l") throw Unsupported();
        }
        if (mode != "" || depth != 0 || (!result.HasDesktop && indirectDesktop)) throw Unsupported();
        if (section == "desktop" || !result.HasDesktop) result.InsertAt = text.Length;
        return result;
    }
    public static string Selected(string text) {
        Info info = Inspect(text); Value value;
        if (!info.Values.TryGetValue("selected-avatar-id", out value)) return null;
        return value.Text.Substring(1, value.Text.Length - 2);
    }
    public static Dictionary<string,object> DesktopBefore(string text) {
        Info info = Inspect(text);
        var result = new Dictionary<string,object>();
        foreach (string key in new string[] { "selected-avatar-id", "avatar-overlay-pet-visible" }) {
            Value value;
            bool present = info.Values.TryGetValue(key, out value);
            object previous = null;
            if (present) previous = key == "selected-avatar-id" ? (object)QuotedKey(value.Text) : value.Text == "true";
            result.Add(key, new Dictionary<string,object> { {"present", present}, {"value", previous} });
        }
        return result;
    }
    public static string Patch(string text, Dictionary<string,string> desired) {
        Info info = Inspect(text);
        string nl = text.Contains("\r\n") ? "\r\n" : "\n";
        List<Value> edits = new List<Value>(); string addition = "";
        foreach (KeyValuePair<string,string> pair in desired) {
            if(pair.Key != "selected-avatar-id" && pair.Key != "avatar-overlay-pet-visible") throw Unsupported();
            Value value;
            if(info.Values.TryGetValue(pair.Key, out value)) {
                edits.Add(String.IsNullOrEmpty(pair.Value)
                    ? new Value {Start=value.LineStart,Length=value.LineLength,Text=value.RemovalText}
                    : new Value {Start=value.Start,Length=value.Length,Text=pair.Value});
            } else if(!String.IsNullOrEmpty(pair.Value)) addition += pair.Key + " = " + pair.Value + nl;
        }
        if(addition != "") {
            string prefix = info.InsertAt > 0 && text[info.InsertAt - 1] != '\n' && text[info.InsertAt - 1] != '\r' ? nl : "";
            if(!info.HasDesktop) prefix += (info.InsertAt > 0 ? nl : "") + "[desktop]" + nl;
            edits.Add(new Value {Start=info.InsertAt,Length=0,Text=prefix+addition});
        }
        edits.Sort(delegate(Value a,Value b) {return b.Start.CompareTo(a.Start);});
        foreach(Value edit in edits) text=text.Remove(edit.Start,edit.Length).Insert(edit.Start,edit.Text);
        Info after=Inspect(text);
        foreach(KeyValuePair<string,string> pair in desired) {
            Value value; bool found=after.Values.TryGetValue(pair.Key,out value);
            if(String.IsNullOrEmpty(pair.Value) ? found : (!found || value.Text != pair.Value)) throw Unsupported();
        }
        return text;
    }
}
'@
}

if (-not ('BailongUninstallArgvV1' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class BailongUninstallArgvV1 {
    [DllImport("shell32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern IntPtr CommandLineToArgvW(string command,out int count);
    [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr memory);
    public static string[] Parse(string command) {
        if(String.IsNullOrWhiteSpace(command)) return new string[0];
        int count; IntPtr memory=CommandLineToArgvW(command,out count);
        if(memory==IntPtr.Zero) return new string[0];
        try { string[] values=new string[count];
            for(int i=0;i<count;i++) values[i]=Marshal.PtrToStringUni(Marshal.ReadIntPtr(memory,i*IntPtr.Size));
            return values;
        } finally { LocalFree(memory); }
    }
}
'@
}

function Parse-Startup([byte[]]$Bytes) {
    if ($null -eq $Bytes) { return $null }
    try {
        $text=if($Bytes.Length -ge 2 -and $Bytes[0] -eq 255 -and $Bytes[1] -eq 254) {
            [Text.Encoding]::Unicode.GetString($Bytes).TrimStart([char]0xFEFF)
        } else { [Text.Encoding]::UTF8.GetString($Bytes).TrimStart([char]0xFEFF) }
        $lines=$text.TrimEnd([char]13,[char]10) -split '\r?\n'
        if ($lines.Count -ne 5 -or $lines[0] -cne 'Option Explicit' -or $lines[1] -cne 'Dim shell' -or
            $lines[2] -cne 'Set shell = CreateObject("WScript.Shell")') { return $null }
        $directory=[regex]::Match($lines[3],'^shell\.CurrentDirectory = "((?:[^"]|"")*)"$')
        $run=[regex]::Match($lines[4],'^shell\.Run "((?:[^"]|"")*)", 0, False$')
        if (-not $directory.Success -or -not $run.Success) { return $null }
        $root=Assert-LocalPath $directory.Groups[1].Value.Replace('""','"')
        $arguments=[BailongUninstallArgvV1]::Parse($run.Groups[1].Value.Replace('""','"'))
        if ($arguments.Count -ne 2 -or -not [IO.Path]::IsPathRooted($arguments[0]) -or
            [IO.Path]::GetFileName($arguments[0]) -ine 'pythonw.exe' -or
            -not (Same-Path $arguments[1] (Join-Path $root 'codex-integration\bridge\bridge_supervisor.py'))) { return $null }
        return [pscustomobject]@{Root=$root;Pythonw=(Assert-LocalPath $arguments[0])}
    } catch { return $null }
}

function Valid-Receipt($ReceiptValue,[string]$ExpectedEdition) {
    $schema=Property $ReceiptValue 'schemaVersion'
    if ($null -eq $ReceiptValue -or ($schema -isnot [int] -and $schema -isnot [long]) -or $schema -ne 1 -or
        (Property $ReceiptValue 'edition') -cne $ExpectedEdition) { return $false }
    $recordRoot=Property $ReceiptValue 'dataRoot'
    $avatar=Property $ReceiptValue 'avatarId'
    $expectedLocal=if($ExpectedEdition -eq 'full') {'custom:bailong-full'} else {'custom:bailong'}
    if ($recordRoot -isnot [string] -or -not [IO.Path]::IsPathRooted($recordRoot) -or
        $avatar -isnot [string] -or ($avatar -cne $expectedLocal -and $avatar -cnotmatch '^pet_[A-Za-z0-9_-]+$')) { return $false }
    try { $null=Assert-LocalPath $recordRoot } catch { return $false }
    $files=Property $ReceiptValue 'files'
    foreach ($name in @('pet.json','spritesheet.webp')) {
        $value=Property $files $name
        if ($value -isnot [string] -or $value -cnotmatch '^[0-9a-f]{64}$') { return $false }
    }
    $before=Property $ReceiptValue 'desktopBefore'
    foreach($key in @('selected-avatar-id','avatar-overlay-pet-visible')) {
        $record=Property $before $key
        $present=Property $record 'present'
        $value=Property $record 'value'
        if ($present -isnot [bool]) { return $false }
        if ($present) {
            if ($key -eq 'selected-avatar-id' -and ($value -isnot [string] -or [string]::IsNullOrWhiteSpace($value))) { return $false }
            if ($key -eq 'avatar-overlay-pet-visible' -and $value -isnot [bool]) { return $false }
        } elseif ($null -ne $value) { return $false }
    }
    $installed=Property $ReceiptValue 'desktopInstalled'
    if ((Property $installed 'selected-avatar-id') -cne $avatar -or
        (Property $installed 'avatar-overlay-pet-visible') -isnot [bool] -or
        (Property $installed 'avatar-overlay-pet-visible') -cne $true) { return $false }
    return $true
}

function Process-Role($Process) {
    $executable=Property $Process 'ExecutablePath'
    $command=Property $Process 'CommandLine'
    if ($executable -isnot [string] -or $command -isnot [string]) { return $null }
    $arguments=[BailongUninstallArgvV1]::Parse($command)
    if ($arguments.Count -lt 2) { return $null }
    $name=[IO.Path]::GetFileName($executable).ToLowerInvariant()
    if ($name -in @('python.exe','pythonw.exe')) {
        $allowed=Same-Path $executable (Join-Path $dataPath ('runtime\'+$name))
        if (-not $allowed -and $legacyProof) {
            $allowed=Same-Path $executable (Join-Path (Split-Path -Parent $legacyProof.Pythonw) $name)
        }
        $script=$arguments[1]
        $tail=@($arguments | Select-Object -Skip 2)
        if (Same-Path $script (Join-Path $bridgePath 'detached_launcher.py')) {
            if ($tail -cnotcontains '--verified-child') { return $null }
            $divider=[Array]::IndexOf($arguments,'--')
            if ($divider -lt 2 -or $divider+1 -ge $arguments.Count) { return $null }
            $script=$arguments[$divider+1]
            $tail=@($arguments | Select-Object -Skip ($divider+2))
        }
        if (Same-Path $script (Join-Path $bridgePath 'bridge_supervisor.py')) { return $(if($allowed){'supervisor'}else{'unverified-supervisor'}) }
        if (Same-Path $script (Join-Path $bridgePath 'bridge_pet.py')) { return $(if($allowed){'worker'}else{'unverified-worker'}) }
        if ((Same-Path $script (Join-Path $dataPath 'install.py')) -and $tail -ccontains '--finish') { return $(if($allowed){'waiter'}else{'unverified-waiter'}) }
    } elseif ($name -eq 'powershell.exe' -and
        (Same-Path $executable (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'))) {
        for($i=1;$i -lt $arguments.Count-1;$i++) {
            if ($arguments[$i] -ieq '-File') {
                foreach($file in @('codex_observer.ps1','native_mini_control.ps1','activity_ui_hints.ps1')) {
                    if (Same-Path $arguments[$i+1] (Join-Path $bridgePath $file)) { return 'observer' }
                }
                return $null
            }
        }
    } elseif ($name -eq 'wscript.exe' -and $startupOwned -and
              (Same-Path $executable (Join-Path $env:SystemRoot 'System32\wscript.exe'))) {
        if (Same-Path $arguments[1] $startupPath) { return 'startup-launcher' }
    }
    return $null
}
function Owned-Processes {
    $found=@()
    foreach($process in @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='powershell.exe' OR Name='wscript.exe'")) {
        $role=Process-Role $process
        if ($role) { $found += [pscustomobject]@{Process=$process;Role=$role;Id=[int]$process.ProcessId;Created=$process.CreationDate} }
    }
    return $found
}
function Can-ForceWorker([int]$ExpectedPid,[DateTime]$Created) {
    try {
        $path=Join-Path $bridgePath 'pet-status.json'
        if (-not [IO.File]::Exists($path) -or ([DateTime]::UtcNow-(Get-Item -LiteralPath $path).LastWriteTimeUtc).TotalSeconds -gt 15) { return $false }
        if((Get-Item -LiteralPath $path).LastWriteTimeUtc -lt $Created.ToUniversalTime()){return $false}
        $status=$utf8.GetString((Read-Bytes $path 65536)) | ConvertFrom-Json
        if((Property $status 'pid') -ne $ExpectedPid){return $false}
        return (Property $status 'phase') -eq 'loading-animations' -or (Property $status 'chatBusy') -ceq $false
    } catch { return $false }
}

$dataPath=Assert-LocalPath $DataRoot
if ([string]::IsNullOrWhiteSpace($CodexHome)) {
    $CodexHome=if($Edition -eq 'native' -and -not [string]::IsNullOrWhiteSpace($env:CODEX_HOME)) { $env:CODEX_HOME }
        else { Join-Path $env:USERPROFILE '.codex' }
}
if ([string]::IsNullOrWhiteSpace($StartupDirectory)) { $StartupDirectory=[Environment]::GetFolderPath('Startup') }
$homePath=Assert-LocalPath $CodexHome
$startupDir=Assert-LocalPath $StartupDirectory
if (Same-Path $dataPath ([IO.Path]::GetPathRoot($dataPath))) { throw '不能将磁盘根目录作为桌宠目录。' }
Set-Location -LiteralPath ([IO.Path]::GetTempPath())
$utf8=New-Object Text.UTF8Encoding($false,$true)
$slug=if($Edition -eq 'full') {'bailong-full'} else {'bailong'}
$localId='custom:'+$slug
$bridgePath=Join-Path $dataPath 'codex-integration\bridge'
$petPath=Assert-LocalPath (Join-Path $homePath ('pets\'+$slug))
$receiptPath=Assert-LocalPath (Join-Path $homePath ('pet-install-receipts\'+$slug+'.json'))
$configPath=Assert-LocalPath (Join-Path $homePath 'config.toml')
$startupPath=Assert-LocalPath (Join-Path $startupDir 'BailongCodexBridge.vbs')
$receiptRaw=Read-Bytes $receiptPath
$receipt=$null
if($null -ne $receiptRaw) {
    try { $receipt=$utf8.GetString($receiptRaw).TrimStart([char]0xFEFF) | ConvertFrom-Json } catch { throw '卸载记录无效；未删除文件。' }
    if(-not (Valid-Receipt $receipt $Edition)) { throw '卸载记录无法验证；未删除文件。' }
}
$receiptOwned=$null -ne $receipt -and (Same-Path (Property $receipt 'dataRoot') $dataPath)
$newerInstallation=$null -ne $receipt -and -not $receiptOwned
$startupBefore=if($Edition -eq 'full') {Read-Bytes $startupPath} else {$null}
$startupRecord=Parse-Startup $startupBefore
$startupOwned=$Edition -eq 'full' -and $null -ne $startupRecord -and (Same-Path $startupRecord.Root $dataPath)
$legacyProof=if($startupOwned) {$startupRecord} else {$null}
$sourceEntry=if($Edition -eq 'full') {Join-Path $dataPath 'codex-integration\full-entry'} else {Join-Path $dataPath 'bailong'}
$expected=@{}
foreach($name in @('pet.json','spritesheet.webp')) {
    if($receiptOwned) { $expected[$name]=Property (Property $receipt 'files') $name }
    else {
        $sourceBytes=Read-Bytes (Join-Path $sourceEntry $name) 33554432
        if($null -ne $sourceBytes) {$expected[$name]=Hash-Bytes $sourceBytes}
    }
}
$currentFiles=@{}
$pairMatches=$expected.Count -eq 2
$anyPetFile=$false
$matchedPresent=0
foreach($name in @('pet.json','spritesheet.webp')) {
    $bytes=Read-Bytes (Join-Path $petPath $name) 33554432
    $currentFiles[$name]=$bytes
    if($null -ne $bytes) {
        $anyPetFile=$true
        if(-not $expected.ContainsKey($name) -or (Hash-Bytes $bytes) -cne $expected[$name]) {$pairMatches=$false} else {$matchedPresent++}
    }
}
$pairMatches=$pairMatches -or -not $anyPetFile
$registrationOwned=-not $newerInstallation -and ($receiptOwned -or ($Edition -eq 'full' -and $startupOwned) -or ($Edition -eq 'native' -and $pairMatches -and $matchedPresent -eq 2))
$removeRegistration=$registrationOwned -and $pairMatches
$modifiedAssets=$registrationOwned -and -not $pairMatches
$ownedIds=New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
[void]$ownedIds.Add($localId)
if($receiptOwned) {[void]$ownedIds.Add((Property $receipt 'avatarId'))}
try {
    $globalBytes=Read-Bytes (Join-Path $homePath '.codex-global-state.json') 33554432
    if($null -ne $globalBytes) {
        $global=$utf8.GetString($globalBytes).TrimStart([char]0xFEFF) | ConvertFrom-Json
        $maps=Property (Property $global 'electron-persisted-atom-state') 'migrated-cloud-pet-ids-v1'
        if($null -ne $maps) {
            foreach($entry in $maps.PSObject.Properties) {
                $value=Property $entry.Value $localId
                if($value -is [string] -and $value -cmatch '^pet_[A-Za-z0-9_-]+$') {[void]$ownedIds.Add($value)}
            }
        }
    }
} catch { }
$beforeConfig=Read-Bytes $configPath
$newConfig=$beforeConfig
$settingsChanged=$false
$configCleanupError=$null
try {
if($removeRegistration -and $null -ne $beforeConfig) {
    $configText=$utf8.GetString($beforeConfig).TrimStart([char]0xFEFF)
    $currentDesktop=[BailongUninstallTomlV1]::DesktopBefore($configText)
    $selected=$currentDesktop['selected-avatar-id']['value']
    if($selected -is [string] -and $ownedIds.Contains($selected)) {
        $desired=New-Object 'System.Collections.Generic.Dictionary[string,string]'
        if($receiptOwned) {
            $old=Property (Property $receipt 'desktopBefore') 'selected-avatar-id'
            $oldValue=Property $old 'value'
            $previousSelectionOwned=(Property $old 'present') -and $ownedIds.Contains($oldValue)
            if((Property $old 'present') -and -not $previousSelectionOwned) {$desired.Add('selected-avatar-id',($oldValue | ConvertTo-Json -Compress))}
            else {$desired.Add('selected-avatar-id',$null)}
            $installed=Property (Property $receipt 'desktopInstalled') 'avatar-overlay-pet-visible'
            if($currentDesktop['avatar-overlay-pet-visible']['value'] -ceq $installed) {
                $visible=Property (Property $receipt 'desktopBefore') 'avatar-overlay-pet-visible'
                $desired.Add('avatar-overlay-pet-visible',$(if($previousSelectionOwned){'false'} elseif(Property $visible 'present') {if(Property $visible 'value') {'true'} else {'false'}} else {$null}))
            }
        } else {
            $desired.Add('selected-avatar-id',$null)
            $desired.Add('avatar-overlay-pet-visible','false')
        }
        $newText=[BailongUninstallTomlV1]::Patch($configText,$desired)
        $newConfig=$utf8.GetBytes($newText)
        if($beforeConfig.Length -ge 3 -and $beforeConfig[0] -eq 239 -and $beforeConfig[1] -eq 187 -and $beforeConfig[2] -eq 191) {
            $newConfig=[byte[]](@(239,187,191)+$newConfig)
        }
        $settingsChanged=-not (Same-Bytes $beforeConfig $newConfig)
    }
}
} catch {
    # Safe unregistration may be unavailable for unusual TOML. Stopping this
    # package's own background helpers is still useful and must not be blocked.
    $configCleanupError='配置写法无法安全修改，已保留宠物条目、安装记录与原设置。'
    $removeRegistration=$false
    $settingsChanged=$false
    $newConfig=$beforeConfig
}
$ownedBefore=@(if($Edition -eq 'full') {Owned-Processes})
if($CheckOnly) {
    [pscustomobject]@{Status='CheckOnly';Edition=$Edition;StartupOwned=$startupOwned;ReceiptOwned=$receiptOwned;NewerInstallation=$newerInstallation;
        RegistrationOwned=$registrationOwned;ModifiedAssets=$modifiedAssets;SettingsWouldChange=$settingsChanged;OwnedProcessCount=$ownedBefore.Count;ConfigCleanupDeferred=($null -ne $configCleanupError)} | ConvertTo-Json -Compress
    exit 0
}

$startupRemoved=$false
if($startupOwned) {
    if(-not (Same-Bytes (Read-Bytes $startupPath) $startupBefore)) {throw '启动项在卸载期间已改变；请重试。'}
    [IO.File]::Delete($startupPath)
    $startupRemoved=$true
}
if($Edition -eq 'full') {
    if([IO.Directory]::Exists($dataPath)) {Atomic-Write (Join-Path $dataPath 'uninstall.request') $utf8.GetBytes('uninstall requested')}
    if([IO.Directory]::Exists($bridgePath)) {
        foreach($name in @('supervisor.stop','pet.stop','observer.stop')) {Atomic-Write (Join-Path $bridgePath $name) $utf8.GetBytes('uninstall requested')}
    }
    $deadline=[DateTime]::UtcNow.AddSeconds(15)
    do {
        $remaining=@(Owned-Processes)
        if($remaining.Count -eq 0) {break}
        Start-Sleep -Milliseconds 250
    } while([DateTime]::UtcNow -lt $deadline)
    foreach($entry in $remaining) {
        if($entry.Role.StartsWith('unverified-')) {continue}
        if($entry.Role -eq 'worker' -and -not (Can-ForceWorker $entry.Id $entry.Created)) {continue}
        $current=Get-CimInstance Win32_Process -Filter ('ProcessId='+$entry.Id) -ErrorAction SilentlyContinue
        if($null -ne $current -and $current.CreationDate -eq $entry.Created -and (Process-Role $current) -ceq $entry.Role) {
            Stop-Process -Id $entry.Id -Force -ErrorAction Stop
        }
    }
    $remaining=@(Owned-Processes)
    if($remaining.Count -gt 0) {throw '桌宠仍在结束聊天或后台任务，已关闭自启动但暂未删除注册素材；请稍后再运行卸载。'}
}
$removed=@()
$receiptRemoved=$false
if($removeRegistration) {
    try {
        if(-not (Same-Bytes (Read-Bytes $configPath) $beforeConfig)) {throw 'Codex 设置在卸载期间已改变；未覆盖新设置。'}
        foreach($name in @('pet.json','spritesheet.webp')) {
            if(-not (Same-Bytes (Read-Bytes (Join-Path $petPath $name) 33554432) $currentFiles[$name])) {throw '宠物素材在卸载期间已改变，已停止删除。'}
        }
        if($settingsChanged) {Atomic-Write $configPath $newConfig}
        foreach($name in @('pet.json','spritesheet.webp')) {
            $path=Join-Path $petPath $name
            if($null -ne $currentFiles[$name]) {[IO.File]::Delete($path);$removed+=$name}
        }
        if($receiptOwned) {
            if(-not (Same-Bytes (Read-Bytes $receiptPath) $receiptRaw)) {throw '安装记录在卸载期间已改变。'}
            [IO.File]::Delete($receiptPath);$receiptRemoved=$true
        }
    } catch {
        foreach($name in $removed) {
            $path=Join-Path $petPath $name
            if(-not [IO.File]::Exists($path)) {Atomic-Write $path $currentFiles[$name]}
        }
        if($settingsChanged -and (Same-Bytes (Read-Bytes $configPath) $newConfig)) {Atomic-Write $configPath $beforeConfig}
        throw
    }
    if([IO.Directory]::Exists($petPath) -and [IO.Directory]::GetFileSystemEntries($petPath).Length -eq 0) {
        [IO.Directory]::Delete($petPath,$false)
    }
}
$status=if($newerInstallation) {'PreservedNewerInstallation'} elseif($modifiedAssets) {'PreservedModifiedAssets'}
    elseif($null -ne $configCleanupError) {'StoppedWithPreservedConfiguration'}
    elseif($removeRegistration -or $startupRemoved -or $ownedBefore.Count) {'Uninstalled'} else {'AlreadyUninstalled'}
[pscustomobject]@{Status=$status;Edition=$Edition;StartupRemoved=$startupRemoved;PetFilesRemoved=$removed.Count;
    SettingsChanged=$settingsChanged;ReceiptRemoved=$receiptRemoved;OwnProcessesReleased=$true;PackageFolderPreserved=$true;
    PersonalHistoryPreserved=$true;OtherInstallationPreserved=$newerInstallation;ConfigCleanupDeferred=($null -ne $configCleanupError);ConfigCleanupError=$configCleanupError} | ConvertTo-Json -Compress
Write-Host '卸载处理完成。原解压目录已保留，可在关闭本窗口后自行删除；聊天记录和其他项目设置保持不变。'
if($newerInstallation) {Write-Host '检测到另一份安装仍在使用，已保留其宠物注册和设置。'}
if($modifiedAssets) {Write-Host '检测到宠物素材已被修改，已保留这些文件和对应设置。'}
if($null -ne $configCleanupError) {Write-Host $configCleanupError}
