"""Tests for ``$XONSH_SUBPROC_CWD_ANCHOR``.

All tests here run *real* external subprocesses (the running Python
interpreter, or a tiny platform native helper) and switch the real
working directory from inside a ``@()`` expression while the pipeline
arguments are evaluated.
"""

import os
import stat
import sys
import textwrap
from subprocess import CalledProcessError

import pytest

from xonsh.built_ins import (
    _subproc_cwd_anchor_var,
    subproc_cwd_anchor_enter,
    subproc_cwd_anchor_exit,
    subproc_cwd_anchor_restore,
    subproc_cwd_anchor_snapshot,
)
from xonsh.platform import ON_WINDOWS
from xonsh.tools import XonshError


CWD_CODE = "import os,sys;sys.stdout.write(os.getcwd())"
READER_CODE = (
    "import sys;"
    "sys.stdout.write(open(sys.argv[1], encoding='utf-8').read())"
)
CWD_ARGV_CODE = (
    "import os,sys;"
    "sys.stdout.write(os.getcwd() + '|' + '|'.join(sys.argv[1:]))"
)
EXIT_CODE = "import sys;sys.exit({code})"


@pytest.fixture
def anchor_env(tmp_path, monkeypatch, xonsh_session, xonsh_execer):
    """Two directories, each with its own ``target.txt``, plus helpers."""
    dira = tmp_path / "dir_a"
    dirb = tmp_path / "dir_b"
    dira.mkdir()
    dirb.mkdir()
    (dira / "target.txt").write_text("content-A", encoding="utf-8")
    (dirb / "target.txt").write_text("content-B", encoding="utf-8")
    (dira / "in.txt").write_text("input-A", encoding="utf-8")

    monkeypatch.chdir(dira)
    monkeypatch.setitem(xonsh_session.env, "XONSH_SUBPROC_CWD_ANCHOR", True)
    monkeypatch.setitem(xonsh_session.env, "XONSH_SUBPROC_RAISE_ERROR", True)

    ns = {"os": os, "sys": sys}

    def chdir_b():
        # ``@()`` atom that switches the real parent cwd to dir_b.
        # Returning an empty list makes the atom contribute no argument
        # (os.chdir itself returns None, which xonsh otherwise turns into
        # a literal "None" argument).
        return f"@(os.chdir({str(dirb)!r}) or [])"

    def run(src, mode="eval"):
        if mode == "eval":
            return xonsh_execer.eval(src, glbs=ns)
        return xonsh_execer.exec(src, glbs=ns)

    def py(code):
        return f"@(sys.executable) -c {code!r}"

    yield {
        "a": str(dira),
        "b": str(dirb),
        "dira": dira,
        "dirb": dirb,
        "ns": ns,
        "run": run,
        "chdir_b": chdir_b,
        "py": py,
        "session": xonsh_session,
        "monkeypatch": monkeypatch,
    }

    # No anchor may survive a test, regardless of its outcome.
    assert _subproc_cwd_anchor_var.get() is None


def _cmd(*atoms):
    return " ".join(atoms)


def _norm(p):
    return os.path.normcase(os.path.realpath(p))


def test_setting_defaults_off(xonsh_session):
    assert xonsh_session.env.get("XONSH_SUBPROC_CWD_ANCHOR") is False


# ---------------------------------------------------------------------------
# External subprocess working directory
# ---------------------------------------------------------------------------


def test_external_process_runs_in_anchor(anchor_env):
    env = anchor_env
    out = env["run"](f"$({_cmd(env['py'](CWD_CODE), env['chdir_b']())})")
    assert _norm(out) == _norm(env["a"])
    # the os.chdir() side effect is kept for the parent process
    assert _norm(os.getcwd()) == _norm(env["b"])


def test_external_process_without_anchor_runs_in_changed_dir(
    anchor_env, xonsh_session
):
    xonsh_session.env["XONSH_SUBPROC_CWD_ANCHOR"] = False
    env = anchor_env
    out = env["run"](f"$({_cmd(env['py'](CWD_CODE), env['chdir_b']())})")
    assert _norm(out) == _norm(env["b"])


def test_relative_literal_argument_uses_anchor(anchor_env):
    env = anchor_env
    # The literal "target.txt" is expanded before the chdir atom, but the
    # external program opens it relative to its own cwd.
    src = f"$({_cmd(env['py'](READER_CODE), 'target.txt', env['chdir_b']())})"
    assert env["run"](src) == "content-A"


def test_glob_before_chdir_still_reads_anchor_file(anchor_env):
    env = anchor_env
    # ``*.txt`` expands in A (before chdir), but without an anchored
    # subprocess cwd the program would open the same name inside B.
    src = f"$({_cmd(env['py'](READER_CODE), 'target*.txt', env['chdir_b']())})"
    assert env["run"](src) == "content-A"


def test_glob_after_chdir_is_anchored(anchor_env):
    env = anchor_env
    src = f"$({_cmd(env['py'](READER_CODE), env['chdir_b'](), 'target*.txt')})"
    assert env["run"](src) == "content-A"


def test_regex_glob_after_chdir_is_anchored(anchor_env):
    env = anchor_env
    # regex glob atom (backticks) in subproc mode; use [.] rather than
    # an escaped dot so the pattern stays valid on Windows, where the
    # regex backslash is also the path separator.
    src = f"$({_cmd(env['py'](READER_CODE), env['chdir_b'](), '`target[.]txt`')})"
    assert env["run"](src) == "content-A"


def test_redirect_output_is_anchored(anchor_env):
    env = anchor_env
    code = "import sys;sys.stdout.write('redirect-A')"
    src = f"$({_cmd(env['py'](code), env['chdir_b']())} > out.txt)"
    env["run"](src)
    assert (env["dira"] / "out.txt").read_text(encoding="utf-8") == "redirect-A"
    assert not (env["dirb"] / "out.txt").exists()


def test_redirect_input_is_anchored(anchor_env):
    env = anchor_env
    code = "import sys;sys.stdout.write(sys.stdin.read())"
    src = f"$({_cmd(env['py'](code), env['chdir_b']())} < in.txt)"
    assert env["run"](src) == "input-A"


# ---------------------------------------------------------------------------
# Pipelines / chains / nesting
# ---------------------------------------------------------------------------


def test_pipeline_shares_one_anchor(anchor_env):
    env = anchor_env
    second = (
        "import os,sys;"
        "d=sys.stdin.read();"
        "sys.stdout.write(os.getcwd()+'::'+d)"
    )
    src = (
        f"$({_cmd(env['py'](CWD_CODE), env['chdir_b']())} | "
        f"{_cmd(env['py'](second))})"
    )
    out = env["run"](src)
    first_cwd, second_in = out.split("::", 1)
    assert _norm(first_cwd) == _norm(env["a"])
    # the upstream ran in A and passed A's cwd to the downstream, which
    # must run in A as well
    assert _norm(second_in) == _norm(env["a"])


def test_semicolon_starts_fresh_anchor_per_pipeline(anchor_env):
    env = anchor_env
    src = textwrap.dedent(
        f"""
        x1 = $({_cmd(env['py'](CWD_CODE), env['chdir_b']())})
        x2 = $({_cmd(env['py'](CWD_CODE))})
        """
    )
    env["run"](src, mode="exec")
    assert _norm(env["ns"]["x1"]) == _norm(env["a"])
    assert _norm(env["ns"]["x2"]) == _norm(env["b"])


def test_boolop_chain_anchors_only_executed_pipelines(anchor_env):
    env = anchor_env
    src = textwrap.dedent(
        f"""
        ok = bool(!({_cmd(env['py'](EXIT_CODE.format(code=0)), env['chdir_b']())}))
        good = ok and $({_cmd(env['py'](CWD_CODE))})
        bad = bool(!({_cmd(env['py'](EXIT_CODE.format(code=1)))}))
        fallback = bad or $({_cmd(env['py'](CWD_CODE))})
        """
    )
    env["run"](src, mode="exec")
    assert env["ns"]["ok"] is True
    # second && pipeline is anchored at the then-current dir (B)
    assert _norm(env["ns"]["good"]) == _norm(env["b"])
    # || fallback likewise runs in B
    assert _norm(env["ns"]["fallback"]) == _norm(env["b"])


def test_nested_capture_has_own_anchor_and_outer_keeps_its(anchor_env):
    env = anchor_env
    inner = _cmd(env["py"](CWD_CODE))
    src = f"$({_cmd(env['py'](CWD_ARGV_CODE), env['chdir_b'](), f'$({inner})')})"
    out = env["run"](src)
    outer_cwd, inner_cwd = out.split("|", 1)
    assert _norm(outer_cwd) == _norm(env["a"])
    assert _norm(inner_cwd) == _norm(env["b"])
    # the outer pipeline finishes with its anchor popped
    assert _subproc_cwd_anchor_var.get() is None


def test_all_capture_forms_anchor(anchor_env):
    env = anchor_env
    os.chdir(env["a"])
    cp = env["run"](f"!({_cmd(env['py'](CWD_CODE), env['chdir_b']())})")
    assert _norm(cp.out.strip()) == _norm(env["a"])

    # uncaptured bare command with a redirect; anchor again starts at A
    os.chdir(env["a"])
    src = f"$[{_cmd(env['py'](CWD_CODE), env['chdir_b']())} > pwd.txt]"
    env["run"](src, mode="exec")
    assert _norm((env["dira"] / "pwd.txt").read_text().strip()) == _norm(
        env["a"]
    )


# ---------------------------------------------------------------------------
# Python expression semantics stay intact
# ---------------------------------------------------------------------------


def test_iterator_evaluates_once_in_order_and_chdir_is_real(anchor_env):
    env = anchor_env
    src = textwrap.dedent(
        f"""
        calls = []
        def gen():
            calls.append('called')
            os.chdir({str(env['b'])!r})
            yield 'it-arg'
        out = $({_cmd(env['py'](CWD_ARGV_CODE), '@(gen())')})
        """
    )
    env["run"](src, mode="exec")
    outer_cwd, arg = env["ns"]["out"].split("|", 1)
    assert _norm(outer_cwd) == _norm(env["a"])
    assert arg == "it-arg"
    assert env["ns"]["calls"] == ["called"]
    assert _norm(os.getcwd()) == _norm(env["b"])


def test_python_mode_glob_after_pipeline_uses_real_cwd(anchor_env):
    env = anchor_env
    env["run"](f"$({_cmd(env['py'](CWD_CODE), env['chdir_b']())})")
    # anchor is gone; plain Python xonsh globs use the real cwd (B)
    names = env["run"]("__xonsh__.globsearch('target.txt')")
    assert list(names) == ["target.txt"]
    assert (env["dirb"] / names[0]).read_text(encoding="utf-8") == "content-B"


# ---------------------------------------------------------------------------
# Failure must not leave anchors behind
# ---------------------------------------------------------------------------


def test_failed_argument_evaluation_does_not_leak_anchor(anchor_env):
    env = anchor_env
    src = textwrap.dedent(
        """
        try:
            echo @(1 / 0)
        except ZeroDivisionError:
            pass
        """
    )
    env["run"](src, mode="exec")
    assert env["run"]("__xonsh__.current_subproc_cwd_anchor()") is None


def test_failed_execution_does_not_leak_anchor(anchor_env):
    env = anchor_env
    # Different environments report the failed launch differently
    # (XonshError from Popen, or CalledProcessError from a platform
    # command-not-found fallback); the anchor cleanup is what matters.
    with pytest.raises((XonshError, CalledProcessError)):
        env["run"](f"$({_cmd('xonsh_no_such_command_xyz', env['chdir_b']())})")
    assert env["run"]("__xonsh__.current_subproc_cwd_anchor()") is None


def test_anchor_lifecycle_tokens(anchor_env):
    # direct unit-level check of the nesting stack
    outer = subproc_cwd_anchor_enter()
    assert outer is not None
    inner = subproc_cwd_anchor_enter()
    assert inner is not None and inner is not outer
    snap = subproc_cwd_anchor_snapshot()
    assert snap is inner
    subproc_cwd_anchor_exit(inner)
    assert subproc_cwd_anchor_snapshot() is outer
    # restoring a leaked inner anchor rewinds to the snapshot
    subproc_cwd_anchor_enter()
    subproc_cwd_anchor_restore(snap)
    assert subproc_cwd_anchor_snapshot() is snap
    subproc_cwd_anchor_exit(outer)
    assert subproc_cwd_anchor_snapshot() is None


# ---------------------------------------------------------------------------
# Relative command paths and relative $PATH entries
# ---------------------------------------------------------------------------


@pytest.fixture
def relative_tool(anchor_env):
    env = anchor_env
    bindir = env["dira"] / "bin"
    bindir.mkdir()
    if ON_WINDOWS:
        name = "anchortool.bat"
        content = "@echo ANCHOR-BIN-A\r\n"
        (bindir / name).write_text(content, encoding="utf-8")
        # a copy directly in A for the explicit-relative-path tests
        (env["dira"] / name).write_text(content, encoding="utf-8")
        explicit = "./anchortool.bat"
    else:
        name = "anchortool"
        content = "#!/bin/sh\necho ANCHOR-BIN-A\n"
        script = bindir / name
        script.write_text(content, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        direct = env["dira"] / name
        direct.write_text(content, encoding="utf-8")
        direct.chmod(direct.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        explicit = "./anchortool"

    old_path = list(env["session"].env["PATH"])
    env["monkeypatch"].setitem(
        env["session"].env, "PATH", ["bin"] + old_path
    )
    yield {**env, "explicit": explicit, "bare": "anchortool"}


def test_explicit_relative_executable_is_anchored(relative_tool):
    env = relative_tool
    out = env["run"](f"$({_cmd(env['explicit'], env['chdir_b']())})")
    assert "ANCHOR-BIN-A" in out


def test_explicit_relative_executable_not_found_without_anchor(
    relative_tool, xonsh_session
):
    xonsh_session.env["XONSH_SUBPROC_CWD_ANCHOR"] = False
    env = relative_tool
    with pytest.raises((XonshError, CalledProcessError)):
        env["run"](f"$({_cmd(env['explicit'], env['chdir_b']())})")


def test_relative_path_entry_is_anchored(relative_tool):
    env = relative_tool
    out = env["run"](f"$({_cmd(env['bare'], env['chdir_b']())})")
    assert "ANCHOR-BIN-A" in out


def test_relative_path_entry_not_found_without_anchor(
    relative_tool, xonsh_session
):
    xonsh_session.env["XONSH_SUBPROC_CWD_ANCHOR"] = False
    env = relative_tool
    with pytest.raises((XonshError, CalledProcessError)):
        env["run"](f"$({_cmd(env['bare'], env['chdir_b']())})")
