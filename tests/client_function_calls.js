// Test helper (read-only, no network): the function names the REAL o-spreadsheet client parser sees in formulas.
//
//   node client_function_calls.js <path of o_spreadsheet.js>   < JSON list of formulas on stdin
//
// Prints one JSON list on stdout, one item per formula: {"calls": [names of FUNCALL nodes, in tree order]} when the
// client parses the formula, {"error": "..."} when it does not. The file is only evaluated in a bare `vm` context
// with stubbed browser globals; nothing from the Odoo instance is touched.
const fs = require("fs");
const vm = require("vm");

const ctx = { console, setTimeout, clearTimeout, Intl, TextEncoder };
ctx.Path2D = class {};
ctx.Image = class {};
ctx.Element = class {};
ctx.HTMLElement = class {};
ctx.document = new Proxy(function () {}, { get: () => ctx.document, apply: () => ctx.document, set: () => true });
ctx.window = ctx;
ctx.self = ctx;
ctx.globalThis = ctx;
ctx.navigator = {};
vm.createContext(ctx);
const stub = () => new Proxy(class {}, {
  get: (target, key) => (key in target ? target[key] : key === "prototype" ? target.prototype : stub()),
  apply: () => stub(),
  construct: () => ({}),
});
ctx.owl = stub();
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const parse = ctx.o_spreadsheet.parse;

function calls(node, found) {
  if (!node || typeof node !== "object") return;
  if (node.type === "FUNCALL") found.push(node.value);
  for (const key of ["args", "left", "right", "operand"]) {
    const child = node[key];
    if (Array.isArray(child)) child.forEach((item) => calls(item, found));
    else if (child && typeof child === "object") calls(child, found);
  }
}

const formulas = JSON.parse(fs.readFileSync(0, "utf8"));
const answers = formulas.map((formula) => {
  try {
    const found = [];
    calls(parse(formula), found);
    return { calls: found };
  } catch (error) {
    return { error: String(error && error.message).slice(0, 80) };
  }
});
process.stdout.write(JSON.stringify(answers));
