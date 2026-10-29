"""Tracing of the environment that xonsh submits to external processes.

The ``envtrace`` builtin (``envtrace on NAME...`` / ``envtrace show`` /
``envtrace off``) explains why the typed values visible in the shell are or
are not handed to an external subprocess as strings.

Design constraints (keep these in mind when touching this module):

* Explanation is collected *along the real execution path* only — inside
  :meth:`xonsh.environ.Env.detype` while it performs the one conversion that
  actually happens, and around :meth:`xonsh.procs.specs.SubprocSpec.run`.
  Recording never runs validators, converters, detypers, default-value
  functions or event hooks a second time, and never calls ``repr()`` on,
  copies, or consumes user objects.  Transient layer bookkeeping stores
  ``id()`` integers only; the committed record holds plain strings.
* When the detyped export cache (``Env._detyped``) is reused, the record says
  so.  Provenance collected when the cache was built is reported as such; if
  the cache predates collection (no stored provenance), the source is marked
  "not collected" instead of re-running any conversion.
* A committed explanation is immutable: later changes to the environment,
  type registrations or local overrides never rewrite it.
"""

import contextlib
import sys
import threading
from typing import NamedTuple

from xonsh.built_ins import XSH
from xonsh.environ import (
    DELETE_VAR,
    DefaultNotGiven,
    VarPattern,
    is_callable_default,
)
from xonsh.tools import unthreadable

# ---------------------------------------------------------------------------
# Layer labels (also used verbatim in the explanation)
# ---------------------------------------------------------------------------

L_GLOBAL = "global environment value"
L_THREAD_LOCAL = "thread-local swap (XSH.env.swap)"
L_OVERLAY = "callable-alias local override (the alias env overlay)"
L_PREFIX = "command-prefix environment ($NAME=value in front of the command)"
L_HOOK = "modification made by an on_pre_spec_run hook"
L_POST = "override applied after detyping, immediately before launch"
L_DEFAULT = "default value that was never explicitly set"
L_ABSENT = "not set"
L_UNCOLLECTED = "unknown (the reused export cache predates envtrace)"

# detype() per-variable outcomes
O_EXPORTED = "exported"
O_MASKED = "masked"
O_NO_DETYPER = "no_detyper"
O_NONE = "none"

# explanation reasons for variables that were not exported
R_MASKED = (
    "masked with XSH.env.DELETE_VAR, so the variable is treated as unset"
)
R_NO_DETYPER = (
    "the effective type rule registers no detyper (detype=None)"
)
R_NONE = "the detyper returned None"
R_DEFAULT_CALLABLE = (
    "only a callable default exists; it has not been evaluated, and "
    "Env.detype() exports explicitly-set variables only"
)
R_DEFAULT = (
    "only a default value exists; the variable was never explicitly set, "
    "and Env.detype() exports explicitly-set variables only"
)
R_ABSENT = "the variable is not set and has no default value"
R_REMOVED = (
    "the detyped value was removed from the environment again before launch"
)
_UNCOLLECTED_RULE = (
    "type rule not collected (the reused export cache predates envtrace)"
)


def _func_name(func):
    """Return a human-readable name without calling or repr()ing ``func``."""
    if func is None:
        return "None"
    name = getattr(func, "__qualname__", None) or getattr(func, "__name__", None)
    if name:
        return name
    return type(func).__name__


class StoredFact(NamedTuple):
    """What detype() established for one variable during a real conversion.

    Only plain data: the exported ``value`` is already a ``str`` produced by
    the user detyper during its single, normally-scheduled invocation.
    """

    outcome: str
    value: "str | None"
    layer: str
    rule: str


class VarTrace(NamedTuple):
    """Immutable explanation entry for one traced variable."""

    name: str
    exported: bool
    value: "str | None"
    layer: str
    rule: str
    reason: "str | None"
    source_collected: bool


class LaunchTrace(NamedTuple):
    """Immutable explanation of one successfully launched external process."""

    command: "tuple[str, ...]"
    cache_used: bool
    sources_collected: bool
    variables: "dict[str, VarTrace]"


class _Scope:
    """Transient, thread-local bookkeeping for one SubprocSpec.run()."""

    __slots__ = (
        "spec_before",
        "spec_after",
        "env_before_local",
        "env_before_global",
        "env_after_local",
        "env_after_global",
        "entries",
        "cache_used",
        "sources_collected",
    )

    def __init__(self):
        self.spec_before: "dict[str, int]" = {}
        self.spec_after: "dict[str, int] | None" = None
        self.env_before_local: "dict[str, int]" = {}
        self.env_before_global: "dict[str, int]" = {}
        self.env_after_local: "dict[str, int] | None" = None
        self.env_after_global: "dict[str, int] | None" = None
        # facts delivered by Env.detype() for this launch
        self.entries: "dict[str, StoredFact] | None" = None
        self.cache_used = False
        self.sources_collected = True

    def attach(self, entries, *, cache_used, sources_collected):
        self.entries = entries
        self.cache_used = cache_used
        self.sources_collected = sources_collected


def _id_map(d):
    """Map keys to object ids without retaining user objects."""
    return {k: id(v) for k, v in d.items()} if d else {}


class _Collector:
    """Collects facts while Env.detype() performs its normal walk."""

    def __init__(self, state, env, scope):
        self._state = state
        self._env = env
        self._scope = scope
        self.entries: "dict[str, StoredFact]" = {}

    # -- layer/rule resolution; structural only, no user code invocation ---

    def _layer(self, key, val):
        env = self._env
        scope = self._scope
        # Overlays win over everything in detype()'s items.update() walk.
        for overlay in reversed(env._overlay_stack):
            if key in overlay:
                return L_OVERLAY
        cur = id(val)
        local = env._d._local
        glob = env._d._global
        if key in local and id(local[key]) == cur:
            # swap() restores originals by writing them back into the
            # thread-local map, so a local entry that is the very same
            # object as the global one is an effective global value, not
            # an active thread-local override.
            mirrored = key in glob and glob[key] is local[key]
            if mirrored:
                if (
                    scope is not None
                    and scope.env_after_global is not None
                    and scope.env_after_global.get(key) == cur
                    and scope.env_before_global.get(key) != cur
                ):
                    return L_HOOK
                return L_GLOBAL
            if scope is not None and scope.env_after_local is not None:
                # The prep_env_subproc swap put spec.env into the local
                # layer; values may have been converted by _set_item, so
                # compare against the pre-swap local map rather than ids
                # stored in spec.env itself.
                if scope.env_after_local.get(key) != cur:
                    if scope.spec_after is not None and key in scope.spec_after:
                        if scope.spec_after.get(key) != scope.spec_before.get(key):
                            return L_HOOK
                        return L_PREFIX
                if scope.env_before_local.get(key) != cur:
                    # A hook mutated the thread-local environment directly.
                    return L_HOOK
            return L_THREAD_LOCAL
        if key in glob:
            if (
                scope is not None
                and scope.env_after_global is not None
                and scope.env_after_global.get(key) == cur
                and scope.env_before_global.get(key) != cur
            ):
                return L_HOOK
            return L_GLOBAL
        return L_GLOBAL

    def _rule(self, key, detyper):
        return describe_rule(self._env, key, detyper)

    # -- notes taken from Env.detype()'s existing decision points ----------

    def note_masked(self, key):
        if not isinstance(key, str):
            return
        self.entries[key] = StoredFact(
            O_MASKED, None, self._layer(key, DELETE_VAR), self._rule(key, None)
        )

    def note_no_detyper(self, key, val, detyper):
        if not isinstance(key, str):
            return
        self.entries[key] = StoredFact(
            O_NO_DETYPER, None, self._layer(key, val), self._rule(key, detyper)
        )

    def note_none(self, key, val, detyper):
        if not isinstance(key, str):
            return
        self.entries[key] = StoredFact(
            O_NONE, None, self._layer(key, val), self._rule(key, detyper)
        )

    def note_exported(self, key, val, strval, detyper):
        if not isinstance(key, str):
            return
        self.entries[key] = StoredFact(
            O_EXPORTED, strval, self._layer(key, val), self._rule(key, detyper)
        )

    # -- delivery -----------------------------------------------------------

    def payload(self):
        """Facts to cache alongside Env._detyped (shared dict, no copies)."""
        return self.entries

    def deliver(self, payload, *, cache_used, sources_collected):
        if self._scope is not None:
            self._scope.attach(
                payload, cache_used=cache_used, sources_collected=sources_collected
            )


def describe_rule(env, key, detyper=None):
    """Describe the typing rule that is in effect for ``key``.

    Only structural lookups (and attribute reads) are performed: no
    validator, converter, detyper or default function is called.
    """
    if detyper is None:
        detyper = env.get_detyper(key)
    var = env._vars.get(key)
    if var is not None:
        if detyper is None:
            return (
                f"explicit type registration for {key} sets detype=None"
            )
        return (
            f"explicit type registration for {key} "
            f"(detyper: {_func_name(detyper)})"
        )
    pat = env._find_var_pattern_name(key)
    if pat is not None:
        pv = env._d.get(pat)
        if not isinstance(pv, VarPattern) and pat in env._vars:
            pv = env._vars[pat].default
        vtype = getattr(pv, "var_type", None)
        more = f", registered type {vtype!r}" if vtype else ""
        return (
            f"{key} matches the name pattern of ${pat}{more} "
            f"(detyper: {_func_name(detyper)})"
        )
    return (
        f"no explicit registration or name pattern for {key}; the default "
        "rule stringifies the value with str()"
    )


class EnvTraceState:
    """Holds the active envtrace round and its last committed explanation."""

    def __init__(self):
        self.active = False
        self.names: "tuple[str, ...]" = ()
        self.record: "LaunchTrace | None" = None
        self.round_no = 0
        self._tls = threading.local()
        self._lock = threading.Lock()

    # -- round control ------------------------------------------------------

    def start(self, names):
        seen = set()
        ordered = []
        for name in names:
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        self.round_no += 1
        self.names = tuple(ordered)
        self.record = None
        self.active = True

    def stop(self):
        """Stop collecting; the last record is retained."""
        self.active = False

    def reset(self):
        """Forget everything (used when a new xonsh session is loaded)."""
        self.active = False
        self.names = ()
        self.record = None
        self.round_no = 0
        scopes = getattr(self._tls, "scopes", None)
        if scopes:
            scopes.clear()

    # -- hooks used by SubprocSpec.run -------------------------------------

    @property
    def _scopes(self):
        scopes = self._tls.__dict__.get("scopes")
        if scopes is None:
            scopes = []
            self._tls.scopes = scopes
        return scopes

    def begin_spec(self, spec):
        env = XSH.env
        scope = _Scope()
        scope.spec_before = _id_map(spec.env)
        scope.env_before_local = _id_map(env._d._local)
        scope.env_before_global = _id_map(env._d._global)
        self._scopes.append(scope)
        return scope

    def hooks_fired(self, spec, scope):
        env = XSH.env
        scope.spec_after = _id_map(spec.env)
        scope.env_after_local = _id_map(env._d._local)
        scope.env_after_global = _id_map(env._d._global)

    def discard(self, scope):
        scopes = self._scopes
        try:
            scopes.remove(scope)
        except ValueError:
            pass

    def commit(self, spec, final_env, scope):
        scopes = self._scopes
        try:
            scopes.remove(scope)
        except ValueError:
            pass
        if not self.active:
            return
        command = tuple(str(a) for a in spec.args)
        env = XSH.env
        variables = {}
        for name in self.names:
            variables[name] = self._explain(name, env, scope, final_env)
        record = LaunchTrace(
            command=command,
            cache_used=scope.cache_used,
            sources_collected=scope.sources_collected,
            variables=variables,
        )
        with self._lock:
            self.record = record

    # -- hooks used by Env.detype ------------------------------------------

    def detype_begin(self, env):
        scope = self._scopes[-1] if self._scopes else None
        return _Collector(self, env, scope)

    def detype_cache_hit(self, env):
        """A detype() call reused Env._detyped verbatim."""
        if not self._scopes:
            return
        scope = self._scopes[-1]
        payload = env._detyped_trace
        scope.attach(
            payload if payload is not None else {},
            cache_used=True,
            sources_collected=payload is not None,
        )

    # -- explanation synthesis ---------------------------------------------

    def _missing(self, env, name):
        """Structural reason for a name the detype walk never saw."""
        var = env._vars.get(name)
        rule = describe_rule(env, name)
        if var is not None and var.default is not DefaultNotGiven:
            if is_callable_default(var.default):
                return R_DEFAULT_CALLABLE, L_DEFAULT, rule
            return R_DEFAULT, L_DEFAULT, rule
        return R_ABSENT, L_ABSENT, rule

    def _explain(self, name, env, scope, final_env):
        final = None
        final_present = False
        if isinstance(final_env, dict):
            with contextlib.suppress(KeyError):
                final = final_env[name]
                final_present = True

        if scope.cache_used and not scope.sources_collected:
            # The cache was built before envtrace collected anything: the
            # only observable truth is the final string. Do not re-run
            # detypers to fill provenance in.
            if final_present and isinstance(final, str):
                return VarTrace(
                    name, True, final, L_UNCOLLECTED, _UNCOLLECTED_RULE, None, False
                )
            reason, _layer, _rule = self._missing(env, name)
            return VarTrace(
                name, False, None, L_UNCOLLECTED, _UNCOLLECTED_RULE, reason, False
            )

        entries = scope.entries or {}
        fact = entries.get(name)
        if fact is None:
            reason, layer, rule = self._missing(env, name)
            if final_present and isinstance(final, str):
                # A value appeared only after detyping (e.g. a post-detype
                # hook on the launch environment).
                return VarTrace(
                    name,
                    True,
                    final,
                    L_POST,
                    f"added after detyping, immediately before launch; "
                    f"otherwise: {rule}",
                    None,
                    True,
                )
            return VarTrace(name, False, None, layer, rule, reason, True)

        if fact.outcome == O_EXPORTED:
            if final_present and isinstance(final, str) and final == fact.value:
                return VarTrace(
                    name, True, fact.value, fact.layer, fact.rule, None, True
                )
            if final_present and isinstance(final, str):
                return VarTrace(
                    name,
                    True,
                    final,
                    L_POST,
                    f"overridden after detyping, immediately before launch; "
                    f"detyped in the {fact.layer} via {fact.rule}",
                    None,
                    True,
                )
            return VarTrace(
                name,
                False,
                None,
                L_POST,
                fact.rule,
                R_REMOVED,
                True,
            )

        reason = {
            O_MASKED: R_MASKED,
            O_NO_DETYPER: R_NO_DETYPER,
            O_NONE: R_NONE,
        }[fact.outcome]
        if final_present and isinstance(final, str):
            # The value was re-instated as a plain string after detyping.
            return VarTrace(
                name,
                True,
                final,
                L_POST,
                f"added after detyping, immediately before launch; "
                f"detype had skipped it ({reason}) in the {fact.layer}",
                None,
                True,
            )
        return VarTrace(name, False, None, fact.layer, fact.rule, reason, True)

    # -- rendering ----------------------------------------------------------

    def render(self):
        if self.active:
            header = (
                "envtrace: recording round "
                f"{self.round_no} for: {', '.join(self.names)}"
            )
        elif self.names:
            header = (
                "envtrace: stopped; showing the retained explanation "
                f"(recording was for: {', '.join(self.names)})"
            )
        else:
            header = "envtrace: off; no recording round has been started"
        lines = [header]
        rec = self.record
        if rec is None:
            lines.append(
                "No external process has been successfully started in this "
                "recording round yet; there is nothing to show."
            )
            return "\n".join(lines)
        lines.append(
            "Last successful external process: " + " ".join(rec.command)
        )
        if rec.cache_used:
            if rec.sources_collected:
                lines.append(
                    "Exported environment: reused the detyped export cache; "
                    "the provenance below was recorded when that cache was "
                    "built."
                )
            else:
                lines.append(
                    "Exported environment: reused an export cache built "
                    "BEFORE envtrace started collecting; value provenance "
                    "was not collected (conversion was not re-run)."
                )
        else:
            lines.append(
                "Exported environment: detyped along this launch path."
            )
        for name in self.names:
            vt = rec.variables.get(name)
            if vt is None:
                lines.append(f"  {name}: (no observation)")
                continue
            if vt.exported:
                lines.append(f"  {name} = {vt.value!r}")
            else:
                lines.append(f"  {name} = NOT EXPORTED")
                lines.append(f"      reason: {vt.reason}")
            lines.append(f"      layer: {vt.layer}")
            lines.append(f"      rule: {vt.rule}")
            if not vt.source_collected:
                lines.append(
                    "      source: not collected (cache predates envtrace)"
                )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Builtin command
# ---------------------------------------------------------------------------

HELP = """\
usage:
  envtrace on NAME [NAME ...]
      Start a new recording round tracing the named environment variables.
      A new round discards the previous explanation.
  envtrace show
      Explain the last external process that was successfully started
      in the current (or, after off, the last) recording round.
  envtrace off
      Stop collecting. The last explanation is retained for `show`.

envtrace never re-runs type conversion and never launches a subprocess
itself; it reports the values that were really submitted (or why a value
was not exported) along xonsh's normal environment layering: global
values, thread-local swaps, callable-alias env overlays, command-prefix
assignments, on_pre_spec_run hook edits, and final pre-launch overrides.
"""


@unthreadable
def envtrace(args, stdin=None, stdout=None, stderr=None, spec=None):
    """Builtin: trace the environment exported to external processes."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    args = list(args or [])
    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP.rstrip(), file=out)
        return 0
    action = args[0]
    state = XSH.envtrace
    if action == "on":
        names = args[1:]
        if not names:
            print("envtrace: `on` requires at least one variable name.", file=err)
            print(HELP.rstrip(), file=err)
            return 1
        state.start(names)
        print(
            "envtrace: started recording round "
            f"{state.round_no} for: {' '.join(state.names)}",
            file=out,
        )
        return 0
    if action == "show":
        print(state.render(), file=out)
        return 0
    if action == "off":
        state.stop()
        print(
            "envtrace: stopped collecting; the last explanation is retained.",
            file=out,
        )
        return 0
    print(f"envtrace: unknown action {action!r}.", file=err)
    print(HELP.rstrip(), file=err)
    return 1
