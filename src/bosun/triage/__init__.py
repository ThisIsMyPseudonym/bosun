"""Error triage: one cause and one next step for every failure.

Triage inspects failed runs, upgrades, refreshes, and fixes to produce a structured Verdict
explaining the likely cause and suggesting one next step chosen from actions Bosun can already
perform. Triage never acts on its own: every suggested step requires a user click.
"""
