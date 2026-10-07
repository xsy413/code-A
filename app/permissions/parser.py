from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field


@dataclass
class ParsedCommand:
    argv: list[str]
    dynamic: bool = False
    start: int = 0
    end: int = 0
    invocation: str = ""
    second_end: int = 0
    canonical: str = ""
    shell: str = ""


@dataclass
class Analysis:
    commands: list[ParsedCommand] = field(default_factory=list)
    redirects: list[tuple[str, str]] = field(default_factory=list)
    uncertain: bool = False
    background: bool = False
    errors: str = ""
    test_uncertain: bool = False


def parse_bash(source: str) -> Analysis:
    import tree_sitter_bash
    from tree_sitter import Language, Parser

    tree = Parser(Language(tree_sitter_bash.language())).parse(source.encode("utf-8"))
    result = Analysis(uncertain=tree.root_node.has_error)

    def literal(node) -> tuple[str, bool]:
        text = node.text.decode("utf-8")
        if node.type == "raw_string":
            return text[1:-1], False
        if node.type == "string":
            if any(c.type not in {"string_content", "escape_sequence"} for c in node.named_children):
                return text, True
            return text[1:-1].replace('\\"', '"').replace('\\\\', '\\'), False
        if node.type in {"word", "command_name", "number"}:
            dynamic = any(c.type in {"expansion", "simple_expansion", "command_substitution"} for c in node.named_children)
            return text.replace("\\ ", " "), dynamic or "$" in text or "`" in text
        if node.type == "concatenation":
            parts = [literal(c) for c in node.named_children]
            return "".join(p[0] for p in parts), any(p[1] for p in parts)
        return text, True

    def visit(node):
        if node.type in {"negated_command", "list", "pipeline", "command_substitution", "test_command"}:
            result.test_uncertain = True
        if node.type == "declaration_command":
            name = next((c.type for c in node.children if c.type in {"export", "declare", "typeset", "local", "readonly"}), "export")
            result.commands.append(ParsedCommand([name, *[c.text.decode("utf-8") for c in node.named_children]], True))
            result.uncertain = True
        if node.type == "command":
            parts = [literal(c) for c in node.named_children if c.type not in {"variable_assignment", "file_redirect", "heredoc_redirect"}]
            if parts:
                head = next((c for c in node.named_children if c.type not in {"variable_assignment", "file_redirect", "heredoc_redirect"}), None)
                elements = [c for c in node.named_children if c.type not in {"variable_assignment", "file_redirect", "heredoc_redirect"}]
                result.commands.append(ParsedCommand([p[0] for p in parts], any(p[1] for p in parts),
                    len(source.encode()[:head.start_byte].decode()), len(source.encode()[:head.end_byte].decode()),
                    second_end=len(source.encode()[:elements[1].end_byte].decode()) if len(elements) > 1 else 0))
        if node.type in {"variable_assignment", "function_definition", "for_statement", "while_statement", "if_statement", "case_statement", "process_substitution", "subshell"}:
            result.uncertain = True
        if node.type in {"file_redirect", "heredoc_redirect", "herestring_redirect"}:
            target = node.child_by_field_name("destination")
            operator = next((c.type for c in node.children if not c.is_named and ">" in c.type or not c.is_named and "<" in c.type), "<")
            value, dynamic = literal(target) if target else ("", True)
            if "&" in operator and value in {"1", "2"}:
                value = "&" + value
            result.redirects.append((operator, value))
            result.uncertain |= dynamic
        if node.type == "&" or node.type == "coproc":
            result.background = True
        for child in node.children:
            visit(child)

    visit(tree.root_node)
    return result


# ParseInput receives stdin as data. It never evaluates the submitted script.
_PS_PARSE = r'''
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$PSModuleAutoLoadingPreference = 'None'
Import-Module ([System.IO.Path]::Combine($PSHOME, 'Modules', 'Microsoft.PowerShell.Utility', 'Microsoft.PowerShell.Utility.psd1'))
Import-Module ([System.IO.Path]::Combine($PSHOME, 'Modules', 'Microsoft.PowerShell.Management', 'Microsoft.PowerShell.Management.psd1'))
$s = [Console]::In.ReadToEnd()
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($s, [ref]$tokens, [ref]$errors)
$commands = @($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.CommandAst]}, $true) | ForEach-Object {
  $dynamic = $false
  $words = @($_.CommandElements | ForEach-Object {
    if ($_ -is [System.Management.Automation.Language.CommandParameterAst] -and $null -eq $_.Argument) { $_.Extent.Text }
    elseif ($_ -is [System.Management.Automation.Language.StringConstantExpressionAst]) { $_.Value }
    elseif ($_ -is [System.Management.Automation.Language.ConstantExpressionAst]) { [string]$_.Value }
    elseif ($_ -is [System.Management.Automation.Language.ExpandableStringExpressionAst] -and $_.NestedExpressions.Count -eq 0) { $_.Value }
    else { $dynamic = $true; $_.Extent.Text }
  })
  $canonical = ''
  if (-not $dynamic -and $words.Count -gt 0) {
    $resolved = Get-Command -Name $words[0] -CommandType Alias,Cmdlet -ErrorAction SilentlyContinue
    if ($resolved -is [System.Management.Automation.AliasInfo]) { $resolved = $resolved.ResolvedCommand }
    if ($resolved -is [System.Management.Automation.CmdletInfo] -and $resolved.ModuleName -in @('Microsoft.PowerShell.Core','Microsoft.PowerShell.Management','Microsoft.PowerShell.Utility')) { $canonical = $resolved.Name }
  }
  @{argv=$words; dynamic=$dynamic; canonical=$canonical; invocation=[string]$_.InvocationOperator; start=$_.CommandElements[0].Extent.StartOffset; end=$_.CommandElements[0].Extent.EndOffset; second_end=$(if ($_.CommandElements.Count -gt 1) {$_.CommandElements[1].Extent.EndOffset} else {0})}
})
$redirects = @($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FileRedirectionAst]}, $true) | ForEach-Object {
  @{op=$(if ($_.Append) {'>>'} else {'>'}); target=$_.Location.Extent.Text.Trim("'", '"')}
})
$complex = @($ast.FindAll({param($n)
  $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -or
  $n -is [System.Management.Automation.Language.AssignmentStatementAst] -or
  $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -or
  $n -is [System.Management.Automation.Language.ScriptBlockExpressionAst] -or
  $n -is [System.Management.Automation.Language.VariableExpressionAst] -or
  $n -is [System.Management.Automation.Language.TypeExpressionAst]
}, $true)).Count -gt 0
$background = @($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.PipelineAst] -and $n.Background}, $true)).Count -gt 0
$testUncertain = @($ast.FindAll({param($n) $n.GetType().Name -in @('IfStatementAst','ForStatementAst','ForEachStatementAst','WhileStatementAst','DoWhileStatementAst','DoUntilStatementAst','SwitchStatementAst','TryStatementAst','TrapStatementAst','PipelineChainAst','ParenExpressionAst','SubExpressionAst')}, $true)).Count -gt 0
@{commands=$commands; redirects=$redirects; uncertain=$complex; background=$background; test_uncertain=$testUncertain; errors=@($errors | ForEach-Object {$_.Message})} | ConvertTo-Json -Depth 8 -Compress
'''


def parse_powershell(source: str, executable: str, env: dict[str, str]) -> Analysis:
    try:
        completed = subprocess.run(
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _PS_PARSE],
            input=source, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, env=env,
        )
        if completed.returncode:
            return Analysis(uncertain=True, errors="PowerShell AST parser unavailable")
        data = json.loads(completed.stdout.lstrip("\ufeff").strip())
        result = Analysis(uncertain=bool(data["uncertain"] or data["errors"]), background=data["background"],
                          test_uncertain=bool(data.get("test_uncertain")))
        for item in data["commands"]:
            offset = lambda n: len(source.encode("utf-16-le")[:n * 2].decode("utf-16-le"))
            result.commands.append(ParsedCommand(item["argv"], bool(item["dynamic"] or item["invocation"] == "Dot"),
                offset(item["start"]), offset(item["end"]), item["invocation"], offset(item["second_end"]), item.get("canonical", "").lower()))
        result.redirects = [(r["op"], r["target"]) for r in data["redirects"]]
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return Analysis(uncertain=True, errors="PowerShell AST parser unavailable")
