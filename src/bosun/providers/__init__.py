"""Every AI call Bosun makes goes through here.

Bosun can use any AI subscription installed on the machine (Claude Code, Codex, Gemini CLI, Grok, Antigravity,
Muse). This package says what each CLI can do (`catalog`), how to call it locked down (`argv`), whether the
installed version still honours that lockdown (`contract`) and which logins it has (`accounts`). Nothing else in
Bosun starts an AI CLI on its own once the later phases land.
"""
