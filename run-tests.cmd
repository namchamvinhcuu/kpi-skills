@echo off
rem Runs the tests of the plugin with the Python standard library only (Windows).
cd /d "%~dp0"
py -3 -m unittest discover -s tests -v
