@echo off
rem Launches Claude Code with the isolated profile ; extra arguments pass through.
setlocal
set "CLAUDE_CONFIG_DIR=G:\HomeProj\USERS\globalDefUser"
if not exist "%CLAUDE_CONFIG_DIR%" mkdir "%CLAUDE_CONFIG_DIR%"
rem The Godot project (and its .claude/, CLAUDE.md, git repo) lives in the g5 subfolder.
claude.exe %*
