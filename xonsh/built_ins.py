"""The xonsh built-ins.

Note that this module is named 'built_ins' so as not to be confused with the
special Python builtins module.
"""

from __future__ import annotations

import atexit
import builtins
import collections.abc as cabc
import contextlib
import inspect
import itertools
import os
import pathlib
import re
import signal
import sys
import types
import uuid
import warnings
from ast import AST
from collections.abc import Iterator
from operator import attrgetter as _attrgetter

from xonsh.debug import XonshDebug
from xonsh.lib.inspectors import Inspector
from xonsh.lib.lazyasd import lazyobject
from xonsh.platform import ON_POSIX, ON_WINDOWS
from xonsh.tools import (
    XonshCalledProcessError,
    XonshError,
    expand_path,
    globpath,
    on_main_thread,
    print_color,
)

INSPECTOR = Inspector()

warnings.filterwarnings("once", category=DeprecationWarning)


@lazyobject
def AT_EXIT_SIGNALS():
    sigs = (
        signal.SIGABRT,
        signal.SIGFPE,
        signal.SIGILL,
        signal.SIGSEGV,
        signal.SIGTERM,
    )
    if ON_POSIX:
        sigs += (signal.SIGTSTP, signal.SIGQUIT, signal.SIGHUP)
    return sigs


def resetting_signal_handle(sig, f):
    """Sets a new signal handle that will automatically restore the old value
    once the new handle is finished.
    """
    # signal.signal() only works on the main thread; from a worker it raises
    # ValueError. When xonsh is set up from a non-main thread (e.g. an embedded
    # host calling xonsh.main.setup() off-main, like the 2020 virtualenv CI
    # case in xonsh/xonsh#3689), skip registration rather than crash — the host
    # owns process signals in that scenario. Same guard is used in procs/posix.py.
    if not on_main_thread():
        return
    prev_signal_handler = signal.getsignal(sig)

    def new_signal_handler(s=None, frame=None):
        f(s, frame)
        signal.signal(sig, prev_signal_handler)
        if sig == signal.SIGHUP:
            """
            SIGHUP means the controlling terminal has been lost. This should be
            propagated to child processes so that they can decide what to do about it.
            See also: https://www.gnu.org/software/bash/manual/bash.html#Signals
            """
            import xonsh.procs.jobs as xj

            xj.hup_all_jobs()
        if sig != 0:
            """
            There is no immediate exiting here.
            The ``sys.exit()`` function raises a ``SystemExit`` exception.
            This exception must be caught and processed in the upstream code.
            """
            sys.exit(sig)

    signal.signal(sig, new_signal_handler)


def helper(x, name=""):
    """Prints help about, and then returns that variable."""
    name = name or getattr(x, "__name__", "")
    INSPECTOR.pinfo(x, oname=name, detail_level=0)
    return x


def superhelper(x, name=""):
    """Prints help about, and then returns that variable."""
    name = name or getattr(x, "__name__", "")
    INSPECTOR.pinfo(x, oname=name, detail_level=1)
    return x


def reglob(path, parts=None, i=None, include_dotfiles=False):
    """Regular expression-based globbing."""
    if parts is None:
        path = os.path.normpath(path)
        drive, tail = os.path.splitdrive(path)
        parts = tail.split(os.sep)
        d = os.sep if os.path.isabs(path) else "."
        d = os.path.join(drive, d)
        return reglob(d, parts, i=0, include_dotfiles=include_dotfiles)
    base = subdir = path
    if i == 0:
        if not os.path.isabs(base):
            base = ""
        elif len(parts) > 1:
            i += 1
    try:
        regex = re.compile(parts[i])
    except re.error as e:
        original = "/".join(parts)
        raise XonshError(
            f"Regex glob error in segment {parts[i]!r} of pattern {original!r}: {e}. "
            f"Regex globs are split by '/' and each segment is compiled separately, "
            f"so groups and backreferences cannot span across '/'. "
            f"See https://xon.sh/globbing.html"
        ) from e

    files = os.listdir(subdir)
    files.sort()
    paths = []
    i1 = i + 1
    if i1 == len(parts):
        for f in files:
            if not include_dotfiles and f.startswith("."):
                continue
            p = os.path.join(base, f)
            if regex.fullmatch(f) is not None:
                paths.append(p)
    else:
        for f in files:
            if not include_dotfiles and f.startswith("."):
                continue
            p = os.path.join(base, f)
            if regex.fullmatch(f) is None or not os.path.isdir(p):
                continue
            paths += reglob(p, parts=parts, i=i1, include_dotfiles=include_dotfiles)
    return paths


# mypy support
if sys.platform == "win32":
    BasePath = pathlib.WindowsPath
else:
    BasePath = pathlib.PosixPath


class XonshPathLiteralChangeDirectoryContextManager:
    """Implements context manager to use in xonsh path literal."""

    def __init__(self, path: XonshPathLiteral):
        self.path = path

    def __enter__(self):
        self._xonsh_old_cwd = os.getcwd()
        os.chdir(self.path)
        return self.path

    def __exit__(self, exc_type, exc, tb):
        os.chdir(self._xonsh_old_cwd)
        return False


class XonshPathLiteral(BasePath):  # type: ignore
    """Extension of ``pathlib.Path`` to support extended functionality."""

    def cd(self) -> XonshPathLiteralChangeDirectoryContextManager:
        """Returns context manager to change the directory
        e.g. ``with p'/tmp'.cd(): $[ls]``
        """
        return XonshPathLiteralChangeDirectoryContextManager(self)

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        """Extension of ``pathlib.Path.mkdir`` that returns ``self`` instead of ``None``."""
        super().mkdir(mode=mode, parents=parents, exist_ok=exist_ok)
        return self

    def chmod(self, mode, *, follow_symlinks=True):
        """Extension of ``pathlib.Path.chmod`` that returns ``self`` instead of ``None``."""
        super().chmod(mode, follow_symlinks=follow_symlinks)
        return self

    def touch(self, mode=0o666, exist_ok=True):
        """Extension of ``pathlib.Path.touch`` that returns ``self`` instead of ``None``."""
        super().touch(mode=mode, exist_ok=exist_ok)
        return self


class XonshList(list):
    """List subclass returned by glob operations with convenience methods.

    All methods return XonshList, enabling chaining::

        g`**/*.py`.files().sorted().paths()
    """

    def _check_paths(self, method):
        """Raise if list contains tuples (e.g. from multi-group m``)."""
        if self and isinstance(self[0], tuple):
            raise TypeError(
                f".{method}() requires string paths, got tuples. "
                f"Use .select(n) to pick a tuple element first, e.g. .select(0).{method}()"
            )

    def select(self, n):
        """Pick the n-th element from each tuple, skipping None values."""
        return XonshList(
            v
            for x in self
            for v in [x[n] if isinstance(x, tuple) else x]
            if v is not None
        )

    def unique(self):
        """Return a XonshList with duplicates removed, preserving order."""
        return XonshList(dict.fromkeys(self))

    def paths(self):
        """Convert string elements to pathlib.Path objects."""
        self._check_paths("paths")
        return XonshList(pathlib.Path(p) for p in self)

    def sorted(self, key=None, reverse=False):
        """Return a new sorted XonshList."""
        return XonshList(builtins.sorted(self, key=key, reverse=reverse))

    def filter(self, func):
        """Return a XonshList with only elements where func(elem) is truthy."""
        return XonshList(x for x in self if func(x))

    def dirs(self):
        """Keep only paths that are existing directories."""
        self._check_paths("dirs")
        return XonshList(p for p in self if os.path.isdir(p))

    def files(self):
        """Keep only paths that are existing files."""
        self._check_paths("files")
        return XonshList(p for p in self if os.path.isfile(p))

    def exists(self):
        """Keep only paths that exist on disk."""
        self._check_paths("exists")
        return XonshList(p for p in self if os.path.exists(p))

    @staticmethod
    def _is_hidden(p):
        """Check if a path is hidden. Cross-platform: dotfiles on Unix,
        FILE_ATTRIBUTE_HIDDEN on Windows."""
        name = os.path.basename(p)
        if name.startswith("."):
            return True
        if ON_WINDOWS:
            try:
                import stat

                attrs = os.stat(p).st_file_attributes
                return bool(attrs & stat.FILE_ATTRIBUTE_HIDDEN)
            except (OSError, AttributeError):
                pass
        return False

    def hidden(self):
        """Keep only hidden files and directories.
        On Unix: names starting with '.'. On Windows: also FILE_ATTRIBUTE_HIDDEN.
        """
        self._check_paths("hidden")
        return XonshList(p for p in self if self._is_hidden(p))

    def visible(self):
        """Keep only visible (non-hidden) files and directories.
        On Unix: names not starting with '.'. On Windows: no FILE_ATTRIBUTE_HIDDEN.
        """
        self._check_paths("visible")
        return XonshList(p for p in self if not self._is_hidden(p))


def path_literal(s):
    s = expand_path(s)
    return XonshPathLiteral(s)


def regexsearch(s):
    s = expand_path(s)
    dotglob = XSH.env.get("DOTGLOB")
    return XonshList(reglob(s, include_dotfiles=dotglob))


def regexmatchsearch(s):
    """Regex glob that returns match groups instead of paths."""
    s = expand_path(s)
    dotglob = XSH.env.get("DOTGLOB")
    regex = re.compile(s)
    # Find the static prefix (path before any regex special chars)
    _RE_SPECIAL = re.compile(r"[\\()\[\]{}.*+?|^$]")
    m = _RE_SPECIAL.search(s)
    if m:
        prefix = s[: m.start()]
        start = prefix.rsplit("/", 1)[0] or "."
    else:
        start = s if os.path.isdir(s) else os.path.dirname(s) or "."
    results = []
    for root, dirs, files in os.walk(start):
        if not dotglob:
            dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in dirs + files:
            if not dotglob and name.startswith("."):
                continue
            path = os.path.join(root, name)
            match = regex.fullmatch(path)
            if match:
                groups = match.groups()
                if not groups:
                    results.append(path)
                elif len(groups) == 1:
                    results.append(groups[0])
                else:
                    results.append(groups)
    results.sort()
    return XonshList(results)


def globsearch(s):
    glob_sorted = XSH.env.get("GLOB_SORTED")
    dotglob = XSH.env.get("DOTGLOB")
    return XonshList(
        globpath(
            s,
            ignore_case=True,
            return_empty=True,
            sort_result=glob_sorted,
            include_dotfiles=dotglob,
        )
    )


def pathsearch(func, s, pymode=False, pathobj=False):
    """
    Takes a string and returns a list of file paths that match (regex, glob,
    or arbitrary search function). If pathobj=True, the return is a list of
    pathlib.Path objects instead of strings.
    """
    if not callable(func) or len(inspect.signature(func).parameters) != 1:
        error = "%r is not a known path search function"
        raise XonshError(error % func)
    o = func(s)
    if pathobj and pymode:
        o = XonshList(map(pathlib.Path, o))
    no_match = XonshList() if pymode else [s]
    return o if len(o) != 0 else no_match


def _check_subproc_helper_raise(in_boolop):
    """Raise ``CalledProcessError`` if the most recently completed
    pipeline (``XSH.lastcmd``) failed and the active settings say we
    should raise.

    Called by the ``subproc_*`` helpers right after they finish, so
    that nested calls like ``echo @$(ls nono)`` raise on the inner
    failure even though the *outer* helper would have succeeded with
    an empty injection.

    Skip conditions:

    * ``in_boolop=True`` — the helper is a direct operand of a
      ``&&``/``||`` chain; let the chain wrapper see the result.
    * ``$XONSH_SUBPROC_RAISE_ERROR`` is False.
    * No completed pipeline (e.g. background job).
    * The pipeline succeeded.
    * ``spec.captured == 'object'`` — ``!(...)`` is the explicit
      "user takes responsibility" form per spec.
    * ``@error_ignore`` was applied (``spec.raise_subproc_error is False``).
    """
    if in_boolop:
        return
    if not XSH.env.get("XONSH_SUBPROC_RAISE_ERROR"):
        return
    cp = XSH.lastcmd
    if cp is None:
        return
    spec = getattr(cp, "spec", None)
    if spec is None:
        return
    # Background job: pipeline is still running; reading .returncode would
    # block on the blocking_property until the child exits.
    if getattr(spec, "background", False):
        return
    if getattr(spec, "captured", None) == "object":
        return
    if getattr(spec, "raise_subproc_error", None) is False:
        return
    rtn = getattr(cp, "returncode", None)
    if rtn is None or rtn == 0:
        return

    import subprocess

    output = getattr(cp, "output", None)
    raise subprocess.CalledProcessError(rtn, spec.args, output=output)


def subproc_captured_stdout(*cmds, envs=None, in_boolop=False):
    """Runs a subprocess, capturing the output. Returns the stdout
    that was produced as a str or list based on ``$XONSH_SUBPROC_OUTPUT_FORMAT``.

    ``in_boolop`` is set by the parser to True when this call is a direct
    operand of a ``&&``/``||`` chain, so the runtime can adjust behavior
    (e.g. suppress ``$XONSH_SUBPROC_CMD_RAISE_ERROR`` to let the chain
    short-circuit on returncode).
    """

    import xonsh.procs.specs

    r = xonsh.procs.specs.run_subproc(
        cmds, captured="stdout", envs=envs, in_boolop=in_boolop
    )
    _check_subproc_helper_raise(in_boolop)
    return r


def subproc_captured_inject(*cmds, envs=None, in_boolop=False):
    """Runs a subprocess, capturing the output. Returns a list of
    whitespace-separated strings of the stdout that was produced.
    The string is split using xonsh's lexer, rather than Python's str.split()
    or shlex.split().
    """
    import xonsh.procs.specs

    o = xonsh.procs.specs.run_subproc(
        cmds, captured="stdout", envs=envs, in_boolop=in_boolop
    )
    _check_subproc_helper_raise(in_boolop)
    o = o if isinstance(o, list) else o.splitlines()
    toks = []
    for line in o:
        line = line.rstrip(os.linesep)
        toks.extend(XSH.execer.parser.lexer.split(line))
    return toks


def subproc_captured_object(*cmds, envs=None, in_boolop=False):
    """
    Runs a subprocess, capturing the output. Returns an instance of
    CommandPipeline representing the completed command.
    """
    import xonsh.procs.specs

    return xonsh.procs.specs.run_subproc(
        cmds, captured="object", envs=envs, in_boolop=in_boolop
    )


def subproc_captured_hiddenobject(*cmds, envs=None, in_boolop=False):
    """Runs a subprocess, capturing the output. Returns an instance of
    HiddenCommandPipeline representing the completed command.
    """
    import xonsh.procs.specs

    return xonsh.procs.specs.run_subproc(
        cmds, captured="hiddenobject", envs=envs, in_boolop=in_boolop
    )


def subproc_uncaptured(*cmds, envs=None, in_boolop=False):
    """Runs a subprocess, without capturing the output. Returns the stdout
    that was produced as a str.
    """
    import xonsh.procs.specs

    r = xonsh.procs.specs.run_subproc(
        cmds, captured=False, envs=envs, in_boolop=in_boolop
    )
    _check_subproc_helper_raise(in_boolop)
    return r


def subproc_check_boolop(value):
    """Raise ``CalledProcessError`` if *value* is the (final) result of
    a subproc statement that ended in failure.

    Used as the AST wrapper around two kinds of nodes:

    * The outermost ``BoolOp`` of a logical chain (``cmd1 && cmd2`` /
      ``cmd1 || cmd2``).  Python ``and``/``or`` short-circuits to the
      pipeline that determined the chain result, so the wrapper sees
      the *final* pipeline.
    * A standalone subproc-helper call at statement level — bare
      commands, ``![...]``, ``$[...]``, ``$(...)``, ``@$(...)``.  The
      explicit-capture form ``!(...)`` is *intentionally* not wrapped:
      per spec it is the user's full responsibility.

    When ``$XONSH_SUBPROC_RAISE_ERROR`` is True (default), this enforces
    "raise on the last executed pipeline's non-zero exit code" semantics:

    * ``ls nono`` (standalone) — rc≠0: raises.
    * ``$(ls nono)`` (standalone) — rc≠0: raises.
    * ``$[ls nono]`` (standalone) — rc≠0: raises.
    * ``echo 1 && echo 2`` — last is ``echo 2`` (rc=0): no raise.
    * ``ls nono || echo 1`` — last is ``echo 1`` (rc=0): no raise.
    * ``ls nono && echo 1`` — last is ``ls nono`` (rc≠0): raises.

    Per-command overrides:

    * ``@error_ignore`` (``spec.raise_subproc_error is False``) on the
      final pipeline suppresses the raise — used e.g. for
      ``echo 1 && @error_ignore ls nono``.
    * ``@error_raise`` raises directly at the pipeline level, so the
      wrapper never sees a failed value.

    Some subproc helpers (``$()``, ``$[]``, ``@$()``) return a *string*
    / *None* / *list* rather than a ``CommandPipeline``, so the wrapper
    can't read ``returncode`` directly off ``value``.  In that case it
    falls back to ``XSH.lastcmd`` — the most recently completed pipeline,
    which is the one this helper just ran.
    """
    if not XSH.env.get("XONSH_SUBPROC_RAISE_ERROR"):
        return value
    # Try to get the pipeline from the value first (BoolOp result, ![...]).
    spec = getattr(value, "spec", None)
    # Background job: pipeline is still running; reading .returncode would
    # block on the blocking_property until the child exits.
    if spec is not None and getattr(spec, "background", False):
        return value
    rtn = getattr(value, "returncode", None)
    cp_for_output = value
    if spec is None or rtn is None:
        # Value isn't a CommandPipeline (e.g. string from $(), None from
        # $[], list from @$()).  Fall back to the most recently
        # completed pipeline so we can still inspect its returncode.
        last = XSH.lastcmd
        if last is None:
            return value
        spec = getattr(last, "spec", None)
        if spec is not None and getattr(spec, "background", False):
            return value
        rtn = getattr(last, "returncode", None)
        cp_for_output = last
    if spec is None or rtn is None or rtn == 0:
        return value
    # ``!(...)`` is the "user takes full responsibility" form — never
    # raise even if it ends up here via a chain like ``cmd && !(cmd)``.
    if getattr(spec, "captured", None) == "object":
        return value
    # @error_ignore on the final pipeline of the chain opts out.
    if getattr(spec, "raise_subproc_error", None) is False:
        return value
    import subprocess

    output = getattr(cp_for_output, "output", None)
    raise subprocess.CalledProcessError(rtn, spec.args, output=output)


_SPECIAL_BUILTINS = {"...": ...}


def builtin_cmd(name):
    """Run a builtin name as a subprocess command if $XONSH_BUILTINS_TO_CMD is set
    and the name is a known command or alias. Otherwise return the builtin value."""
    env = XSH.env or {}
    if env.get("XONSH_BUILTINS_TO_CMD"):
        has_cmd = name in (XSH.aliases or {})
        if not has_cmd and XSH.commands_cache:
            has_cmd = XSH.commands_cache.locate_binary(name) is not None
        if has_cmd:
            return subproc_captured_hiddenobject([name])
    if name in _SPECIAL_BUILTINS:
        return _SPECIAL_BUILTINS[name]
    import builtins as _builtins

    return getattr(_builtins, name, None)


def ensure_list_of_strs(x):
    """Ensures that x is a list of strings."""
    if isinstance(x, str):
        rtn = [x]
    elif isinstance(x, cabc.Sequence):
        rtn = [i if isinstance(i, str) else str(i) for i in x]
    else:
        rtn = [str(x)]
    return rtn


def ensure_str_or_callable(x):
    """Ensures that x is single string or function."""
    if isinstance(x, str) or callable(x):
        return x
    if isinstance(x, bytes):
        # ``os.fsdecode`` decodes using "surrogateescape" on linux and "strict" on windows.
        # This is used to decode bytes for interfacing with the os, notably for command line arguments.
        # See https://www.python.org/dev/peps/pep-0383/#specification
        return os.fsdecode(x)
    return str(x)


def list_of_strs_or_callables(x):
    """
    Ensures that x is a list of strings or functions.
    This is called when using the ``@()`` operator to expand it's content.
    """
    if isinstance(x, str | bytes) or callable(x):
        rtn = [ensure_str_or_callable(x)]
    elif isinstance(x, cabc.Iterable):
        rtn = list(map(ensure_str_or_callable, x))
    else:
        rtn = [ensure_str_or_callable(x)]
    return rtn


def list_of_list_of_strs_outer_product(x):
    """Takes an outer product of a list of strings"""
    lolos = map(ensure_list_of_strs, x)
    rtn = []
    for los in itertools.product(*lolos):
        s = "".join(los)
        if "*" in s:
            rtn.extend(XSH.glob(s))
        else:
            rtn.append(XSH.expand_path(s))
    return rtn


def eval_fstring_field(field):
    """Evaluates the argument in Xonsh context."""
    res = XSH.execer.eval(
        field[0].strip(), glbs=globals(), locs=XSH.ctx, filename=field[1]
    )
    return res


@lazyobject
def MACRO_FLAG_KINDS():
    return {
        "s": str,
        "str": str,
        "string": str,
        "a": AST,
        "ast": AST,
        "c": types.CodeType,
        "code": types.CodeType,
        "compile": types.CodeType,
        "v": eval,
        "eval": eval,
        "x": exec,
        "exec": exec,
        "t": type,
        "type": type,
    }


def _convert_kind_flag(x):
    """Puts a kind flag (string) a canonical form."""
    x = x.lower()
    kind = MACRO_FLAG_KINDS.get(x, None)
    if kind is None:
        raise TypeError(f"{x!r} not a recognized macro type.")
    return kind


def _split_macro_kind_mode(kind):
    """Canonicalizes a macro annotation into a ``(kind, mode)`` pair.

    String flags are converted to the corresponding builtin objects via
    :data:`MACRO_FLAG_KINDS`; unknown flags raise ``TypeError``.
    """
    mode = None
    if isinstance(kind, cabc.Sequence) and not isinstance(kind, str):
        # have (kind, mode) tuple
        kind, mode = kind
    if isinstance(kind, str):
        kind = _convert_kind_flag(kind)
    return kind, mode


def _prepare_macro_arg(
    raw_arg, kind, glbs, locs, *, name="<arg>", macroname="<macro>"
):
    """Does everything that is needed to convert a string macro argument
    *without executing it*.

    Returns a ``(tag, obj, mode)`` triple where ``tag`` is one of:

    * ``"value"`` -- ``obj`` is the converted argument itself (str/None
      arguments, parsed AST nodes, and compiled code objects);
    * ``"eval"`` -- ``obj`` is a code object to be evaluated;
    * ``"exec"`` -- ``obj`` is a code object to be execed in ``mode``;
    * ``"type"`` -- ``obj`` is a code object whose result's type is wanted.

    Use :func:`_run_prepared_macro_arg` to finish the conversion.
    Splitting preparation and execution lets prechecked macro calls parse
    and compile every argument up front (against a fixed namespace) while
    still only evaluating each argument once, later.

    Parameters
    ----------
    raw_arg : str
        The str representation of the macro argument.
    kind : object
        A flag or type representing how to convert the argument.
    glbs : Mapping
        The globals from the call site (may be a frozen-key view).
    locs : Mapping or None
        The locals from the call site (may be a frozen-key view).
    name : str, optional
        The macro argument name.
    macroname : str, optional
        The name of the macro itself.
    """
    kind, mode = _split_macro_kind_mode(kind)
    if kind is str or kind is None:
        return ("value", raw_arg, None)  # nothing to parse, compile, or run
    # select from kind and prepare
    execer = XSH.execer
    filename = macroname + "(" + name + ")"
    if kind is AST:
        ctx = set(dir(builtins)) | set(glbs.keys())
        if locs is not None:
            ctx |= set(locs.keys())
        mode = mode or "eval"
        if mode != "eval" and not raw_arg.endswith("\n"):
            raw_arg += "\n"
        arg = execer.parse(raw_arg, ctx, mode=mode, filename=filename)
        return ("value", arg, None)
    elif kind is types.CodeType or kind is compile:  # NOQA
        mode = mode or "eval"
        arg = execer.compile(
            raw_arg, mode=mode, glbs=glbs, locs=locs, filename=filename
        )
        return ("value", arg, None)
    elif kind is eval:
        # note: the mode of an eval argument is historically ignored, so it
        # is intentionally not consulted here either
        code = execer.compile(
            raw_arg.rstrip("\n"),
            mode="eval",
            glbs=glbs,
            locs=locs,
            filename=filename,
        )
        return ("eval", code, None)
    elif kind is exec:
        mode = mode or "exec"
        if not raw_arg.endswith("\n"):
            raw_arg += "\n"
        code = execer.compile(
            raw_arg, mode=mode, glbs=glbs, locs=locs, filename=filename
        )
        return ("exec", code, mode)
    elif kind is type:
        code = execer.compile(
            raw_arg.rstrip("\n"),
            mode="eval",
            glbs=glbs,
            locs=locs,
            filename=filename,
        )
        return ("type", code, None)
    else:
        msg = "kind={0!r} and mode={1!r} was not recognized for macro argument {2!r}"
        raise TypeError(msg.format(kind, mode, name))


def _run_prepared_macro_arg(prepared, glbs, locs):
    """Finishes converting a macro argument prepared by
    :func:`_prepare_macro_arg`, executing it if necessary."""
    tag, obj, mode = prepared
    if tag == "value":
        return obj
    elif tag == "eval":
        return XSH.execer.eval(obj, glbs=glbs, locs=locs)
    elif tag == "exec":
        return XSH.execer.exec(obj, mode=mode, glbs=glbs, locs=locs)
    elif tag == "type":
        return type(XSH.execer.eval(obj, glbs=glbs, locs=locs))
    else:
        # should be unreachable
        raise AssertionError(f"unknown prepared macro argument tag: {tag!r}")


def convert_macro_arg(raw_arg, kind, glbs, locs, *, name="<arg>", macroname="<macro>"):
    """Converts a string macro argument based on the requested kind.

    Parameters
    ----------
    raw_arg : str
        The str representation of the macro argument.
    kind : object
        A flag or type representing how to convert the argument.
    glbs : Mapping
        The globals from the call site.
    locs : Mapping or None
        The locals from the call site.
    name : str, optional
        The macro argument name.
    macroname : str, optional
        The name of the macro itself.

    Returns
    -------
    The converted argument.
    """
    prepared = _prepare_macro_arg(
        raw_arg, kind, glbs, locs, name=name, macroname=macroname
    )
    return _run_prepared_macro_arg(prepared, glbs, locs)


@contextlib.contextmanager
def in_macro_call(f, glbs, locs):
    """Attaches macro globals and locals temporarily to function as a
    context manager.

    Parameters
    ----------
    f : callable object
        The function that is called as ``f(*args)``.
    glbs : Mapping
        The globals from the call site.
    locs : Mapping or None
        The locals from the call site.
    """
    prev_glbs = getattr(f, "macro_globals", None)
    prev_locs = getattr(f, "macro_locals", None)
    f.macro_globals = glbs
    f.macro_locals = locs
    yield
    if prev_glbs is None:
        del f.macro_globals
    else:
        f.macro_globals = prev_glbs
    if prev_locs is None:
        del f.macro_locals
    else:
        f.macro_locals = prev_locs


MACRO_PRECHECK_ATTR = "__xonsh_macro_precheck__"


def macro_precheck(f):
    """Decorator enabling whole-call argument preflight for a function macro.

    Attach it to a macro function (or set ``f.__xonsh_macro_precheck__ = True``
    manually) to make the macro caller validate the *entire* call before any
    macro argument expression is executed or the macro body is entered. See
    :func:`call_macro` and the macro documentation for the exact guarantees
    and limitations.
    """
    setattr(f, MACRO_PRECHECK_ATTR, True)
    return f


def call_macro(f, raw_args, glbs, locs):
    """Calls a function as a macro, returning its result.

    Parameters
    ----------
    f : callable object
        The function that is called as ``f(*args)``.
    raw_args : tuple of str
        The str representation of arguments of that were passed into the
        macro. These strings will be parsed, compiled, evaled, or left as
        a string depending on the annotations of f.
    glbs : Mapping
        The globals from the call site.
    locs : Mapping or None
        The locals from the call site.

    Notes
    -----
    By default macro arguments are converted one at a time, so a failing
    later argument does not undo the side effects of earlier ones. If ``f``
    is marked with :func:`macro_precheck` (i.e.
    ``getattr(f, "__xonsh_macro_precheck__", False)`` is true), the whole
    call is preflighted first: its argument shape is checked against the
    signature and every argument source is validated according to the
    annotation/mode that will actually be used (strings are left untouched,
    AST sources are parsed, code/eval/exec/type sources are compiled). Only
    if all of that succeeds are the conversions and regular argument
    expressions executed exactly once, in their original order, followed by
    the macro body itself.
    """
    sig = inspect.signature(f)
    macroname = f.__name__
    if getattr(f, MACRO_PRECHECK_ATTR, False):
        plan = _precheck_macro_call(sig, raw_args, glbs, locs, macroname)
        return _run_prechecked_macro_call(f, plan, glbs, locs)
    empty = inspect.Parameter.empty
    i = 0
    args = []
    for (key, param), raw_arg in zip(sig.parameters.items(), raw_args, strict=False):
        i += 1
        if raw_arg == "*":
            break
        kind = param.annotation
        if kind is empty or kind is None:
            kind = str
        arg = convert_macro_arg(
            raw_arg, kind, glbs, locs, name=key, macroname=macroname
        )
        args.append(arg)
    reg_args, kwargs = _eval_regular_args(raw_args[i:], glbs, locs)
    args += reg_args
    with in_macro_call(f, glbs, locs):
        rtn = f(*args, **kwargs)
    return rtn


@lazyobject
def KWARG_RE():
    return re.compile(r"([A-Za-z_]\w*=|\*\*)")


def _starts_as_arg(s):
    """Tests if a string starts as a non-kwarg string would."""
    return KWARG_RE.match(s) is None


def _eval_regular_args(raw_args, glbs, locs):
    if not raw_args:
        return [], {}
    arglist = list(itertools.takewhile(_starts_as_arg, raw_args))
    kwarglist = raw_args[len(arglist) :]
    execer = XSH.execer
    if not arglist:
        args = arglist
        kwargstr = "dict({})".format(", ".join(kwarglist))
        kwargs = execer.eval(kwargstr, glbs=glbs, locs=locs)
    elif not kwarglist:
        argstr = "({},)".format(", ".join(arglist))
        args = execer.eval(argstr, glbs=glbs, locs=locs)
        kwargs = {}
    else:
        argstr = "({},)".format(", ".join(arglist))
        kwargstr = "dict({})".format(", ".join(kwarglist))
        both = f"({argstr}, {kwargstr})"
        args, kwargs = execer.eval(both, glbs=glbs, locs=locs)
    return args, kwargs


class _FrozenKeysMapping(cabc.Mapping):
    """A read-only mapping view that exposes a fixed key set while still
    reading values from the underlying, possibly mutated, mapping.

    This is how prechecked macro calls freeze the name set used for
    Python/subprocess interpretation without freezing the values themselves.
    """

    def __init__(self, data, keys):
        self._data = data
        self._keys = frozenset(keys)

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._keys)

    def __len__(self):
        return len(self._keys)

    def keys(self):
        return set(self._keys)


def _freeze_macro_namespaces(glbs, locs):
    """Builds frozen-key views of the call-site namespaces for preflight."""
    glb_data = glbs if glbs is not None else {}
    loc_data = locs if locs is not None else {}
    fglbs = _FrozenKeysMapping(glb_data, set(glb_data.keys()))
    flocs = _FrozenKeysMapping(loc_data, set(loc_data.keys()))
    return fglbs, flocs


def _split_raw_macro_args(raw_args):
    """Splits raw arguments at the lone ``*`` separator into
    ``(macro_args, regular_args)``, mirroring the non-prechecked caller."""
    for i, raw_arg in enumerate(raw_args):
        if raw_arg == "*":
            return list(raw_args[:i]), list(raw_args[i + 1 :])
    return list(raw_args), []


def _precheck_macro_call(sig, raw_args, glbs, locs, macroname):
    """Validates a whole macro call without executing any of its arguments.

    Raises before executing anything if the function is not eligible
    (var-positional/var-keyword parameters), the call contains dynamic
    ``*args``/``**kwargs`` unpacking, the argument shape does not match the
    signature, an annotation cannot be resolved, or any argument source
    fails parsing/compilation under the annotation/mode that would be used.

    Returns a plan consumable by :func:`_run_prechecked_macro_call`.
    """
    params = list(sig.parameters.values())
    # only ordinary functions with stable, fully-bound signatures are covered
    for param in params:
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            raise TypeError(
                f"macro precheck is not available for {macroname}(): it has a "
                f"var-positional parameter *{param.name}."
            )
        elif param.kind == inspect.Parameter.VAR_KEYWORD:
            raise TypeError(
                f"macro precheck is not available for {macroname}(): it has a "
                f"var-keyword parameter **{param.name}."
            )
    macro_raw, regular_raw = _split_raw_macro_args(raw_args)
    # split the regular part into positional expressions and ``name=expr``
    # pairs, exactly like the non-prechecked caller does
    arglist = list(itertools.takewhile(_starts_as_arg, regular_raw))
    kwarglist = regular_raw[len(arglist) :]
    for raw_arg in arglist:
        if raw_arg.startswith("*"):
            raise TypeError(
                f"macro precheck cannot handle dynamic argument unpacking in "
                f"{macroname}(): {raw_arg!r}; pass the expanded arguments "
                f"explicitly instead."
            )
    reg_kwargs = []
    for raw_arg in kwarglist:
        if raw_arg.startswith("**"):
            raise TypeError(
                f"macro precheck cannot handle dynamic keyword unpacking in "
                f"{macroname}(): {raw_arg!r}; pass the keyword arguments "
                f"explicitly instead."
            )
        match = KWARG_RE.match(raw_arg)
        # guaranteed by the _starts_as_arg split: either "name=" or "**"
        name = match.group(1)[:-1]
        reg_kwargs.append(name)
    # too many macro arguments for the signature
    if len(macro_raw) > len(params):
        raise TypeError(
            f"{macroname}() takes {len(params)} macro argument(s) but "
            f"{len(macro_raw)} were given."
        )
    # check the overall shape with a placeholder binding: this raises the
    # canonical errors for missing/duplicate/unexpected arguments, defaults
    # are accepted automatically
    n_positional = sum(
        1
        for i in range(len(macro_raw))
        if params[i].kind != inspect.Parameter.KEYWORD_ONLY
    ) + len(arglist)
    kw_names = {}
    for i in range(len(macro_raw)):
        if params[i].kind == inspect.Parameter.KEYWORD_ONLY:
            kw_names[params[i].name] = None
    for name in reg_kwargs:
        # a dict would silently collapse a repeated keyword, and the
        # placeholder bind below could no longer see the conflict
        if name in kw_names:
            raise TypeError(
                f"{macroname}() got multiple values for argument {name!r}."
            )
        kw_names[name] = None
    missing = object()
    sig.bind(
        *([missing] * n_positional), **{name: missing for name in kw_names}
    )
    # freeze the names used while interpreting sources
    fglbs, flocs = _freeze_macro_namespaces(glbs, locs)
    # prepare macro arguments: parse/compile per annotation, never execute
    empty = inspect.Parameter.empty
    prepared_macro = []
    for i, raw_arg in enumerate(macro_raw):
        param = params[i]
        kind = param.annotation
        if kind is empty or kind is None:
            kind = str
        prepared = _prepare_macro_arg(
            raw_arg,
            kind,
            fglbs,
            flocs,
            name=param.name,
            macroname=macroname,
        )
        if param.kind == inspect.Parameter.KEYWORD_ONLY:
            slot = ("kw", param.name)
        else:
            slot = ("pos", None)
        prepared_macro.append((slot, prepared))
    # regular argument expressions only need to compile in eval mode;
    # build exactly the same sources the non-prechecked caller evaluates
    execer = XSH.execer
    pos_code = None
    if arglist:
        pos_src = "({},)".format(", ".join(arglist))
        pos_code = execer.compile(
            pos_src.rstrip("\n"), mode="eval", glbs=fglbs, locs=flocs
        )
    kw_code = None
    if kwarglist:
        kw_src = "dict({})".format(", ".join(kwarglist))
        kw_code = execer.compile(
            kw_src.rstrip("\n"), mode="eval", glbs=fglbs, locs=flocs
        )
    return {
        "macro": prepared_macro,
        "pos_code": pos_code,
        "kw_code": kw_code,
    }


def _run_prechecked_macro_call(f, plan, glbs, locs):
    """Executes a preflighted macro call plan: each conversion and regular
    argument expression runs exactly once, in the original order, and only
    then is the macro body called. Values are read from the live call-site
    namespaces, even though their name set was frozen during preflight."""
    args = []
    kwargs = {}
    for slot, prepared in plan["macro"]:
        value = _run_prepared_macro_arg(prepared, glbs, locs)
        if slot[0] == "kw":
            kwargs[slot[1]] = value
        else:
            args.append(value)
    if plan["pos_code"] is not None:
        args.extend(XSH.execer.eval(plan["pos_code"], glbs=glbs, locs=locs))
    if plan["kw_code"] is not None:
        kwargs.update(XSH.execer.eval(plan["kw_code"], glbs=glbs, locs=locs))
    with in_macro_call(f, glbs, locs):
        rtn = f(*args, **kwargs)
    return rtn


def enter_macro(obj, raw_block, glbs, locs):
    """Prepares to enter a context manager macro by attaching the contents
    of the macro block, globals, and locals to the object. These modifications
    are made in-place and the original object is returned.

    Parameters
    ----------
    obj : context manager
        The object that is about to be entered via a with-statement.
    raw_block : str
        The str of the block that is the context body.
        This string will be parsed, compiled, evaled, or left as
        a string depending on the return annotation of obj.__enter__.
    glbs : Mapping
        The globals from the context site.
    locs : Mapping or None
        The locals from the context site.

    Returns
    -------
    obj : context manager
        The same context manager but with the new macro information applied.
    """
    # recurse down sequences
    if isinstance(obj, cabc.Sequence):
        for x in obj:
            enter_macro(x, raw_block, glbs, locs)
        return obj
    # convert block as needed
    kind = getattr(obj, "__xonsh_block__", str)
    macroname = getattr(obj, "__name__", "<context>")
    block = convert_macro_arg(
        raw_block, kind, glbs, locs, name="<with!>", macroname=macroname
    )
    # attach attrs
    obj.macro_globals = glbs
    obj.macro_locals = locs
    obj.macro_block = block
    return obj


@contextlib.contextmanager
def xonsh_builtins(execer=None):
    """A context manager for using the xonsh builtins only in a limited
    scope. Likely useful in testing.
    """
    XSH.load(execer=execer)
    yield
    XSH.unload()


class InlineImporter:
    """Inline importer allows to import and use module attribute or function in one line."""

    def __getattr__(self, name):
        if name.startswith("__"):
            return getattr(super(), name)
        return __import__(name)


class Cmd:
    """A command group."""

    def __init__(
        self,
        xsh: XonshSession,
        *args: str,
        bg=False,
        redirects: dict[str, str] | None = None,
    ):
        self.xsh = xsh
        self.args: list[list[str | tuple[str, str]] | str] = []
        self._add_proc(*args, redirects=redirects or {})
        if bg:
            self.args.append("&")

    def _expand(self, *args: str | list[str]) -> Iterator[str]:
        for arg in args:
            if isinstance(arg, str):
                yield expand_path(arg)
            else:
                yield from (expand_path(str(a)) for a in arg)

    def _add_proc(self, *args: str, redirects: dict[str, str] | None = None) -> None:
        """a single Popen process args"""
        cmds: list[str | tuple[str, str]] = list(self._expand(*args))
        if redirects:
            for k, v in redirects.items():
                cmds.append((k, expand_path(v)))
        self.args.append(cmds)

    def out(self):
        """dispatch $()"""
        return self.xsh.subproc_captured_stdout(*self.args)

    def run(self):
        """dispatch $[]"""
        return self.xsh.subproc_uncaptured(*self.args)

    def hide(self):
        """dispatch ![]"""
        return self.xsh.subproc_captured_hiddenobject(*self.args)

    def obj(self):
        """dispatch !()"""
        return self.xsh.subproc_captured_object(*self.args)

    def pipe(self, *args):
        """combine $() | $[]"""
        self.args.append("|")
        self._add_proc(*args)
        return self


class XonshSessionInterface:
    """Xonsh Session Interface

    Attributes
    ----------
    env : xonsh.environ.Env
        A xonsh environment e.g. `@.env.get('HOME', '/tmp')`.

    history : xonsh.history.History
        Xonsh history backend e.g. `@.history[-1].cmd`.
        See also `history --help` to manage history from
        command line.

    imp : xonsh.built_ins.InlineImporter
        The inline importer provides instant access to library
        functions and attributes e.g. `@.imp.time.time()`.

    lastcmd : xonsh.procs.pipelines.CommandPipeline
        Last executed subprocess-mode command pipeline
        e.g. `@.lastcmd.rtn` returns exit code.

    debug : xonsh.debug.XonshDebug
        Debug helpers for the running session e.g.
        ``@.debug.breakpoint()`` to drop into a debugger.
    """

    debug = None  # type: ignore
    env = None  # type: ignore
    history = None  # type: ignore
    imp: InlineImporter = InlineImporter()
    lastcmd = None  # type: ignore


class XonshSession:
    """All components defining a xonsh session.

    Warning! If you use this object for any reason and access ``__xonsh__``
    or ``xonsh.built_ins.XSH`` attributes or functions, you do so at your
    own risk, as the internal contents and behavior of this object may
    change with any release. For repeatable use cases, find a way
    to improve ``XonshSessionInterface`` or ``xonsh.api``.
    """

    def __init__(self):
        """
        Attributes
        ----------
        exit: int or None
            Session attribute. In case of integer value it signals xonsh to exit
            with returning this value as exit code.
        """
        self.interface = XonshSessionInterface()
        self.execer = None
        self.ctx = {}
        self.builtins_loaded = False
        self.history = None
        self.shell = None
        self.env = None
        self.imp = InlineImporter()
        self.debug = XonshDebug()
        self.interface.debug = self.debug
        self.rc_files = None

        # AST-invoked functions
        self.help = helper
        self.superhelp = superhelper
        self.pathsearch = pathsearch
        self.globsearch = globsearch
        self.regexsearch = regexsearch
        self.regexmatchsearch = regexmatchsearch
        self.glob = lambda *a, **kw: XonshList(globpath(*a, **kw))
        self.expand_path = expand_path

        self.subproc_captured_stdout = subproc_captured_stdout
        self.subproc_captured_inject = subproc_captured_inject
        self.subproc_captured_object = subproc_captured_object
        self.subproc_captured_hiddenobject = subproc_captured_hiddenobject
        self.subproc_uncaptured = subproc_uncaptured
        self.subproc_check_boolop = subproc_check_boolop
        self.call_macro = call_macro
        self.enter_macro = enter_macro
        self.path_literal = path_literal

        self.builtin_cmd = builtin_cmd
        self.list_of_strs_or_callables = list_of_strs_or_callables
        self.list_of_list_of_strs_outer_product = list_of_list_of_strs_outer_product
        self.eval_fstring_field = eval_fstring_field

        # Session attributes
        self.exit = None
        self.stdout_uncaptured = None
        self.stderr_uncaptured = None
        self._py_exit = None
        self._py_quit = None
        self.commands_cache = None
        self.modules_cache = None
        self.all_jobs = None
        self._completers = None
        self.builtins = None
        self._initial_builtin_names = None
        self.lastcmd = None
        self._last = None
        self.sessionid = str(uuid.uuid4())

    @property
    def last(self):
        warnings.warn(
            "The `last` attribute is deprecated and will be removed. Use `lastcmd`.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._last

    @last.setter
    def last(self, value):
        self._last = value

    def cmd(self, *args: str, **kwargs):
        return Cmd(self, *args, **kwargs)

    @property
    def aliases(self):
        if self.commands_cache is None:
            return
        return self.commands_cache.aliases

    @property
    def completers(self):
        """Returns a list of all available completers. Init when first accessing the attribute"""
        if self._completers is None:
            from xonsh.completers.init import default_completers

            self._completers = default_completers(self.commands_cache)
        return self._completers

    def _disable_python_exit(self):
        # Disable Python interactive quit/exit
        if hasattr(builtins, "exit"):
            self._py_exit = builtins.exit
            del builtins.exit

        if hasattr(builtins, "quit"):
            self._py_quit = builtins.quit
            del builtins.quit

    def _restore_python_exit(self):
        if self._py_exit is not None:
            builtins.exit = self._py_exit
        if self._py_quit is not None:
            builtins.quit = self._py_quit

    def load(
        self,
        execer=None,
        ctx=None,
        inherit_env=True,
        save_origin_env=False,
        **kwargs,
    ):
        """Loads the session with default values.

        Parameters
        ----------
        execer : Execer, optional
            Xonsh execution object, may be None to start
        ctx : Mapping, optional
            Context to start xonsh session with.
        inherit_env : bool
            If ``True``: inherit environment variables from ``os.environ``.
            If ``False``: use default values for environment variables and
            set ``$XONSH_ENV_INHERITED = False``.
        """
        from xonsh.commands_cache import CommandsCache
        from xonsh.environ import Env, default_env, save_origin_env_to_file

        if not hasattr(builtins, "__xonsh__"):
            builtins.__xonsh__ = self
        if ctx is not None:
            self.ctx = ctx

        if "env" in kwargs:
            self.env = kwargs.pop("env")
        elif inherit_env:
            self.env = Env(default_env())
        else:
            no_env = {"XONSH_ENV_INHERITED": False}
            # TERM is essential for proper terminal operation
            no_env["TERM"] = os.environ.get("TERM", "xterm")
            self.env = Env(no_env)
        self.interface.env = self.env

        if save_origin_env:
            save_origin_env_to_file(self.env, self.sessionid)

        self.exit = None
        self.stdout_uncaptured = None
        self.stderr_uncaptured = None

        self._disable_python_exit()

        self.execer = execer
        self.modules_cache = {}
        self.all_jobs = {}

        self.builtins = get_default_builtins(execer)
        self._initial_builtin_names = frozenset(vars(self.builtins))

        aliases_given = kwargs.pop("aliases", None)
        for attr, value in kwargs.items():
            if hasattr(self, attr):
                setattr(self, attr, value)
        self.commands_cache = (
            kwargs.pop("commands_cache")
            if "commands_cache" in kwargs
            else CommandsCache(self.env, aliases_given)
        )
        self.link_builtins()
        self.builtins_loaded = True

        def flush_on_exit(s=None, f=None):
            if self.history is not None:
                self.history.flush(at_exit=True)

        self._flush_on_exit = flush_on_exit
        atexit.register(flush_on_exit)

        # Add one-shot handler for exit
        for sig in AT_EXIT_SIGNALS:
            resetting_signal_handle(sig, flush_on_exit)

    def link_builtins(self):
        # public built-ins
        for refname in self._initial_builtin_names:
            objname = f"__xonsh__.builtins.{refname}"
            proxy = DynamicAccessProxy(refname, objname)
            setattr(builtins, refname, proxy)

        # sneak the path search functions into the aliases
        # Need this inline/lazy import here since we use locate_binary that
        # relies on __xonsh__.env in default aliases
        builtins.default_aliases = builtins.aliases = self.aliases

    def unlink_builtins(self):
        for name in self._initial_builtin_names:
            if hasattr(builtins, name):
                delattr(builtins, name)

    def unload(self):
        if not hasattr(builtins, "__xonsh__"):
            self.builtins_loaded = False
            return

        if hasattr(self.env, "undo_replace_env"):
            self.env.undo_replace_env()

        self._restore_python_exit()

        if not self.builtins_loaded:
            return

        if self.history is not None:
            self.history.flush(at_exit=True)

        if hasattr(self, "_flush_on_exit"):
            atexit.unregister(self._flush_on_exit)

        self.unlink_builtins()
        delattr(builtins, "__xonsh__")
        self.builtins_loaded = False
        self._completers = None


def get_default_builtins(execer=None):
    from xonsh.events import events

    return types.SimpleNamespace(
        XonshError=XonshError,
        XonshCalledProcessError=XonshCalledProcessError,
        evalx=None if execer is None else execer.eval,
        execx=None if execer is None else execer.exec,
        compilex=None if execer is None else execer.compile,
        events=events,
        print_color=print_color,
        printx=print_color,
    )


class DynamicAccessProxy:
    """Proxies access dynamically."""

    def __init__(self, refname, objname):
        """
        Parameters
        ----------
        refname : str
            '.'-separated string that represents the new, reference name that
            the user will access.
        objname : str
            '.'-separated string that represents the name where the target
            object actually lives that refname points to.
        """
        super().__setattr__("refname", refname)
        super().__setattr__("objname", objname)

    def _obj(self):
        """Dynamically grabs object.

        This is a method instead of a ``@property`` as a workaround for
        a Nuitka bugs.
        - https://github.com/Nuitka/Nuitka/issues/3859
        - https://github.com/Nuitka/Nuitka/issues/3860
        When the bug is fixed upstream, this can be reverted to
        ``@property def obj(self)``.

        Uses ``object.__getattribute__`` to read *objname* so that the
        lookup never falls through to ``__getattr__`` — which would
        create an infinite loop under Nuitka-compiled builds.

        Uses :func:`operator.attrgetter` instead of an explicit ``getattr``
        loop: the loop form compiled under Nuitka 4.0 produces wrong
        results for ``getattr(builtins, "__xonsh__")`` (it doesn't advance
        ``obj`` on the first iteration), yielding ``AttributeError:
        module 'builtins' has no attribute 'builtins'``. ``attrgetter``
        delegates the whole walk to a C builtin and sidesteps the bug.
        """
        return _attrgetter(object.__getattribute__(self, "objname"))(builtins)

    def __getattr__(self, name):
        return getattr(self._obj(), name)

    def __setattr__(self, name, value):
        return setattr(self._obj(), name, value)

    def __delattr__(self, name):
        return delattr(self._obj(), name)

    def __getitem__(self, item):
        return self._obj().__getitem__(item)

    def __setitem__(self, item, value):
        return self._obj().__setitem__(item, value)

    def __delitem__(self, item):
        del self._obj()[item]

    def __call__(self, *args, **kwargs):
        return self._obj().__call__(*args, **kwargs)

    def __dir__(self):
        return self._obj().__dir__()

    def __repr__(self):
        return repr(self._obj())

    def __str__(self):
        return str(self._obj())


# singleton
XSH = XonshSession()
