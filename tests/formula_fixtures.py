"""Synthetic inputs for the `validate_formulas` tests (not a test module). Built in Python, no data file.

Everything here is invented: a miniature `o_spreadsheet.js` with the layout `core_function_names` recognises, a
miniature Odoo addons tree, dashboard JSONs and frozen lists. Nothing is read from a real Odoo checkout.
"""
import json
import os

from helpers import REPO  # noqa: F401  (puts scripts/ on sys.path)

import validate_formulas as vf  # noqa: E402

CORE_VERSION = "17.0.99"
CORE_JS_PARTS = vf.O_SPREADSHEET_JS


def default_categories(count=10, with_sum=False):
    """{category variable: [entry names]}; names are invented except a few real-looking ones used by the tests."""
    categories = {"Cat%d" % i: ["FUNC%dA" % i, "FUNC%d_B" % i] for i in range(1, count + 1)}
    categories["Cat1"] = ["UNARY_PERCENT", "CEILING_MATH", "LOG10"] + (["SUM"] if with_sum else [])
    return categories


def expected_core_names(categories):
    return {name.replace("_", ".") for names in categories.values() for name in names}


def core_source(categories=None, version=CORE_VERSION, entries=None, extra="", newline="\n", version_offset=0):
    """A miniature o_spreadsheet.js.

    `categories` {var: [names]} (default: ten categories); `entries` {var: [raw entry lines]} overrides the lines of a
    category (to build a malformed one); `extra` is appended before the end; `version_offset` pads the header so the
    `@version` tag moves down the file.
    """
    categories = default_categories() if categories is None else categories
    lines = ["/**", " * @version " + version, " */"] if version else ["/**", " * no version tag", " */"]
    lines[1:1] = [" * padding"] * version_offset
    lines += ["(function (exports) {", "    'use strict';"]
    for var, names in categories.items():
        raw = (entries or {}).get(var, ["%s: %s" % (n, n) for n in names])
        body = [("        " + line + ("," if i < len(raw) - 1 else "")) if line else "" for i, line in enumerate(raw)]
        lines += ["    var %s = /*#__PURE__*/Object.freeze({" % var, "        __proto__: null,"] + body + ["    });", ""]
    lines += ["    const categories = ["]
    lines += ['        { name: "%s", functions: %s },' % (var, var) for var in categories]
    lines += ["    ];", extra, "})(this);", ""]
    return newline.join(lines)


def write(path, text=None, data=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data if data is not None else text.encode("utf-8"))
    return path


def make_tree(root, core=None, modules=None, extra_files=None):
    """An addons folder `root` with `spreadsheet` (the core js) and the given other client modules.

    `modules` = {module: {relative path inside `static/src`: js text}}; `extra_files` = {path relative to root: text}.
    """
    core = core_source() if core is None else core
    write(os.path.join(root, "spreadsheet", *CORE_JS_PARTS), core)
    for module, files in (modules or {}).items():
        for rel, text in files.items():
            write(os.path.join(root, module, "static", "src", *rel.split("/")), text)
    for rel, text in (extra_files or {}).items():
        write(os.path.join(root, *rel.split("/")), text)
    return root


def registry_js(*names, word="functionRegistry"):
    return "".join('%s.add("%s", {\n    description: "x",\n});\n' % (word, name) for name in names)


def frozen_document(names, schema=1):
    return {"schema": schema, "functions": sorted(names), "sources": []}


def write_frozen(path, names, **kwargs):
    return write(path, data=vf.dump_bytes(frozen_document(names, **kwargs)))


def dashboard(cells, extra_sheets=()):
    """A dashboard JSON whose first sheet holds `cells` {ref: content}; content is stored as given."""
    first = {"cells": {ref: {"content": content} for ref, content in cells.items()}}
    return {"sheets": [first, *extra_sheets]}


def write_json(path, data):
    return write(path, json.dumps(data))


def pivot(months, measures=("__count",)):
    """An evaluator pivot `{"measures": [...], "months": {"MM/yyyy": {measure: value}}}`; `months` maps the key to a
    dict, or to a number that fills every measure."""
    return {"measures": list(measures),
            "months": {key: ({m: float(value) for m in measures} if not isinstance(value, dict) else value)
                       for key, value in months.items()}}
