"""Keep the configuration inventory in sync with environment reads."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "CONFIGURATION.md"
ENV_OBJECTS = {"os.environ", "environ", "env", "environment", "self.environ", "self.env"}


def _reads(path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    constants = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    names = set()
    for node in ast.walk(tree):
        arg = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
            obj = ast.unparse(node.func.value)
            if (node.func.attr in {"get", "pop", "setdefault"} and obj in ENV_OBJECTS) or (obj == "os" and node.func.attr == "getenv"):
                arg = node.args[0]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_read_bool" and len(node.args) > 1:
            arg = node.args[1]
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value) in ENV_OBJECTS and isinstance(node.ctx, ast.Load):
            arg = node.slice
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            names.add(arg.value)
        elif isinstance(arg, ast.Name) and arg.id in constants:
            names.add(constants[arg.id])
    return names


def _inventory():
    names = set()
    for directory in ("token_tracker", "scripts", "packaging"):
        for pattern in ("*.py", "*.spec"):
            for path in (ROOT / directory).rglob(pattern):
                names.update(_reads(path))
    # These names are computed from the feature dictionary and provider policy.
    feature_tree = ast.parse((ROOT / "token_tracker/features.py").read_text(encoding="utf-8"))
    feature_map = next(n.value for n in feature_tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ENV" for t in n.targets))
    names.update(ast.literal_eval(feature_map).values())
    policy = (ROOT / "token_tracker/provider_policy.py").read_text(encoding="utf-8")
    assert 'prefix = f"TOKEN_TRACKER_{_ENV_NAMES[name]}_"' in policy
    assert all(f'prefix + "{flag}"' in policy for flag in ("COLLECT", "SHOW", "SEND"))
    policy_tree = ast.parse(policy)
    mapping = next(n.value for n in policy_tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_ENV_NAMES" for t in n.targets))
    names.update(f"TOKEN_TRACKER_{provider}_{flag}" for provider in ast.literal_eval(mapping).values() for flag in ("COLLECT", "SHOW", "SEND"))
    swift = (ROOT / "macos/Sources/Host.swift").read_text(encoding="utf-8")
    names.update(re.findall(r'(?:environment|env)\["([A-Za-z_][A-Za-z0-9_]*)"\]', swift))
    shell = (ROOT / "macos/build.sh").read_text(encoding="utf-8")
    names.update(re.findall(r'\$\{?(BRANCHGLANCE_[A-Z_]+)', shell))
    if "$HOME" in shell:
        names.add("HOME")
    # Git Bash discovery iterates over this tuple of OS-provided paths.
    hook_tree = ast.parse((ROOT / "token_tracker/claude_hook_install.py").read_text(encoding="utf-8"))
    for node in ast.walk(hook_tree):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == "variable" and isinstance(node.iter, ast.Tuple):
            names.update(ast.literal_eval(node.iter))
    # The parent supplies the macOS host secret, which the child consumes via a constant.
    return names


def test_environment_inventory_matches_documentation():
    source = _inventory()
    doc = DOC.read_text(encoding="utf-8")
    table = doc.split("<!-- environment inventory -->", 1)[1].split("<!-- end environment inventory -->", 1)[0]
    documented = set(re.findall(r'^\| `([^`]+)` \|', table, re.MULTILINE))
    assert source == documented, f"undocumented: {sorted(source - documented)}; stale: {sorted(documented - source)}"


def test_scanner_recognizes_security_relevant_names():
    assert {"TOKEN_TRACKER_LIMIT_RESET", "TOKEN_TRACKER_CLAUDE_LIVE_USAGE", "TOKEN_TRACKER_LEDGER", "TOKEN_TRACKER_CLAUDE_SEND", "BRANCHGLANCE_MACOS_HOST_SECRET"} <= _inventory()
