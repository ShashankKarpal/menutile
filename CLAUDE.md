# Decision log

## 2026-09-14: menutile, the public release of a private menu bar renderer
- What this is: the generic SwiftBar/xbar-dialect renderer that runs a status script from inside its own signed .app bundle with its own LaunchAgent, extracted from a private menu bar host and sanitized for release. The private host keeps its fleet-specific plugins, rosters and installers; this repo carries only the renderer, a generic per-tile installer, the shared bundle builder, parser tests and the README's notification-identity explanation.
- Why a bundle (the ruling this repo exists to document): macOS presents banners and attributes process names only to real LaunchServices-registered .app bundles. An Info.plist beside the interpreter or the framework Python's own Python.app identity both yield an API that accepts notifications while the system files them without presenting them and without any System Settings entry. The objective test is UNUserNotificationCenter authorizationStatus, not a clean return.
- Pattern: bundle = copy of the uv-managed static CPython + Info.plist + optional icns + ad-hoc signature; stdlib and site-packages via PYTHONHOME and PYTHONPATH in the LaunchAgent; the renderer scrubs both from children. Known failure: a removed or moved PYTHONHOME exits 78 before Python runs a line, silently; the README tells the reader to read `launchctl list` and re-run install.sh.
- Differences from the private original, on purpose: the bash= allowlist is the script's own directory plus system bin dirs plus --allow-root (the original hard-codes the owner's roots); defaults for bundle id and label are com.menutile.<slug>; the installer takes one tile per invocation with a name-collision check; no fleet plugins.
- scripts/make_bundle.sh is a verbatim copy of the one in the switchdeck repo (public), which owns it; keep them byte-identical (cmp) when either moves.
- Naming: "menutile" was checked on 2026-09-14 against GitHub (no repos of that name), the process list, launchctl and /Applications before the folder was created.

## Rules
1. Never commit secrets; templates belong in *.example files.
2. Public text names no private repository of the author's; the original host's name stays out of this repo, its history and its commit messages.
3. End of session: delete unused code, review diffs before deleting.
