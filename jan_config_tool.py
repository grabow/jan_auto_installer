import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

try:
    from build_version import __version__ as APP_VERSION
except Exception:
    APP_VERSION = "dev"

try:
    import plyvel
except Exception:
    plyvel = None


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


def _localstorage_sqlite_candidates(data_dir: Optional[Path] = None) -> Iterable[Path]:
    patterns = []
    if _is_macos():
        roots = [
            _expand("~/Library/WebKit/jan.ai.app/WebsiteData"),
            _expand("~/Library/WebKit/Jan.ai.app/WebsiteData"),
            _expand("~/Library/Application Support/jan.ai.app/WebKit/WebsiteData"),
            _expand("~/Library/Application Support/Jan.ai.app/WebKit/WebsiteData"),
        ]
        if data_dir:
            roots.append(data_dir.parent.parent / "jan.ai.app" / "WebKit" / "WebsiteData")
            roots.append(data_dir.parent.parent / "Jan.ai.app" / "WebKit" / "WebsiteData")
        for root in roots:
            patterns.extend(
                [
                    str(root / "Default" / "**" / "LocalStorage" / "localstorage.sqlite3"),
                    str(root / "Default" / "**" / "LocalStorage" / "localstorage.sqlite"),
                    str(root / "Default" / "**" / "Local Storage" / "localstorage.sqlite3"),
                    str(root / "Default" / "**" / "Local Storage" / "localstorage.sqlite"),
                ]
            )
    if _is_linux():
        base = _expand("~/.local/share/jan.ai.app/WebKit/WebsiteData")
        patterns.append(str(base / "Default" / "**" / "LocalStorage" / "localstorage.sqlite3"))
    if _is_windows():
        appdata = os.environ.get("APPDATA")
        localappdata = os.environ.get("LOCALAPPDATA")
        roots = []
        if data_dir:
            roots.append(data_dir)
            roots.append(data_dir.parent)
        if appdata:
            roots.append(Path(appdata) / "Jan")
            roots.append(Path(appdata) / "jan.ai.app")
        if localappdata:
            roots.append(Path(localappdata) / "Jan")
            roots.append(Path(localappdata) / "jan.ai.app")

        for root in roots:
            patterns.extend(
                [
                    str(root / "localstorage.sqlite3"),
                    str(root / "localstorage.sqlite"),
                    str(root / "data" / "localstorage.sqlite3"),
                    str(root / "data" / "localstorage.sqlite"),
                    str(root / "data" / "db" / "*.db"),
                    str(root / "**" / "localstorage.sqlite3"),
                    str(root / "**" / "localstorage.sqlite"),
                    str(root / "**" / "LocalStorage" / "localstorage.sqlite3"),
                    str(root / "**" / "LocalStorage" / "localstorage.sqlite"),
                    str(root / "**" / "Local Storage" / "localstorage.sqlite3"),
                    str(root / "**" / "Local Storage" / "localstorage.sqlite"),
                ]
            )

    seen = set()
    for pattern in patterns:
        for match in glob.glob(pattern, recursive=True):
            path = Path(match)
            if path.is_file() and path not in seen:
                seen.add(path)
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


def _detect_localstorage_sqlite(data_dir: Optional[Path] = None) -> Optional[Path]:
    candidates = list(_localstorage_sqlite_candidates(data_dir=data_dir))
    if not candidates:
        return None

    def score(path: Path) -> Tuple[int, int, float]:
        has_item_table = False
        has_model_provider = False
        try:
            con = sqlite3.connect(str(path))
            cur = con.execute(
                "select 1 from sqlite_master where type='table' and name='ItemTable' limit 1"
            )
            has_item_table = cur.fetchone() is not None
            if has_item_table:
                cur = con.execute("select 1 from ItemTable where key='model-provider' limit 1")
                has_model_provider = cur.fetchone() is not None
            con.close()
        except Exception:
            has_item_table = False
            has_model_provider = False
        mtime = path.stat().st_mtime
        return (int(has_item_table), int(has_model_provider), mtime)

    scored = [(path, score(path)) for path in candidates]
    valid = [(path, s) for path, s in scored if s[0] == 1]
    if not valid:
        return None
    valid.sort(key=lambda item: (item[1][1], item[1][2]), reverse=True)
    return valid[0][0]


def _iter_windows_webview_db_candidates() -> Iterable[Path]:
    if not _is_windows():
        return

    localappdata = os.environ.get("LOCALAPPDATA")
    if not localappdata:
        return

    roots = [
        Path(localappdata) / "jan.ai.app" / "EBWebView" / "Default",
        Path(localappdata) / "Jan.ai.app" / "EBWebView" / "Default",
    ]
    seen = set()
    for root in roots:
        for name in ("History", "Web Data"):
            candidate = root / name
            if candidate.is_file() and candidate not in seen:
                seen.add(candidate)
                yield candidate


def _iter_windows_localstorage_leveldb_candidates(data_dir: Optional[Path] = None) -> Iterable[Path]:
    if not _is_windows():
        return

    roots = []
    if data_dir:
        roots.append(data_dir.parent.parent / "jan.ai.app" / "EBWebView" / "Default")
        roots.append(data_dir.parent.parent / "Jan.ai.app" / "EBWebView" / "Default")

    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        roots.append(Path(localappdata) / "jan.ai.app" / "EBWebView" / "Default")
        roots.append(Path(localappdata) / "Jan.ai.app" / "EBWebView" / "Default")

    seen = set()
    for root in roots:
        candidate = root / "Local Storage" / "leveldb"
        if candidate.is_dir() and (candidate / "CURRENT").exists() and candidate not in seen:
            seen.add(candidate)
            yield candidate


def _encode_leveldb_localstorage_value(value: str, existing: Optional[bytes]) -> bytes:
    prefix = b"\x01"
    if existing and isinstance(existing, (bytes, bytearray)) and len(existing) > 0:
        prefix = bytes(existing[:1])
    return prefix + value.encode("utf-8")


def _patch_windows_leveldb_localstorage(
    values: Dict[str, str],
    backup: bool,
    data_dir: Optional[Path] = None,
) -> Tuple[int, int, int]:
    localstorage_values = {
        key: value
        for key, value in values.items()
        if key in set(LOCALSTORAGE_KEYS) and isinstance(value, str)
    }
    if not localstorage_values:
        return (0, 0, 0)
    if plyvel is None:
        return (0, 0, 0)

    db_dirs = list(_iter_windows_localstorage_leveldb_candidates(data_dir=data_dir))
    if not db_dirs:
        return (0, 0, 0)

    target_suffixes = {
        key: (b"\x01" + key.encode("utf-8"))
        for key in localstorage_values
    }

    patched_dbs = 0
    patched_keys = 0
    synced_keys = 0
    for db_dir in db_dirs:
        if backup:
            try:
                shutil.copytree(db_dir, _backup_path(db_dir))
            except Exception:
                pass

        db = None
        try:
            db = plyvel.DB(str(db_dir), create_if_missing=False)
            key_map: Dict[str, list[bytes]] = {key: [] for key in localstorage_values}
            default_prefix = b"_http://tauri.localhost\x00"

            for raw_key, _ in db:
                if not isinstance(raw_key, (bytes, bytearray)):
                    continue
                key_bytes = bytes(raw_key)
                if key_bytes.startswith(b"_") and b"\x01" in key_bytes:
                    split_at = key_bytes.rfind(b"\x01")
                    if split_at > 0:
                        default_prefix = key_bytes[:split_at]
                for name, suffix in target_suffixes.items():
                    if key_bytes.endswith(suffix):
                        key_map[name].append(key_bytes)

            db_updates = 0
            with db.write_batch() as wb:
                for key_name, new_value in localstorage_values.items():
                    candidate_keys = key_map.get(key_name) or []
                    if not candidate_keys:
                        candidate_keys = [default_prefix + target_suffixes[key_name]]

                    for storage_key in candidate_keys:
                        old_value = db.get(storage_key)
                        encoded = _encode_leveldb_localstorage_value(new_value, old_value)
                        if old_value == encoded:
                            synced_keys += 1
                            continue
                        wb.put(storage_key, encoded)
                        db_updates += 1

            if db_updates > 0:
                patched_dbs += 1
                patched_keys += db_updates
        except Exception:
            pass
        finally:
            if db is not None:
                db.close()

    return (patched_dbs, patched_keys, synced_keys)


def _decode_db_value(raw: object) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
        for encoding in ("utf-8", "utf-16le", "latin-1"):
            try:
                return data.decode(encoding)
            except Exception:
                pass
    return None


def _encode_db_value(value: str, original: object) -> object:
    if isinstance(original, str):
        return value
    if isinstance(original, memoryview):
        original = original.tobytes()
    if isinstance(original, (bytes, bytearray)):
        data = bytes(original)
        for encoding in ("utf-8", "utf-16le"):
            try:
                data.decode(encoding)
                return value.encode(encoding)
            except Exception:
                pass
        return value.encode("utf-8")
    return value


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _json_load_layers(text: str, max_layers: int = 2) -> Tuple[object, int]:
    parsed: object = text
    layers = 0
    while layers < max_layers and isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
            layers += 1
        except Exception:
            break
    return parsed, layers


def _json_dump_layers(value: object, layers: int) -> Optional[str]:
    dumped: object = value
    for _ in range(layers):
        dumped = json.dumps(dumped, ensure_ascii=True)
    if isinstance(dumped, str):
        return dumped
    return None


def _looks_like_model_provider_payload_obj(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    if isinstance(obj.get("providers"), list):
        return True

    state = obj.get("state")
    if isinstance(state, dict):
        if isinstance(state.get("providers"), list):
            return True
        if isinstance(state.get("selectedProvider"), str):
            return True

    return isinstance(obj.get("selectedProvider"), str)


def _looks_like_model_provider_payload(text: str) -> bool:
    parsed, layers = _json_load_layers(text, max_layers=2)
    if layers == 0:
        return False
    return _looks_like_model_provider_payload_obj(parsed)


def _maybe_patch_json_payload_text(text: str, values: Dict[str, str]) -> Optional[str]:
    if not text:
        return None

    parsed, layers = _json_load_layers(text, max_layers=2)
    if layers == 0:
        if (
            "model-provider" in values
            and _looks_like_model_provider_payload(text)
            and text != values["model-provider"]
        ):
            return values["model-provider"]
        return None

    changed = False
    patched: object = parsed
    if isinstance(patched, dict):
        for key, replacement in values.items():
            current = patched.get(key)
            if isinstance(current, str) and current != replacement:
                patched[key] = replacement
                changed = True

        nested_localstorage = patched.get("localstorage")
        if isinstance(nested_localstorage, dict):
            for key, replacement in values.items():
                current = nested_localstorage.get(key)
                if isinstance(current, str) and current != replacement:
                    nested_localstorage[key] = replacement
                    changed = True

        model_provider_value = values.get("model-provider")
        if isinstance(model_provider_value, str) and _looks_like_model_provider_payload_obj(patched):
            replacement_obj: object = model_provider_value
            try:
                replacement_obj = json.loads(model_provider_value)
            except Exception:
                replacement_obj = model_provider_value
            if patched != replacement_obj:
                patched = replacement_obj
                changed = True
    elif isinstance(patched, str):
        model_provider_value = values.get("model-provider")
        if (
            isinstance(model_provider_value, str)
            and _looks_like_model_provider_payload(patched)
            and patched != model_provider_value
        ):
            patched = model_provider_value
            changed = True
    else:
        return None

    if not changed:
        return None
    return _json_dump_layers(patched, layers)


def _is_key_like_column(name: str) -> bool:
    normalized = name.lower().replace("-", "_").strip()
    if normalized in {"key", "name", "setting", "setting_key", "pref_key", "path"}:
        return True
    return normalized.endswith("_key")


def _is_value_like_column(name: str) -> bool:
    normalized = name.lower().replace("-", "_").strip()
    if normalized in {"value", "data", "payload", "json", "content", "state"}:
        return True
    return normalized.endswith("_value") or normalized.startswith("value_")


def _is_text_or_blob_column(col_type: str) -> bool:
    normalized = (col_type or "").upper().strip()
    if not normalized:
        return True
    return any(token in normalized for token in ("CHAR", "CLOB", "TEXT", "BLOB"))


def _looks_relevant_payload_text(text: str) -> bool:
    if not text:
        return False
    markers = (
        "model-provider",
        "last-used-model",
        "last-used-assistant",
        "tool-availability",
        "selectedProvider",
        "providers",
    )
    if any(marker in text for marker in markers):
        return True
    stripped = text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[") or stripped.startswith('"')


def _patch_windows_webview_key_value_rows(
    con: sqlite3.Connection,
    table_name: str,
    key_col: str,
    value_col: str,
    values: Dict[str, str],
) -> int:
    if not values:
        return 0

    q_table = _quote_ident(table_name)
    q_key = _quote_ident(key_col)
    q_value = _quote_ident(value_col)
    placeholders = ", ".join("?" for _ in values)
    sql = (
        f"select rowid, {q_key}, {q_value} from {q_table} "
        f"where {q_key} in ({placeholders}) and {q_value} is not null"
    )
    try:
        rows = con.execute(sql, tuple(values.keys())).fetchall()
    except Exception:
        return 0

    updates = []
    for rowid, raw_key, raw_value in rows:
        key = _decode_db_value(raw_key)
        if not isinstance(key, str):
            continue
        replacement = values.get(key)
        if not isinstance(replacement, str):
            continue
        current = _decode_db_value(raw_value)
        if current is None or current == replacement:
            continue
        updates.append((_encode_db_value(replacement, raw_value), rowid))

    if not updates:
        return 0
    try:
        con.executemany(
            f"update {q_table} set {q_value} = ? where rowid = ?",
            updates,
        )
    except Exception:
        return 0
    return len(updates)


def _patch_windows_webview_json_rows(
    con: sqlite3.Connection,
    table_name: str,
    col_name: str,
    values: Dict[str, str],
) -> int:
    q_table = _quote_ident(table_name)
    q_col = _quote_ident(col_name)
    sql = (
        f"select rowid, {q_col} from {q_table} "
        f"where {q_col} is not null and length({q_col}) > 1 and length({q_col}) <= 300000"
    )
    try:
        cur = con.execute(sql)
    except Exception:
        return 0

    updates = []
    while True:
        rows = cur.fetchmany(512)
        if not rows:
            break
        for rowid, raw in rows:
            text = _decode_db_value(raw)
            if not isinstance(text, str) or not _looks_relevant_payload_text(text):
                continue
            patched = _maybe_patch_json_payload_text(text, values)
            if not isinstance(patched, str) or patched == text:
                continue
            updates.append((_encode_db_value(patched, raw), rowid))

    if not updates:
        return 0
    try:
        con.executemany(
            f"update {q_table} set {q_col} = ? where rowid = ?",
            updates,
        )
    except Exception:
        return 0
    return len(updates)


def _patch_windows_webview_localstorage(values: Dict[str, str], backup: bool) -> Tuple[int, int]:
    localstorage_values = {
        key: value
        for key, value in values.items()
        if key in set(LOCALSTORAGE_KEYS) and isinstance(value, str)
    }
    if not localstorage_values:
        return (0, 0)

    dbs = list(_iter_windows_webview_db_candidates())
    if not dbs:
        return (0, 0)

    patched_dbs = 0
    patched_rows = 0
    for db_path in dbs:
        if backup:
            try:
                shutil.copy2(db_path, _backup_path(db_path))
            except Exception:
                pass

        con = sqlite3.connect(str(db_path))
        db_updates = 0
        try:
            table_rows = con.execute(
                "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
            ).fetchall()
            for (table_name,) in table_rows:
                try:
                    cols = con.execute(f"pragma table_info({_quote_ident(table_name)})").fetchall()
                except Exception:
                    continue

                key_like_cols = [col[1] for col in cols if _is_key_like_column(col[1])]
                value_like_cols = [
                    col[1]
                    for col in cols
                    if col[1] not in key_like_cols and _is_value_like_column(col[1])
                ]
                for key_col in key_like_cols:
                    for value_col in value_like_cols:
                        db_updates += _patch_windows_webview_key_value_rows(
                            con,
                            table_name,
                            key_col,
                            value_col,
                            localstorage_values,
                        )

                for col in cols:
                    col_name = col[1]
                    col_type = col[2] if len(col) > 2 else ""
                    if not _is_text_or_blob_column(col_type):
                        continue
                    db_updates += _patch_windows_webview_json_rows(
                        con,
                        table_name,
                        col_name,
                        localstorage_values,
                    )

            if db_updates > 0:
                con.commit()
                patched_dbs += 1
                patched_rows += db_updates
            else:
                con.rollback()
        except Exception:
            con.rollback()
        finally:
            con.close()

    return (patched_dbs, patched_rows)


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

    payload_file = payload_dir / "localstorage.json"
    db_path = _detect_localstorage_sqlite(data_dir=data_dir)
    if not db_path:
        if payload_file.exists():
            payload_file.unlink()
        print(
            "WARNING: LocalStorage sqlite database not found. "
            "Exported assistants only. Configure provider manually in Jan."
        )
        print(f"Exported payload to {payload_dir}")
        return 0

    values = _read_localstorage_keys(db_path, LOCALSTORAGE_KEYS)
    if "model-provider" in values:
        values["model-provider"] = _sanitize_model_provider(
            values["model-provider"], keep_api_keys=args.keep_api_keys
        )

    payload = {"localstorage": values}
    with payload_file.open("w", encoding="utf-8") as f:
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

    data_dir = _detect_data_dir(args.data_dir)
    if not data_dir:
        print("Jan data directory not found.")
        return 1

    assistants_src = payload_dir / "assistants"
    assistants_dst = data_dir / "assistants"
    _copy_assistants(assistants_src, assistants_dst, backup=args.backup)

    values: Dict[str, str] = {}
    if payload_file.exists():
        with payload_file.open("r", encoding="utf-8") as f:
            payload = json.load(f)

        parsed_values = payload.get("localstorage", {})
        if not isinstance(parsed_values, dict):
            print("Invalid payload format.")
            return 1
        values = parsed_values
    elif args.require_localstorage:
        print("Payload localstorage.json not found.")
        return 1
    else:
        print(
            "WARNING: Payload localstorage.json not found. "
            "Skipping LocalStorage import. Configure provider manually in Jan."
        )

    if values:
        hs_key: Optional[str]
        if args.hs_offenburg_api_key is not None:
            hs_key = args.hs_offenburg_api_key
        else:
            hs_key = os.environ.get("HS_OFFENBURG_API_KEY")

        if hs_key is not None and hs_key.strip() and isinstance(values.get("model-provider"), str):
            values["model-provider"] = _set_hs_offenburg_api_key(
                values["model-provider"], hs_key.strip()
            )

    imported_localstorage = False
    bootstrap_localstorage: Optional[Dict[str, str]] = None
    if values:
        db_path = _detect_localstorage_sqlite(data_dir=data_dir)
        if db_path:
            _write_localstorage_keys(db_path, values)
            imported_localstorage = True
        elif _is_windows():
            patched_dbs, patched_keys, synced_keys = _patch_windows_leveldb_localstorage(
                values,
                backup=args.backup,
                data_dir=data_dir,
            )
            if patched_keys > 0 or synced_keys > 0:
                if patched_keys > 0:
                    print(
                        f"Patched Windows Local Storage LevelDB in {patched_keys} key(s) "
                        f"across {patched_dbs} database(s)."
                    )
                else:
                    print(
                        f"Windows Local Storage LevelDB already contains {synced_keys} matching key(s)."
                    )
                imported_localstorage = True
            else:
                patched_dbs, patched_rows = _patch_windows_webview_localstorage(
                    values,
                    backup=args.backup,
                )
                if patched_rows > 0:
                    print(
                        f"Patched WebView profile localstorage payload in {patched_rows} row(s) "
                        f"across {patched_dbs} database(s)."
                    )
                    imported_localstorage = True
                else:
                    print(
                        "WARNING: Windows Local Storage LevelDB and WebView sqlite files "
                        "were scanned but no writable localstorage payload entry was found."
                    )
                    if args.patch_assistant_sort:
                        bootstrap_localstorage = values
    elif args.hs_offenburg_api_key or os.environ.get("HS_OFFENBURG_API_KEY"):
        print("WARNING: No LocalStorage payload found; HS-Offenburg API key was not applied.")

    if args.patch_assistant_sort:
        _, bootstrap_ready = _patch_assistant_extension_sorting(
            data_dir,
            bootstrap_localstorage=bootstrap_localstorage,
        )
        if bootstrap_localstorage and bootstrap_ready and not imported_localstorage:
            imported_localstorage = True
            print(
                "Injected one-time Windows localStorage bootstrap into assistant extension. "
                "Launch Jan once to apply provider config."
            )

    if values and not imported_localstorage:
        if args.require_localstorage:
            print("LocalStorage import target not found.")
            return 1
        print(
            "WARNING: LocalStorage import target not found. "
            "Skipping LocalStorage import. Configure provider manually in Jan."
        )

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


def _patch_assistant_extension_sorting(
    data_dir: Optional[Path],
    bootstrap_localstorage: Optional[Dict[str, str]] = None,
) -> Tuple[bool, bool]:
    ext_path = _find_assistant_extension_index(data_dir)
    if not ext_path:
        return (False, False)

    text = ext_path.read_text(encoding="utf-8")

    sort_marker = "assistantsData.sort((a, b) => {"
    needle = "\t\treturn assistantsData;\n\t}"
    changed = False

    if sort_marker not in text:
        if needle not in text:
            return (False, False)
        sort_patch = (
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
        text = text.replace(needle, sort_patch)
        changed = True

    bootstrap_ready = False
    if bootstrap_localstorage:
        payload = {
            key: value
            for key, value in bootstrap_localstorage.items()
            if key in set(LOCALSTORAGE_KEYS) and isinstance(value, str)
        }
        if payload:
            payload_json = json.dumps(payload, ensure_ascii=True)
            payload_version = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()[:12]
            start_marker = "\t\t// jan-installer-localstorage-bootstrap:start\n"
            end_marker = "\t\t// jan-installer-localstorage-bootstrap:end\n"
            block = (
                start_marker
                + f"\t\tconst JAN_INSTALLER_LOCALSTORAGE_VERSION = \"{payload_version}\";\n"
                + "\t\tif (\n"
                + "\t\t\ttypeof localStorage !== \"undefined\" &&\n"
                + "\t\t\tlocalStorage.getItem(\"__jan_installer_localstorage_version\") !== JAN_INSTALLER_LOCALSTORAGE_VERSION\n"
                + "\t\t) {\n"
                + f"\t\t\tconst janInstallerLocalStorage = {payload_json};\n"
                + "\t\t\tfor (const [bootstrapKey, bootstrapValue] of Object.entries(janInstallerLocalStorage)) {\n"
                + "\t\t\t\tif (typeof bootstrapValue === \"string\") {\n"
                + "\t\t\t\t\tlocalStorage.setItem(bootstrapKey, bootstrapValue);\n"
                + "\t\t\t\t}\n"
                + "\t\t\t}\n"
                + "\t\t\tlocalStorage.setItem(\"__jan_installer_localstorage_version\", JAN_INSTALLER_LOCALSTORAGE_VERSION);\n"
                + "\t\t}\n"
                + end_marker
            )

            existing_start = text.find(start_marker)
            existing_end = text.find(end_marker)
            if existing_start >= 0 and existing_end >= existing_start:
                existing_end += len(end_marker)
                existing = text[existing_start:existing_end]
                if existing != block:
                    text = text[:existing_start] + block + text[existing_end:]
                    changed = True
            else:
                insertion_anchor = "\t\tassistantsData.sort((a, b) => {\n"
                if insertion_anchor in text:
                    text = text.replace(insertion_anchor, block + insertion_anchor, 1)
                    changed = True

            bootstrap_ready = f'JAN_INSTALLER_LOCALSTORAGE_VERSION = "{payload_version}"' in text

    if changed:
        ext_path.write_text(text, encoding="utf-8")

    sort_ready = sort_marker in text
    return (sort_ready, bootstrap_ready)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export and install Jan.ai configuration.")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {APP_VERSION}",
        help="Show version and exit",
    )
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
    install_p.add_argument(
        "--require-localstorage",
        action="store_true",
        dest="require_localstorage",
        default=False,
        help="Fail if LocalStorage import target is not found",
    )
    install_p.set_defaults(func=install_payload)

    return parser


def main() -> int:
    parser = build_parser()
    if getattr(sys, "frozen", False) and len(sys.argv) == 1:
        print(f"jan-config-install {APP_VERSION}")
        sys.argv.append("install")
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
