# winget upgrade fails: hash mismatch or "Installer failed"

Bosun runs `winget upgrade --id <id> --exact --silent --accept-package-agreements --accept-source-agreements
--disable-interactivity`. `list_jobs` gives a hint for each failed job; `diagnose` has winget's own output.

| winget says | What it means | What to do |
|---|---|---|
| `Installer hash does not match` | The vendor replaced the download before winget's catalog caught up. winget refuses it, and that is the safe result. | Wait for the catalog to update (usually within a day) and retry. Do not override the hash check. |
| `exit code: 1618`, `0x80070652` | Another installer is running. | Wait for it to finish, retry. |
| `0x80073d28`, "administrator privileges are required", `0x80070005`, "Access is denied", `1223`, "cancelled by the user" | Windows asked for administrator approval (UAC) and nobody approved it. | The user retries at the PC and approves the prompt. |
| "install technology is different", "version number cannot be determined" | The app was installed another way, or updates itself. | Update it from inside the app. |
| `exit code: 1641`, `exit code: 3010` | Installed; Windows needs a restart to finish. | Restart when convenient. |
| "No applicable upgrade found" | Already current. | Nothing; the next refresh clears it. |
| "in use", "being used by another process", `Installer failed` with exit code 1 on Git, VirtualBox or Docker Desktop | Files are in use. | Close the app (shut down VMs, quit Docker Desktop, close terminals running git), retry. |
| `timed out after ...s` | The installer sat waiting, usually on a prompt. | Retry while at the PC. |

Bosun refuses to start an upgrade of VirtualBox, Docker Desktop or Git while they are running, because their
installers force-close them.
