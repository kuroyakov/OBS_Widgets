"""
拡張ディレクトリ機構 (server/extensions.py, Issue #220 / #222)
- 環境変数 OBS_WIDGETS_EXTENSIONS_DIR (絶対パス) で指定された外部ディレクトリから、
  公開側ツリー外のウィジェット (静的ファイル) と任意サーバーモジュールを読み込む。
- 環境変数が未設定の場合、server.py と同じ階層に extensions/ ディレクトリが存在すれば
  デフォルトの拡張ディレクトリとして使用する。空文字が明示的に設定されている場合は
  無効化として扱う。
- 名前衝突時は BASE_DIR 側を優先し、起動時に標準エラーへ警告を出す。
"""

import importlib
import importlib.util
import os
import posixpath
import sys
import urllib.parse

ENV_VAR_NAME = "OBS_WIDGETS_EXTENSIONS_DIR"
DEFAULT_EXTENSIONS_SUBDIR = "extensions"
SERVER_MODULES_SUBDIR = "server_modules"
_DEFAULT_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 静的マウントの対象外とするトップレベル名 (Python モジュール置き場や保護対象ディレクトリ)
_RESERVED_STATIC_NAMES = frozenset({
    "server",
    "certs",
    "logs",
    "tools",
    SERVER_MODULES_SUBDIR,
})


def resolve_extensions_dir(raw_value=None, *, base_dir=None, warn_stream=None):
    """OBS_WIDGETS_EXTENSIONS_DIR を検証して正規化パスを返す。

    - 環境変数が設定されている場合: その値を優先する。空文字は明示的な無効化
      として警告なしで None を返す。絶対パスでない場合やディレクトリとして存在
      しない場合は標準エラーへ警告を出したうえで None を返す（サーバー自体は継続）。
    - 環境変数が未設定の場合: base_dir（省略時は server.py と同じ階層）直下に
      extensions/ ディレクトリが存在すればその正規化パスを返す。存在しなければ
      警告なしで None を返す。
    """
    stream = warn_stream if warn_stream is not None else sys.stderr
    if raw_value is None:
        if ENV_VAR_NAME in os.environ:
            raw_value = os.environ[ENV_VAR_NAME]
        else:
            root = base_dir if base_dir is not None else _DEFAULT_BASE_DIR
            default_dir = os.path.join(root, DEFAULT_EXTENSIONS_SUBDIR)
            if os.path.isdir(default_dir):
                return os.path.realpath(default_dir)
            return None
    raw = raw_value.strip()
    if not raw:
        return None
    if not os.path.isabs(raw):
        print(
            f"[extensions] 警告: {ENV_VAR_NAME} は絶対パスで指定してください (無効化します): {raw}",
            file=stream,
        )
        return None
    if not os.path.isdir(raw):
        print(
            f"[extensions] 警告: {ENV_VAR_NAME} が存在しないかディレクトリではありません (無効化します): {raw}",
            file=stream,
        )
        return None
    return os.path.realpath(raw)


def _dir_has_any_file(path):
    """ディレクトリ配下に再帰的にファイルが1つ以上存在すれば True を返す。

    走査中にエラー (権限エラー等) が発生した場合は安全側に倒し、衝突扱いとして True を返す。
    """
    had_error = False

    def _on_error(_exc):
        nonlocal had_error
        had_error = True

    for _root, _dirs, files in os.walk(path, onerror=_on_error):
        if files:
            return True
    return had_error


def discover_static_mounts(base_dir, extensions_dir, *, warn_stream=None):
    """拡張ディレクトリ直下の静的マウント対象ディレクトリを列挙する。

    返り値は `{第1セグメント名: 実パス}` の辞書。
    BASE_DIR 側に同名のファイルまたは中身のある（再帰的にファイルが1つ以上ある）
    ディレクトリが存在する場合は BASE_DIR 側を優先し、起動時に警告ログを出す。
    BASE_DIR 側に空ディレクトリだけが残存している場合は衝突とせず、情報ログを出して
    拡張側をマウントする。ドット始まりや予約名 (server_modules 等) はマウント対象外とする。
    """
    if not extensions_dir or not os.path.isdir(extensions_dir):
        return {}
    stream = warn_stream if warn_stream is not None else sys.stderr
    mounts = {}
    try:
        entries = sorted(os.listdir(extensions_dir))
    except OSError as exc:
        print(
            f"[extensions] 警告: 拡張ディレクトリの走査に失敗しました ({extensions_dir}): {exc}",
            file=stream,
        )
        return {}
    for name in entries:
        if name.startswith(".") or name in _RESERVED_STATIC_NAMES:
            continue
        ext_entry = os.path.join(extensions_dir, name)
        if not os.path.isdir(ext_entry):
            continue
        base_entry = os.path.join(base_dir, name)
        if os.path.exists(base_entry):
            if os.path.isdir(base_entry) and not _dir_has_any_file(base_entry):
                print(
                    f"[extensions] 情報: BASE_DIR 側の '{name}' は空ディレクトリのため"
                    f" 名前衝突とみなさず拡張側をマウントします: {ext_entry}",
                    file=stream,
                )
            else:
                print(
                    f"[extensions] 警告: 拡張ディレクトリ '{name}' は BASE_DIR 側と名前衝突するため"
                    f" 無効化します (BASE_DIR 側を優先): {ext_entry}",
                    file=stream,
                )
                continue
        mounts[name] = os.path.realpath(ext_entry)
    return mounts


def get_extension_module_path(module_name, extensions_dir):
    """拡張側の server_modules/<module_name>.py が存在すればそのパスを返す。"""
    if not extensions_dir or not module_name or not module_name.isidentifier():
        return None
    candidate = os.path.join(extensions_dir, SERVER_MODULES_SUBDIR, f"{module_name}.py")
    if os.path.isfile(candidate):
        return candidate
    return None


def _base_module_exists(base_dir, module_name):
    if not module_name or not module_name.isidentifier():
        return False
    mod_file = os.path.join(base_dir, "server", f"{module_name}.py")
    mod_pkg = os.path.join(base_dir, "server", module_name, "__init__.py")
    return os.path.isfile(mod_file) or os.path.isfile(mod_pkg)


def load_optional_module(module_name, *, base_dir, extensions_dir=None, warn_stream=None):
    """任意モジュールを読み込む。BASE_DIR 側にファイル不在の場合のみ拡張側へフォールバックする。

    - BASE_DIR/server/<module_name>.py が存在する場合: 通常の `server.<module_name>` の
      import のみを試み、失敗しても拡張側へフォールバックしない。拡張側にも同名ファイルが
      ある場合は名前衝突の警告を出す（BASE_DIR 側が勝つ）。
    - BASE_DIR/server/<module_name>.py が不在の場合: まず通常 import を試み、対象モジュール
      自体の ModuleNotFoundError に限って `$OBS_WIDGETS_EXTENSIONS_DIR/server_modules/<module_name>.py`
      から importlib で読み込む。読み込み成功時は `sys.modules["server.<module_name>"]` に登録し、
      他のモジュールからの `from server.<module_name> import ...` も解決できるようにする。
    """
    stream = warn_stream if warn_stream is not None else sys.stderr
    full_name = f"server.{module_name}"
    base_exists = _base_module_exists(base_dir, module_name)
    ext_path = get_extension_module_path(module_name, extensions_dir)

    if base_exists:
        if ext_path:
            print(
                f"[extensions] 警告: 拡張モジュール '{module_name}' は BASE_DIR/server/{module_name}.py と"
                f" 名前衝突するため無効化します (BASE_DIR 側を優先): {ext_path}",
                file=stream,
            )
        return importlib.import_module(full_name)

    try:
        return importlib.import_module(full_name)
    except ModuleNotFoundError as exc:
        if exc.name != full_name:
            raise

    if not ext_path:
        raise ModuleNotFoundError(f"No module named {full_name!r}", name=full_name)

    spec = importlib.util.spec_from_file_location(full_name, ext_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create module spec for {ext_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(full_name, None)
        raise

    server_pkg = sys.modules.get("server")
    if server_pkg is not None:
        setattr(server_pkg, module_name, module)
    return module


def load_optional_modules(module_names, *, base_dir=None, extensions_dir=None, warn_stream=None):
    """複数の任意モジュールを依存順に読み込み、最後のモジュールを返す。

    start_server.command / stop_server.command / obs_mothership.lua のような
    短命 subprocess から launcher 等を呼ぶ際に、依存先 (browser_refresh,
    display_recapture) ごと server.<name> として解決するために使う。
    """
    root = base_dir if base_dir is not None else _DEFAULT_BASE_DIR
    ext_dir = (
        extensions_dir
        if extensions_dir is not None
        else resolve_extensions_dir(base_dir=root, warn_stream=warn_stream)
    )
    last_module = None
    for name in module_names:
        last_module = load_optional_module(
            name, base_dir=root, extensions_dir=ext_dir, warn_stream=warn_stream
        )
    return last_module


def import_symbols(module, names):
    """モジュールから指定シンボルを取り出す。不在なら from ... import ... と同様に ImportError を送出する。"""
    values = []
    for attr in names:
        if not hasattr(module, attr):
            raise ImportError(f"cannot import name {attr!r} from {module.__name__!r}")
        values.append(getattr(module, attr))
    return tuple(values)


def translate_extension_path(request_path, static_mounts):
    """リクエストパスが静的マウント対象の拡張ディレクトリに一致する場合、実ファイルパスを返す。

    一致しない場合やシンボリックリンク等でマウント先外へ抜ける場合は None を返す。
    """
    if not static_mounts:
        return None
    path = request_path.split("?", 1)[0]
    path = path.split("#", 1)[0]
    trailing_slash = path.rstrip().endswith("/")
    try:
        path = urllib.parse.unquote(path, errors="surrogatepass")
    except UnicodeDecodeError:
        path = urllib.parse.unquote(path)
    path = posixpath.normpath(path)
    words = []
    for word in path.split("/"):
        if not word:
            continue
        if os.path.dirname(word) or word in (os.curdir, os.pardir):
            continue
        words.append(word)
    if not words:
        return None
    mount_root = static_mounts.get(words[0])
    if not mount_root:
        return None
    fs_path = os.path.join(mount_root, *words[1:]) if len(words) > 1 else mount_root
    try:
        real_target = os.path.realpath(fs_path)
        if os.path.commonpath([mount_root, real_target]) != mount_root:
            return None
    except ValueError:
        return None
    if trailing_slash:
        fs_path += "/"
    return fs_path
