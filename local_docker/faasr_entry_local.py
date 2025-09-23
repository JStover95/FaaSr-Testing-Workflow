#!/usr/bin/env python3
"""
Custom FaaSr entrypoint that uses LocalScheduler instead of the standard Scheduler.
This file patches the Scheduler import to use LocalScheduler without modifying the original entrypoint.
"""

import sys

# Add the current directory to Python path to find local_scheduler
sys.path.insert(0, "/action")

from local_scheduler import LocalScheduler

print("Patching Scheduler!")

import faasr_entry

faasr_entry.Scheduler = LocalScheduler

if __name__ == "__main__":
    faasr_entry.handler()
