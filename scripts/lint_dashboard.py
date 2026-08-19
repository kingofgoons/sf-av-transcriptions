#!/usr/bin/env python3
"""Pre-flight checks for the modular Streamlit dashboard.

WHY THIS EXISTS: on 2026-08-19 the modularized app was deployed with two runtime-fatal
bugs that `python -m compileall` cannot detect, because they are NAME errors rather than
syntax errors:

    sf_exports.py  used datetime.now() with no `from datetime import datetime`
    tab_browse.py  used `session` but its render() signature did not accept it

Both surfaced only in the browser as "Application error: name 'datetime' is not defined".
Compiling proves a file parses; it does not prove the names it references exist. This
script closes that gap and is run by 09_deploy_dashboard.sh BEFORE upload.

Checks:
  1. every module parses
  2. no undefined names at module or function scope
  3. every tab_*.py exposes render(), and its signature matches the entrypoint's call
  4. no module-level reads of NAMES.* (they must be read at call time - see sf_config)
  5. no stdlib shadowing, and no pages/ directory

Exits non-zero on any failure.
"""

import ast
import builtins
import pathlib
import re
import sys

BUILTINS = set(dir(builtins))
STDLIB_SHADOW = {
    'config', 'data', 'json', 'io', 're', 'types', 'time', 'base64', 'traceback',
    'string', 'random', 'math', 'os', 'sys', 'copy', 'enum', 'abc', 'csv', 'uuid',
}
ENTRYPOINT = 'transcription_dashboard.py'


def collect_bound(tree, extra):
    """Names bound anywhere in the module. Deliberately flat rather than scope-accurate:
    a false negative is better than blocking a deploy on a scoping subtlety."""
    bound = set(extra) | BUILTINS | {'__name__', '__file__', '__doc__'}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                bound.add((a.asname or a.name).split('.')[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                bound.add(a.asname or a.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                for x in ast.walk(t):
                    if isinstance(x, ast.Name):
                        bound.add(x.id)
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            for x in ast.walk(n.target):
                if isinstance(x, ast.Name):
                    bound.add(x.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, ast.withitem) and n.optional_vars is not None:
            for x in ast.walk(n.optional_vars):
                if isinstance(x, ast.Name):
                    bound.add(x.id)
        elif isinstance(n, ast.Global):
            bound.update(n.names)
    return bound


def main(app_dir):
    app = pathlib.Path(app_dir)
    pyfiles = sorted(app.glob('*.py'))
    if not pyfiles:
        print(f"FAIL: no .py modules found in {app}")
        return 1
    if not (app / ENTRYPOINT).is_file():
        print(f"FAIL: entrypoint {ENTRYPOINT} must be in the ROOT of {app} "
              f"(warehouse runtime requirement)")
        return 1
    if (app / 'pages').is_dir():
        print("FAIL: pages/ exists. Streamlit treats it as automatic multipage "
              "navigation, which restructures the app. Use flat tab_*.py modules.")
        return 1

    # environment.yml: required, and must not pin `python`.
    #
    # Required because with no environment.yml Snowflake resolves the OLDEST supported
    # Streamlit (1.22.0), which breaks file_uploader, download_button, fragment and
    # hide_index. The app still loads, so the failure is invisible without a check.
    #
    # `python=` is rejected because on a warehouse-runtime STREAMLIT object the entry is
    # translated into a Python function package spec `python==3.11` - not a resolvable
    # package - and the app dies at load with "Packages not found: python==3.11". The
    # Snowflake docs show `- python=3.11` in their example, so this is an easy trap to walk
    # back into. It took the app down on 2026-08-19.
    env = app / 'environment.yml'
    if not env.is_file():
        print("FAIL: environment.yml missing from the source root. Without it Snowflake "
              "resolves Streamlit 1.22.0 and several components break silently.")
        return 1
    env_lines = [ln.split('#', 1)[0].strip() for ln in env.read_text().splitlines()]
    if any(re.match(r'^-\s*python\s*[=<>]', ln) for ln in env_lines):
        print("FAIL: environment.yml pins `python`. On warehouse runtime this becomes the "
              "package spec `python==3.11`, which does not resolve, and the app dies at "
              "load with 'Packages not found: python==3.11'. Remove the python entry - the "
              "base environment already provides 3.11.")
        return 1
    if not any(re.match(r'^-\s*streamlit\s*=\s*\d+\.\d+', ln) for ln in env_lines):
        print("FAIL: environment.yml does not pin a streamlit version (expected e.g. "
              "`- streamlit=1.52.2`). Unpinned means Snowflake resolves 1.22.0.")
        return 1

    local = {p.stem for p in pyfiles}
    failures = []

    # 1 + 2: parse and undefined names
    trees = {}
    for f in pyfiles:
        if f.stem in STDLIB_SHADOW:
            failures.append(f"{f.name}: shadows a stdlib module name")
        try:
            trees[f] = ast.parse(f.read_text())
        except SyntaxError as e:
            failures.append(f"{f.name}:{e.lineno}: syntax error: {e.msg}")

    for f, tree in trees.items():
        bound = collect_bound(tree, local)
        seen = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                seen.setdefault(n.id, n.lineno)
        for name, ln in sorted(seen.items(), key=lambda kv: kv[1]):
            if name not in bound:
                failures.append(f"{f.name}:{ln}: undefined name '{name}'")

    # 3: tab render() signatures must match how the entrypoint calls them
    entry_src = (app / ENTRYPOINT).read_text()
    for f, tree in trees.items():
        if not f.stem.startswith('tab_'):
            continue
        fn = next((n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == 'render'), None)
        if fn is None:
            failures.append(f"{f.name}: no render() function")
            continue
        params = [a.arg for a in fn.args.args]
        m = re.search(re.escape(f.stem) + r'\.render\(([^)]*)\)', entry_src)
        if not m:
            failures.append(f"{f.name}: render() is never called from {ENTRYPOINT}")
            continue
        args = [a.strip() for a in m.group(1).split(',') if a.strip()]
        if len(args) != len(params):
            failures.append(
                f"{f.name}: render({', '.join(params)}) takes {len(params)} arg(s) "
                f"but {ENTRYPOINT} calls it with {len(args)}: ({', '.join(args)})")

    # 4: NAMES must be read at call time, never at import time
    for f, tree in trees.items():
        fnlines = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                fnlines.update(range(n.lineno, (n.end_lineno or n.lineno) + 1))
        for n in ast.walk(tree):
            if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                    and n.value.id == 'NAMES' and n.lineno not in fnlines):
                failures.append(
                    f"{f.name}:{n.lineno}: NAMES.{n.attr} read at MODULE scope. Object "
                    f"names are resolved from the live session, so a module-level read "
                    f"binds the fallback value before the session exists. Move it inside "
                    f"a function.")

    if failures:
        print(f"PRE-FLIGHT FAILED ({len(failures)} problem(s)):")
        for x in failures:
            print(f"  {x}")
        return 1

    print(f"pre-flight OK: {len(pyfiles)} modules, no undefined names, "
          f"tab signatures match, NAMES read at call time")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else '.'))
