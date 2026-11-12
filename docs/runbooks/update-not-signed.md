# Bosun's own update was refused: not signed by a trusted key

**Looks like:** every refresh reports "update refused: <commit> is not signed by a trusted key" or "this
checkout has no trusted_signers file, so no update can be verified".

**Why:** Bosun fast-forwards its own checkout daily and then runs that code as you. So the commit it moves to
must carry an SSH signature from a key listed in `trusted_signers` at the repository root, and the list is read
from the commit the checkout is on now, never from the incoming one. Whoever can push to the repository still
cannot make this machine run their code.

**Effect:** Bosun stays on the commit it has and keeps working. The error repeats until it is resolved.

**Fix (the user decides this, never an agent):**
- The commit should have been signed: sign it (same key, or a key already listed) and push again.
- A new machine signs commits: add its public key to `trusted_signers` in a commit signed with a key already
  on the list.
- A fork: it needs its own `trusted_signers`; without one, self-update installs nothing.
- Unknown commit on the upstream branch: treat it as a possible compromise of the repository or account, and
  look at it before doing anything else.

Never edit `trusted_signers` locally, merge by hand or set `BOSUN_SELF_UPDATE=0` just to make the message go
away: the refusal is the protection working.
