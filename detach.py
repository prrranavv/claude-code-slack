#!/usr/bin/env python3
"""Detach from parent process group via setsid, then exec the given command.

macOS has no `setsid` binary. launchd reaps the entire process group of the
launched job when the main process exits, which would kill in-flight workers.
This helper calls os.setsid() to move each worker into a new session so it
survives daemon restarts.
"""
import os
import sys

if len(sys.argv) < 2:
    sys.stderr.write("usage: detach.py <command> [args...]\n")
    sys.exit(2)

os.setsid()

devnull = os.open(os.devnull, os.O_RDWR)
os.dup2(devnull, 0)
os.dup2(devnull, 1)
os.dup2(devnull, 2)
if devnull > 2:
    os.close(devnull)

os.execvp(sys.argv[1], sys.argv[1:])
