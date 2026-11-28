"""
離席状態の単一管理基盤 (server/away_state.py, Issue #75)

責務:
- 離席状態 {"active": bool, "seq": int, "updated_at": float, "source": str, "reason": str}
  を単一ストアで直列管理する（書き込み窓口は set_away_state に一本化）。
- active が変化したときのみ seq をインクリメントし、登録済みリアクタへ通知する。
- 各リアクタは専用ワーカースレッド（最新 seq へ合流するディスパッチ）上で、
  個別に例外隔離して実行する。1 つのリアクタの失敗・未起動が、離席状態の巻き戻しや
  他リアクタへの通知停止を起こしてはならない。
- import 時にはスレッドを起動しない（T7）。
"""

import sys
import threading
import time


def _make_initial_state():
    return {
        "active": False,
        "seq": 0,
        "updated_at": 0.0,
        "source": "",
        "reason": "",
    }


def _validate_state_args(active, source, reason):
    if not isinstance(active, bool):
        raise ValueError("active must be a boolean")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string")
    if reason is None:
        norm_reason = ""
    elif isinstance(reason, str):
        norm_reason = reason.strip()
    else:
        raise ValueError("reason must be a string")
    return active, source.strip(), norm_reason


class _ReactorWorker:
    """単一リアクタ用の専用ワーカースレッド（最新 seq 合流 + 例外隔離）。"""

    def __init__(self, name, callback):
        self.name = name
        self._callback = callback
        self._cond = threading.Condition()
        self._latest_state = None
        self._last_processed_seq = 0
        self._busy = False
        self._stopped = False
        self._thread = None

    def notify(self, state_snapshot):
        with self._cond:
            if self._stopped:
                return
            seq = state_snapshot.get("seq", 0)
            if self._latest_state is None or seq > self._latest_state.get("seq", 0):
                self._latest_state = dict(state_snapshot)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    name=f"away-reactor-{self.name}",
                    daemon=True,
                )
                self._thread.start()
            self._cond.notify_all()

    def _run(self):
        while True:
            try:
                with self._cond:
                    while not self._stopped and (
                        self._latest_state is None
                        or self._latest_state.get("seq", 0) <= self._last_processed_seq
                    ):
                        self._cond.wait()
                    if self._stopped:
                        return
                    target_state = dict(self._latest_state)
                    self._busy = True

                try:
                    self._callback(dict(target_state))
                except Exception as exc:
                    print(
                        f"[away_state] リアクタ '{self.name}' の実行で例外が発生しました "
                        f"(seq={target_state.get('seq')}): {exc}",
                        file=sys.stderr,
                    )
                finally:
                    with self._cond:
                        seq = target_state.get("seq", 0)
                        if seq > self._last_processed_seq:
                            self._last_processed_seq = seq
                        self._busy = False
                        self._cond.notify_all()
            except Exception as exc:
                # ワーカーループ自体の予期しない例外でもスレッドを終了させない (T4)
                print(
                    f"[away_state] リアクタ '{self.name}' ワーカーで予期しないエラーが発生しました: {exc}",
                    file=sys.stderr,
                )
                with self._cond:
                    if self._stopped:
                        return
                    self._busy = False
                    self._cond.wait(timeout=0.1)

    def wait_idle(self, target_seq=None, timeout=5.0):
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            while True:
                if self._stopped:
                    return True
                desired = target_seq
                if desired is None:
                    desired = self._latest_state.get("seq", 0) if self._latest_state is not None else 0
                if not self._busy and self._last_processed_seq >= desired:
                    if self._latest_state is None or self._last_processed_seq >= self._latest_state.get("seq", 0):
                        return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=remaining)

    def stop(self, timeout=2.0):
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)


class AwayStateStore:
    """離席状態の単一ストアとリアクタ通知ディスパッチャ。"""

    def __init__(self, *, time_fn=None):
        self._lock = threading.RLock()
        self._state = _make_initial_state()
        self._reactors = {}
        self._time_fn = time_fn or time.time

    def get_state(self):
        with self._lock:
            return dict(self._state)

    def set_state(self, active, *, source, reason=""):
        norm_active, norm_source, norm_reason = _validate_state_args(active, source, reason)
        with self._lock:
            if norm_active == self._state["active"]:
                return dict(self._state)
            self._state = {
                "active": norm_active,
                "seq": self._state["seq"] + 1,
                "updated_at": float(self._time_fn()),
                "source": norm_source,
                "reason": norm_reason,
            }
            snapshot = dict(self._state)
            workers = list(self._reactors.values())

        for worker in workers:
            worker.notify(snapshot)
        return dict(snapshot)

    def register_reactor(self, name, callback):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("reactor name must be a non-empty string")
        if not callable(callback):
            raise ValueError("reactor callback must be callable")
        norm_name = name.strip()
        old_worker = None
        with self._lock:
            old_worker = self._reactors.get(norm_name)
            self._reactors[norm_name] = _ReactorWorker(norm_name, callback)
        if old_worker is not None:
            old_worker.stop(timeout=2.0)

    def unregister_reactor(self, name, *, timeout=2.0):
        if not isinstance(name, str):
            return
        norm_name = name.strip()
        with self._lock:
            worker = self._reactors.pop(norm_name, None)
        if worker is not None:
            worker.stop(timeout=timeout)

    def list_reactors(self):
        with self._lock:
            return list(self._reactors.keys())

    def wait_for_reactors(self, timeout=5.0):
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            target_seq = self._state["seq"]
            workers = list(self._reactors.values())
        all_ok = True
        for worker in workers:
            remaining = max(0.0, deadline - time.monotonic())
            if not worker.wait_idle(target_seq=target_seq, timeout=remaining):
                all_ok = False
        return all_ok

    def shutdown_reactors(self, timeout=2.0):
        with self._lock:
            workers = list(self._reactors.values())
            self._reactors.clear()
        deadline = time.monotonic() + max(0.0, float(timeout))
        for worker in workers:
            remaining = max(0.0, deadline - time.monotonic())
            worker.stop(timeout=remaining)

    def reset(self, *, shutdown_timeout=2.0):
        self.shutdown_reactors(timeout=shutdown_timeout)
        with self._lock:
            self._state = _make_initial_state()


_DEFAULT_STORE = AwayStateStore()


def get_away_state():
    return _DEFAULT_STORE.get_state()


def set_away_state(active, *, source, reason=""):
    return _DEFAULT_STORE.set_state(active, source=source, reason=reason)


def register_away_reactor(name, callback):
    _DEFAULT_STORE.register_reactor(name, callback)


def unregister_away_reactor(name, *, timeout=2.0):
    _DEFAULT_STORE.unregister_reactor(name, timeout=timeout)


def list_away_reactors():
    return _DEFAULT_STORE.list_reactors()


def wait_for_away_reactors(timeout=5.0):
    return _DEFAULT_STORE.wait_for_reactors(timeout=timeout)


def shutdown_away_reactors(timeout=2.0):
    _DEFAULT_STORE.shutdown_reactors(timeout=timeout)


def reset_away_state():
    _DEFAULT_STORE.reset()
