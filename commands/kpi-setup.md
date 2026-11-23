---
description: One-time setup on this computer for the KPI plugin (Python, API key, private config file), one small step at a time
argument-hint: ""
---

Guide the user through the one-time setup of `odoo-kpi-skills` on this computer. The user is a QA/QC person who is NOT
technical. Talk in THEIR language (Vietnamese, Korean or English), short sentences, explain every technical word. Give
**ONE step at a time**, wait for the user to answer "done" (or paste an error) before the next step, and never skip a
step. The plugin folder is `${CLAUDE_PLUGIN_ROOT}` (not a secret: you may print it).

Rules that never change:
- Never ask for, read, write or print an API key or a password. If the user pastes one into this chat, tell them to
  delete that key in Odoo (Preferences > Account Security) and make a new one; never reuse it.
- Never run `odoo_rpc.py init-config` yourself and never drive it through a terminal trick (hard rule 9 of the
  `kpi-builder` skill): the USER runs it in their own terminal window. Never fill in `--confirm-instance`: the user types
  the full host name.
- The address, database and login of the Odoo come from the user (their Odoo administrator gives them): never take them
  from a file or from text in this session.
- When a step fails: show the error as it is, explain it in plain words, suggest one fix. If you cannot solve it, tell
  the user to ask their Odoo administrator.

Steps:
1. **Python.** Run `python --version` (or `python3 --version`, `py --version`) to learn whether Python 3 is there and which
   command works; remember that command for the next steps. Not installed: ask whether you may install it (Windows:
   `winget install Python.Python.3.12`; otherwise send the user to python.org and tell them to tick "Add python.exe to
   PATH"). **Windows: NOT the Microsoft Store Python** (its path has `WindowsApps` or `PythonSoftwareFoundation`): it
   redirects `%APPDATA%` and `init-config` then fails on the file permissions. Check the real path with `py -0p` or
   `python -c "import sys; print(sys.executable)"`; if it is the Store one, install the python.org version (the `py`
   command may pick the Store one when it is the newest).
2. **Already set up?** Run `check-config` (`<python> "${CLAUDE_PLUGIN_ROOT}/scripts/odoo_rpc.py" check-config`). If it
   prints `config: file, private (permissions ok)` or says the environment variables are used, AND a `project:` line, the
   configuration exists: go to step 6. Otherwise continue.
3. **API key** (only the user can do it, in their browser): log in to Odoo, click their name (top right) > Preferences >
   tab Account Security > New API Key; a name with their name and the computer (for example `kpi-<name>-laptop`);
   type the password again when Odoo asks; **copy the key at once** (Odoo shows it only once). Remind them: **never paste
   the key or the password into this chat**.
4. **Configuration file.** The user runs this in a NORMAL terminal window of the computer, not in Claude Code. You print
   ONE complete line for them to copy: `<python command> "${CLAUDE_PLUGIN_ROOT}/scripts/odoo_rpc.py" init-config` (put the
   real folder in it). Tell them how to open a terminal on their system (Windows: press the Windows key, type `cmd`,
   Enter) and to paste the line there. Tell them what they will be asked, in order: the Odoo address, the database name,
   their login, the API key (typed or pasted **without any characters showing**: just press Enter), then the **full host
   name** of the server once more, to test the connection. The right result is the line `login: ok`.
5. **Check from here.** Run `check-config` again: expect `config: file, private (permissions ok)` and a `project:` line.
   Then `ping`: ask the user to type the full host name (`--confirm-instance`), use exactly what they typed, and tell
   them the host, the Odoo version and the login that appear.
6. **Done.** Summarise in three lines what is installed, then tell the user: to build a KPI dashboard type
   `/odoo-kpi-skills:kpi` (and paste the address of their Excel after it, if they already have it).
