"""Tests the xonsh builtins."""

import os
import re
import shutil
import types
import ast
from ast import AST, Attribute, Call, Expression, Interactive, Module, Name
from pathlib import Path

import pytest

from xonsh.built_ins import (
    DynamicAccessProxy,
    call_macro,
    convert_macro_arg,
    ensure_list_of_strs,
    enter_macro,
    expand_path,
    globsearch,
    helper,
    in_macro_call,
    list_of_list_of_strs_outer_product,
    list_of_strs_or_callables,
    macro_precheck,
    path_literal,
    pathsearch,
    regexmatchsearch,
    regexsearch,
    reglob,
    resetting_signal_handle,
    superhelper,
)
from xonsh.environ import Env
from xonsh.pytest.tools import skip_if_on_windows

HOME_PATH = os.path.expanduser("~")


def test_dynamic_access_proxy_setattr():
    """__setattr__ must write to the target object, not the proxy."""
    import builtins

    target = types.SimpleNamespace(x=1)
    builtins.__test_proxy_target__ = target
    proxy = DynamicAccessProxy("tp", "__test_proxy_target__")
    try:
        proxy.x = 42
        assert target.x == 42
        assert proxy.x == 42

        proxy.new_attr = "hello"
        assert target.new_attr == "hello"
        assert proxy.new_attr == "hello"
    finally:
        del builtins.__test_proxy_target__


@pytest.fixture(autouse=True)
def xonsh_execer_autouse(xonsh_execer):
    return xonsh_execer


@pytest.mark.parametrize("testfile", reglob("test_.*"))
def test_reglob_tests(testfile):
    assert testfile.startswith("test_")


@skip_if_on_windows
class TestDotglob:
    """Test that $DOTGLOB is respected uniformly by all globbing forms."""

    @pytest.fixture(autouse=True)
    def glob_tree(self, tmp_path, xession, monkeypatch):
        # chdir into the tmp dir and use relative patterns so reglob does
        # not have to walk from the filesystem root — '/' is not listable
        # on sandboxed Linux setups like Android/Termux.
        (tmp_path / "visible").touch()
        (tmp_path / ".hidden").touch()
        monkeypatch.chdir(tmp_path)
        self.xession = xession

    def _basenames(self, paths):
        return {os.path.basename(p) for p in paths}

    def _glob(self):
        return self._basenames(globsearch("*"))

    def _reglob(self):
        results = regexsearch(".*")
        return self._basenames(results)

    def test_glob_excludes_dotfiles_by_default(self):
        self.xession.env["DOTGLOB"] = False
        assert ".hidden" not in self._glob()

    def test_glob_includes_dotfiles_when_enabled(self):
        self.xession.env["DOTGLOB"] = True
        assert ".hidden" in self._glob()

    def test_reglob_excludes_dotfiles_by_default(self):
        self.xession.env["DOTGLOB"] = False
        assert ".hidden" not in self._reglob()

    def test_reglob_includes_dotfiles_when_enabled(self):
        self.xession.env["DOTGLOB"] = True
        assert ".hidden" in self._reglob()

    def test_all_forms_agree(self):
        """Both glob and reglob must give the same answer for $DOTGLOB."""
        for dotglob in (False, True):
            self.xession.env["DOTGLOB"] = dotglob
            glob_has_hidden = ".hidden" in self._glob()
            reglob_has_hidden = ".hidden" in self._reglob()
            assert glob_has_hidden == reglob_has_hidden == dotglob


@pytest.fixture
def home_env(xession):
    """Set `__xonsh__.env ` to a new Env instance on `xonsh_builtins`"""
    xession.env["HOME"] = HOME_PATH
    return xession


@skip_if_on_windows
def test_repath_backslash(home_env, tmp_path, monkeypatch):
    # chdir + relative path so reglob never has to walk through '/' (the
    # root isn't listable on sandboxed Linux setups like Android/Termux).
    monkeypatch.chdir(tmp_path)
    path = Path("test_repath_backslash")
    path.mkdir(exist_ok=True, parents=True)
    (path / ".git").touch()
    (path / "dir").touch()
    (path / "file").touch()
    exp = os.listdir(path)
    exp = {p for p in exp if re.match(r"\w\w.*", p)}
    exp = {os.path.join(str(path), p) for p in exp}
    obs = set(pathsearch(regexsearch, rf"{path}{os.sep}\w\w.*"))
    assert exp == obs


def _set_relative_home(xession, tmp_path, monkeypatch):
    """Point HOME at a relative tmp dir under CWD so reglob/pathsearch
    can resolve '~' without traversing from the filesystem root."""
    monkeypatch.chdir(tmp_path)
    home_rel = "fake_home"
    os.makedirs(home_rel)
    monkeypatch.setenv("HOME", home_rel)
    xession.env["HOME"] = home_rel
    return home_rel


@skip_if_on_windows
def test_repath_HOME_PATH_itself(home_env, tmp_path, monkeypatch):
    exp = _set_relative_home(home_env, tmp_path, monkeypatch)
    obs = pathsearch(regexsearch, "~")
    assert 1 == len(obs)
    assert exp == obs[0]


@skip_if_on_windows
def test_repath_HOME_PATH_contents(home_env, tmp_path, monkeypatch):
    home_rel = _set_relative_home(home_env, tmp_path, monkeypatch)
    home_env.env["DOTGLOB"] = True
    (Path(home_rel) / "a").touch()
    (Path(home_rel) / "b").touch()
    (Path(home_rel) / ".dotfile").touch()
    exp = os.listdir(home_rel)
    exp = {os.path.join(home_rel, p) for p in exp}
    obs = set(pathsearch(regexsearch, "~/.*"))
    assert exp == obs


@skip_if_on_windows
def test_repath_HOME_PATH_var(home_env, tmp_path, monkeypatch):
    exp = _set_relative_home(home_env, tmp_path, monkeypatch)
    obs = pathsearch(regexsearch, "$HOME")
    assert 1 == len(obs)
    assert exp == obs[0]


@skip_if_on_windows
def test_repath_HOME_PATH_var_brace(home_env, tmp_path, monkeypatch):
    exp = _set_relative_home(home_env, tmp_path, monkeypatch)
    obs = pathsearch(regexsearch, '${"HOME"}')
    assert 1 == len(obs)
    assert exp == obs[0]


# helper
def check_repath(path, pattern):
    base_testdir = Path("re_testdir")
    testdir = base_testdir / path
    testdir.mkdir(parents=True)
    try:
        obs = regexsearch(str(base_testdir / pattern))
        assert [str(testdir)] == obs
    finally:
        shutil.rmtree(base_testdir)


@skip_if_on_windows
@pytest.mark.parametrize(
    "path, pattern",
    [
        ("test*1/model", ".*/model"),
        ("hello/test*1/model", "hello/.*/model"),
    ],
)
def test_repath_containing_asterisk(path, pattern):
    check_repath(path, pattern)


@pytest.mark.parametrize(
    "path, pattern",
    [
        ("test+a/model", ".*/model"),
        ("hello/test+1/model", "hello/.*/model"),
    ],
)
def test_repath_containing_plus_sign(path, pattern):
    check_repath(path, pattern)


@skip_if_on_windows
class TestRegexMatchSearch:
    """Test m`` modifier — regex glob returning capture groups."""

    @pytest.fixture(autouse=True)
    def _cd_tmp(self, tmp_path, monkeypatch):
        # chdir + relative patterns so reglob doesn't have to walk from
        # '/' (not listable on sandboxed Linux setups like Termux).
        monkeypatch.chdir(tmp_path)

    def test_returns_tuples(self, xession):
        Path("data/train/images").mkdir(parents=True)
        Path("data/train/images/cat.png").touch()
        results = regexmatchsearch("data/(.*)/(.*)/(.*)\\.png")
        assert ("train", "images", "cat") in results

    def test_single_group_returns_strings(self, xession):
        Path("main.py").touch()
        Path("utils.py").touch()
        results = regexmatchsearch("./(.*)\\.py")
        assert "main" in results
        assert "utils" in results
        assert all(isinstance(r, str) for r in results)

    def test_no_groups_returns_paths(self, xession):
        Path("subdir").mkdir()
        Path("subdir/hello.txt").touch()
        results = regexmatchsearch("subdir/hello\\.txt")
        assert "subdir/hello.txt" in results
        assert all(isinstance(r, str) for r in results)

    def test_respects_dotglob(self, xession):
        Path(".hidden").touch()
        Path("visible").touch()
        xession.env["DOTGLOB"] = False
        results = regexmatchsearch("./(.*)")
        assert ".hidden" not in results
        xession.env["DOTGLOB"] = True
        results = regexmatchsearch("./(.*)")
        assert ".hidden" in results


def test_helper_int(home_env):
    helper(int, "int")


def test_helper_helper(home_env):
    helper(helper, "helper")


def test_helper_env(home_env):
    helper(Env, "Env")


def test_superhelper_int(home_env):
    superhelper(int, "int")


def test_superhelper_helper(home_env):
    superhelper(helper, "helper")


def test_superhelper_env(home_env):
    superhelper(Env, "Env")


@pytest.mark.parametrize(
    "exp, inp", [(["yo"], "yo"), (["yo"], ["yo"]), (["42"], 42), (["42"], [42])]
)
def test_ensure_list_of_strs(exp, inp):
    obs = ensure_list_of_strs(inp)
    assert exp == obs


f = lambda x: 20


@pytest.mark.parametrize(
    "exp, inp",
    [
        (["yo"], "yo"),
        (["yo"], ["yo"]),
        (["42"], 42),
        (["42"], [42]),
        ([f], f),
        ([f], [f]),
    ],
)
def test_list_of_strs_or_callables(exp, inp):
    obs = list_of_strs_or_callables(inp)
    assert exp == obs


@pytest.mark.parametrize(
    "inp, exp",
    [
        (["x", ["y", "z"]], ["xy", "xz"]),
        (["x", ["y", "z"], ["a"]], ["xya", "xza"]),
        ([["y", "z"], ["a", "b"]], ["ya", "yb", "za", "zb"]),
    ],
)
def test_list_of_list_of_strs_outer_product(xession, inp, exp):
    obs = list_of_list_of_strs_outer_product(inp)
    assert exp == obs


@pytest.mark.parametrize(
    "s",
    [
        "~",
        "~/",
        "x=~/place",
        "x=one:~/place",
        "x=one:~/place:~/yo",
        "x=~/one:~/place:~/yo",
    ],
)
def test_expand_path(s, home_env):
    if os.sep != "/":
        s = s.replace("/", os.sep)
    if os.pathsep != ":":
        s = s.replace(":", os.pathsep)
    assert expand_path(s) == s.replace("~", HOME_PATH)


@pytest.mark.parametrize("kind", [str, "s", "S", "str", "string"])
def test_convert_macro_arg_str(kind):
    raw_arg = "value"
    arg = convert_macro_arg(raw_arg, kind, None, None)
    assert arg is raw_arg


@pytest.mark.parametrize("kind", [AST, "a", "Ast"])
def test_convert_macro_arg_ast(kind):
    raw_arg = "42"
    arg = convert_macro_arg(raw_arg, kind, {}, None)
    assert isinstance(arg, AST)


@pytest.mark.parametrize("kind", [types.CodeType, compile, "c", "code", "compile"])
def test_convert_macro_arg_code(kind):
    raw_arg = "42"
    arg = convert_macro_arg(raw_arg, kind, {}, None)
    assert isinstance(arg, types.CodeType)


@pytest.mark.parametrize("kind", [eval, "v", "eval"])
def test_convert_macro_arg_eval(kind):
    # literals
    raw_arg = "42"
    arg = convert_macro_arg(raw_arg, kind, {}, None)
    assert arg == 42
    # exprs
    raw_arg = "x + 41"
    arg = convert_macro_arg(raw_arg, kind, {}, {"x": 1})
    assert arg == 42


@pytest.mark.parametrize("kind", [exec, "x", "exec"])
def test_convert_macro_arg_exec(kind):
    # at global scope
    raw_arg = "def f(x, y):\n    return x + y"
    glbs = {}
    arg = convert_macro_arg(raw_arg, kind, glbs, None)
    assert arg is None
    assert "f" in glbs
    assert glbs["f"](1, 41) == 42
    # at local scope
    raw_arg = "def g(z):\n    return x + z\ny += 42"
    glbs = {"x": 40}
    locs = {"y": 1}
    arg = convert_macro_arg(raw_arg, kind, glbs, locs)
    assert arg is None
    assert "g" in locs
    assert locs["g"](1) == 41
    assert "y" in locs
    assert locs["y"] == 43


@pytest.mark.parametrize("kind", [type, "t", "type"])
def test_convert_macro_arg_type(kind):
    # literals
    raw_arg = "42"
    arg = convert_macro_arg(raw_arg, kind, {}, None)
    assert arg is int
    # exprs
    raw_arg = "x + 41"
    arg = convert_macro_arg(raw_arg, kind, {}, {"x": 1})
    assert arg is int


def test_in_macro_call():
    def f():
        pass

    with in_macro_call(f, True, True):
        assert f.macro_globals
        assert f.macro_locals
    assert not hasattr(f, "macro_globals")
    assert not hasattr(f, "macro_locals")


@pytest.mark.parametrize("arg", ["x", "42", "x + y"])
def test_call_macro_str(arg):
    def f(x: str):
        return x

    rtn = call_macro(f, [arg], None, None)
    assert rtn is arg


@pytest.mark.parametrize("arg", ["x", "42", "x + y"])
def test_call_macro_ast(arg):
    def f(x: AST):
        return x

    rtn = call_macro(f, [arg], {}, None)
    assert isinstance(rtn, AST)


@pytest.mark.parametrize("arg", ["x", "42", "x + y"])
def test_call_macro_code(arg):
    def f(x: compile):
        return x

    rtn = call_macro(f, [arg], {}, None)
    assert isinstance(rtn, types.CodeType)


@pytest.mark.parametrize("arg", ["x", "42", "x + y"])
def test_call_macro_eval(arg):
    def f(x: eval):
        return x

    rtn = call_macro(f, [arg], {"x": 42, "y": 0}, None)
    assert rtn == 42


@pytest.mark.parametrize(
    "arg", ["if y:\n    pass", "if 42:\n    pass", "if x + y:\n    pass"]
)
def test_call_macro_exec(arg):
    def f(x: exec):
        return x

    rtn = call_macro(f, [arg], {"x": 42, "y": 0}, None)
    assert rtn is None


@pytest.mark.parametrize("arg", ["x", "42", "x + y"])
def test_call_macro_raw_arg(arg):
    def f(x: str):
        return x

    rtn = call_macro(f, ["*", arg], {"x": 42, "y": 0}, None)
    assert rtn == 42


@pytest.mark.parametrize("arg", ["x", "42", "x + y"])
def test_call_macro_raw_kwarg(arg):
    def f(x: str):
        return x

    rtn = call_macro(f, ["*", "x=" + arg], {"x": 42, "y": 0}, None)
    assert rtn == 42


@pytest.mark.parametrize("arg", ["x", "42", "x + y"])
def test_call_macro_raw_kwargs(arg):
    def f(x: str):
        return x

    rtn = call_macro(f, ["*", '**{"x" :' + arg + "}"], {"x": 42, "y": 0}, None)
    assert rtn == 42


def test_call_macro_ast_eval_expr():
    def f(x: ("ast", "eval")):
        return x

    rtn = call_macro(f, ["x == 5"], {}, None)
    assert isinstance(rtn, Expression)


def test_call_macro_ast_single_expr():
    def f(x: ("ast", "single")):
        return x

    rtn = call_macro(f, ["x == 5"], {}, None)
    assert isinstance(rtn, Interactive)


def test_call_macro_ast_exec_expr():
    def f(x: ("ast", "exec")):
        return x

    rtn = call_macro(f, ["x == 5"], {}, None)
    assert isinstance(rtn, Module)


def test_call_macro_ast_eval_statement():
    def f(x: ("ast", "eval")):
        return x

    try:
        call_macro(f, ["x = 5"], {}, None)
        assert False
    except SyntaxError:
        # It doesn't make sense to pass a statement to
        # something that expects to be evaled
        assert True
    else:
        assert False


def test_call_macro_ast_single_statement():
    def f(x: ("ast", "single")):
        return x

    rtn = call_macro(f, ["x = 5"], {}, None)
    assert isinstance(rtn, Interactive)


def test_call_macro_ast_exec_statement():
    def f(x: ("ast", "exec")):
        return x

    rtn = call_macro(f, ["x = 5"], {}, None)
    assert isinstance(rtn, Module)


# ---------------------------------------------------------------------------
# Whole-call argument precheck for function macros
# ---------------------------------------------------------------------------


def _is_subproc_tree(tree):
    """True if a parsed macro argument was interpreted as a subprocess call."""
    for node in ast.walk(tree):
        if isinstance(node, Call) and isinstance(node.func, Attribute):
            value = node.func.value
            if isinstance(value, Name) and value.id == "__xonsh__":
                if node.func.attr.startswith("subproc"):
                    return True
    return False


def test_macro_precheck_decorator_marks_function():
    @macro_precheck
    def f(x: eval):
        return x

    assert f.__xonsh_macro_precheck__ is True


def test_macro_precheck_attribute_opt_in():
    def f(x: eval):
        return x

    f.__xonsh_macro_precheck__ = True
    assert call_macro(f, ["40 + 2"], {}, None) == 42


def test_macro_precheck_runs_everything_once_in_order():
    log = []

    @macro_precheck
    def f(a: eval, b: exec, c: eval):
        log.append(("body", a, c))

    glbs = {"log": log, "x": 10}
    call_macro(f, ["x", "log.append(('exec', x))", "x + 5"], glbs, None)
    assert log == [("exec", 10), ("body", 10, 15)]


def test_macro_precheck_missing_argument_runs_nothing():
    log = []

    @macro_precheck
    def f(a: eval, b: eval):
        log.append("body")

    with pytest.raises(TypeError):
        call_macro(f, ["log.append('arg')"], {"log": log}, None)
    assert log == []


@pytest.mark.parametrize("bad_kind", ["bogus", ("nope", "eval")])
def test_macro_precheck_invalid_annotation_runs_nothing(bad_kind):
    log = []

    def f(a: eval, b: bad_kind):
        log.append("body")

    macro_precheck(f)
    with pytest.raises(TypeError):
        call_macro(f, ["log.append('arg')", "1"], {"log": log}, None)
    assert log == []


@pytest.mark.parametrize(
    "kind,src",
    [
        (compile, "x ="),
        ("code", "x ="),
        (eval, "x ="),
        (type, "x ="),
        (exec, "return 1"),
        (("ast", "eval"), "x = 1"),
    ],
)
def test_macro_precheck_uncompilable_later_arg_runs_nothing(kind, src):
    log = []

    @macro_precheck
    def f(a: exec, b: kind):
        log.append("body")

    with pytest.raises(SyntaxError):
        call_macro(f, ["log.append('earlier')", src], {"log": log}, None)
    assert log == []


def test_without_precheck_earlier_side_effects_remain():
    # documents the old, opt-out semantics: a failing later argument does
    # not undo an earlier eval/exec argument
    log = []

    def f(a: exec, b: compile):
        log.append("body")

    with pytest.raises(SyntaxError):
        call_macro(f, ["log.append('earlier')", "x ="], {"log": log}, None)
    assert log == ["earlier"]


def test_macro_precheck_allows_arbitrary_dsl_in_string_args():
    seen = []

    @macro_precheck
    def f(a: str, b: ("s", "whatever"), c: compile):
        seen.extend([a, b])
        return c

    code = call_macro(
        f,
        ["std::vector<std::string> x = {}", "import os ; weird DSL !!!", "42"],
        {},
        None,
    )
    assert isinstance(code, types.CodeType)
    assert seen == ["std::vector<std::string> x = {}", "import os ; weird DSL !!!"]


def test_macro_precheck_does_not_enforce_mode_ignored_by_eval():
    # the eval kind historically ignores its mode, so precheck must neither
    # tighten it (statements still fail) nor start honoring it
    @macro_precheck
    def f(a: (eval, "exec")):
        return a

    with pytest.raises(SyntaxError):
        call_macro(f, ["x = 1"], {}, None)
    assert call_macro(f, ["6 * 7"], {}, None) == 42


def test_macro_precheck_defaults_positional_only_and_keyword_only():
    calls = []

    @macro_precheck
    def f(a: eval, /, b: eval = 10, *, c: eval = 100):
        calls.append((a, b, c))

    # keyword-only macro slot is passed by keyword internally
    call_macro(f, ["1", "2", "3"], {}, None)
    assert calls == [(1, 2, 3)]
    # missing macro arguments fall back to their defaults
    call_macro(f, ["1"], {}, None)
    assert calls[-1] == (1, 10, 100)


def test_macro_precheck_keyword_only_slot_evaluation_order():
    log = []

    @macro_precheck
    def f(a: eval, *, b: eval):
        log.append(("body", a, b))

    call_macro(
        f,
        ["log.append('a') or 1", "log.append('b') or 2"],
        {"log": log},
        None,
    )
    assert log == ["a", "b", ("body", 1, 2)]


def test_macro_precheck_regular_args_after_separator():
    calls = []

    @macro_precheck
    def f(a: str, b, c=0, *, d=0):
        calls.append((a, b, c, d))

    call_macro(f, ["src", "*", "41", "c=1", "d=2"], {}, None)
    assert calls == [("src", 41, 1, 2)]
    # duplicate value for the already-macro-bound 'a'
    with pytest.raises(TypeError):
        call_macro(f, ["src", "*", "a=1"], {}, None)
    # too many positional arguments (d is keyword-only)
    with pytest.raises(TypeError):
        call_macro(f, ["src", "*", "1", "2", "3"], {}, None)
    # unexpected keyword argument
    with pytest.raises(TypeError):
        call_macro(f, ["src", "*", "nope=1"], {}, None)
    # body never ran for the failing shapes
    assert calls == [("src", 41, 1, 2)]


def test_macro_precheck_positional_only_cannot_be_keyword():
    @macro_precheck
    def f(a: eval, /, b: eval = 0):
        return (a, b)

    assert call_macro(f, ["*", "1", "b=2"], {}, None) == (1, 2)
    with pytest.raises(TypeError):
        call_macro(f, ["*", "a=1"], {}, None)


def test_macro_precheck_rejects_repeated_keyword_arguments():
    ran = []

    @macro_precheck
    def f(a: eval, *, b: eval = 0):
        ran.append((a, b))

    # keyword-only macro slot supplied again as a regular keyword
    with pytest.raises(TypeError):
        call_macro(f, ["1", "2", "*", "b=3"], {}, None)
    # same regular keyword given twice
    with pytest.raises(TypeError):
        call_macro(f, ["1", "*", "b=2", "b=3"], {}, None)
    assert ran == []
    # but the keyword-only slot alone works
    call_macro(f, ["1", "2"], {}, None)
    assert ran == [(1, 2)]


@pytest.mark.parametrize("extra", [["*items"], ["**d"]])
def test_macro_precheck_rejects_dynamic_unpacking(extra):
    ran = []

    @macro_precheck
    def f(a: eval):
        ran.append(True)

    glbs = {"items": [1], "d": {"a": 1}}
    with pytest.raises(TypeError):
        call_macro(f, ["*"] + extra, glbs, None)
    assert ran == []


@pytest.mark.parametrize(
    "src", ["def f(*args):\n    pass\n", "def f(**kwargs):\n    pass\n"]
)
def test_macro_precheck_rejects_var_parameters(src):
    ns = {}
    exec(src, ns)
    f = ns["f"]
    macro_precheck(f)
    with pytest.raises(TypeError):
        call_macro(f, ["1"], {}, None)


def test_macro_precheck_freezes_interpretation_namespace():
    seen = {}

    @macro_precheck
    def f(a: exec, b: ("ast", "exec")):
        seen["b"] = b

    glbs = {}
    call_macro(f, ["mycmd = 1", "mycmd"], glbs, None)
    assert glbs["mycmd"] == 1  # the executed value is read live
    # 'mycmd' was unknown when preflight started, so b stays a subprocess node
    assert _is_subproc_tree(seen["b"])

    # without precheck the later argument is re-parsed after a defines the name
    def g(a: exec, b: ("ast", "exec")):
        seen["g"] = b

    call_macro(g, ["mycmd = 2", "mycmd"], glbs, None)
    assert not _is_subproc_tree(seen["g"])


def test_macro_precheck_still_reads_live_values():
    @macro_precheck
    def f(a: exec, b: eval):
        return b

    # 'x' is part of the frozen name set so b stays a Python expression,
    # even though its value changes before b actually runs
    glbs = {"x": 0}
    assert call_macro(f, ["x = 40", "x + 2"], glbs, None) == 42
    assert glbs["x"] == 40


def test_macro_precheck_does_not_catch_runtime_errors_or_roll_back():
    log = []

    @macro_precheck
    def f(a: exec, b: eval):
        log.append("body")

    # sources compile fine; the runtime failure is not promised away and the
    # earlier exec side effect is not rolled back
    with pytest.raises(ZeroDivisionError):
        call_macro(f, ["log.append('earlier')", "1 / 0"], {"log": log}, None)
    assert log == ["earlier"]


def test_macro_precheck_compilable_but_failing_arg_skips_body():
    log = []

    @macro_precheck
    def f(a: eval):
        log.append("body")

    with pytest.raises(ValueError):
        call_macro(f, ["int('not a number')"], {"log": log}, None)
    assert log == []


def test_macro_precheck_end_to_end(xonsh_execer):
    glbs = {"macro_precheck": macro_precheck, "out": []}
    src = (
        "@macro_precheck\n"
        "def m(a: eval, *, b: eval):\n"
        "    out.append((a, b))\n"
        "m!(10, *, b=32)\n"
    )
    xonsh_execer.exec(src, glbs=glbs, locs=glbs)
    assert glbs["out"] == [(10, 32)]


def test_macro_precheck_end_to_end_failure_runs_nothing(xonsh_execer):
    glbs = {"macro_precheck": macro_precheck, "out": []}
    src = (
        "@macro_precheck\n"
        "def m(a: eval, b: eval):\n"
        "    out.append('body')\n"
        "m!(out.append('arg'), x =)\n"
    )
    with pytest.raises(SyntaxError):
        xonsh_execer.exec(src, glbs=glbs, locs=glbs)
    assert glbs["out"] == []


def test_enter_macro():
    obj = lambda: None
    rtn = enter_macro(obj, "wakka", True, True)
    assert obj is rtn
    assert obj.macro_block == "wakka"
    assert obj.macro_globals
    assert obj.macro_locals


def test_xonshpathliteral_contextmanager(tmp_path):
    start_cwd = os.getcwd()
    p = path_literal(str(tmp_path))
    try:
        with p.cd():
            assert os.getcwd() == str(p)
        assert os.getcwd() == start_cwd
    finally:
        try:
            os.chdir(start_cwd)
        except Exception:
            pass


def test_resetting_signal_handle_off_main_thread_is_noop():
    """Regression for xonsh#3689: setup() called from a non-main thread must
    not crash with ValueError('signal only works in main thread')."""
    import signal
    import threading

    errors: list[BaseException] = []

    def worker():
        try:
            resetting_signal_handle(signal.SIGTERM, lambda s, f: None)
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert errors == []
