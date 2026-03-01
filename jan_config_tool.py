import argparse
import datetime as dt
import glob
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple


LOCALSTORAGE_KEYS = [
    "model-provider",
    "last-used-model",
    "last-used-assistant",
    "tool-availability",
]


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path))).resolve()


def _detect_data_dir(override: Optional[str] = None) -> Optional[Path]:
    if override:
        candidate = _expand(override)
        return candidate if candidate.exists() else None

    candidates = []
    if _is_macos():
        candidates.append("~/Library/Application Support/Jan/data")
    if _is_windows():
        appdata = os.environ.get("APPDATA")
        localappdata = os.environ.get("LOCALAPPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "Jan", "data"))
        if localappdata:
            candidates.append(os.path.join(localappdata, "Jan", "data"))
    if _is_linux():
        candidates.append("~/.local/share/Jan/data")

    for path in candidates:
        candidate = _expand(path)
        if candidate.exists():
            return candidate
    return None


def _localstorage_sqlite_candidates() -> Iterable[Path]:
    patterns = []
    if _is_macos():
        base = _expand("~/Library/WebKit/jan.ai.app/WebsiteData")
        patterns.append(str(base / "Default" / "**" / "LocalStorage" / "localstorage.sqlite3"))
    if _is_linux():
        base = _expand("~/.local/share/jan.ai.app/WebKit/WebsiteData")
        patterns.append(str(base / "Default" / "**" / "LocalStorage" / "localstorage.sqlite3"))
    if _is_windows():
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            base = Path(localappdata) / "jan.ai.app" / "WebKit" / "WebsiteData"
            patterns.append(str(base / "Default" / "**" / "LocalStorage" / "localstorage.sqlite3"))

    for pattern in patterns:
        for match in glob.glob(pattern, recursive=True):
            path = Path(match)
            if path.is_file():
                yield path


def _decode_localstorage_value(raw: bytes) -> Optional[str]:
    try:
        return raw.decode("utf-16le")
    except Exception:
        try:
            return raw.decode("utf-8")
        except Exception:
            return None


def _encode_localstorage_value(value: str) -> bytes:
    return value.encode("utf-16le")


def _read_localstorage_keys(db_path: Path, keys: Iterable[str]) -> Dict[str, str]:
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.execute("select key, value from ItemTable")
        rows = cur.fetchall()
    finally:
        con.close()

    results: Dict[str, str] = {}
    key_set = set(keys)
    for key, raw in rows:
        if key not in key_set:
            continue
        value = _decode_localstorage_value(raw)
        if value is not None:
            results[key] = value
    return results


def _write_localstorage_keys(db_path: Path, values: Dict[str, str]) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("create table if not exists ItemTable (key text primary key, value blob)")
        for key, value in values.items():
            con.execute(
                "insert or replace into ItemTable (key, value) values (?, ?)",
                (key, _encode_localstorage_value(value)),
            )
        con.commit()
    finally:
        con.close()


def _detect_localstorage_sqlite() -> Optional[Path]:
    candidates = list(_localstorage_sqlite_candidates())
    if not candidates:
        return None

    def score(path: Path) -> Tuple[int, float]:
        try:
            con = sqlite3.connect(str(path))
            cur = con.execute("select key from ItemTable")
            keys = {row[0] for row in cur.fetchall()}
            con.close()
        except Exception:
            keys = set()
        match_score = int("model-provider" in keys)
        mtime = path.stat().st_mtime
        return (match_score, mtime)

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.bak-{_timestamp()}")


def _copy_assistants(src: Path, dst: Path, backup: bool) -> None:
    if not src.exists():
        return
    if dst.exists() and backup:
        backup_path = _backup_path(dst)
        shutil.copytree(dst, backup_path)
    _ensure_dir(dst.parent)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _sanitize_model_provider(value: str, keep_api_keys: bool) -> str:
    if keep_api_keys:
        return value
    try:
        data = json.loads(value)
    except Exception:
        return value

    def scrub(obj):
        if isinstance(obj, dict):
            for k in list(obj.keys()):
                if k in {"api_key", "api-key"}:
                    obj[k] = ""
            for v in obj.values():
                scrub(v)
            if obj.get("key") == "api-key":
                props = obj.get("controller_props")
                if isinstance(props, dict) and "value" in props:
                    props["value"] = ""
        elif isinstance(obj, list):
            for v in obj:
                scrub(v)

    scrub(data)
    return json.dumps(data, ensure_ascii=True)


def _payload_has_api_keys(value: str) -> bool:
    try:
        data = json.loads(value)
    except Exception:
        return False

    def has_key(obj) -> bool:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in {"api_key", "api-key"} and isinstance(v, str) and v.strip():
                    return True
                if k == "key" and v == "api-key":
                    props = obj.get("controller_props")
                    if isinstance(props, dict):
                        raw = props.get("value")
                        if isinstance(raw, str) and raw.strip():
                            return True
                if has_key(v):
                    return True
            return False
        if isinstance(obj, list):
            return any(has_key(v) for v in obj)
        return False

    return has_key(data)


def _set_hs_offenburg_api_key(model_provider_value: str, api_key: str) -> str:
    try:
        data = json.loads(model_provider_value)
    except Exception:
        return model_provider_value

    state = data.get("state")
    if not isinstance(state, dict):
        return model_provider_value
    providers = state.get("providers")
    if not isinstance(providers, list):
        return model_provider_value

    for provider in providers:
        if not isinstance(provider, dict):
            continue
        if provider.get("provider") != "HS-Offenburg":
            continue

        provider["api_key"] = api_key

        settings = provider.get("settings")
        if isinstance(settings, list):
            for setting in settings:
                if not isinstance(setting, dict):
                    continue
                if setting.get("key") != "api-key":
                    continue
                props = setting.get("controller_props")
                if isinstance(props, dict):
                    props["value"] = api_key
        break

    return json.dumps(data, ensure_ascii=True)


def _default_payload_dir() -> str:
    if getattr(sys, "frozen", False):
        bundle_root = Path(getattr(sys, "_MEIPASS", ""))
        bundled = bundle_root / "jan_config_payload"
        if bundled.exists():
            return str(bundled)
    return "./jan_config_payload"


def export_payload(args: argparse.Namespace) -> int:
    data_dir = _detect_data_dir(args.data_dir)
    if not data_dir:
        print("Jan data directory not found.")
        return 1

    payload_dir = _expand(args.payload_dir)
    _ensure_dir(payload_dir)

    assistants_src = data_dir / "assistants"
    assistants_dst = payload_dir / "assistants"
    _copy_assistants(assistants_src, assistants_dst, backup=False)

    db_path = _detect_localstorage_sqlite()
    if not db_path:
        print("LocalStorage sqlite database not found.")
        return 1

    values = _read_localstorage_keys(db_path, LOCALSTORAGE_KEYS)
    if "model-provider" in values:
        values["model-provider"] = _sanitize_model_provider(
            values["model-provider"], keep_api_keys=args.keep_api_keys
        )

    payload = {"localstorage": values}
    with (payload_dir / "localstorage.json").open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=True)

    contains_keys = False
    if "model-provider" in values:
        contains_keys = _payload_has_api_keys(values["model-provider"])
    if contains_keys:
        print("WARNING: Payload still contains API keys.")
    else:
        print("Payload does not contain API keys.")

    print(f"Exported payload to {payload_dir}")
    return 0


def install_payload(args: argparse.Namespace) -> int:
    payload_dir = _expand(args.payload_dir)
    payload_file = payload_dir / "localstorage.json"
    if not payload_file.exists():
        print("Payload localstorage.json not found.")
        return 1

    data_dir = _detect_data_dir(args.data_dir)
    if not data_dir:
        print("Jan data directory not found.")
        return 1

    assistants_src = payload_dir / "assistants"
    assistants_dst = data_dir / "assistants"
    _copy_assistants(assistants_src, assistants_dst, backup=args.backup)

    db_path = _detect_localstorage_sqlite()
    if not db_path:
        print("LocalStorage sqlite database not found.")
        return 1

    with payload_file.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    values = payload.get("localstorage", {})
    if not isinstance(values, dict):
        print("Invalid payload format.")
        return 1

    hs_key: Optional[str]
    if args.hs_offenburg_api_key is not None:
        hs_key = args.hs_offenburg_api_key
    else:
        hs_key = os.environ.get("HS_OFFENBURG_API_KEY")

    if hs_key is not None and hs_key.strip() and isinstance(values.get("model-provider"), str):
        values["model-provider"] = _set_hs_offenburg_api_key(values["model-provider"], hs_key.strip())

    _write_localstorage_keys(db_path, values)
    if args.patch_assistant_sort:
        _patch_assistant_extension_sorting(data_dir)
    print("Config installed successfully.")
    return 0


def _candidate_extension_paths(data_dir: Optional[Path]) -> Iterable[Path]:
    if data_dir:
        yield data_dir / "extensions" / "@janhq" / "assistant-extension" / "dist" / "index.js"

    if _is_macos():
        yield _expand(
            "~/Library/Application Support/Jan/data/extensions/"
            "@janhq/assistant-extension/dist/index.js"
        )
    if _is_linux():
        yield _expand(
            "~/.local/share/Jan/data/extensions/"
            "@janhq/assistant-extension/dist/index.js"
        )
    if _is_windows():
        appdata = os.environ.get("APPDATA")
        localappdata = os.environ.get("LOCALAPPDATA")
        if appdata:
            yield _expand(
                os.path.join(
                    appdata,
                    "Jan",
                    "data",
                    "extensions",
                    "@janhq",
                    "assistant-extension",
                    "dist",
                    "index.js",
                )
            )
        if localappdata:
            yield _expand(
                os.path.join(
                    localappdata,
                    "Jan",
                    "data",
                    "extensions",
                    "@janhq",
                    "assistant-extension",
                    "dist",
                    "index.js",
                )
            )


def _find_assistant_extension_index(data_dir: Optional[Path]) -> Optional[Path]:
    for candidate in _candidate_extension_paths(data_dir):
        if candidate.exists():
            return candidate

    roots = []
    home = os.path.expanduser("~")
    if home:
        roots.append(home)
    for root in roots:
        pattern = os.path.join(
            root,
            "**",
            "extensions",
            "@janhq",
            "assistant-extension",
            "dist",
            "index.js",
        )
        matches = sorted(glob.glob(pattern, recursive=True))
        for match in matches:
            norm = match.replace("\\", "/")
            if "/Jan/data/" in norm:
                return Path(match)
    return None


def _patch_assistant_extension_sorting(data_dir: Optional[Path]) -> None:
    ext_path = _find_assistant_extension_index(data_dir)
    if not ext_path:
        return
    text = ext_path.read_text(encoding="utf-8")
    if "test-assistent" in text and "assistantsData.sort" in text:
        return
    needle = "\t\treturn assistantsData;\n\t}"
    if needle not in text:
        return
    patch = (
        "\t\tassistantsData.sort((a, b) => {\n"
        "\t\t\tif (a.id === \"jan\") return -1;\n"
        "\t\t\tif (b.id === \"jan\") return 1;\n"
        "\t\t\tif (a.id === \"test-assistent\") return -1;\n"
        "\t\t\tif (b.id === \"test-assistent\") return 1;\n"
        "\t\t\tconst aKey = (a.name || a.id || \"\").toLowerCase();\n"
        "\t\t\tconst bKey = (b.name || b.id || \"\").toLowerCase();\n"
        "\t\t\treturn aKey.localeCompare(bKey, \"de\");\n"
        "\t\t});\n"
        "\t\treturn assistantsData;\n\t}"
    )
    ext_path.write_text(text.replace(needle, patch), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export and install Jan.ai configuration.")
    parser.add_argument("--data-dir", help="Override Jan data directory")

    sub = parser.add_subparsers(dest="command", required=True)

    export_p = sub.add_parser("export", help="Export configuration to payload directory")
    export_p.add_argument(
        "--payload-dir",
        default=_default_payload_dir(),
        help="Output directory for the payload",
    )
    export_p.add_argument(
        "--keep-api-keys",
        action="store_true",
        help="Keep API keys in the exported payload",
    )
    export_p.set_defaults(func=export_payload)

    install_p = sub.add_parser("install", help="Install configuration from payload directory")
    install_p.add_argument(
        "--payload-dir",
        default=_default_payload_dir(),
        help="Payload directory to install from",
    )
    install_p.add_argument(
        "--backup",
        action="store_true",
        help="Backup existing assistants directory",
    )
    install_p.add_argument(
        "--no-patch-assistant-sort",
        action="store_false",
        dest="patch_assistant_sort",
        default=True,
        help="Disable assistant sorting patch (enabled by default)",
    )
    install_p.add_argument(
        "--hs-offenburg-api-key",
        default=None,
        help="Set HS-Offenburg API key (or use env HS_OFFENBURG_API_KEY)",
    )
    install_p.set_defaults(func=install_payload)

    return parser


def main() -> int:
    parser = build_parser()
    if getattr(sys, "frozen", False) and len(sys.argv) == 1:
        sys.argv.append("install")
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

