"""Raw-argument completion for plain Python function macros (``f!(...)``).

Function macros pause the normal parsing of their arguments: the source text
between the top-level commas is captured *raw* and handed to the macro
function (see :doc:`/macros`).  A macro that embeds a small DSL, e.g.
``query!(SELECT ..., ...)``, therefore cannot get useful candidates from the
ordinary Python/path completers -- its arguments are not Python expressions.

This module lets a macro author register a completer *on the function
itself*.  Registration is documented at :func:`macro_completer`.  The
dispatcher (:func:`complete_macro_raw`)

* locates the macro call with a purely lexical scan -- macros, argument
  expressions, annotations and call expressions are never executed or
  evaluated;
* only honours a *plain Python function* that is reachable by its **direct
  name** in the current execution namespace (``PythonContext.ctx``);
* passes the complete input, the verbatim text of the current raw argument,
  its index among the macro's top-level arguments, its input range and the
  cursor position to the registered completer;
* uses exactly the candidate text and the left-of-cursor replacement length
  returned by that completer -- candidates are inserted verbatim, with no
  automatic quoting, escaping or appended spaces, and an empty result
  suppresses the usual Python/path fallback;
* treats a lone ``*`` top-level argument as the boundary after which normal
  Python completion applies again (nested macro calls inside that region are
  still dispatched on their own; closing them restores the outer context).
"""

import inspect
import re
from typing import Any, Callable, NamedTuple

from xonsh.completers.tools import (
    RichCompletion,
    contextual_completer,
    exclusive_empty_completer,
)
from xonsh.parsers.completion_context import CompletionContext
from xonsh.parsers.lexer import Lexer

#: Attribute used for the documented, function-level registration.
#:
#: Either assign a completer to it directly::
#:
#:      query.__xonsh_macro_completer__ = complete_query
#:
#: or use the :func:`macro_completer` decorator.
MACRO_COMPLETER_ATTR = "__xonsh_macro_completer__"


class MacroRawCompletionContext(NamedTuple):
    """Information passed to a registered macro raw-argument completer.

    The completer is a single callable taking exactly this object and
    returning either ``None``/an empty iterable (no candidates, fallback
    completers stay suppressed) or an iterable of candidate strings,
    optionally wrapped in a ``(candidates, replace_len)`` tuple where
    ``replace_len`` is the number of characters to the left of the cursor
    that the candidate replaces.  Without the tuple the whole raw-argument
    prefix before the cursor is replaced.
    """

    macro_name: str
    """The direct name the macro was called with (e.g. ``"query"``)."""
    code: str
    """The complete input of the current Python context (not just the raw
    argument)."""
    raw_arg: str
    """The verbatim source text of the current top-level macro argument,
    including surrounding whitespace and any brackets/strings/newlines."""
    arg_index: int
    """Zero-based index of the current argument among the macro call's
    top-level comma-separated arguments."""
    arg_start: int
    """Absolute index in ``code`` where ``raw_arg`` starts (right after the
    opening parenthesis or the preceding top-level comma)."""
    arg_end: int
    """Absolute index in ``code`` where ``raw_arg`` ends (right before the
    next top-level comma/closing parenthesis, or ``len(code)`` for an
    unclosed macro call)."""
    cursor_index: int
    """Absolute cursor position in ``code``."""

    @property
    def cursor_in_arg(self) -> int:
        """Cursor position relative to :attr:`arg_start`."""
        return self.cursor_index - self.arg_start

    @property
    def prefix(self) -> str:
        """The raw-argument text to the left of the cursor."""
        return self.raw_arg[: self.cursor_in_arg]

    @property
    def suffix(self) -> str:
        """The raw-argument text to the right of the cursor."""
        return self.raw_arg[self.cursor_in_arg :]

    @property
    def arg_span(self) -> slice:
        """The :attr:`raw_arg` range in :attr:`code`."""
        return slice(self.arg_start, self.arg_end)


#: A user-provided raw-argument completer.
MacroRawCompleter = Callable[[MacroRawCompletionContext], Any]


def macro_completer(completer: MacroRawCompleter) -> Callable:
    """Decorator registering a raw-argument completer on a function macro.

    Parameters
    ----------
    completer
        A callable receiving a :class:`MacroRawCompletionContext` and
        returning candidates (see that class for the return protocol).

    Examples
    --------
    .. code-block:: python

        from xonsh.completers.macro import macro_completer

        def complete_sql(info):
            prefix = info.prefix
            matches = {
                k for k in ("SELECT", "FROM", "WHERE") if k.startswith(prefix)
            }
            return matches, len(prefix)

        @macro_completer(complete_sql)
        def query(sql: str):
            ...

    Only plain Python functions that can be looked up by their direct name
    in the current execution namespace are dispatched.  The decorator (or a
    direct assignment to ``__xonsh_macro_completer__``) does not change how
    the function behaves when called or executed as a macro.
    """

    def decorate(func: Callable) -> Callable:
        setattr(func, MACRO_COMPLETER_ATTR, completer)
        return func

    return decorate


def get_macro_completer(func: Any) -> "MacroRawCompleter | None":
    """Return the raw-argument completer registered on ``func``, if any."""
    completer = getattr(func, MACRO_COMPLETER_ATTR, None)
    return completer if callable(completer) else None


# ----------------------------------------------------------------------------
# Lexical location of the raw argument around the cursor
# ----------------------------------------------------------------------------

# Opening tokens that group tokens (and therefore top-level commas) together.
# The set mirrors the ``any_nested_raw`` rule of the macro grammar, plus the
# plain Python grouping delimiters.
_OPEN_TO_CLOSE = {
    "LPAREN": ")",
    "LBRACKET": "]",
    "LBRACE": "}",
    "BANG_LPAREN": ")",
    "BANG_LBRACKET": "]",
    "AT_LPAREN": ")",
    "ATBANG_LPAREN": ")",
    "ATDOLLAR_LPAREN": ")",
    "DOLLAR_LPAREN": ")",
    "DOLLAR_LBRACKET": "]",
    "DOLLAR_LBRACE": "}",
}
_CLOSE_TOKENS = {"RPAREN": ")", "RBRACKET": "]", "RBRACE": "}"}
# Tokens without any structural meaning for macro argument splitting.
_IGNORED_TOKENS = frozenset(
    {"WS", "NEWLINE", "NL", "COMMENT", "INDENT", "DEDENT", "ENCODING", "ENDMARKER"}
)


class _Frame:
    __slots__ = ("kind", "closer", "name", "arg_start", "arg_index", "star")

    def __init__(self, kind, closer, name, arg_start):
        self.kind = kind  # "macro" (a direct-name ``f!(``) or "bracket"
        self.closer = closer
        self.name = name  # direct macro name, or None for plain brackets
        # Fields meaningful for macro frames:
        self.arg_start = arg_start  # start of the current top-level segment
        self.arg_index = 0  # index of the current top-level segment
        self.star = False  # a completed segment was a lone "*"


class _Region(NamedTuple):
    name: str
    arg_index: int
    arg_start: int
    arg_end: int


def _lex_tokens(code: str):
    """Yield ``(type, start, end)`` for ``code`` with absolute positions.

    The tolerant, Python-mode lexer is the same machinery the macro grammar
    is built on, so bracket/string/f-string/xonsh-subexpression handling
    matches real macro argument splitting, including for partial input.
    """
    lexer = Lexer(tolerant=True, pymode=True)
    lexer.input(code)
    line_starts = [0]
    # ``lineno`` is 1-indexed and points at the line where the token starts.
    # Multi-line tokens (e.g. triple-quoted strings) keep their original
    # text in ``token.value``, so ``end`` is still exact.
    for match in re.finditer("\n", code):
        line_starts.append(match.start() + 1)
    line_starts = tuple(line_starts)
    while True:
        tok = lexer.token()
        if tok is None:
            return
        start = line_starts[tok.lineno - 1] + tok.lexpos
        yield tok.type, start, start + len(tok.value)


def locate_raw_region(code: str, cursor: int) -> "_Region | None":
    """Locate the innermost active macro raw-argument region at ``cursor``.

    Returns ``None`` when the cursor is not inside a direct-name function
    macro call whose current top-level argument is captured raw (i.e. after
    the macro's lone ``*`` separator ordinary Python completion applies).

    Nothing in this function looks at the execution namespace; namespace and
    registration checks happen in :func:`complete_macro_raw`.
    """
    if "!(" not in code or not (0 <= cursor <= len(code)):
        return None

    frames: list[_Frame] = []
    fstring_depth = 0
    # The two previous non-ignored tokens, for recognizing ``name!(``.
    prev_sig = None
    prev2_sig = None

    def on_comma(pos):
        # A comma splits the topmost macro only when no frame (bracket,
        # nested macro, ...) is open inside it.
        if frames and frames[-1].kind == "macro":
            frame = frames[-1]
            if code[frame.arg_start : pos].strip() == "*":
                # A completed lone "*" switches the rest to regular args.
                frame.star = True
            frame.arg_start = pos + 1
            frame.arg_index += 1

    tokens = _lex_tokens(code)
    # Phase 1: walk everything up to the cursor, recording open structure.
    # A token starting exactly at the cursor hasn't been typed yet.
    for typ, start, end in tokens:
        if start >= cursor:
            phase1_last = (typ, start, end)
            break
        if typ in _IGNORED_TOKENS:
            continue
        if typ == "FSTRING_START":
            fstring_depth += 1
        elif typ == "FSTRING_END":
            if fstring_depth:
                fstring_depth -= 1
        elif fstring_depth == 0:
            if typ == "COMMA":
                on_comma(start)
            elif typ in _CLOSE_TOKENS:
                if frames and frames[-1].closer == _CLOSE_TOKENS[typ]:
                    frames.pop()
            elif typ in _OPEN_TO_CLOSE:
                name = None
                # A function macro is a direct ``name!(`` trailer: the
                # previous token is an adjacent NAME that is not an
                # attribute after a period (``obj.f!(``).
                if (
                    typ == "BANG_LPAREN"
                    and prev_sig is not None
                    and prev_sig[0] == "NAME"
                    and prev_sig[2] == start
                    and (prev2_sig is None or prev2_sig[0] != "PERIOD")
                ):
                    name = code[prev_sig[1] : prev_sig[2]]
                frames.append(
                    _Frame(
                        "macro" if name is not None else "bracket",
                        _OPEN_TO_CLOSE[typ],
                        name,
                        end,
                    )
                )
        prev2_sig = prev_sig
        prev_sig = (typ, start, end)
    else:
        phase1_last = None

    # Resolve the innermost macro whose raw zone contains the cursor.
    target = None
    for frame in reversed(frames):
        if frame.kind == "macro":
            if frame.star or code[frame.arg_start : cursor].strip() == "*":
                # Regular-argument region of this macro; nested macros that
                # sit above it on the stack were already considered.
                return None
            target = frame
            break
        # brackets (and the f-string counter) are transparent: their
        # contents are still part of the enclosing raw argument.
    if target is None:
        return None

    # Phase 2: continue scanning past the cursor to find the end of the
    # current top-level argument -- the next top-level comma, the macro's
    # closing parenthesis, or the end of the (possibly unclosed) input.
    rest = [phase1_last] if phase1_last is not None else []
    rest += list(tokens)
    arg_end = len(code)
    for typ, start, end in rest:
        if typ in _IGNORED_TOKENS:
            continue
        if typ == "FSTRING_START":
            fstring_depth += 1
            continue
        elif typ == "FSTRING_END":
            if fstring_depth:
                fstring_depth -= 1
            continue
        if fstring_depth == 0:
            if typ == "COMMA":
                if frames and frames[-1] is target:
                    arg_end = start
                    break
            elif typ in _CLOSE_TOKENS:
                if frames and frames[-1] is target:
                    arg_end = start
                    break
                if frames and frames[-1].closer == _CLOSE_TOKENS[typ]:
                    frames.pop()
            elif typ in _OPEN_TO_CLOSE:
                frames.append(_Frame("bracket", _OPEN_TO_CLOSE[typ], None, end))

    return _Region(
        name=target.name,
        arg_index=target.arg_index,
        arg_start=target.arg_start,
        arg_end=arg_end,
    )


# ----------------------------------------------------------------------------
# Completer
# ----------------------------------------------------------------------------


@contextual_completer
@exclusive_empty_completer
def complete_macro_raw(context: CompletionContext):
    """Complete a raw argument of a registered plain Python function macro.

    Runs as an exclusive contextual completer: whenever the cursor sits in
    the raw region of a macro whose direct name resolves, in the current
    execution namespace, to a plain Python function carrying a registered
    raw-argument completer, its candidates (and only those) are returned.
    In every other situation this completer abstains (``None``) and the
    ordinary completers run unchanged.
    """
    python = context.python
    if python is None or python.ctx is None:
        return None

    code = python.multiline_code
    cursor = python.cursor_index
    region = locate_raw_region(code, cursor)
    if region is None:
        return None

    # Direct name lookup only -- never evaluate the (possibly DSL) call
    # expression, and only dispatch for plain Python functions.
    try:
        func = python.ctx.get(region.name)
    except AttributeError:
        return None
    if not inspect.isfunction(func):
        return None
    user_completer = get_macro_completer(func)
    if user_completer is None:
        return None

    info = MacroRawCompletionContext(
        macro_name=region.name,
        code=code,
        raw_arg=code[region.arg_start : region.arg_end],
        arg_index=region.arg_index,
        arg_start=region.arg_start,
        arg_end=region.arg_end,
        cursor_index=cursor,
    )

    result = user_completer(info)
    if result is None:
        candidates, replace_len = (), 0
    elif isinstance(result, tuple) and len(result) == 2:
        candidates, replace_len = result
        replace_len = int(replace_len)
    else:
        candidates, replace_len = result, info.cursor_in_arg

    if not 0 <= replace_len <= info.cursor_in_arg:
        raise ValueError(
            f"macro completer for {region.name!r} returned replace_len="
            f"{replace_len}, which is outside the raw-argument prefix of "
            f"length {info.cursor_in_arg}"
        )

    # Only the candidate text and the left-of-cursor replacement length are
    # honoured.  Force plain RichCompletions so the pipeline never appends
    # quotes/spaces and the other arguments and the text after the cursor
    # are left untouched.
    wrapped = {
        RichCompletion(
            str(candidate),
            prefix_len=replace_len,
            append_closing_quote=False,
            append_space=False,
            provider=f"macro:{region.name}",
        )
        for candidate in candidates
    }
    return wrapped, replace_len
