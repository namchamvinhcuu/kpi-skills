# odoo-kpi-skills

A Claude Code plugin: skills and small scripts that let QA/QC staff ask Claude Code to build a KPI dashboard
in Odoo from an Excel file that sits in Documents, bound to data sources that already exist.

Status: **0.1.2, work in progress**. QA/QC staff: read `README.vi.md` (Vietnamese guide).

What is in the plugin:

| part | file | what it does |
|---|---|---|
| skills | `skills/kpi-builder`, `skills/kpi-source-catalog` | the workflow Claude follows and the map of data sources |
| command | `commands/kpi.md` | `/odoo-kpi-skills:kpi <Excel address> [design file]` starts the workflow |
| connection | `scripts/odoo_rpc.py` | JSON-RPC client, private config file check, full-host-name confirmation |
| intake | `scripts/intake_xlsx.py` | Excel in Documents -> cropped, deterministic baseline JSON (File > Snapshot or Download as JSON) |
| formulas | `scripts/validate_formulas.py`, `scripts/client_functions.json` | only functions the Odoo client registers; test evaluator |
| scan | `scripts/scan_excel.py` | offline: proposes the "row -> source" mapping from the labels of the Excel (aliases in the catalog, optional user `--aliases` file) and a draft spec |
| binder | `scripts/bind_kpi.py`, `scripts/kpi_catalogs.py`, `catalogs/<project>.json` | baseline + KPI spec -> dashboard JSON (`ODOO.LIST`, `ODOO.PIVOT`, derived formulas); the source catalog is chosen by `--project` |
| reconcile | `scripts/reconcile_kpi.py` | compares the numbers the Excel had typed with the live numbers of the bound dashboard (read only) |
| publisher | `scripts/publish_dashboard.py` | creates / archives / deletes the user's OWN dashboards, dry run by default |
| admin setup | `scripts/setup_instance.py` | administrator only: group "KPI (QA/QC)", who may publish |

This repository is PUBLIC: it must never contain customer data, Excel files, passwords, API keys or the
address of a real instance. A test (`tests/test_repo_hygiene.py`) scans the tree for those.

## Install

From the GitHub repository of the plugin:

    /plugin marketplace add namchamvinhcuu/kpi-skills
    /plugin install odoo-kpi-skills@odoo-kpi

Local check of the plugin folder: `claude plugin validate .`

## Connection settings

Easiest: in a normal terminal window of your own (NOT inside Claude Code: it is refused there, and an agent must never
run it) run

    python scripts/odoo_rpc.py init-config

It asks the address, database, login and API key (typed hidden), writes the private config file through a checked
temp file, and can test the connection. Or create the config file by hand (or set the four environment variables
below). Find its location with:

    python scripts/odoo_rpc.py config-path

Content (`[odoo]` section):

    [odoo]
    url = https://your-instance.example.com
    db = your_database
    login = your.login
    api_key = <an API key created in Odoo: Preferences > Account Security>
    project = <optional: the name of a shipped source catalog, see "Projects" below>

Environment variables instead of the file: `ODOO_KPI_URL`, `ODOO_KPI_DB`, `ODOO_KPI_LOGIN`, `ODOO_KPI_API_KEY`
(all four must be set). `ODOO_KPI_PROJECT` is optional and is not one of the four: it overrides the file's `project`.

The file must be private to you, and the scripts refuse to read it otherwise:
- Linux / macOS: `chmod 600 <file>` (and you must own it);
- Windows: keep it in your own profile folder (the default `%APPDATA%\odoo-kpi`). The permissions are read as SIDs
  (so the check works on any display language) and only you, SYSTEM and Administrators may have an allow entry.
  Any other entry (Everyone, Users, Domain Users, another account) is refused: remove it with `icacls`.
  If the check cannot read the permissions at all, the scripts refuse as well: use the environment variables above
  instead of a file.

Check the file and the connection:

    python scripts/odoo_rpc.py check-config
    python scripts/odoo_rpc.py ping --confirm-instance <full-host-name>

`check-config` only checks the config (it prints `config: file, private (permissions ok)` or `config: env`, never
the key), and one line about the project: `project: <name>`, `project: not set (installed: ...)`,
`project: invalid (installed: ...)` or `catalogs: unreadable` (a bad `project` never makes it fail). `ping` logs in and prints the instance name, where the config came from, the server version and your
user id.

`--confirm-instance` is required for every host that is not a local dev host. The scripts do not suggest the value:
you type the FULL host name yourself (`my-instance.example.com`; a shorter name such as `my-instance` is refused, so a
look-alike address cannot pass). Claude must ask you for it and never fill it in on its own. Hosts that end in `.test`
or `.localhost` are treated as local and may use plain http: only use such a name for a machine of your own, because
the API key travels in clear over http. Redirects are never followed.

If the server answers `HTTP 403 ... error 1010`, Cloudflare blocks the client signature: ask the owner of the
instance to allow it.

## Projects (one source catalog per Odoo project)

The data a KPI may read is a CLOSED list per project: `catalogs/<project>.json` (models, columns, selector values,
caveats, label aliases). `scan_excel.py`, `bind_kpi.py` and `reconcile_kpi.py` take `--project <name>`; when exactly one
catalog is installed it is used without the flag, when several are installed the flag is required (the skill asks the
user, and `init-config` asks for the project once so that it is remembered as `project =` in the config). A catalog is
chosen BY NAME from the files that ship with the plugin: never by a path, and a user-supplied catalog folder does not
exist on purpose (a catalog decides which models and columns may reach a formula, so it is trusted code).

Using the plugin for another project (checklist):
1. The models or SQL views the KPIs will read EXIST in that project's Odoo (the plugin cannot create them).
2. Write `catalogs/<project>.json` (schema 1: read an existing file of `catalogs/` for the shape; names and caveats only, NO
   customer data, no figures). The name is `[a-z0-9][a-z0-9_-]{0,29}`, not a Windows device name (`con`, `nul`, `com1` ...),
   and neither contains `aliases` nor starts with `dashboard`, `baseline`, `kpi_spec` or `reconcile` (`.gitignore`
   treats those as workflow output).
3. Write `skills/kpi-source-catalog/projects/<project>.md`, the human map of that catalog (a test requires it to name
   every source of the catalog).
4. Add tests with a synthetic fixture (see `tests/second_catalog_fixtures.py`), never real customer data.
5. Ask the owner of the repository: catalogs are PUBLIC with the plugin, so the names of models, groups and customers in
   a catalog must be cleared for publication, project by project.

Notes for whoever adds the first second project: many existing tests call the scripts against the REAL `catalogs/`
folder and assume exactly one shipped catalog (default catalog, no project prompt in `init-config`): shipping a second
catalog turns them red until `IsolatedTestCase` in `tests/helpers.py` points `kpi_catalogs.CATALOG_DIR` at a temporary
folder holding only a copy of the first catalog; the SHA-256 pin of the shipped catalog and the only-catalog test in
`tests/test_kpi_catalogs.py` are pins to update on purpose. About the config: an `init-config` run that does not ask for
the project (fewer than two catalogs installed) writes a file WITHOUT a `project =` line (it never reads the old file),
and when all four `ODOO_KPI_*` credential variables are set the config file is not read at all, so its `project =` line
is ignored: the project then comes from `ODOO_KPI_PROJECT` only.

## Tests

Standard library only:

    ./run-tests.sh          # Linux / macOS (PYTHON=/path/to/python to choose the interpreter)
    run-tests.cmd           # Windows
