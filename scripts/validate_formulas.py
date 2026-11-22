#!/usr/bin/env python3
"""Formula safety net for generated dashboards: only functions the vendored client registers, and a small evaluator.

Ported from the DKSK module test `autonsi_kpi_dksk/tests/test_kpi_dashboard_data.py` (function-name guard + sheet
evaluator) WITHOUT any `odoo` import; standard library only.

1. Function names. `client_functions.json` (next to this file) is the FROZEN list of the functions the o-spreadsheet
   client of an Odoo 17 instance registers (core `o_spreadsheet.js` + the Odoo additions of `spreadsheet*` modules).
   A formula that calls anything else shows `#NAME?` in the browser (the vault note
   Skill-Odoo-Spreadsheet-Formula-Functions-Must-Exist-In-Vendored-O-Spreadsheet: `N(` did). The guard checks NAMES
   only (not arity, argument types or parsing): one real-browser look at the final formulas is still needed.
   Commands:
     python scripts/validate_formulas.py check dashboard.json
     python scripts/validate_formulas.py derive --addons-path ./addons,./odoo/addons [--addons-path ...] --out scripts/client_functions.json
     python scripts/validate_formulas.py check-drift --addons-path ./addons,./odoo/addons [--addons-path ...]
   `derive` / `check-drift` are for the plugin maintainer, on a machine that holds the Odoo sources: each
   `--addons-path` is one comma separated Odoo addons path (a module is taken from the first folder that holds it,
   like Odoo does); the frozen list is the INTERSECTION over all of them. A client upgrade that changes the file
   layout makes the derivation fail loudly instead of weakening the guard.
2. `SheetEvaluator`: evaluates the first sheet of a dashboard JSON for given list rows, so tests can compare formula
   results with an oracle. It knows only the functions the generated sheets use; any other one raises
   `UnknownFunction` (the client's #NAME?).
"""
import argparse
import datetime
import functools
import hashlib
import json
import math
import os
import re
import sys

SCHEMA = 1
FROZEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_functions.json")
O_SPREADSHEET_JS = ("static", "src", "o_spreadsheet", "o_spreadsheet.js")   # inside the `spreadsheet` module
CLIENT_MODULES = ("spreadsheet", "spreadsheet_dashboard", "spreadsheet_dashboard_edition", "spreadsheet_edition")
MAX_FILE = 64 * 1024 * 1024
EXCEL_EPOCH = datetime.date(1899, 12, 30)     # day 0 of the spreadsheet date serials
MAX_RANGE_CELLS = 100000                      # a range wider than this is a defect (or hostile): never expanded
# a literal ends at any `"` not directly preceded by a backslash and an unterminated one runs to the end of the cell
# (the client's tokenizer rule, same pattern as intake_xlsx.LITERAL_OR_SHEET_MARK), so the scan stays linear
_LITERAL_OR_SHEET_MARK = re.compile(r'"(?:[^"]|(?<=\\)")*(?:(?<!\\)"|\Z)|[\'!]')
# the client reads `$`, `.`, `_` and any Unicode letter or digit as part of a symbol; U+FEFF counts as a space
_FUNCTION_CALL_RE = re.compile(r"(?<![\w.$])([\w.$]+)[\s\N{ZERO WIDTH NO-BREAK SPACE}]*\(")


class ValidateError(Exception):
    """A usage / input problem (message is safe to print)."""


# ---------------------------------------------------------------- function names

def _formula_texts(data):
    """[(where, text)] of every formula the client would compile: `=` cells, and the `=` values of conditional formats
    and data validations (the client compiles those too)."""
    found = []
    sheets = data.get("sheets", [])
    for sheet in sheets if isinstance(sheets, list) else []:
        if not isinstance(sheet, dict):
            continue
        cells = sheet.get("cells", {})
        for ref, cell in (cells.items() if isinstance(cells, dict) else []):
            content = cell.get("content") if isinstance(cell, dict) else None
            if isinstance(content, str) and content.startswith("="):
                found.append((ref, content))
        for key, path in (("conditionalFormats", ("rule", "values")), ("dataValidationRules", ("criterion", "values"))):
            items = sheet.get(key)
            for index, item in enumerate(items if isinstance(items, list) else []):
                node = item
                for step in path:
                    node = node.get(step) if isinstance(node, dict) else None
                for value in node if isinstance(node, list) else []:
                    if isinstance(value, str) and value.startswith("="):
                        found.append(("%s[%d]" % (key, index), value))
    return found


def _blank_literals(content):
    """(the formula with every string literal blanked, True when it cannot be read with certainty). Closes a literal at
    any `"` not directly preceded by a backslash, like the client's tokenizer; a quote or `!` outside a literal (a
    sheet reference) or a literal that never closes makes the formula unreadable: it is refused, not guessed."""
    unsure = []

    def blank(match):
        mark = match.group(0)
        if mark in ("'", "!") or not (len(mark) >= 2 and mark[-1] == '"' and mark[-2] != "\\"):
            unsure.append(mark)
        return " " if mark[0] == '"' else mark

    text = _LITERAL_OR_SHEET_MARK.sub(blank, content)
    return text, bool(unsure) or '"' in text


def formula_function_names(data):
    """{UPPERCASE function name: [cell refs]} for every formula of the document (string literals ignored).
    A name is the whole symbol run before `(`, as the client reads it (`$SUM` and `ÉSUM` are not `SUM`)."""
    used = {}
    for ref, content in _formula_texts(data):
        text, _ = _blank_literals(content)
        for name in _FUNCTION_CALL_RE.findall(text):
            used.setdefault(name.upper(), []).append(ref)
    return used


def unknown_function_problems(data, known):
    """One problem per function name used by a formula that the client does not register (#NAME?), and one per
    formula that cannot be read with certainty (a quote or `!` outside a literal, an unterminated literal)."""
    known = {name.upper() for name in known}
    problems = ["function %s() is not defined by the vendored client (cells: %s)" % (name, ", ".join(sorted(refs)[:3]))
                for name, refs in sorted(formula_function_names(data).items()) if name not in known]
    unreadable = sorted(ref for ref, content in _formula_texts(data) if _blank_literals(content)[1])
    if unreadable:
        problems.append("formula(s) that cannot be checked (a quote or `!` outside a string, or an unterminated "
                        "string) in: %s" % ", ".join(unreadable[:3]))
    return problems


def core_function_names(source):
    """Names registered by the o-spreadsheet core: the `categories` list maps each category to a frozen
    `var <category> = Object.freeze({__proto__: null, NAME: NAME, ...})` and the registry loop turns `_` into `.`
    (UNARY_PERCENT -> UNARY.PERCENT). Raises (never returns a partial set) when the file layout is not recognised."""
    block = re.search(r"const categories = \[(.*?)\n\s*\];", source, re.S)
    if not block:
        raise ValidateError("o_spreadsheet.js: `const categories = [...]` not found")
    category_vars = re.findall(r"functions:\s*([A-Za-z_$][\w$]*)\s*\}", block.group(1))
    if len(category_vars) < 10:
        raise ValidateError("o_spreadsheet.js: only %d function categories recognised" % len(category_vars))
    names = set()
    for var in category_vars:
        body = re.search(r"var %s = /\*#__PURE__\*/Object\.freeze\(\{(.*?)\n\s*\}\);" % re.escape(var), source, re.S)
        if not body:
            raise ValidateError("o_spreadsheet.js: `var %s = Object.freeze({...})` not found" % var)
        entries = [line for line in body.group(1).splitlines() if line.strip() and "__proto__" not in line]
        keys = re.findall(r"^\s*([A-Z][A-Z0-9_]*):\s*\1\b", body.group(1), re.M)
        if len(keys) != len(entries):
            raise ValidateError("o_spreadsheet.js: category %s has %d entries but %d are `NAME: NAME`"
                                % (var, len(entries), len(keys)))
        names.update(key.replace("_", ".") for key in keys)
    return names


def module_path(addons_path, module):
    """The folder of `module` in the first addons folder that holds it (Odoo's own lookup), or None."""
    for root in addons_path:
        candidate = os.path.join(root, module)
        if os.path.isdir(candidate):
            return candidate
    return None


def odoo_function_names(addons_path, modules=CLIENT_MODULES):
    """Names the Odoo modules add with `functionRegistry.add("NAME", ...)` in their static/src JS; the core file and
    folders called `tests` are skipped."""
    names = set()
    for module in modules:
        base = module_path(addons_path, module)
        if not base:
            continue
        for root, dirs, files in os.walk(os.path.join(base, "static", "src")):
            dirs[:] = sorted(d for d in dirs if d != "tests")
            for fname in sorted(files):
                if not fname.endswith(".js") or fname == "o_spreadsheet.js":
                    continue
                text = _read_text(os.path.join(root, fname))
                if "functionRegistry" in text:
                    names.update(name.upper() for name in re.findall(r'\.add\(\s*"([A-Za-z_][A-Za-z0-9_.]*)"', text))
    return names


def _read_text(path):
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_FILE + 1)
    except OSError as exc:
        raise ValidateError("cannot read %s: %s" % (path, exc.strerror or "error")) from None
    if len(data) > MAX_FILE:
        raise ValidateError("%s is larger than %d MB" % (path, MAX_FILE // (1024 * 1024)))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidateError("%s is not UTF-8 text" % path) from None


def derive_tree(addons_path):
    """({function names}, source info) of ONE addons path."""
    core = module_path(addons_path, "spreadsheet")
    if not core:
        raise ValidateError("the module `spreadsheet` is not in %s" % ",".join(addons_path))
    source = _read_text(os.path.join(core, *O_SPREADSHEET_JS))
    version = re.search(r"@version\s+(\S+)", source[:2000])
    names = core_function_names(source) | odoo_function_names(addons_path)
    info = {"o_spreadsheet_version": version.group(1) if version else "unknown",
            "o_spreadsheet_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "function_count": len(names),
            "modules": [m for m in CLIENT_MODULES if module_path(addons_path, m)]}
    return names, info


def derive(trees):
    """The frozen document for one or more addons paths: the intersection of their function sets."""
    if not trees:
        raise ValidateError("give at least one --addons-path")
    sets, infos = zip(*(derive_tree(tree) for tree in trees))
    return {"schema": SCHEMA, "functions": sorted(set.intersection(*sets)), "sources": list(infos)}


def dump_bytes(document):
    return (json.dumps(document, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")


def load_frozen(path=None):
    """The set of frozen function names."""
    try:
        with open(path or FROZEN_PATH, "rb") as fh:
            document = json.loads(fh.read(MAX_FILE).decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise ValidateError("cannot read the frozen function list: %s" % type(exc).__name__) from None
    names = document.get("functions") if isinstance(document, dict) else None
    if (not isinstance(document, dict) or document.get("schema") != SCHEMA or not isinstance(names, list)
            or not names or not all(isinstance(n, str) for n in names)):
        raise ValidateError("the frozen function list has an unexpected shape")
    return set(names)


def drift(frozen, derived):
    """(removed from the client, added to the client) between the frozen set and a fresh derivation."""
    return sorted(frozen - derived), sorted(derived - frozen)


# ---------------------------------------------------------------- formula evaluator

class SheetError(Exception):
    """A spreadsheet error value (#DIV/0!, #N/A ...) or a formula shape this evaluator does not support."""


class NotCatchable(SheetError):
    """A defect of the formula / of the test input that the client would not turn into a catchable error value
    (a compile error, a list the test did not provide): IFERROR must never hide it."""


class UnknownFunction(NotCatchable):
    """A function the evaluator (and therefore the sheet) does not know: the client's #NAME?."""


_TOKEN_RE = re.compile(r"""
    \s*(?:
      (?P<str>"[^"]*")
     |(?P<num>\d+(?:\.\d+)?)
     |(?P<func>[A-Za-z_][A-Za-z0-9_.]*)(?=\()
     |(?P<ref>\$?[A-Z]{1,2}\$?\d+)
     |(?P<op><>|<=|>=|[-+*/=(),:&<>])
    )""", re.VERBOSE)
_COMPARISONS = ("=", "<>", "<", ">", "<=", ">=")


def _tokenize(text):
    pos, tokens = 0, []
    while pos < len(text):
        if not text[pos:].strip():
            break
        match = _TOKEN_RE.match(text, pos)
        if not match:
            raise NotCatchable("cannot tokenize %r at %d" % (text[:60], pos))
        pos = match.end()
        kind = match.lastgroup
        tokens.append((kind, match.group(kind)))
    return tokens


def _split_ref(ref):
    match = re.fullmatch(r"\$?([A-Z]{1,2})\$?(\d+)", ref)
    return match.group(1), int(match.group(2))


class _Parser:
    """Precedence (lowest first): comparison, `&`, `+ -`, `* /`, unary minus."""

    def __init__(self, text):
        self.tokens = _tokenize(text)
        self.i = 0
        self.tree = self.expr()
        if self.i != len(self.tokens):
            raise NotCatchable("trailing tokens in %r" % text[:60])

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def take(self, value=None):
        kind, tok = self.peek()
        if kind is None or (value is not None and tok != value):
            raise NotCatchable("expected %r, got %r" % (value, tok))
        self.i += 1
        return kind, tok

    def expr(self):
        node = self.concat()
        while self.peek()[0] == "op" and self.peek()[1] in _COMPARISONS:
            op = self.take()[1]
            node = ("bin", op, node, self.concat())
        return node

    def concat(self):
        node = self.additive()
        while self.peek() == ("op", "&"):
            self.take()
            node = ("bin", "&", node, self.additive())
        return node

    def additive(self):
        node = self.term()
        while self.peek() in (("op", "+"), ("op", "-")):
            op = self.take()[1]
            node = ("bin", op, node, self.term())
        return node

    def term(self):
        node = self.unary()
        while self.peek() in (("op", "*"), ("op", "/")):
            op = self.take()[1]
            node = ("bin", op, node, self.unary())
        return node

    def unary(self):
        if self.peek() == ("op", "-"):
            self.take()
            return ("neg", self.unary())
        return self.primary()

    def primary(self):
        kind, tok = self.take()
        if kind == "num":
            return ("num", float(tok))
        if kind == "str":
            return ("str", tok[1:-1])
        if kind == "ref":
            if self.peek() == ("op", ":"):
                self.take()
                other = self.take()
                if other[0] != "ref":
                    raise NotCatchable("bad range end %r" % (other,))
                return ("range", _split_ref(tok), _split_ref(other[1]))
            return ("ref", _split_ref(tok))
        if kind == "func":
            self.take("(")
            args = []
            if self.peek() != ("op", ")"):
                args.append(self.expr())
                while self.peek() == ("op", ","):
                    self.take()
                    args.append(self.expr())
            self.take(")")
            return ("call", tok, args)
        if (kind, tok) == ("op", "("):
            node = self.expr()
            self.take(")")
            return node
        raise NotCatchable("unexpected token %r" % (tok,))


@functools.lru_cache(maxsize=None)
def parse_formula(content):
    """The parse tree is never mutated, so it is cached."""
    if not content.startswith("="):
        raise NotCatchable("not a formula: %r" % content[:60])
    return _Parser(content[1:]).tree


def _col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _col_letters(index):
    letters = ""
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def referenced_cells(node):
    """Every (column letters, row) a parsed formula reads, ranges expanded (a range over MAX_RANGE_CELLS is refused
    BEFORE it is built, so a hostile `A1:ZZ99999999` cannot eat the memory)."""
    kind = node[0]
    if kind == "ref":
        return [node[1]]
    if kind == "range":
        (c1, r1), (c2, r2) = node[1], node[2]
        width, height = _col_index(c2) - _col_index(c1) + 1, r2 - r1 + 1
        if width > 0 and height > 0 and width * height > MAX_RANGE_CELLS:
            raise NotCatchable("a range of %d x %d cells is too large for the evaluator" % (width, height))
        return [(_col_letters(col), row) for col in range(_col_index(c1), _col_index(c2) + 1)
                for row in range(r1, r2 + 1)]
    if kind == "neg":
        return referenced_cells(node[1])
    if kind == "bin":
        return referenced_cells(node[2]) + referenced_cells(node[3])
    if kind == "call":
        return [ref for arg in node[2] for ref in referenced_cells(arg)]
    return []


class SheetEvaluator:
    """Evaluates the first sheet of a dashboard JSON.

    `lists` = `{list_id: rows}`: the rows the list data source `list_id` returns for the year-filtered domain, in
    model order, each row a dict `{field: value}`. ODOO.LIST(n, position, field) reads `rows[position - 1][field]`
    and is `""` when that row does not exist, like the real client (`list_data_source.js` getListCellValue). A list
    nobody provided, or a field the row lacks, raises `NotCatchable`: IFERROR must not turn a forgotten input into
    a silent 0. `pivots` = `{pivot_id: {"measures": [...], "months": {"MM/yyyy": {measure: value}}}}`, the month
    columns of a pivot grouped by `<date field>:month`: ODOO.PIVOT(id, measure, "<field>:month", "MM/yyyy") reads one
    cell and is `""` for a month the pivot has no column for (the client shows an empty cell), a pivot nobody provided
    or a measure it does not hold raises `NotCatchable`; ODOO.PIVOT.HEADER(id, "#<field>:month", 1) is the date serial
    of the FIRST month column (0 when the pivot has none: the client then shows the date 0, year 1899, measured in
    the browser) and YEAR(serial) its year. `translations` = `{msgid: msgstr}` for `_t`; a missing msgid is the
    identity (the source language).
    Arithmetic on `""` is 0 (o_spreadsheet.js `toNumber`) but on any other text it is `#VALUE!`. SUM and MAX ignore
    text in references / ranges and reject non-numeric text given directly. AVERAGEIF follows `o_spreadsheet.js`:
    only numeric cells that satisfy a numeric criterion count; no match is a divide-by-zero error. Only the functions
    the generated sheets use are known; any other one raises `UnknownFunction` (the client's #NAME?).
    """

    def __init__(self, data, lists=None, translations=None, pivots=None):
        self.cells = {ref: c.get("content", "") for ref, c in data["sheets"][0]["cells"].items()}
        self.lists = dict(lists or {})
        self.pivots = dict(pivots or {})
        self.translations = translations or {}
        self.cache = {}
        self.in_progress = set()

    def value(self, ref):
        if ref in self.cache:
            return self.cache[ref]
        if ref in self.in_progress:
            raise SheetError("circular reference at %s" % ref)
        content = self.cells.get(ref)
        if content is None or content == "":
            result = None                                  # empty cell
        elif content.startswith("="):
            self.in_progress.add(ref)
            try:
                result = self.eval(parse_formula(content))
            finally:
                self.in_progress.discard(ref)
        else:
            try:
                result = float(content)                    # numeric literal, stored as text in the JSON
            except ValueError:
                result = content
        self.cache[ref] = result
        return result

    def eval(self, node):
        kind = node[0]
        if kind in ("num", "str"):
            return node[1]
        if kind == "neg":
            return -self._num(self.eval(node[1]))
        if kind == "ref":
            return self.value("%s%d" % node[1])
        if kind == "range":
            raise SheetError("a range is only valid as a SUM / MAX / AVERAGEIF argument")
        if kind == "bin":
            return self._binary(node[1], node[2], node[3])
        if kind == "call":
            return self._call(node[1], node[2])
        raise SheetError("unknown node %r" % (node,))

    @staticmethod
    def _is_number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    @classmethod
    def _num(cls, value):
        """`toNumber`: an empty cell / `""` is 0, any other text is #VALUE!."""
        if value is None or value == "":
            return 0.0
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, str):                         # a numeric string converts, any other text is #VALUE!
            try:
                return float(value)
            except ValueError:
                raise SheetError("#VALUE! %r is not a number" % (value,)) from None
        if not cls._is_number(value):
            raise SheetError("#VALUE! %r is not a number" % (value,))
        return float(value)

    @classmethod
    def _strict_num(cls, value):
        """`strictToNumber` (a scalar argument of SUM): `""` is rejected as well."""
        if value == "":
            raise SheetError("#VALUE! an empty string is not a number")
        return cls._num(value)

    @staticmethod
    def _str(value):
        """`toString`: numbers print the JS way (2026 -> "2026", 0.5 -> "0.5"), an empty cell is ""."""
        if value is None:
            return ""
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return str(int(value)) if float(value).is_integer() else repr(float(value))
        return str(value)

    @staticmethod
    def _bool(value):
        """`toBoolean`: only booleans, numbers, an empty cell and the strings TRUE / FALSE are conditions."""
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str) and value.upper() in ("TRUE", "FALSE"):
            return value.upper() == "TRUE"
        raise SheetError("#VALUE! %r is not a logical value" % (value,))

    @classmethod
    def _kind(cls, value):
        if value is None:
            return "empty"
        if isinstance(value, bool):
            return "boolean"
        return "number" if cls._is_number(value) else "string"

    def _equal(self, a, b):
        """o-spreadsheet EQ: an empty side takes the neutral value of the other side's type, text compares
        case-insensitively, numbers almost equal, values of different types are different."""
        neutral = {"number": 0.0, "string": "", "boolean": False}
        ka, kb = self._kind(a), self._kind(b)
        if ka == "empty" and kb == "empty":
            return True
        if ka == "empty":
            a, ka = neutral[kb], kb
        if kb == "empty":
            b, kb = neutral[ka], ka
        if ka == "string":
            a = a.upper()
        if kb == "string":
            b = b.upper()
        if ka == kb == "number":
            return abs(a - b) < 2e-16
        return ka == kb and a == b

    def _binary(self, op, left, right):
        a, b = self.eval(left), self.eval(right)
        if op == "&":
            return self._str(a) + self._str(b)
        if op == "=":
            return self._equal(a, b)
        if op == "<>":
            return not self._equal(a, b)
        if op in ("<", ">", "<=", ">="):
            if self._kind(a) not in ("number", "empty") or self._kind(b) not in ("number", "empty"):
                raise NotCatchable("the test evaluator compares only numbers with %s" % op)
            a, b = self._num(a), self._num(b)
            return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
        a, b = self._num(a), self._num(b)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if b == 0:
            raise SheetError("#DIV/0!")
        return a / b

    def _numbers(self, args):
        """The numbers SUM / MAX see: those of references and ranges (text, booleans, empties skipped) and the
        strictly converted scalars given in the formula."""
        numbers = []
        for arg in args:
            if arg[0] in ("ref", "range"):
                numbers += [val for val in (self.value("%s%d" % cell) for cell in referenced_cells(arg))
                            if self._is_number(val)]
            else:
                numbers.append(self._strict_num(self.eval(arg)))
        return numbers

    def _averageif(self, args):
        """AVERAGEIF(criteria_range, criterion[, average_range]) with a numeric criterion (`">0"`, `">=0"`, `"=5"`
        ...). A criterion shape the evaluator does not model is `NotCatchable` (loud), never a silent no-match."""
        if (len(args) not in (2, 3) or args[0][0] not in ("ref", "range")
                or (len(args) == 3 and args[2][0] not in ("ref", "range"))):
            raise NotCatchable("AVERAGEIF needs a range, a criterion and an optional average range")
        criteria = self.eval(args[1])
        match = re.fullmatch(r"(>=|<=|>|<|=)?\s*(-?\d+(?:\.\d+)?)", self._str(criteria))
        if not match:
            raise NotCatchable("AVERAGEIF criterion %r is not a numeric comparison the evaluator models" % (criteria,))
        operator, operand = match.group(1) or "=", float(match.group(2))
        tests = {">": lambda v: v > operand, ">=": lambda v: v >= operand, "<": lambda v: v < operand,
                 "<=": lambda v: v <= operand, "=": lambda v: v == operand}
        criteria_cells = referenced_cells(args[0])
        average_cells = referenced_cells(args[2]) if len(args) == 3 else criteria_cells
        count, total = 0, 0.0
        for index, (c_col, c_row) in enumerate(criteria_cells):
            test_value = self.value("%s%d" % (c_col, c_row))
            # a shorter average range: the client reads `_averageRange[i][j]` = undefined -> not a number -> skipped
            average_value = self.value("%s%d" % average_cells[index]) if index < len(average_cells) else None
            if self._is_number(test_value) and tests[operator](test_value) and self._is_number(average_value):
                count += 1
                total += average_value
        if count == 0:
            raise SheetError("#DIV/0! AVERAGEIF matched no numeric cell")
        return total / count

    def _call(self, name, args):
        if name == "_t":
            text = self._str(self.eval(args[0]))
            return self.translations.get(text, text)       # the client `_t` takes ONE argument, no placeholders
        if name == "SUBSTITUTE":
            # every occurrence of search_for is replaced; an empty search returns the text unchanged; the optional
            # 4th argument (the n-th occurrence only) is not used by the sheets -> loud
            if len(args) != 3:
                raise NotCatchable("the test evaluator models SUBSTITUTE with 3 arguments only")
            text, search, replacement = (self._str(self.eval(arg)) for arg in args)
            return text if search == "" else text.replace(search, replacement)
        if name == "IFERROR":
            try:
                return self.eval(args[0])
            except NotCatchable:
                raise                                      # a compile-type defect: IFERROR can't hide it
            except (SheetError, ZeroDivisionError):
                return self.eval(args[1]) if len(args) > 1 else ""
        if name == "IF":
            if self._bool(self.eval(args[0])):
                return self.eval(args[1])
            return self.eval(args[2]) if len(args) > 2 else False
        if name == "INT":
            return float(math.floor(self._num(self.eval(args[0]))))
        if name == "SUM":
            return sum(self._numbers(args), 0.0)
        if name == "MAX":
            numbers = self._numbers(args)                  # nothing numeric: -Infinity in the client -> 0
            return max(numbers) if numbers else 0.0
        if name == "AVERAGEIF":
            return self._averageif(args)
        if name == "RIGHT":
            text = self._str(self.eval(args[0]))
            count = self._num(self.eval(args[1])) if len(args) > 1 else 1.0
            if count < 0:
                raise SheetError("#ERROR! the number_of_characters must be positive or null")
            return text[max(len(text) - int(count), 0):]
        if name == "YEAR":
            if len(args) != 1:
                raise NotCatchable("YEAR takes 1 argument")
            return float((EXCEL_EPOCH + datetime.timedelta(days=int(self._num(self.eval(args[0]))))).year)
        if name == "ODOO.PIVOT":
            if len(args) != 4:
                raise NotCatchable("ODOO.PIVOT takes 4 arguments here (pivot, measure, group, month key)")
            pivot_id, measure, group, key = (self.eval(a) for a in args)
            pivot = self._pivot(pivot_id, group)
            if measure not in pivot["measures"]:
                raise NotCatchable("pivot %r holds no measure %r" % (pivot_id, measure))
            column = pivot["months"].get(self._str(key))
            return "" if column is None else column.get(measure, 0.0)
        if name == "ODOO.PIVOT.HEADER":
            if len(args) != 3:
                raise NotCatchable("ODOO.PIVOT.HEADER takes 3 arguments here")
            pivot_id, group, position = (self.eval(a) for a in args)
            pivot = self._pivot(pivot_id, str(group).lstrip("#"))
            if position != 1:
                raise NotCatchable("the test evaluator reads the FIRST month header only")
            months = sorted(pivot["months"], key=lambda k: (int(k.split("/")[1]), int(k.split("/")[0])))
            if not months:
                return 0.0
            month, year = (int(part) for part in months[0].split("/"))
            return float((datetime.date(year, month, 1) - EXCEL_EPOCH).days)
        if name == "ODOO.LIST":
            if len(args) != 3:
                raise NotCatchable("ODOO.LIST takes 3 arguments here")
            list_id, position, field_name = (self.eval(a) for a in args)
            rows = self.lists.get(int(list_id))
            if rows is None:
                raise NotCatchable("list %r is not provided to the evaluator" % list_id)
            index = int(position) - 1
            if index < 0 or index >= len(rows):
                return ""                                  # the client returns "" for a missing position
            if field_name not in rows[index]:
                raise NotCatchable("list %r rows have no field %r" % (list_id, field_name))
            return rows[index][field_name]
        raise UnknownFunction("function %s is not supported by the test evaluator" % name)

    def _pivot(self, pivot_id, group):
        pivot = self.pivots.get(int(pivot_id))
        if pivot is None:
            raise NotCatchable("pivot %r is not provided to the evaluator" % pivot_id)
        if not str(group).endswith(":month"):
            raise NotCatchable("the test evaluator models pivots grouped by month only")
        return pivot

    def evaluate_all(self):
        return {ref: self.value(ref) for ref in self.cells}


# ---------------------------------------------------------------- command line

def _read_json(path):
    try:
        data = json.loads(_read_text(path))
    except (ValueError, RecursionError):
        raise ValidateError("%s is not JSON" % path) from None
    if not isinstance(data, dict) or not isinstance(data.get("sheets"), list):
        raise ValidateError("%s is not a spreadsheet JSON (no sheets)" % path)
    return data


def build_parser():
    parser = argparse.ArgumentParser(prog="validate_formulas.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="every function of the dashboard JSON must be in the frozen list")
    check.add_argument("dashboard_json")
    check.add_argument("--functions", help="another frozen list (default: client_functions.json)")
    for name, text in (("derive", "write the frozen list from the Odoo sources"),
                       ("check-drift", "fail when the Odoo sources no longer match the frozen list")):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--addons-path", action="append", required=True,
                         help="comma separated addons folders of ONE Odoo tree; repeat for several trees")
        if name == "derive":
            cmd.add_argument("--out", default=FROZEN_PATH)
        else:
            cmd.add_argument("--functions", help="another frozen list (default: client_functions.json)")
    return parser


def run(args, out, err):
    if args.command == "check":
        problems = unknown_function_problems(_read_json(args.dashboard_json), load_frozen(args.functions))
        for problem in problems:
            print(problem, file=err)
        print("functions: %s" % ("ok" if not problems else "%d unknown" % len(problems)), file=out)
        return 1 if problems else 0
    trees = [[p for p in tree.split(",") if p] for tree in args.addons_path]
    document = derive(trees)
    if args.command == "derive":
        with open(args.out, "wb") as fh:
            fh.write(dump_bytes(document))
        print("wrote %s: %d functions from %d tree(s)" % (args.out, len(document["functions"]), len(trees)), file=out)
        return 0
    removed, added = drift(load_frozen(args.functions), set(document["functions"]))
    for name in removed:
        print("no longer registered by the client: %s" % name, file=err)
    for name in added:
        print("new in the client, not in the frozen list: %s" % name, file=err)
    print("drift: %s" % ("none" if not (removed or added) else "%d removed, %d added" % (len(removed), len(added))),
          file=out)
    return 1 if (removed or added) else 0


def main(argv=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        return run(args, out, err)
    except ValidateError as exc:
        print("error: %s" % exc, file=err)
        return 2
    except OSError as exc:
        print("error: %s" % (exc.strerror or "cannot write the file"), file=err)
        return 2
    except Exception as exc:  # noqa: BLE001 - last resort: one line, no traceback
        print("error: unexpected %s" % type(exc).__name__, file=err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
