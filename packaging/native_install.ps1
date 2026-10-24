[CmdletBinding()]
param([string]$DataRoot, [string]$CodexHome, [switch]$CheckOnly)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($DataRoot)) { $DataRoot = $PSScriptRoot }
if ([string]::IsNullOrWhiteSpace($CodexHome)) {
    $CodexHome = if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) { Join-Path $env:USERPROFILE '.codex' } else { $env:CODEX_HOME }
}

function Assert-LocalPath([string]$Path) {
    $absolute = [IO.Path]::GetFullPath($Path)
    if ($absolute.Length -gt [IO.Path]::GetPathRoot($absolute).Length) { $absolute = $absolute.TrimEnd('\') }
    if ($absolute.StartsWith('\\')) { throw '仅支持本机普通磁盘路径。' }
    $current = $absolute
    while ($current) {
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($null -ne $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw '安装路径不能经过符号链接或目录联接。'
        }
        $parent = [IO.Directory]::GetParent($current)
        if ($null -eq $parent) { break }
        $current = $parent.FullName
    }
    return $absolute
}

function Read-OptionalBytes([string]$Path, [long]$Limit = 1048576) {
    $null = Assert-LocalPath $Path
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $item = Get-Item -LiteralPath $Path
    if ($item.PSIsContainer -or $item.Length -gt $Limit) { throw '安装文件类型或大小异常，未修改配置。' }
    return ,([IO.File]::ReadAllBytes($Path))
}

function Equal-Bytes($Left, $Right) {
    if ($null -eq $Left -or $null -eq $Right) { return ($null -eq $Left -and $null -eq $Right) }
    return [Convert]::ToBase64String([byte[]]$Left) -ceq [Convert]::ToBase64String([byte[]]$Right)
}

function Write-Atomic([string]$Path, [byte[]]$Bytes) {
    $null = Assert-LocalPath $Path
    $parent = Split-Path -Parent $Path
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $temp = Join-Path $parent ('.bailong-install-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllBytes($temp, $Bytes)
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temp, $Path, [NullString]::Value) }
        else { [IO.File]::Move($temp, $Path) }
    }
    finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force } }
}

# A scoped lexer, not a general TOML serializer. It only edits two simple
# desktop values and preserves all other characters. Ambiguous syntax fails
# before any filesystem mutation. Strings/comments cannot impersonate tables.
if (-not ('BailongNativeTomlV1' -as [type])) {
    Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Text;
using System.Text.RegularExpressions;
public static class BailongNativeTomlV1 {
    public sealed class Value { public int Start, Length; public string Text; }
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
                    result.Values.Add(keys[0], new Value { Start = lm.Index + equals + 1 + v.Index, Length = v.Length, Text = v.Value });
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
    public static string Update(string text, string selected) {
        if (!Regex.IsMatch(selected, @"^(custom:bailong|pet_[A-Za-z0-9_-]+)$")) throw Unsupported();
        Info info = Inspect(text);
        string nl = text.Contains("\r\n") ? "\r\n" : "\n";
        Dictionary<string,string> desired = new Dictionary<string,string> {
            {"selected-avatar-id", "\"" + selected + "\""}, {"avatar-overlay-pet-visible", "true"} };
        List<Value> edits = new List<Value>(); string addition = "";
        foreach (KeyValuePair<string,string> pair in desired) {
            Value value;
            if (info.Values.TryGetValue(pair.Key, out value))
                edits.Add(new Value { Start = value.Start, Length = value.Length, Text = pair.Value });
            else addition += pair.Key + " = " + pair.Value + nl;
        }
        if (addition != "") {
            string prefix = info.InsertAt > 0 && text[info.InsertAt - 1] != '\n' && text[info.InsertAt - 1] != '\r' ? nl : "";
            if (!info.HasDesktop) prefix += (info.InsertAt > 0 ? nl : "") + "[desktop]" + nl;
            edits.Add(new Value { Start = info.InsertAt, Length = 0, Text = prefix + addition });
        }
        edits.Sort(delegate(Value a, Value b) { return b.Start.CompareTo(a.Start); });
        foreach (Value edit in edits) text = text.Remove(edit.Start, edit.Length).Insert(edit.Start, edit.Text);
        Info after = Inspect(text);
        if (Selected(text) != selected || after.Values["avatar-overlay-pet-visible"].Text != "true") throw Unsupported();
        return text;
    }
}
'@
}

$dataPath = Assert-LocalPath $DataRoot
$homePath = Assert-LocalPath $CodexHome
$sourcePath = Join-Path $dataPath 'bailong'
$targetPath = Join-Path $homePath 'pets\bailong'
$utf8 = New-Object Text.UTF8Encoding($false, $true)
[byte[]]$manifestBytes = Read-OptionalBytes (Join-Path $sourcePath 'pet.json')
[byte[]]$spriteBytes = Read-OptionalBytes (Join-Path $sourcePath 'spritesheet.webp') 33554432
if ($null -eq $manifestBytes -or $null -eq $spriteBytes) { throw '请完整解压安装包，缺少白龙素材。' }
$manifest = $utf8.GetString($manifestBytes).TrimStart([char]0xFEFF) | ConvertFrom-Json
foreach ($key in @('displayName','spriteVersionNumber','spritesheetPath')) {
    if ($null -eq $manifest.PSObject.Properties[$key]) { throw '白龙清单缺少必需字段。' }
}
if ($manifest.displayName -cne '白龙' -or $manifest.spriteVersionNumber -ne 1 -or $manifest.spritesheetPath -cne 'spritesheet.webp') {
    throw '此安装器只接受简化原生白龙的固定素材清单。'
}
if ($spriteBytes.Length -lt 12 -or [Text.Encoding]::ASCII.GetString($spriteBytes,0,4) -ne 'RIFF' -or
    [Text.Encoding]::ASCII.GetString($spriteBytes,8,4) -ne 'WEBP' -or
    ([uint64][BitConverter]::ToUInt32($spriteBytes,4) + 8) -ne $spriteBytes.Length) { throw '白龙图集文件无效。' }

$configPath = Join-Path $homePath 'config.toml'
[byte[]]$beforeConfig = Read-OptionalBytes $configPath
$hadBom = $null -ne $beforeConfig -and $beforeConfig.Length -ge 3 -and $beforeConfig[0] -eq 239 -and $beforeConfig[1] -eq 187 -and $beforeConfig[2] -eq 191
$offset = if ($hadBom) { 3 } else { 0 }
$configText = if ($null -eq $beforeConfig) { '' } else { $utf8.GetString($beforeConfig, $offset, $beforeConfig.Length - $offset) }
$currentSelected = [BailongNativeTomlV1]::Selected($configText)
$selected = 'custom:bailong'
if ($currentSelected -cmatch '^pet_[A-Za-z0-9_-]+$') {
    try {
        [byte[]]$globalBytes = Read-OptionalBytes (Join-Path $homePath '.codex-global-state.json') 33554432
        if ($null -ne $globalBytes) {
            $global = $utf8.GetString($globalBytes).TrimStart([char]0xFEFF) | ConvertFrom-Json
            $maps = $global.'electron-persisted-atom-state'.'migrated-cloud-pet-ids-v1'
            foreach ($mapping in $maps.PSObject.Properties) {
                $entry = $mapping.Value.PSObject.Properties['custom:bailong']
                if ($null -ne $entry -and $entry.Value -is [string] -and $entry.Value -ceq $currentSelected) {
                    $selected = $currentSelected
                    break
                }
            }
        }
    }
    catch { $selected = 'custom:bailong' }
}
$newText = [BailongNativeTomlV1]::Update($configText, $selected)
[byte[]]$newConfig = $utf8.GetBytes($newText)
if ($hadBom) { $newConfig = [byte[]](@(239,187,191) + $newConfig) }

$targets = @(
    [PSCustomObject]@{ Path=(Join-Path $targetPath 'pet.json'); Bytes=$manifestBytes; Before=$null },
    [PSCustomObject]@{ Path=(Join-Path $targetPath 'spritesheet.webp'); Bytes=$spriteBytes; Before=$null },
    [PSCustomObject]@{ Path=$configPath; Bytes=$newConfig; Before=$null }
)
foreach ($target in $targets) { $target.Before = Read-OptionalBytes $target.Path 33554432 }
if (-not (Equal-Bytes $targets[2].Before $beforeConfig)) { throw 'Codex 配置正在变化，请稍后再安装；未修改文件。' }
$changed = @($targets | Where-Object { -not (Equal-Bytes $_.Before $_.Bytes) })
$backupPath = $null
if (-not $CheckOnly -and $changed.Count -gt 0) {
    $backupPath = Join-Path $homePath ('pet-install-backups\bailong\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
    $written = New-Object 'System.Collections.Generic.List[object]'
    try {
        foreach ($target in $changed) {
            if (-not (Equal-Bytes (Read-OptionalBytes $target.Path 33554432) $target.Before)) { throw '安装时目标文件发生变化，已停止并回滚本次修改。' }
            if ($null -ne $target.Before) { Write-Atomic (Join-Path $backupPath ([IO.Path]::GetFileName($target.Path))) $target.Before }
            Write-Atomic $target.Path $target.Bytes
            $written.Add($target)
        }
    }
    catch {
        for ($i=$written.Count-1; $i -ge 0; $i--) {
            $target = $written[$i]
            if (Equal-Bytes (Read-OptionalBytes $target.Path 33554432) $target.Bytes) {
                if ($null -eq $target.Before) { Remove-Item -LiteralPath $target.Path -Force }
                else { Write-Atomic $target.Path $target.Before }
            }
        }
        throw
    }
}
$status = if ($CheckOnly) { 'CheckOnly' } elseif ($changed.Count -eq 0) { 'AlreadyInstalled' } else { 'Installed' }
[PSCustomObject]@{ Status=$status; Target=$targetPath; FilesChanged=$changed.Count; SettingsChanged=(-not (Equal-Bytes $beforeConfig $newConfig)); BackupDirectory=$backupPath }
if (-not $CheckOnly) {
    Write-Host '安装完成：“白龙”已导入并设为默认宠物，已允许显示。'
    Write-Host '完成当前任务后重开 Codex 加载白龙；若 Mini 此前隐藏，请点击一次“显示 Mini”。'
}
