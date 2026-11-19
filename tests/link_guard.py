#!/usr/bin/env python3
"""No test source may create a link.

House rule (the owner's, stated twice): symlinks are prohibited anywhere in
this repository, junctions and other reparse points included, so a test that
needs an alias proves the decision that refuses one by handing in its answer
- never by making the alias on the host first. A fixture that creates a link
is also a fixture that needs a privilege Windows does not grant by default,
which turns a real property into a skip.

Hard links are not symlinks and are outside this rule; a fixture reaching for
one is a review question, not something this check answers.

Python is read as a syntax tree, so a comment, a message, or a `mock.patch`
that *refuses* a link is never a finding, an alias is followed whether it is
imported (`from os import symlink as link`) or assigned (`make = os.symlink`,
`other = make`), and a command handed to `subprocess`, `os.system`, or
`os.popen` is read from its words.

TypeScript, JavaScript, shell, and PowerShell are read for the commands they
name. Shell and PowerShell are tokenized, so a quoted argument keeps the
structure it belongs to - `ln "-s" a b` and `New-Item -ItemType "Junction"`
are read as the commands they are - while a quoted example inside another
command (`echo 'ln -s a b'`) is not promoted into a command of its own.
JavaScript is read as one source: its comments are blanked and its strings
masked at equal length, every `child_process` call is found in the mask and
bounded by its own balanced parentheses, and the literal command that call
runs is read as the command it is. What this cannot see is an operation
built at run time from pieces this check does not resolve (a command name
read from a file, a template literal with an interpolation, say): it is a
static barrier and a regression guard, not a proof that no reparse point
can ever be created.

    python3 tests/link_guard.py            # scan this repository's tests/

Exit 0 when nothing creates a link, 1 with a `file: line: what` finding for
every site that does.
"""

import ast
import os
import re
import sys
import warnings

SUFFIXES = (".py", ".ts", ".mts", ".cts", ".cjs", ".mjs", ".js",
            ".sh", ".bash", ".bat", ".cmd", ".ps1")
SHELL_SUFFIXES = (".sh", ".bash", ".bat", ".cmd", ".ps1")
PRUNE = {"node_modules", "__pycache__", ".git", ".pytest_cache", ".vendor"}

# Python: `os.symlink`, `Path.symlink_to`, and any alias of either.
PY_ATTRS = {"symlink", "symlink_to"}
PY_MODULES = {"os", "pathlib"}
PY_IMPORTED = {"symlink", "symlink_to"}
# Python: a link made by a command this file runs.
PY_RUNNERS = {"run", "Popen", "call", "check_call", "check_output", "spawnv",
              "spawnl"}
PY_SUBPROCESS = {"subprocess"}
PY_SYSTEM = {"system", "popen"}

# TypeScript/JavaScript: node:fs's symlink and symlinkSync, and the
# child_process calls that run a command line through a shell.
JS_FS_MODULES = ("fs", "node:fs", "fs/promises", "node:fs/promises")
JS_CHILD_MODULES = ("child_process", "node:child_process")
JS_FS_IMPORT = re.compile(
    r"import\s+(?:\*\s+as\s+([A-Za-z_$][\w$]*)|\{([^}]*)\}|([A-Za-z_$][\w$]*))"
    r"\s+from\s*[\"']([^\"']+)[\"']")
JS_FS_REQUIRE = re.compile(
    r"(?:\*\s+as\s+([A-Za-z_$][\w$]*)|const\s+([A-Za-z_$][\w$]*)\s*=|"
    r"const\s*\{([^}]*)\}\s*=)\s*require\(\s*[\"']([^\"']+)[\"']\s*\)")
JS_LINK_NAMES = ("symlink", "symlinkSync")
JS_RUNNERS = ("exec", "execSync", "execFile", "execFileSync", "spawn",
              "spawnSync")
# The exec pair takes one command line; the other family takes an executable
# and its arguments separately.
JS_SHELL_RUNNERS = ("exec", "execSync")

# Shell and PowerShell: ln -s / --symbolic, mklink, New-Item -ItemType.
SHELL_ITEM_TYPES = ("symboliclink", "junction")
SHELL_SHELLS = {"bash", "sh", "zsh", "dash", "cmd", "powershell", "pwsh",
                "pwsh.exe", "cmd.exe"}
SHELL_COMMAND_FLAGS = {"-c", "/c", "//c", "-command", "-encodedcommand"}
SHELL_WRAPPERS = {"sudo", "env", "command", "nohup", "time"}
SHELL_SEPARATORS = (";", "|", "&")


class LinkCreationGuard:
    """Find a test source that would make a link on the host."""

    def __init__(self, root):
        self.root = root
        self.findings = []

    def check(self):
        """Every finding, as `tests/relative/path: line N: what`."""
        for path in self.sources():
            relative = os.path.relpath(path, self.root).replace(os.sep, "/")
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError as failure:
                # Fail closed: a test source nobody could read is a test
                # source nobody vouched for.
                self.findings.append(f"{relative}: unreadable: {failure}")
                continue
            if path.endswith(".py"):
                sites = LinkCreationGuard.python_sites(text)
            else:
                sites = LinkCreationGuard.text_sites(
                    text, path.endswith(SHELL_SUFFIXES))
            for line, what in sites:
                self.findings.append(f"{relative}: line {line}: {what}")
        return self.findings

    def sources(self):
        """Every test source under the repository's tests directory."""
        base = os.path.join(self.root, "tests")
        found = []

        def unreadable(failure):
            self.findings.append(
                f"tests: unreadable directory: {getattr(failure, 'filename', failure)}")

        for dirpath, dirs, files in os.walk(base, onerror=unreadable):
            dirs[:] = sorted(d for d in dirs if d not in PRUNE)
            for name in sorted(files):
                if name.endswith(SUFFIXES):
                    found.append(os.path.join(dirpath, name))
        return found

    @staticmethod
    def python_sites(text):
        """(line, what) for every link-creating call, from the syntax tree."""
        try:
            with warnings.catch_warnings():
                # A test source with an unrecognised escape is a lint
                # finding elsewhere, not a link finding here.
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(text)
        except SyntaxError as failure:
            return [(failure.lineno or 1,
                     f"does not parse, so it cannot be cleared: {failure.msg}")]
        modules, functions, runners = LinkCreationGuard.bindings(tree)
        sites = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if isinstance(target, ast.Attribute) and target.attr in PY_ATTRS:
                receiver = target.value
                if isinstance(receiver, ast.Name) and receiver.id in modules:
                    sites.append((node.lineno,
                                  f"{receiver.id}.{target.attr} creates a link"))
                elif target.attr == "symlink_to":
                    sites.append((node.lineno, "symlink_to creates a link"))
            elif isinstance(target, ast.Name) and target.id in functions:
                sites.append((node.lineno, f"{target.id} creates a link"))
            sites.extend(LinkCreationGuard.command_sites(node, modules, runners))
        return sorted(set(sites))

    @staticmethod
    def bindings(tree):
        """The names a test source binds: modules, link creators, runners.

        An alias is followed however it is made, because `make = os.symlink`
        then `make(a, b)` creates a link exactly as the direct call does.
        """
        modules = set()
        functions = set()
        runners = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for name in node.names:
                    head = name.name.split(".")[0]
                    if head in PY_MODULES:
                        modules.add(name.asname or head)
                    if head in PY_SUBPROCESS:
                        runners.add(name.asname or head)
            elif isinstance(node, ast.ImportFrom) and node.module:
                for name in node.names:
                    bound = name.asname or name.name
                    if node.module in PY_MODULES and name.name in PY_IMPORTED:
                        functions.add(bound)
                    if node.module in PY_MODULES and name.name in PY_SYSTEM:
                        runners.add(bound)
                    if node.module in PY_SUBPROCESS \
                            and name.name in PY_RUNNERS:
                        runners.add(bound)
        assignments = [(node, LinkCreationGuard.assigned(node))
                       for node in ast.walk(tree)
                       if isinstance(node, (ast.Assign, ast.AnnAssign))]
        changed = True
        while changed:
            # A chain of assignments resolves in whatever order it appears.
            changed = False
            for node, names in assignments:
                value = node.value
                if value is None or not names:
                    continue
                creates = LinkCreationGuard.creates_link(value, modules,
                                                         functions)
                runs = LinkCreationGuard.runs_a_command(value, modules,
                                                        runners)
                for name in names:
                    if creates and name not in functions:
                        functions.add(name)
                        changed = True
                    if runs and name not in runners:
                        runners.add(name)
                        changed = True
        return modules, functions, runners

    @staticmethod
    def assigned(node):
        """The plain names an assignment binds."""
        if isinstance(node, ast.Assign):
            return [target.id for target in node.targets
                    if isinstance(target, ast.Name)]
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            return [node.target.id]
        return []

    @staticmethod
    def creates_link(value, modules, functions):
        """Whether an expression names something that creates a link."""
        if isinstance(value, ast.Name):
            return value.id in functions
        if isinstance(value, ast.Attribute):
            receiver = value.value
            if isinstance(receiver, ast.Name) and receiver.id in modules:
                return value.attr in PY_ATTRS
            return value.attr == "symlink_to"
        return False

    @staticmethod
    def runs_a_command(value, modules, runners):
        """Whether an expression names something that runs a command."""
        if isinstance(value, ast.Name):
            return value.id in runners
        if isinstance(value, ast.Attribute):
            receiver = value.value
            if isinstance(receiver, ast.Name) and receiver.id in modules:
                return value.attr in PY_SYSTEM
            if isinstance(receiver, ast.Name) and receiver.id in runners:
                return value.attr in PY_RUNNERS
        return False

    @staticmethod
    def command_sites(node, modules, runners):
        """A link made by a command a Python call runs."""
        target = node.func
        runner = False
        if isinstance(target, ast.Attribute):
            receiver = target.value
            if isinstance(receiver, ast.Name) and receiver.id in modules \
                    and target.attr in PY_SYSTEM:
                runner = True
            elif isinstance(receiver, ast.Name) and receiver.id in runners \
                    and target.attr in PY_RUNNERS:
                runner = True
        elif isinstance(target, ast.Name) and target.id in runners:
            runner = True
        if not runner or not node.args:
            return []
        for candidate in LinkCreationGuard.commands(node.args[0]):
            for command in LinkCreationGuard.commands_of(candidate):
                sites = LinkCreationGuard.shell_sites(command, node.lineno)
                if sites:
                    return [(node.lineno,
                             "a command run from the test creates a link: "
                             + sites[0][1])]
        return []

    @staticmethod
    def commands(node):
        """The commands an argument names, one per spelling."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, (ast.List, ast.Tuple)):
            pieces = [item.value for item in node.elts
                      if isinstance(item, ast.Constant) and isinstance(item.value, str)]
            return [" ".join(pieces)] if pieces else []
        return []

    @staticmethod
    def text_sites(text, shell):
        """(line, what) for every link-creating call in TS/JS or shell source."""
        if shell:
            sites = []
            for lineno, raw in enumerate(text.splitlines(), 1):
                for command in LinkCreationGuard.commands_of(raw):
                    sites.extend(LinkCreationGuard.shell_sites(command, lineno))
            return sorted(set(sites))
        imported, namespaces = LinkCreationGuard.module_names(
            text, JS_FS_MODULES, JS_LINK_NAMES)
        runners, runner_namespaces = LinkCreationGuard.module_names(
            text, JS_CHILD_MODULES, JS_RUNNERS)
        code = LinkCreationGuard.code_only(text)
        mask = LinkCreationGuard.masked(code)
        sites = []
        for lineno, line in enumerate(mask.splitlines(), 1):
            sites.extend(LinkCreationGuard.js_sites(line, lineno, imported,
                                                    namespaces))
        sites.extend(LinkCreationGuard.js_command_sites(
            code, mask, runners, runner_namespaces))
        return sorted(set(sites))

    @staticmethod
    def module_names(text, sources, wanted):
        """The names a module imports: (named aliases, namespaces)."""
        named = set()
        namespaces = set()
        for match in JS_FS_IMPORT.finditer(text):
            namespace, braces, default, source = match.groups()
            if source not in sources:
                continue
            if namespace:
                namespaces.add(namespace)
            if default:
                namespaces.add(default)
            for piece in (braces or "").split(","):
                parts = [part.strip() for part in re.split(r"\s+as\s+", piece.strip())]
                if parts and parts[-1] and parts[0] in wanted:
                    named.add(parts[-1])
        for match in JS_FS_REQUIRE.finditer(text):
            namespace, default, braces, source = match.groups()
            if source not in sources:
                continue
            if namespace:
                namespaces.add(namespace)
            if default:
                namespaces.add(default)
            for piece in (braces or "").split(","):
                parts = [part.strip() for part in piece.split(":")]
                if parts and parts[-1] and parts[0] in wanted:
                    named.add(parts[-1])
        return named, namespaces

    @staticmethod
    def js_sites(line, lineno, imported, namespaces):
        """(line, what) for one masked JS/TS line."""
        sites = []
        for name in sorted(imported):
            if re.search(r"(?<![\w$.])" + re.escape(name) + r"\s*\(", line):
                sites.append((lineno, f"{name} creates a link"))
        for namespace in sorted(namespaces):
            pattern = (r"(?<![\w$])" + re.escape(namespace)
                       + r"\s*(?:\.\s*promises)?\s*\.\s*(symlinkSync|symlink)\s*\(")
            match = re.search(pattern, line)
            if match:
                sites.append((lineno, f"{namespace}.{match.group(1)} creates a link"))
        return sites

    @staticmethod
    def js_command_sites(source, mask, runners, namespaces):
        """Every child_process call, each read from its own bounded span.

        The call is found in the mask, so a runner named inside a string or
        a comment is not a call, and its arguments are read from the source
        between its own parentheses, so a call split across lines and a
        second call on the same line are both read.
        """
        alternatives = [re.escape(name) for name in sorted(runners)]
        family = "|".join(JS_RUNNERS)
        alternatives[:0] = [re.escape(namespace) + r"\s*\.\s*(?:" + family
                            + r")" for namespace in sorted(namespaces)]
        if not alternatives:
            return []
        call = re.compile(r"(?<![\w$.])(?:" + "|".join(alternatives)
                          + r")\s*\(")
        sites = []
        position = 0
        while position < len(mask):
            match = call.search(mask, position)
            if match is None:
                break
            opened = match.end() - 1
            closed = LinkCreationGuard.closing(mask, opened)
            if closed is None:
                break
            name = re.split(r"\s*\(", match.group(0))[0].split(".")[-1]
            literals = LinkCreationGuard.js_strings(source[opened + 1:closed])
            if name in JS_SHELL_RUNNERS:
                command = literals[0] if literals else ""
            else:
                command = " ".join(literals)
            for words in LinkCreationGuard.commands_of(command):
                found = LinkCreationGuard.shell_sites(words)
                if found:
                    line = source.count("\n", 0, match.start()) + 1
                    sites.append((line, "a command run from the test creates "
                                  "a link: " + found[0][1]))
                    break
            position = closed + 1
        return sites

    @staticmethod
    def closing(mask, opened):
        """The offset that closes the parenthesis at `opened`, or None."""
        depth = 0
        index = opened
        while index < len(mask):
            if mask[index] == "(":
                depth += 1
            elif mask[index] == ")":
                depth -= 1
                if depth == 0:
                    return index
            index += 1
        return None

    @staticmethod
    def js_strings(text):
        """The literal contents of the strings in one line of source."""
        literals = []
        index = 0
        while index < len(text):
            char = text[index]
            if char not in ("\"", "'", "`"):
                index += 1
                continue
            index += 1
            pieces = []
            while index < len(text) and text[index] != char:
                if text[index] == "\\" and index + 1 < len(text):
                    pieces.append(text[index + 1])
                    index += 2
                    continue
                pieces.append(text[index])
                index += 1
            literals.append("".join(pieces))
            index += 1
        return literals

    @staticmethod
    def tokens(raw):
        """The words of one line: quotes removed, comments dropped.

        A quoted argument stays one word, so `ln "-s" target link` keeps its
        flag and `New-Item -ItemType "Junction"` keeps its type; a word is
        never promoted into a command of its own.
        """
        words = []
        current = []
        started = False
        quote = None
        index = 0
        while index < len(raw):
            char = raw[index]
            nxt = raw[index + 1] if index + 1 < len(raw) else ""
            if quote:
                if char == quote:
                    quote = None
                elif char == "\\" and quote == "\"" and nxt:
                    current.append(nxt)
                    index += 1
                else:
                    current.append(char)
                index += 1
                continue
            if char in ("\"", "'"):
                quote = char
                started = True
                index += 1
                continue
            if char == "`" and nxt:
                # PowerShell escapes the next character with a backtick.
                current.append(nxt)
                started = True
                index += 2
                continue
            if char == "#" and not started:
                break
            if char.isspace():
                if started:
                    words.append("".join(current))
                    current = []
                    started = False
                index += 1
                continue
            if char in ";&|":
                if started:
                    words.append("".join(current))
                    current = []
                    started = False
                words.append(char)
                index += 1
                continue
            current.append(char)
            started = True
            index += 1
        if started:
            words.append("".join(current))
        return words

    @staticmethod
    def commands_of(raw):
        """The word lists of one line, one per command it runs."""
        commands = []
        words = []
        for word in LinkCreationGuard.tokens(raw):
            if word in SHELL_SEPARATORS:
                if words:
                    commands.append(words)
                words = []
                continue
            words.append(word)
        if words:
            commands.append(words)
        return commands

    @staticmethod
    def shell_sites(words, lineno=None):
        """(line, what) for one command, given as its words."""
        words = list(words)
        while words:
            head = os.path.basename(words[0]).lower()
            if head in SHELL_WRAPPERS or words[0].startswith("-") \
                    or "=" in words[0]:
                words = words[1:]
                continue
            break
        if not words:
            return []
        name = os.path.basename(words[0]).lower()
        if name in SHELL_SHELLS and len(words) > 1:
            # A shell wrapper: read the command it wraps as a command.
            for index, word in enumerate(words[1:], 1):
                if word.lower() in SHELL_COMMAND_FLAGS:
                    sites = []
                    for command in LinkCreationGuard.commands_of(
                            " ".join(words[index + 1:])):
                        sites.extend(LinkCreationGuard.shell_sites(
                            command, lineno))
                    return sites
            return []
        if name in ("ln", "ln.exe"):
            flags = [word for word in words[1:] if word.startswith("-")]
            if any("s" in flag for flag in flags):
                return [(lineno, "ln with a symbolic flag creates a link")]
            return []
        if name in ("mklink", "mklink.exe"):
            return [(lineno, "mklink creates a link")]
        if name in ("new-item", "ni"):
            for index, word in enumerate(words[:-1]):
                if word.lower() in ("-itemtype", "-type") \
                        and words[index + 1].lower() in SHELL_ITEM_TYPES:
                    return [(lineno, "a PowerShell reparse point is created")]
        return []

    @staticmethod
    def code_only(text):
        """The source with its comments blanked, its offsets and lines kept."""
        out = list(text)
        index = 0
        quote = None
        while index < len(text):
            char = text[index]
            nxt = text[index + 1] if index + 1 < len(text) else ""
            if quote:
                if char == "\\" and nxt:
                    index += 2
                    continue
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in ("\"", "'", "`"):
                quote = char
                index += 1
                continue
            if char == "/" and nxt == "/":
                while index < len(text) and text[index] != "\n":
                    out[index] = " "
                    index += 1
                continue
            if char == "/" and nxt == "*":
                end = text.find("*/", index + 2)
                stop = len(text) if end == -1 else end + 2
                for at in range(index, stop):
                    if out[at] != "\n":
                        out[at] = " "
                index = stop
                continue
            index += 1
        return "".join(out)

    @staticmethod
    def masked(text):
        """The source with its strings blanked, its offsets and lines kept."""
        out = list(text)
        index = 0
        quote = None
        while index < len(text):
            char = text[index]
            nxt = text[index + 1] if index + 1 < len(text) else ""
            if quote:
                if char == "\\" and nxt:
                    out[index] = " "
                    out[index + 1] = " "
                    index += 2
                    continue
                if char == quote:
                    quote = None
                if out[index] != "\n":
                    out[index] = " "
                index += 1
                continue
            if char in ("\"", "'", "`"):
                quote = char
                out[index] = " "
                index += 1
                continue
            index += 1
        return "".join(out)

    @staticmethod
    def self_test():
        """Detector cases in memory: no file, no scratch, and no link."""
        python = LinkCreationGuard.python_sites
        script = lambda text: LinkCreationGuard.text_sites(  # noqa: E731
            text, True)
        js = lambda text: LinkCreationGuard.text_sites(  # noqa: E731
            text, False)
        cases = [
            (python, "import os\nos.symlink(a, b)\n", 1,
             "os.symlink is found"),
            (python, "import os as operating\noperating.symlink(a, b)\n", 1,
             "an aliased module is found"),
            (python, "from os import symlink as link\nlink(a, b)\n", 1,
             "an aliased import is found"),
            (python, "from pathlib import Path\nPath(b).symlink_to(a)\n", 1,
             "Path.symlink_to is found"),
            (python, "import os\nmake_link = os.symlink\nmake_link(a, b)\n", 1,
             "an assigned alias is found"),
            (python, "import os\nmake = os.symlink\nother = make\n"
                     "other(a, b)\n", 1, "an assigned alias of a chain is found"),
            (python, "import subprocess\n"
                     "subprocess.run(['ln', '-sfn', 'a', 'b'])\n", 1,
             "a subprocess command is found"),
            (python, "import os\nos.system('ln -s a b')\n", 1,
             "an os.system command is found"),
            (python, "from os import system\nsystem('ln -s a b')\n", 1,
             "an imported os.system is found"),
            (python, "from os import popen as run_it\nrun_it('mklink a b')\n", 1,
             "an aliased os.popen is found"),
            (python, "import subprocess\nsubprocess.run(['git', 'status'])\n", 0,
             "an ordinary command is silent"),
            (python, "# os.symlink(a, b)\nmessage = 'a symlink is refused'\n", 0,
             "a comment and a message are silent"),
            (python, "mock.patch.object(Path, 'symlink_to', side_effect=refuse)\n",
             0, "refusing a link is not creating one"),
            (python, "def broken(:\n", 1,
             "an unparsable source is a finding"),
            (js, "import { symlinkSync } from 'node:fs';\nsymlinkSync(a, b);\n",
             1, "symlinkSync is found"),
            (js, "import { symlinkSync as makeLink } from 'node:fs';\n"
                 "makeLink(a, b);\n", 1, "a renamed import is found"),
            (js, "import * as fs from 'node:fs';\nfs.symlink(a, b);\n", 1,
             "an fs namespace is found"),
            (js, "import * as io from 'node:fs';\nio.promises.symlink(a, b);\n",
             1, "fs.promises.symlink is found"),
            (js, "const fs = require('node:fs');\nfs.symlinkSync(a, b);\n", 1,
             "a require'd fs namespace is found"),
            (js, "const message = 'symlinkSync(a, b)';\n", 0,
             "a JavaScript string is silent"),
            (js, "// symlinkSync(a, b) is forbidden here\n", 0,
             "a comment about a link is silent"),
            (js, "const value = 1; /* symlinkSync(a, b) */\n", 0,
             "an inline block comment is silent"),
            (js, "import { execSync } from 'node:child_process';\n"
                 "execSync(\"ln -s a b\");\n", 1,
             "a command run by a child_process import is found"),
            (js, "import { execSync } from 'node:child_process';\n"
                 "execSync(\"git status\");\n", 0,
             "an ordinary child_process command is silent"),
            (js, "import { execSync } from 'node:child_process';\n"
                 "// execSync(\"ln -s a b\");\n", 0,
             "a commented child_process call is silent"),
            (js, "const cp = require('node:child_process');\n"
                 "cp.execFileSync('ln', ['-s', 'a', 'b']);\n", 1,
             "a child_process namespace call with arguments is found"),
            (js, "import { execSync } from 'node:child_process';\n"
                 "execSync(\"git status\"); execSync(\"ln -s a b\");\n", 1,
             "a second call on the same line is found"),
            (js, "import { execSync } from 'node:child_process';\n"
                 "const example = 'execSync(\"ln -s a b\")';\n", 0,
             "a runner named inside a string is silent"),
            (js, "import { execSync } from 'node:child_process';\n"
                 "execSync(\n  \"ln -s a b\"\n);\n", 1,
             "a call split across lines is found"),
            (script, "ln -sfn target link\n", 1,
             "a combined flag cluster is found"),
            (script, "ln -s target link\n", 1, "a separate -s is found"),
            (script, "ln --symbolic target link\n", 1, "the long flag is found"),
            (script, "ln \"-s\" target link\n", 1, "a quoted flag is found"),
            (script, "ln target link\n", 0, "a plain ln is not a link creation"),
            (script, "grep --symbolic file\n", 0,
             "the long flag outside ln is silent"),
            (script, "# ln -s target link\n", 0, "a shell comment is silent"),
            (script, "mklink link target\n", 1, "mklink is found"),
            (script, "cmd //c mklink link target\n", 1,
             "a wrapped mklink is found"),
            (script, "New-Item -ItemType SymbolicLink -Path link\n", 1,
             "a PowerShell symbolic item is found"),
            (script, "New-Item -ItemType \"Junction\" -Path link -Target x\n", 1,
             "a quoted PowerShell junction is found"),
            (script, "New-Item -Path link -ItemType Directory\n", 0,
             "a PowerShell directory is silent"),
            (script, "echo 'ln -s a b' > notes.txt\n", 0,
             "a quoted example is silent on its own"),
            (script, "bash -c \"ln -s a b\"\n", 1,
             "a hidden shell command is found"),
            (script, "sh -c 'ln -s a b'\n", 1,
             "a hidden shell command in single quotes is found"),
        ]
        problems = []
        for detect, text, expected, label in cases:
            try:
                count = len(detect(text))
            except Exception as failure:  # a detector that raises proves nothing
                problems.append(f"self-test: {label}: raised {failure!r}")
                continue
            if count != expected:
                problems.append(
                    f"self-test: {label}: {count} findings, expected {expected}")
        return problems


def root_of():
    """The repository root this file belongs to: its tests/ parent."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    root = argv[0] if argv else root_of()
    problems = LinkCreationGuard.self_test()
    for problem in problems:
        print(f"link guard: {problem}", file=sys.stderr)
    findings = LinkCreationGuard(root).check()
    for finding in findings:
        print(f"link guard: {finding}", file=sys.stderr)
    if findings or problems:
        return 1
    print("link guard: no test source creates a link")
    return 0


if __name__ == "__main__":
    sys.exit(main())
