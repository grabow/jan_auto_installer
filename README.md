# Jan Config Installer

## Students: download the installer

Do not build this project yourself.
Download the prebuilt installer from the GitHub Releases page and run it on your machine:

- https://github.com/grabow/jan_auto_installer/releases/latest

This repository packages a preconfigured Jan.ai setup (proxy/gateway settings and assistants)
into a single executable using PyInstaller. The executable installs the configuration on a
student's machine after they install Jan.ai themselves.

## Disclaimer

- This project is not affiliated with Jan.ai.
- This repo contains organization-specific defaults (e.g. upstream gateway URL and assistants).
- Do not publish API keys. By default, the export strips API keys from the payload.

## What gets installed

- Provider/gateway configuration (from Jan's LocalStorage database)
- Assistants (from Jan's `assistants` folder)

## Prerequisites

- Jan.ai installed and run at least once on the target machine
- Jan.ai is closed while installing the config

## Create the payload (teacher machine)

1. Ensure your Jan.ai configuration is exactly as desired.
2. Export the payload:

```bash
uv run python jan_config_tool.py export
```

This creates `./jan_config_payload` with:

- `assistants/` (always)
- `localstorage.json` (only if Jan LocalStorage sqlite DB is found)

## Build the macOS executable

```bash
uv sync
uv run pyinstaller jan_config_tool.spec
```

Output:

- `dist/jan-config-install`

## GitHub Releases (students)

When you push a tag like `v1.2.3`, GitHub Actions builds Windows and macOS binaries and attaches them to the Release.

## GitHub CLI auth (maintainers)

This repo uses SSH for Git operations. Once SSH is set up, you can push tags and releases without re-authenticating each time.

Check GitHub CLI auth:

```bash
gh auth status
```

Login (if needed):

```bash
gh auth login -h github.com
```

Switch between saved accounts:

```bash
gh auth switch
```

Verify SSH auth:

```bash
ssh -T git@github.com
```

## Install on student machines

Copy `dist/jan-config-install` to the student machine and run (defaults to `install`):

```bash
./jan-config-install
```

If needed, you can set the HS-Offenburg API key during install (non-interactive):

```bash
./jan-config-install install --hs-offenburg-api-key "..."
```

Or via environment variable:

```bash
HS_OFFENBURG_API_KEY="..." ./jan-config-install
```

If you omit the key, the installer does not modify the HS-Offenburg API key. You can enter it later in Jan.

If `localstorage.json` or Jan's `localstorage.sqlite` is not found, install continues and only skips LocalStorage import.
To enforce strict behavior (fail when LocalStorage import is not possible):

```bash
./jan-config-install install --require-localstorage
```

By default the installer patches Jan to sort assistants in the "+" menu (Jan first, then Test-Assistent, then alphabetical).
To disable the patch:

```bash
./jan-config-install install --no-patch-assistant-sort
```

By default the executable uses the bundled `jan_config_payload`. If you want to override it:

```bash
./jan-config-install --payload-dir /path/to/jan_config_payload
```

## Notes

- The payload strips API keys by default. If you want to include them, export with:

```bash
uv run python jan_config_tool.py export --keep-api-keys
```

- A backup of the existing assistants folder can be created by running:

```bash
./jan-config-install --backup
```

- Sorting patch details:
  The installer patches Jan's assistant extension to enforce ordering in the "+" menu by default.
  It resolves the extension path from Jan's data directory and, if needed, searches for
  `extensions/@janhq/assistant-extension/dist/index.js` under the user's home directory,
  but only accepts matches inside `.../Jan/data/...` to avoid patching unrelated files.
  Use `--no-patch-assistant-sort` to disable.

- If no LocalStorage sqlite DB is found during export, the payload is still exported
  with `assistants/` only and without `localstorage.json`.

## Full Uninstall (macOS)

To completely remove Jan and all local data, run:

```bash
rm -rf /Applications/Jan.app \
  ~/Library/Application\ Support/Jan \
  ~/Library/WebKit/jan.ai.app \
  ~/Library/Caches/jan.ai.app \
  ~/Library/Logs/jan.ai.app \
  ~/Library/Preferences/jan.ai.app.plist
```

Verify removal:

```bash
ls -la /Applications/Jan.app \
  ~/Library/Application\ Support/Jan \
  ~/Library/WebKit/jan.ai.app \
  ~/Library/Caches/jan.ai.app \
  ~/Library/Logs/jan.ai.app \
  ~/Library/Preferences/jan.ai.app.plist
```
