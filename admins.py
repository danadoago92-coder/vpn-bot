"""Reload administrator IDs from admins.json on every authorization check."""
import fcntl
import json
import os
from pathlib import Path
import tempfile


def read_admins(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("admin_ids")
    if not isinstance(data, list) or any(isinstance(x, bool) or not str(x).isascii() or not str(x).isdigit() or not 0 < int(x) < 2**63 for x in data):
        raise ValueError("admins.json باید لیستی از شناسه‌های عددی مثبت تلگرام باشد.")
    return tuple(dict.fromkeys(int(x) for x in data))


def admin_ids(settings):
    path = getattr(settings, "admins_file", None)
    if path is None:  # Programmatic settings and tests; production always has a path.
        return settings.admin_ids
    try:
        return read_admins(path)
    except (OSError, ValueError, TypeError):
        return ()  # Invalid/removed file revokes access, never falls back to stale IDs.


def write_admins(path, ids):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("فایل ادمین نباید symbolic link باشد.")
    previous = path.stat() if path.exists() else None
    fd, name = tempfile.mkstemp(prefix=".admins-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        if previous and os.geteuid() == 0:
            os.fchown(fd, previous.st_uid, previous.st_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(list(ids), stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def update_admins(path, *, add=None, remove=None, actor=None):
    """Atomically add or remove one administrator across concurrent bot updates."""
    path = Path(path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = list(read_admins(path))
        if (add is None) == (remove is None):
            raise ValueError("فقط یک عملیات افزودن یا حذف ادمین مجاز است.")
        if add is not None:
            admin_id = int(add)
            if admin_id <= 0 or admin_id >= 2**63:
                raise ValueError("Chat ID ادمین معتبر نیست.")
            if admin_id in current:
                raise ValueError("این کاربر از قبل ادمین است.")
            current.append(admin_id)
        else:
            admin_id = int(remove)
            if actor is not None and admin_id == int(actor):
                raise ValueError("برای جلوگیری از قفل شدن پنل، نمی‌توانی خودت را حذف کنی.")
            if admin_id not in current:
                raise ValueError("این ادمین دیگر در فهرست نیست.")
            if len(current) <= 1:
                raise ValueError("حداقل یک ادمین باید باقی بماند.")
            current.remove(admin_id)
        write_admins(path, current)
        return tuple(current)
