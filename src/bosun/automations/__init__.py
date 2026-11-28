"""Owner-written automations: when a trigger fires, if its conditions hold, do one Bosun action.

Every action an automation takes runs through `Actions.permit` with actor `automation:<id>`, never around it.
"""
