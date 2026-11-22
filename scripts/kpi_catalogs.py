"""The source catalogs shipped with the plugin: one JSON file per project in `catalogs/` (next to `scripts/`).

A catalog is a TRUST ROOT (it decides which models, columns and selector values may reach a formula or a domain), so a
project is chosen by NAME from the files that ship with the plugin, never by a path typed or found in a document:
- the name must be a member of `list_projects()` (membership first); the file path is built from the LISTED entry only,
  never from the typed text, so `../x`, `a/b`, an absolute path or a name with a newline can only be "not installed";
- a name is `[a-z0-9][a-z0-9_-]{0,29}` matched with `fullmatch`, is not a Windows device name, does not contain `aliases`
  and does not start with a prefix that `.gitignore` treats as workflow output (such a file would silently miss the
  commit and the installed copy);
- a symlink or anything that is not a regular file is not a catalog.

Standard library only; it imports nothing of this plugin, so it cannot form a cycle. Errors are `CatalogError`; the
callers wrap it into their own error class (`BindError`, `ConfigError`) so every command keeps its exit code 2.
The default folder is the module constant `CATALOG_DIR`, read at CALL time (tests redirect it with `mock.patch.object`).
"""
import os
import re

CATALOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "catalogs")
NAME_RULE = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}")
WINDOWS_DEVICE_NAMES = frozenset({"con", "prn", "aux", "nul"}
                                 | {"com%d" % n for n in range(1, 10)} | {"lpt%d" % n for n in range(1, 10)})
# .gitignore patterns for workflow output: dashboard*, baseline*, kpi_spec*, reconcile*, *aliases*
IGNORED_PREFIXES = ("dashboard", "baseline", "kpi_spec", "reconcile")
IGNORED_PARTS = ("aliases",)


class CatalogError(Exception):
    """A catalog name or the catalogs folder is not usable (message is safe to print: it never echoes the typed name)."""


def valid_name(name):
    """True for a name that may be a shipped catalog (the rule above); anything that is not a string is refused."""
    return (isinstance(name, str) and NAME_RULE.fullmatch(name) is not None
            and name not in WINDOWS_DEVICE_NAMES and not name.startswith(IGNORED_PREFIXES)
            and not any(part in name for part in IGNORED_PARTS))


def list_projects(catalog_dir=None):
    """Sorted names of the installed catalogs (regular, non-symlink `<name>.json` files whose name passes the rule)."""
    folder = CATALOG_DIR if catalog_dir is None else catalog_dir
    try:
        entries = os.listdir(folder)
    except OSError:
        raise CatalogError("the catalogs folder cannot be read") from None
    names = []
    for entry in entries:
        stem, extension = os.path.splitext(entry)
        if extension != ".json" or not valid_name(stem):
            continue
        path = os.path.join(folder, entry)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        names.append(stem)
    return sorted(names)              # sorted STEMS (`a-b` before `a` when sorting the file names `a-b.json`, `a.json`)


def installed_text(names):
    return ", ".join(names) if names else "none"


def catalog_path(project=None, catalog_dir=None):
    """The path of the catalog of `project`; with no project, the only installed catalog (several or none: refused)."""
    folder = CATALOG_DIR if catalog_dir is None else catalog_dir
    names = list_projects(folder)
    if project is None:
        if len(names) == 1:
            return os.path.join(folder, names[0] + ".json")
        if not names:
            raise CatalogError("no source catalog is installed in the catalogs folder")
        raise CatalogError("several source catalogs are installed: pass --project <name> (installed: %s)"
                           % installed_text(names))
    if not isinstance(project, str) or project not in names:
        raise CatalogError("that project is not one of the installed source catalogs (installed: %s)"
                           % installed_text(names))
    return os.path.join(folder, project + ".json")
