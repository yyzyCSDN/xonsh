"""Tests for raw-argument completion of plain Python function macros.

Covers the lexical region locator, the function-level registration, the
exclusive completer pipeline semantics (verbatim insertion, suppression of
fallback candidates, lone ``*`` handling and nested macro dispatch) and the
guarantee that macros/arguments are never executed during completion.
"""

import pytest

from xonsh.completers.macro import (
    MACRO_COMPLETER_ATTR,
    MacroRawCompletionContext,
    complete_macro_raw,
    locate_raw_region,
    macro_completer,
)
from xonsh.completers.tools import RichCompletion
from xonsh.parsers.completion_context import CompletionContext, PythonContext

X = "\x00"  # cursor marker


@pytest.fixture(autouse=True)
def xonsh_execer_autouse(xession, xonsh_execer):
    return xonsh_execer


# ---------------------------------------------------------------------------
# Macro functions used from the (faked) current execution namespace.
# ---------------------------------------------------------------------------

SQL_KEYWORDS = ("SELECT", "FROM", "WHERE")

received: list = []


def sql_completer(info: MacroRawCompletionContext):
    """Raw-argument completer recording every invocation."""
    received.append(info)
    prefix = info.prefix
    matches = [k for k in SQL_KEYWORDS if k.startswith(prefix)]
    if not matches:
        # an explicit empty result still owns the region
        return None
    return matches, len(prefix)


@macro_completer(sql_completer)
def sql_query(sql: str):
    raise AssertionError("the macro function must never be executed")


def list_completer2(info: MacroRawCompletionContext):
    # bare iterable: default replace length is the whole raw prefix
    return ["item one", "item(two)"]


@macro_completer(list_completer2)
def list_macro(x: str):
    raise AssertionError("the macro function must never be executed")


@macro_completer(lambda info: {"NESTED"})
def nested_macro(x: str):
    raise AssertionError("the macro function must never be executed")


# A macro without a registered completer: completion must stay untouched.
def unregistered_macro(x: str):
    raise AssertionError("the macro function must never be executed")


class NotAPlainFunction:
    def __call__(self, x: str = ""):
        return x


@pytest.fixture(autouse=True)
def _clear_received():
    received.clear()
    yield
    received.clear()


def pycontext(code, cursor=None, ctx=None):
    if X in code:
        cursor = code.index(X)
        code = code.replace(X, "")
    elif cursor is None:
        cursor = len(code)
    return CompletionContext(
        python=PythonContext(code, cursor, ctx=ctx if ctx is not None else CTX)
    )


CTX = {
    "sql_query": sql_query,
    "list_macro": list_macro,
    "nested_macro": nested_macro,
    "unregistered_macro": unregistered_macro,
    "SECRET_NAME": 1,  # a normal Python name that starts with "SE"
}


# ---------------------------------------------------------------------------
# Locator
# ---------------------------------------------------------------------------


def region(code_with_cursor):
    code = code_with_cursor.replace(X, "")
    return locate_raw_region(code, code_with_cursor.index(X))


def test_locate_basic_unclosed():
    r = region("sql_query!(SE" + X)
    assert r is not None
    assert r.name == "sql_query"
    assert r.arg_index == 0
    assert (r.arg_start, r.arg_end) == (len("sql_query!("), len("sql_query!(SE"))
    assert "SE" == "sql_query!(SE"[r.arg_start : r.arg_end]


def test_locate_arg_end_at_comma_and_paren():
    # text after the cursor is included in the raw argument
    code_with_cursor = "sql_query!(SEL" + X + "ECT a, other)"
    code = code_with_cursor.replace(X, "")
    r = region(code_with_cursor)
    assert r.arg_index == 0
    assert code[r.arg_start : r.arg_end] == "SELECT a"


def test_locate_arg_indexes_split_on_top_level_commas():
    assert region("sql_query!(a, b" + X + ", c)").arg_index == 1
    assert region("sql_query!(a, b, c" + X).arg_index == 2
    # empty current segment right after a comma
    code = "sql_query!(a, )"
    r = region("sql_query!(a, " + X + ")")
    assert r.arg_index == 1
    assert code[r.arg_start : r.arg_end] == " "


def test_locate_commas_in_brackets_strings_newlines_are_part_of_raw_arg():
    r = region("sql_query!(['x, y'], SE" + X + ")")
    assert r.arg_index == 1
    r0 = region("sql_query!(['x, y'" + X + "], SE)")
    assert r0.arg_index == 0
    assert "['x, y']" in "sql_query!(['x, y'], SE)"[r0.arg_start : r0.arg_end]

    r_str = region("sql_query!('a, b, c', S" + X + ")")
    assert r_str.arg_index == 1

    r_nl = region("sql_query!(a,\n  b" + X + ")")
    assert r_nl.arg_index == 1
    assert "\n  b" == "sql_query!(a,\n  b)"[r_nl.arg_start : r_nl.arg_end]

    # f-string replacement-field commas don't split either
    r_f = region('sql_query!(f"x={1,2}", S' + X + ")")
    assert r_f.arg_index == 1


def test_locate_inner_macro_text_is_part_of_outer_raw_arg():
    # cursor after the nested call closed: belongs to the outer raw argument
    r = region("sql_query!(nested_macro!(a, b)" + X + ", z)")
    assert r.name == "sql_query"
    assert r.arg_index == 0
    assert (
        "sql_query!(nested_macro!(a, b), z)"[r.arg_start : r.arg_end]
        == "nested_macro!(a, b)"
    )


def test_locate_inside_inner_macro_dispatches_inner():
    r = region("sql_query!(a, nested_macro!(NE" + X + "), z)")
    assert r.name == "nested_macro"
    assert r.arg_index == 0
    assert "NE" == "sql_query!(a, nested_macro!(NE), z)"[r.arg_start : r.arg_end]


def test_locate_unclosed_macro_finds_fragment():
    r = region("sql_query!(SELECT a, b FR" + X)
    assert r is not None
    assert r.arg_index == 1
    assert r.arg_end == len("sql_query!(SELECT a, b FR")


def test_locate_lone_star_switches_to_python():
    assert region("sql_query!(a, *, " + X + ")") is None
    assert region("sql_query!(a, *" + X + ")") is None
    assert region("sql_query!(a, *" + X + ", b)") is None
    # star inside brackets is not a separator
    r = region("sql_query!([*]" + X + ")")
    assert r is not None and r.arg_index == 0
    # **kwargs marker is not the lone separator
    r = region("sql_query!(**x" + X + ")")
    assert r is not None and r.arg_index == 0
    # starred expression is not the lone separator
    r = region("sql_query!(*x" + X + ")")
    assert r is not None and r.arg_index == 0


def test_locate_nested_macro_after_star_then_restore():
    # inside the nested macro in the regular zone -> nested raw region
    r = region("sql_query!(a, *, nested_macro!(NE" + X + "))")
    assert r is not None and r.name == "nested_macro"
    # nested macro closed -> back to the outer macro's regular (python) zone
    assert region("sql_query!(a, *, nested_macro!(NE)" + X + ")") is None


def test_locate_only_direct_plain_names():
    assert region("obj.sql_query!(SE" + X) is None
    assert region("sql_query(SE" + X) is None  # ordinary call, no bang
    assert region("sql_query ! (SE" + X) is None
    assert region(X + "sql_query!(SE)") is None
    assert region("sql_query!(SE)" + X) is None


def test_locate_without_marker_is_cheap_none():
    assert locate_raw_region("no macros here", 5) is None
    assert locate_raw_region("sql_query!(a)", -1) is None


# ---------------------------------------------------------------------------
# The registered completer itself
# ---------------------------------------------------------------------------


def test_completer_basic_info_and_candidates():
    out = complete_macro_raw(pycontext("sql_query!(SE"))
    assert out is not None
    comps, replace_len = out
    assert replace_len == 2
    values = {str(c) for c in comps}
    assert values == {"SELECT"}

    info = received[-1]
    assert isinstance(info, MacroRawCompletionContext)
    assert info.macro_name == "sql_query"
    assert info.code == "sql_query!(SE"
    assert info.raw_arg == "SE"
    assert info.arg_index == 0
    assert info.arg_start == len("sql_query!(")
    assert info.arg_end == len("sql_query!(SE")
    assert info.cursor_index == len("sql_query!(SE")
    assert info.cursor_in_arg == 2
    assert info.prefix == "SE"
    assert info.suffix == ""
    assert info.code[info.arg_span] == "SE"


def test_completer_info_includes_text_right_of_cursor():
    code = "sql_query!(SEL, b)"
    cursor = len("sql_query!(SEL")
    out = complete_macro_raw(
        CompletionContext(python=PythonContext(code, cursor, ctx=CTX))
    )
    assert out is not None and str(next(iter(out[0]))) == "SELECT"
    info = received[-1]
    assert info.raw_arg == "SEL"
    assert info.cursor_in_arg == 3
    assert code[info.arg_start : info.arg_end] == "SEL"


def test_completer_candidates_are_verbatim_with_replace_prefix_only():
    out = complete_macro_raw(pycontext("list_macro!(item"))
    comps, replace_len = out
    assert replace_len == len("item")
    by_value = {str(c): c for c in comps}
    for c in comps:
        assert isinstance(c, RichCompletion)
        assert c.prefix_len == len("item")  # replace only the typed prefix
        assert c.append_closing_quote is False  # no auto quotes
        assert c.append_space is False  # no appended spaces
    assert by_value["item one"].value == "item one"
    assert by_value["item(two)"].value == "item(two)"


def test_completer_empty_result_still_owns_region():
    # no keyword starts with "ZZ": user completer returns None
    out = complete_macro_raw(pycontext("sql_query!(ZZ"))
    assert out is not None
    comps, replace_len = out
    assert set(comps) == set()
    assert replace_len == 0
    # the user completer did run, so the region was recognized
    assert len(received) == 1


def test_completer_abstains_without_registration_or_plain_function():
    # unregistered macro function
    assert complete_macro_raw(pycontext("unregistered_macro!(SE")) is None
    # name missing from the namespace entirely
    assert complete_macro_raw(pycontext("ghost_macro!(SE")) is None
    # direct name resolves to a callable instance, not a plain function
    ctx = dict(CTX)
    ctx["sql_query"] = NotAPlainFunction()
    assert complete_macro_raw(pycontext("sql_query!(SE", ctx=ctx)) is None
    # direct name resolves to a non-callable object
    ctx["sql_query"] = 42
    assert complete_macro_raw(pycontext("sql_query!(SE", ctx=ctx)) is None
    # no python context / no namespace
    assert complete_macro_raw(CompletionContext(python=None)) is None
    assert complete_macro_raw(
        CompletionContext(python=PythonContext("sql_query!(SE", 12, ctx=None))
    ) is None


def test_completer_does_not_evaluate_anything():
    # the macro function raises if called; the argument is not valid Python
    complete_macro_raw(pycontext("sql_query!(SELECT totally not python !!"))
    assert len(received) == 1
    info = received[-1]
    assert info.raw_arg == "SELECT totally not python !!"


def test_completer_rejects_replace_length_outside_prefix():
    def bad(info):
        return {"X"}, info.cursor_in_arg + 1

    func = sql_query  # re-register temporarily
    old = getattr(func, MACRO_COMPLETER_ATTR)
    try:
        setattr(func, MACRO_COMPLETER_ATTR, bad)
        with pytest.raises(ValueError):
            complete_macro_raw(pycontext("sql_query!(SE"))
    finally:
        setattr(func, MACRO_COMPLETER_ATTR, old)


def test_completer_default_replace_len_is_whole_raw_prefix():
    out = complete_macro_raw(pycontext("list_macro!(ab"))
    comps, replace_len = out
    assert replace_len == 2


# ---------------------------------------------------------------------------
# Pipeline integration (exclusivity / fallback suppression / @(...) / star)
# ---------------------------------------------------------------------------


def _complete_line(completer_obj, line, cursor, ctx):
    context = completer_obj.parse(line, cursor, ctx)
    prefix = line[:cursor].rsplit(" ", 1)[-1]
    begidx = cursor - len(prefix)
    return completer_obj.complete_from_context(
        context, (prefix, line, begidx, cursor, ctx)
    )


def test_pipeline_registered_region_has_only_registered_candidates(completer_obj):
    line = "sql_query!(SE"
    comps, lprefix = _complete_line(completer_obj, line, len(line), CTX)
    assert {str(c) for c in comps} == {"SELECT"}
    # a ctx name starting with "SE" is a Python candidate that must be absent
    assert "SECRET_NAME" not in {str(c) for c in comps}
    assert lprefix == 2


def test_pipeline_registered_empty_suppresses_python_and_path(completer_obj):
    line = "sql_query!(SECRET"  # user completer returns None; SECRET_NAME would match
    comps, _ = _complete_line(completer_obj, line, len(line), CTX)
    assert tuple(comps) == ()


def test_pipeline_unregistered_keeps_python_completion(completer_obj):
    line = "unregistered_macro!(SECRET_NA"
    comps, _ = _complete_line(completer_obj, line, len(line), CTX)
    assert "SECRET_NAME" in {str(c) for c in comps}


def test_pipeline_inside_shell_at_paren(completer_obj):
    line = "echo @(sql_query!(SE))"
    cursor = len("echo @(sql_query!(SE")
    comps, lprefix = _complete_line(completer_obj, line, cursor, CTX)
    assert {str(c) for c in comps} == {"SELECT"}
    assert lprefix == 2
    info = received[-1]
    assert info.code == "sql_query!(SE"  # the @() python sub-expression
    assert info.arg_start == len("sql_query!(")


def test_pipeline_after_star_uses_python_completion(completer_obj):
    line = "sql_query!(a, *, SECRET_NA"
    comps, _ = _complete_line(completer_obj, line, len(line), CTX)
    assert "SECRET_NAME" in {str(c) for c in comps}
    # no raw completer ran in the regular-argument region
    assert received == []


def test_pipeline_nested_macro_after_star(completer_obj):
    line = "sql_query!(a, *, nested_macro!(NES))"
    cursor = len("sql_query!(a, *, nested_macro!(NES")
    comps, lprefix = _complete_line(completer_obj, line, cursor, CTX)
    assert {str(c) for c in comps} == {"NESTED"}
    assert lprefix == 3


def test_registration_attribute_is_usable_directly():
    def macro(x: str):
        return x

    assert getattr(macro, MACRO_COMPLETER_ATTR, None) is None
    setattr(macro, MACRO_COMPLETER_ATTR, lambda info: {"Q"})
    ctx = {"macro": macro}
    out = complete_macro_raw(pycontext("macro!(", ctx=ctx))
    assert out is not None
    assert {str(c) for c in out[0]} == {"Q"}
