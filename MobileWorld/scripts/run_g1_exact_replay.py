#!/usr/bin/env python3
"""Entrypoint for the CPU-only G1.4 exact-request replay tooling."""

from mobile_world.offline.causal_replay_runner.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
