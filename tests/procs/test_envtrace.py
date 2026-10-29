"""Tests for the ``envtrace`` builtin: explanations of the typed environment
xonsh really exports to external processes.

These tests launch real subprocesses (``sys.executable -c ...``) through the
unmocked ``run_subproc`` and inspect both what the child received and the
immutable explanation recorded on ``XSH.envtrace``.
"""

import io
import sys
import types

import pytest

from xonsh.aliases import make_default_aliases
from xonsh.built_ins import XSH
from xonsh.environ import DefaultNotGiven, VarPattern, default_value
from xonsh.envtrace import (
    L_ABSENT,
    L_GLOBAL,
    L_HOOK,
    L_OVERLAY,
    L_POST,
    L_PREFIX,
    L_THREAD_LOCAL,
    L_UNCOLLECTED,
    R_ABSENT,
    R_DEFAULT_CALLABLE,
    R_MASKED,
    R_NONE,
    R_NO_DETYPER,
    envtrace as envtrace_alias,
)
from xonsh.platform import ON_WINDOWS
from xonsh.procs.specs import run_subproc
from xonsh.pytest.tools import skip_if_on_windows


CHILD_GET = (
    "import os,sys; "
    "sys.stdout.write(os.environ.get({name!r}, '<unset>'))"
)


def child_get_env(name):
    """A real external process that prints NAME from its own environment."""
    return [sys.executable, "-c", CHILD_GET.format(name=name)]


def run_envtrace(*args):
    out, err = io.StringIO(), io.StringIO()
    rc = envtrace_alias(list(args), stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def envtrace_session(xonsh_session):
    """Full xonsh session (real subprocess launches), foreground threads off."""
    env = xonsh_session.env
    env["THREAD_SUBPROCS"] = False
    yield xonsh_session
    # Make sure no round leaks into other tests even if a test forgets off.
    state = getattr(XSH, "_envtrace", None)
    if state is not None:
        state.stop()


def test_builtin_is_registered():
    assert callable(make_default_aliases()["envtrace"])


def test_show_without_record_says_so(envtrace_session):
    rc, _, _ = run_envtrace("on", "ENVTR_A")
    assert rc == 0
    rc, out, _ = run_envtrace("show")
    assert rc == 0
    assert "No external process has been successfully started" in out


def test_exported_typed_value_real_subprocess(envtrace_session):
    env = XSH.env

    class Weird:
        def __init__(self, v):
            self.v = v

    def weird_detyper(x):
        return f"weird:{x.v}"

    env.register(
        "ENVTR_W",
        validate=lambda x: isinstance(x, Weird),
        convert=lambda x: Weird(int(x)),
        detype=weird_detyper,
    )
    env["ENVTR_W"] = Weird(42)
    run_envtrace("on", "ENVTR_W")
    child_value = run_subproc([child_get_env("ENVTR_W")], captured="stdout")
    assert child_value == "weird:42"

    rec = XSH.envtrace.record
    assert rec is not None
    assert sys.executable in rec.command
    assert rec.cache_used is False
    vt = rec.variables["ENVTR_W"]
    assert vt.exported is True
    assert vt.value == "weird:42"
    assert vt.layer == L_GLOBAL
    assert "explicit type registration" in vt.rule
    assert "weird_detyper" in vt.rule

    rc, out, _ = run_envtrace("show")
    assert "ENVTR_W = 'weird:42'" in out
    assert L_GLOBAL in out


def test_no_detyper_means_not_exported(envtrace_session):
    env = XSH.env
    env.register(
        "ENVTR_ND",
        validate=lambda x: isinstance(x, (list, tuple)),
        convert=tuple,
        detype=None,
    )
    env["ENVTR_ND"] = (1, 2)
    run_envtrace("on", "ENVTR_ND")
    child_value = run_subproc([child_get_env("ENVTR_ND")], captured="stdout")
    assert child_value == "<unset>"
    vt = XSH.envtrace.record.variables["ENVTR_ND"]
    assert vt.exported is False
    assert vt.value is None
    assert vt.reason == R_NO_DETYPER
    assert "detype=None" in vt.rule


def test_detyper_returning_none_means_not_exported(envtrace_session):
    env = XSH.env
    calls = []

    def detyper_none(v):
        calls.append(1)
        return None

    env.register("ENVTR_NN", detype=detyper_none)
    env["ENVTR_NN"] = "x"
    run_envtrace("on", "ENVTR_NN")
    child_value = run_subproc([child_get_env("ENVTR_NN")], captured="stdout")
    assert child_value == "<unset>"
    vt = XSH.envtrace.record.variables["ENVTR_NN"]
    assert vt.exported is False
    assert vt.reason == R_NONE
    # show must not invoke the detyper again
    n_calls = len(calls)
    run_envtrace("show")
    assert len(calls) == n_calls


def test_command_prefix_layer(envtrace_session):
    run_envtrace("on", "ENVTR_P")
    child_value = run_subproc(
        [child_get_env("ENVTR_P")],
        captured="stdout",
        envs=[{"ENVTR_P": "prefix-value"}],
    )
    assert child_value == "prefix-value"
    vt = XSH.envtrace.record.variables["ENVTR_P"]
    assert vt.exported is True
    assert vt.value == "prefix-value"
    assert vt.layer == L_PREFIX
    assert "default rule stringifies" in vt.rule


def test_delete_var_mask_in_prefix_is_explained(envtrace_session):
    env = XSH.env
    env["ENVTR_MASK"] = "keep"
    run_envtrace("on", "ENVTR_MASK")
    child_value = run_subproc(
        [child_get_env("ENVTR_MASK")],
        captured="stdout",
        envs=[{"ENVTR_MASK": env.DELETE_VAR}],
    )
    assert child_value == "<unset>"
    vt = XSH.envtrace.record.variables["ENVTR_MASK"]
    assert vt.exported is False
    assert vt.reason == R_MASKED
    assert vt.layer == L_PREFIX


def test_thread_local_swap_layer(envtrace_session):
    env = XSH.env
    env["ENVTR_T"] = "global-value"
    run_envtrace("on", "ENVTR_T")
    with env.swap(ENVTR_T="swapped-value"):
        child_value = run_subproc(
            [child_get_env("ENVTR_T")], captured="stdout"
        )
    assert child_value == "swapped-value"
    vt = XSH.envtrace.record.variables["ENVTR_T"]
    assert vt.layer == L_THREAD_LOCAL
    assert vt.value == "swapped-value"
    # scope restored
    assert env["ENVTR_T"] == "global-value"


def test_layer_is_global_again_after_swap_restores(envtrace_session):
    # swap() restores originals through the thread-local map; once the
    # scope is gone the effective value is the global one again.
    env = XSH.env
    env["ENVTR_T2"] = "g"
    run_envtrace("on", "ENVTR_T2")
    with env.swap(ENVTR_T2="s"):
        run_subproc([child_get_env("ENVTR_T2")], captured="stdout")
        assert XSH.envtrace.record.variables["ENVTR_T2"].layer == L_THREAD_LOCAL
    run_subproc([child_get_env("ENVTR_T2")], captured="stdout")
    vt = XSH.envtrace.record.variables["ENVTR_T2"]
    assert vt.layer == L_GLOBAL
    assert vt.value == "g"


def test_callable_alias_overlay_layer(envtrace_session):
    def overlay_alias(args, stdin=None, stdout=None, stderr=None, env=None):
        env["ENVTR_A"] = "overlay-value"
        return run_subproc([child_get_env("ENVTR_A")], captured="stdout")

    XSH.aliases["envtr_overlay_alias"] = overlay_alias
    run_envtrace("on", "ENVTR_A")
    run_subproc([["envtr_overlay_alias"]], captured="stdout")
    rec = XSH.envtrace.record
    assert rec is not None
    vt = rec.variables["ENVTR_A"]
    assert vt.exported is True
    assert vt.value == "overlay-value"
    assert vt.layer == L_OVERLAY


@pytest.mark.flaky(reruns=3, reruns_delay=1)
def test_threaded_callable_alias_overlay_layer(envtrace_session):
    # The alias body (and therefore the child launch) runs on a worker
    # thread; swapped values are propagated and the record is committed
    # back to the shared state.
    XSH.env["THREAD_SUBPROCS"] = True

    def overlay_alias(args, stdin=None, stdout=None, stderr=None, env=None):
        env["ENVTR_AT"] = "thread-overlay"
        return run_subproc([child_get_env("ENVTR_AT")], captured="stdout")

    XSH.aliases["envtr_overlay_thread_alias"] = overlay_alias
    run_envtrace("on", "ENVTR_AT")
    run_subproc([["envtr_overlay_thread_alias"]], captured="stdout")
    vt = XSH.envtrace.record.variables["ENVTR_AT"]
    assert vt.exported is True
    assert vt.value == "thread-overlay"
    assert vt.layer == L_OVERLAY


def test_pre_spec_hook_spec_env_layer(envtrace_session, xonsh_events):
    def hook(spec, **kwargs):
        if spec.cmd and spec.cmd[0] == sys.executable:
            spec.env = spec.env or {}
            spec.env["ENVTR_H"] = "hook-value"

    xonsh_events.on_pre_spec_run(hook)
    run_envtrace("on", "ENVTR_H")
    child_value = run_subproc([child_get_env("ENVTR_H")], captured="stdout")
    assert child_value == "hook-value"
    vt = XSH.envtrace.record.variables["ENVTR_H"]
    assert vt.layer == L_HOOK
    assert vt.value == "hook-value"


def test_pre_spec_hook_direct_env_mutation_layer(envtrace_session, xonsh_events):
    def hook(spec, **kwargs):
        if spec.cmd and spec.cmd[0] == sys.executable:
            XSH.env["ENVTR_HG"] = "hook-global"

    xonsh_events.on_pre_spec_run(hook)
    run_envtrace("on", "ENVTR_HG")
    child_value = run_subproc([child_get_env("ENVTR_HG")], captured="stdout")
    assert child_value == "hook-global"
    vt = XSH.envtrace.record.variables["ENVTR_HG"]
    assert vt.layer == L_HOOK


@pytest.mark.skipif(not ON_WINDOWS, reason="PROMPT injection is Windows-only")
def test_windows_prompt_post_override(envtrace_session):
    run_envtrace("on", "PROMPT")
    run_subproc([[sys.executable, "-c", "pass"]], captured=False)
    vt = XSH.envtrace.record.variables["PROMPT"]
    assert vt.exported is True
    assert vt.value == "$P$G"
    assert vt.layer == L_POST


def test_name_pattern_rule_is_explained(envtrace_session):
    env = XSH.env
    env["ENVTR_RULES_PATTERN"] = VarPattern(r"ENVTR_PAT_.*", "str")
    env["ENVTR_PAT_VAR"] = "pval"
    run_envtrace("on", "ENVTR_PAT_VAR")
    child_value = run_subproc(
        [child_get_env("ENVTR_PAT_VAR")], captured="stdout"
    )
    assert child_value == "pval"
    vt = XSH.envtrace.record.variables["ENVTR_PAT_VAR"]
    assert vt.exported is True
    assert "name pattern" in vt.rule
    assert "ENVTR_RULES_PATTERN" in vt.rule


def test_unevaluated_callable_default_not_exported_and_not_called(
    envtrace_session,
):
    env = XSH.env
    calls = []

    @default_value
    def envtr_default(env):
        calls.append(1)
        raise AssertionError("default function must not be called")

    env.register("ENVTR_DEF", default=envtr_default, detype=lambda x: str(x))
    # Sanity: the variable is not explicitly stored.
    assert not env.is_manually_set("ENVTR_DEF")
    run_envtrace("on", "ENVTR_DEF")
    child_value = run_subproc([child_get_env("ENVTR_DEF")], captured="stdout")
    assert child_value == "<unset>"
    assert calls == []
    vt = XSH.envtrace.record.variables["ENVTR_DEF"]
    assert vt.exported is False
    assert vt.reason == R_DEFAULT_CALLABLE
    run_envtrace("show")
    assert calls == []


def test_absent_variable(envtrace_session):
    env = XSH.env
    # Registered with an explicit "no default" as well as fully unknown.
    env.register("ENVTR_NOVAL", default=DefaultNotGiven)
    run_envtrace("on", "ENVTR_NOVAL", "ENVTR_DOES_NOT_EXIST")
    run_subproc([child_get_env("ENVTR_NOVAL")], captured="stdout")
    rec = XSH.envtrace.record
    assert rec.variables["ENVTR_NOVAL"].reason == R_ABSENT
    assert rec.variables["ENVTR_NOVAL"].layer == L_ABSENT
    assert rec.variables["ENVTR_DOES_NOT_EXIST"].reason == R_ABSENT


def _commit_detyped_dict():
    """Drive a cache-hit detype through the real state hooks without
    launching a process (cache hits are POSIX-only end to end because
    Windows executable resolution reads PATHEXT before every launch)."""
    state = XSH.envtrace
    env = XSH.env
    spec = types.SimpleNamespace(env=None, args=["probe"])
    scope = state.begin_spec(spec)
    state.hooks_fired(spec, scope)
    denv = env.detype()
    state.commit(spec, denv, scope)
    return state.record


def test_detyped_cache_payload_collected_and_uncollected(envtrace_session):
    env = XSH.env
    state = XSH.envtrace

    # Cache built while collecting: payload exists and is reused truthfully.
    env["ENVTR_CC"] = "collected"
    env._invalidate_detyped_cache()
    state.start(["ENVTR_CC"])
    env.detype()  # build with envtrace active (no launch scope)
    rec = _commit_detyped_dict()
    assert rec.cache_used is True
    assert rec.sources_collected is True
    vt = rec.variables["ENVTR_CC"]
    assert vt.exported is True
    assert vt.value == "collected"
    assert vt.layer == L_GLOBAL

    # Cache built while NOT collecting: payload missing, provenance
    # uncollected; the observable string is still reported and conversion
    # is not re-run to fill the gap.
    state.stop()
    env["ENVTR_UC"] = "uncollected"
    env._invalidate_detyped_cache()
    env.detype()  # untraced build
    assert env._detyped_trace is None
    state.start(["ENVTR_UC"])
    rec = _commit_detyped_dict()
    assert rec.cache_used is True
    assert rec.sources_collected is False
    vt = rec.variables["ENVTR_UC"]
    assert vt.exported is True
    assert vt.value == "uncollected"
    assert vt.layer == L_UNCOLLECTED
    assert vt.source_collected is False


def test_non_idempotent_detyper_called_once_per_export(envtrace_session):
    env = XSH.env
    values = iter(["first", "second", "third"] + ["x"] * 10)
    calls = []

    def detyper(v):
        calls.append(1)
        return next(values)

    env.register("ENVTR_NI", detype=detyper)
    env["ENVTR_NI"] = object()
    run_envtrace("on", "ENVTR_NI")

    # First launch builds the detyped env once.
    run_subproc([child_get_env("ENVTR_NI")], captured=False)
    assert len(calls) == 1
    assert XSH.envtrace.record.variables["ENVTR_NI"].value == "first"

    # A cache invalidation forces exactly one new conversion on launch.
    env["ENVTR_OTHER"] = "x"
    child_2 = run_subproc([child_get_env("ENVTR_NI")], captured="stdout")
    assert child_2 == "second"
    assert len(calls) == 2

    # The record and show never consume extra conversions.
    run_envtrace("show")
    assert len(calls) == 2
    del env["ENVTR_OTHER"]


@skip_if_on_windows
def test_uncaptured_launch_reuses_export_cache_without_reconvert(
    envtrace_session, capfd
):
    env = XSH.env
    calls = []

    def detyper(v):
        calls.append(1)
        return f"n{len(calls)}"

    env.register("ENVTR_RC", detype=detyper)
    env["ENVTR_RC"] = object()
    run_envtrace("on", "ENVTR_RC")

    run_subproc([child_get_env("ENVTR_RC")], captured=False)
    assert len(calls) == 1
    assert "n1" in capfd.readouterr().out
    # Nothing invalidated the cache between the two launches.
    run_subproc([child_get_env("ENVTR_RC")], captured=False)
    assert len(calls) == 1
    rec = XSH.envtrace.record
    assert rec.cache_used is True
    assert rec.sources_collected is True
    assert rec.variables["ENVTR_RC"].value == "n1"
    assert "n1" in capfd.readouterr().out


@skip_if_on_windows
def test_cache_built_before_collection_is_marked_uncollected(envtrace_session):
    env = XSH.env
    env["ENVTR_C"] = "cached-before"
    env._invalidate_detyped_cache()
    # Build the cache while envtrace is off.
    run_subproc([child_get_env("ENVTR_C")], captured=False)
    assert env._detyped is not None
    assert env._detyped_trace is None

    run_envtrace("on", "ENVTR_C")
    run_subproc([child_get_env("ENVTR_C")], captured=False)
    rec = XSH.envtrace.record
    assert rec.cache_used is True
    assert rec.sources_collected is False
    vt = rec.variables["ENVTR_C"]
    # The submitted string is observable and truthful...
    assert vt.exported is True
    assert vt.value == "cached-before"
    # ...but provenance is explicitly uncollected, not fabricated.
    assert vt.layer == L_UNCOLLECTED
    assert vt.source_collected is False
    _, out, _ = run_envtrace("show")
    assert "before envtrace started" in out


@skip_if_on_windows
def test_cache_collected_in_previous_round_is_reused_truthfully(
    envtrace_session,
):
    env = XSH.env
    env["ENVTR_C1"] = "one"
    env["ENVTR_C2"] = "two"
    env._invalidate_detyped_cache()

    # Round 1 builds a fully collected cache while tracing C1.
    run_envtrace("on", "ENVTR_C1")
    run_subproc([child_get_env("ENVTR_C1")], captured=False)
    assert XSH.envtrace.record.cache_used is False

    # Round 2 traces C2 without invalidating the cache.
    run_envtrace("on", "ENVTR_C2")
    run_subproc([child_get_env("ENVTR_C2")], captured=False)
    rec = XSH.envtrace.record
    assert rec.cache_used is True
    assert rec.sources_collected is True
    vt = rec.variables["ENVTR_C2"]
    assert vt.exported is True
    assert vt.value == "two"
    assert vt.layer == L_GLOBAL


def test_on_starts_new_round_and_clears_record(envtrace_session):
    env = XSH.env
    env["ENVTR_R1"] = "1"
    run_envtrace("on", "ENVTR_R1")
    run_subproc([child_get_env("ENVTR_R1")], captured="stdout")
    assert XSH.envtrace.record is not None
    run_envtrace("on", "ENVTR_OTHER")
    assert XSH.envtrace.record is None
    rc, out, _ = run_envtrace("show")
    assert "No external process has been successfully started" in out


def test_off_retains_record_but_stops_collecting(envtrace_session):
    env = XSH.env
    env["ENVTR_O"] = "on-value"
    run_envtrace("on", "ENVTR_O")
    run_subproc([child_get_env("ENVTR_O")], captured="stdout")
    rec = XSH.envtrace.record
    assert rec is not None

    rc, _, _ = run_envtrace("off")
    assert rc == 0
    assert XSH.envtrace.active is False
    # A later launch neither updates nor clears the explanation.
    run_subproc([child_get_env("ENVTR_O")], captured=False)
    assert XSH.envtrace.record is rec
    _, out, _ = run_envtrace("show")
    assert "stopped" in out
    assert "'on-value'" in out


def test_off_preserves_execution_capture_and_trace(envtrace_session, capfd):
    env = XSH.env
    run_envtrace("on", "ENVTR_X")
    run_envtrace("off")
    # Ordinary captured execution still works.
    out = run_subproc(
        [[sys.executable, "-c", "import sys; sys.stdout.write('hello-envtrace')"]],
        captured="stdout",
    )
    assert out == "hello-envtrace"
    pipeline = run_subproc(
        [[sys.executable, "-c", "import sys; sys.stdout.write('x')"]],
        captured="object",
    )
    pipeline.end()
    assert pipeline.rtn == 0
    # XONSH_SUBPROC_TRACE is unaffected.
    env["XONSH_SUBPROC_TRACE"] = 1
    run_subproc([[sys.executable, "-c", "pass"]], captured=False)
    traced = capfd.readouterr().err
    assert "Trace run_subproc" in traced


def test_record_immutable_after_launch_changes(envtrace_session):
    env = XSH.env

    class Boom:
        def __repr__(self):
            raise AssertionError("user object must not be repr'd")

    def boom_detyper(b):
        return "boom-string"

    env.register(
        "ENVTR_IM",
        detype=boom_detyper,
        validate=lambda x: True,
    )
    env["ENVTR_IM"] = Boom()
    run_envtrace("on", "ENVTR_IM")
    run_subproc([child_get_env("ENVTR_IM")], captured="stdout")
    rec = XSH.envtrace.record
    assert rec.variables["ENVTR_IM"].value == "boom-string"

    # Change the value and the rules, then show: the explanation is frozen
    # and the user object is never repr()ed.
    env["ENVTR_IM"] = "later"
    env.register("ENVTR_IM", detype=lambda x: "LATER")
    _, out, _ = run_envtrace("show")
    assert "boom-string" in out
    assert "LATER" not in out
    assert XSH.envtrace.record is rec


def test_show_does_not_overwrite_record_or_launch(envtrace_session):
    env = XSH.env
    env["ENVTR_S"] = "s"
    run_envtrace("on", "ENVTR_S")
    run_subproc([child_get_env("ENVTR_S")], captured=False)
    rec = XSH.envtrace.record
    run_envtrace("show")
    run_envtrace("show")
    assert XSH.envtrace.record is rec
    assert XSH.envtrace.record.command == rec.command


def test_validator_and_converter_not_called_by_recording(envtrace_session):
    env = XSH.env
    validator_calls = []
    converter_calls = []

    env.register(
        "ENVTR_VC",
        validate=lambda x: validator_calls.append(1) is None and isinstance(x, str),
        convert=lambda x: (converter_calls.append(1), x)[1],
    )
    env["ENVTR_VC"] = "set-once"
    base_v, base_c = len(validator_calls), len(converter_calls)

    run_envtrace("on", "ENVTR_VC")
    run_subproc([child_get_env("ENVTR_VC")], captured="stdout")
    run_envtrace("show")
    assert len(validator_calls) == base_v
    assert len(converter_calls) == base_c


def test_unknown_action_and_on_without_names_fail(envtrace_session):
    rc, _, err = run_envtrace("frobnicate")
    assert rc == 1
    assert "unknown action" in err
    rc, _, err = run_envtrace("on")
    assert rc == 1
    assert "requires at least one variable name" in err


def test_last_successful_process_in_pipeline_is_recorded(envtrace_session):
    # Two real processes in a pipeline; the explanation belongs to the
    # most recently started process.
    env = XSH.env
    env["ENVTR_PL"] = "pipeline"
    run_envtrace("on", "ENVTR_PL")
    run_subproc(
        [
            [sys.executable, "-c", "import sys;sys.stdout.write('x\\n')"],
            "|",
            [sys.executable, "-c", "import sys;sys.stdin.read()"],
        ],
        captured="stdout",
    )
    rec = XSH.envtrace.record
    assert rec is not None
    assert rec.variables["ENVTR_PL"].exported is True
    assert rec.variables["ENVTR_PL"].value == "pipeline"
