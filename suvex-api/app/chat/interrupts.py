"""运行中打断：让用户**在模型跑着的时候**暂停它、补一句话、然后接着跑（v1.25）。

## 为什么不能靠"插话"（`interject`）

`interject` 现在写入 user 消息后还会发 `steer` 信号：流式输出处会停，
工具执行中仍等工具返回（不砍断已产生副作用的命令）。用户看到的
是「我让它停，它还在那儿写」。（CyberStrikeAI 把这两件事分得很清楚：
插话 = 排队；中断并继续 = 真的按停当前输出再合并上下文。）

## 本模块只做一件事：**跨线程的中断信号**

`run_turn` 跑在一个 worker 线程里（`stream.py` 用 `asyncio.to_thread` 之外的
task + 同步 DB session），而打断请求来自另一个 HTTP 请求。两者之间唯一
安全的通信方式就是进程内的一个小字典 + 锁 —— 本项目是**单 worker**
（`suvex-api.service` 的 uvicorn 没有 `--workers`），因此进程内状态跨请求可见。
⚠️ 哪天加 `--workers>1`，这里必须换成 Redis/DB（与 `chat/sandbox.py` 同一个前提）。

## 两种中断

- **继续**（`request_continue`）：带一句可选说明。引擎在当前流式输出处停下，
  把已产出的正文落库，再把说明作为一条 user 消息注入，然后继续下一轮。
- **停止**（`request_stop`）：引擎在下一个检查点退出这一轮，状态落 `cancelled`
  （**不是** `interrupted` —— 后者会被后台 `resumer` 自动接手，而"用户说停"
  的意思恰恰是别再自动跑）。

## 检查点在哪（用户看得见的时效）

- 流式输出中：**每个增量**都查一次 → 立即停在当前句。
- 工具执行中：查不到（我们是在等一个阻塞的子进程）→ **等这个工具返回**
  才生效。这一点必须在界面上说实话，否则用户会以为"点了没反应"。
"""

from __future__ import annotations

import threading

_LOCK = threading.Lock()
#: session_id -> 用户补充（还没被引擎取走）。空串表示"没写说明"。
_NOTES: dict[str, str] = {}
#: 要求停止的会话集合。
_STOPS: set[str] = set()
#: 要求“停在当前模型输出、下一轮读新消息”的会话（插话用）。
_STEERS: set[str] = set()


def request_continue(session_id: str, note: str) -> None:
    """请求「中断并继续」，可带一句说明（可为空）。"""
    with _LOCK:
        _NOTES[session_id] = (note or "").strip()
        _STOPS.discard(session_id)


def request_steer(session_id: str) -> None:
    """插话：停在当前模型输出处，但**不注入额外模板**。

    用户消息已经由 `/interject` 写进历史；下一轮重建上下文时自然读到。
    与 `request_continue` 的区别是：不把「用户补充：…」再写一条过程消息。
    """
    with _LOCK:
        _STEERS.add(session_id)


def take_steer(session_id: str) -> bool:
    with _LOCK:
        if session_id not in _STEERS:
            return False
        _STEERS.discard(session_id)
        return True


def request_stop(session_id: str) -> None:
    """请求「彻底停止」。同时清掉可能挂着的说明（停止优先）。"""
    with _LOCK:
        _STOPS.add(session_id)
        _NOTES.pop(session_id, None)
        _STEERS.discard(session_id)


def stop_requested(session_id: str) -> bool:
    with _LOCK:
        return session_id in _STOPS


def take_note(session_id: str) -> str | None:
    """取走挂着的补充说明（`None` = 没有请求）。取走即消费。"""
    with _LOCK:
        if session_id not in _NOTES:
            return None
        return _NOTES.pop(session_id)


def has_pending(session_id: str) -> bool:
    with _LOCK:
        return session_id in _STOPS or session_id in _NOTES or session_id in _STEERS


def should_break(session_id: str) -> bool:
    """流式循环里的判据：该不该立刻停止收字。"""
    return has_pending(session_id)


def clear(session_id: str) -> None:
    """一轮结束时清干净 —— 否则残留的停止信号会把**下一轮**也打断。"""
    with _LOCK:
        _STOPS.discard(session_id)
        _NOTES.pop(session_id, None)
        _STEERS.discard(session_id)


def reset_all() -> None:
    """测试用：清空全部状态。"""
    with _LOCK:
        _STOPS.clear()
        _NOTES.clear()
        _STEERS.clear()
