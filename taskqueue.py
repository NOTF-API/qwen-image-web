# -*- coding: utf-8 -*-
"""持久化任务队列 + 单工作线程调度器。

设计要点
--------
* 每个生成请求 = 一条任务记录, 落盘在 ``<输出目录>/tasks/tasks.json``。
  服务器重启后任务仍在, 未完成的会恢复为「待开始」(最多自动重试 MAX_ATTEMPTS 次),
  已完成的任务保留参数与产物, 可继续编辑 / 重新生成 / 删除。
* 只有一个工作线程: GPU 只有一张, 串行执行是唯一正确的选择。
  队列顺序 = 提交顺序(FIFO), 未开始的任务可取消、可编辑、可删除。
* 取消是真实的: 运行中的任务通过 ``progress`` 回调(接到 diffusers 的
  callback_on_step_end)在每个采样步之间检查取消标记并抛出 ``Canceled``,
  按 30 步 / 约 90 秒估计, 取消通常在一个采样步(数秒)内生效。
* 所有写盘都是原子替换(临时文件 + os.replace), 进程被强杀也不会写坏 JSON。

本模块不知道 diffusers 的存在: 真正的出图逻辑由调用方以 ``run`` 回调注入,
入参是任务记录(setup 阶段已归一化的 params), 返回 ``{"outputs": [...], "warnings": [...]}``。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("qwen-image.queue")

STATUS_PENDING = "pending"        # 待开始(已进入队列, 未占用 GPU)
STATUS_RUNNING = "running"        # 正在生成
STATUS_CANCELING = "canceling"    # 运行中, 已请求取消, 等待采样步边界生效
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"

ACTIVE_STATUSES = (STATUS_RUNNING, STATUS_CANCELING)
SETTLED_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELED)
ALL_STATUSES = (STATUS_PENDING,) + ACTIVE_STATUSES + SETTLED_STATUSES

MAX_ATTEMPTS = int(os.environ.get("QWEN_TASK_MAX_ATTEMPTS", "2"))
MAX_TASKS = int(os.environ.get("QWEN_TASK_MAX_KEEP", "500"))   # 已完成任务的保留上限
POLL_SEC = 0.4

# 取消信号: 由 progress 回调抛出, 一路穿过 pipeline 的采样循环
class Canceled(Exception):
    """任务被用户取消(非错误)。"""


class TaskNotFound(KeyError):
    pass


class TaskConflict(RuntimeError):
    pass


def _now() -> float:
    return time.time()


def _released(task: dict) -> bool:
    """任务是否已被释放执行(auto_start 关闭时新任务先暂存)。"""
    return bool(task.get("released", True))


class TaskStore:
    """任务记录的线程安全持久化存储。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "tasks.json"
        self.refs_dir = self.root / "refs"
        self.refs_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.state = {
            "version": 1,
            "next_seq": 1,
            # auto_start=False 时新任务只"暂存", 等前端点「开始队列」才释放
            "auto_start": bool(int(os.environ.get("QWEN_QUEUE_AUTOSTART", "1"))),
            "worker": {"started_at": None, "current": None,
                       "done": 0, "failed": 0, "canceled": 0},
            "tasks": [],
        }
        self._dirty = False
        self._load()

    # ------------------------------------------------------------ 读写
    def _load(self):
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:                      # 损坏时留档, 不让服务起不来
            bad = self.path.with_suffix(f".corrupt-{int(_now())}.json")
            try:
                self.path.replace(bad)
            except OSError:
                pass
            log.error("tasks.json 解析失败(%s), 已备份到 %s, 队列从空开始", e, bad.name)
            return
        if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), list):
            log.error("tasks.json 结构异常, 队列从空开始")
            return
        # 注意: 这里必须深拷贝一份任务列表。state.update(raw) 会让新实例与
        # 解析出来的 raw 共享同一个 list/dict, 两个 store 实例(例如测试里模拟
        # 重启, 或将来在同进程开第二个 store)会互相改到对方的记录。
        self.state.update(raw)
        self.state["tasks"] = [json.loads(json.dumps(t)) for t in raw["tasks"]
                              if isinstance(t, dict)]
        self.state["worker"] = dict(raw.get("worker") or {})
        self.state["next_seq"] = int(raw.get("next_seq", len(self.state["tasks"]) + 1) or 1)

    def _flush(self):
        """原子写盘(调用方需持有 self.lock)。"""
        tmp = self.path.with_suffix(".json.tmp")
        data = json.dumps(self.state, ensure_ascii=False, indent=1)
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, self.path)
        self._dirty = False

    def flush(self):
        with self.lock:
            self._flush()

    def _touch(self, flush=False):
        self._dirty = True
        if flush:
            self._flush()

    # ------------------------------------------------------------ 启动恢复
    def recover_interrupted(self) -> int:
        """上次进程留下的「运行中」任务: 回到待开始(有限次)或判失败。

        由服务启动时显式调用 —— 判断依据是状态本身, 所以只能这样调一次,
        不能在 TaskStore() 里做(否则会误判成重启)。
        """
        recovered = 0
        with self.lock:
            for t in self.state["tasks"]:
                if t["status"] not in ACTIVE_STATUSES:
                    continue
                t["cancel_requested"] = False
                t["finished_at"] = None
                # 中断前已经开始执行, 恢复后直接重新排队, 不要退回暂存状态
                t["released"] = True
                if int(t.get("attempts", 0)) < MAX_ATTEMPTS:
                    t["status"] = STATUS_PENDING
                    t["error"] = (f"上次服务中断时任务仍在运行，已自动重新排队"
                                  f"（第 {int(t.get('attempts', 0)) + 1} 次尝试）")
                    t["progress"] = {"step": 0, "total": int(t["params"].get("steps") or 0)}
                    recovered += 1
                    log.warning("恢复中断任务 %s -> 待开始", t["id"])
                else:
                    t["status"] = STATUS_FAILED
                    t["finished_at"] = _now()
                    t["error"] = "服务中断且已达到自动重试上限，可手动「重新生成」"
                    log.warning("中断任务 %s 重试超限 -> 失败", t["id"])
            self.state["worker"]["current"] = None
            self.state["worker"]["started_at"] = None
            self.state["worker"]["recovered"] = self.state["worker"].get("recovered", 0) + recovered
            self._touch(flush=True)
        return recovered

    # ------------------------------------------------------------ 查询
    def get(self, task_id: str) -> dict:
        with self.lock:
            for t in self.state["tasks"]:
                if t["id"] == task_id:
                    return t
        raise TaskNotFound(task_id)

    def list(self, status: Optional[str] = None, limit: Optional[int] = None) -> list:
        with self.lock:
            tasks = list(self.state["tasks"])
        if status:
            wanted = {s for s in str(status).split(",") if s}
            tasks = [t for t in tasks if t["status"] in wanted]
        # 队列视图: 待开始/运行中在前(按提交顺序), 已结束在后(按结束时间倒序)
        active = [t for t in tasks if t["status"] not in SETTLED_STATUSES]
        settled = [t for t in tasks if t["status"] in SETTLED_STATUSES]
        active.sort(key=lambda t: t["seq"])
        settled.sort(key=lambda t: t.get("finished_at") or t["created_at"], reverse=True)
        out = active + settled
        if limit:
            out = out[: int(limit)]
        return out

    def counts(self) -> dict:
        with self.lock:
            tasks = self.state["tasks"]
            c = {s: 0 for s in ALL_STATUSES}
            for t in tasks:
                c[t["status"]] = c.get(t["status"], 0) + 1
            c["total"] = len(tasks)
            staged = [t for t in tasks
                      if t["status"] == STATUS_PENDING and not _released(t)]
            c["staged"] = len(staged)
            c["queued"] = c[STATUS_PENDING]
            c["ready"] = c[STATUS_PENDING] - c["staged"]
            c["finished"] = sum(c[s] for s in SETTLED_STATUSES)
            c["auto_start"] = bool(self.state.get("auto_start", True))
            w = self.state["worker"]
            c["worker_done"] = w.get("done", 0)
            c["worker_failed"] = w.get("failed", 0)
            c["worker_canceled"] = w.get("canceled", 0)
            return c

    def queue_position(self, task_id: str) -> Optional[int]:
        """待开始任务的排队位置(1 起); 非待开始返回 None。"""
        with self.lock:
            queue = sorted((t for t in self.state["tasks"]
                            if t["status"] == STATUS_PENDING), key=lambda t: t["seq"])
        for i, t in enumerate(queue):
            if t["id"] == task_id:
                return i + 1
        return None

    def view(self, task: dict) -> dict:
        """给前端的任务视图(补充排队位置/暂存标记, 不落盘)。"""
        v = dict(task)
        v["staged"] = (v["status"] == STATUS_PENDING and not _released(v))
        if v["status"] == STATUS_PENDING:
            v["queue_position"] = self.queue_position(v["id"])
        else:
            v["queue_position"] = None
        return v

    # ------------------------------------------------------------ 释放/暂存
    def set_auto_start(self, on: bool) -> dict:
        """开启自动开始时, 顺手释放所有已暂存任务。"""
        with self.lock:
            self.state["auto_start"] = bool(on)
            released_ids = []
            if on:
                for t in self.state["tasks"]:
                    if t["status"] == STATUS_PENDING and not _released(t):
                        t["released"] = True
                        t["updated_at"] = _now()
                        released_ids.append(t["id"])
            self._touch(flush=True)
            return {"auto_start": bool(on), "released": released_ids}

    def release(self, task_id: str) -> dict:
        """释放单条暂存任务(开始执行)。"""
        with self.lock:
            t = self.get(task_id)
            if t["status"] != STATUS_PENDING:
                raise TaskConflict("只有未开始的任务需要释放")
            t["released"] = True
            t["updated_at"] = _now()
            self._touch(flush=True)
            return t

    def hold(self, task_id: str) -> dict:
        """把尚未开始的已释放任务退回暂存状态(不影响正在运行的任务)。"""
        with self.lock:
            t = self.get(task_id)
            if t["status"] != STATUS_PENDING:
                raise TaskConflict("任务已开始，无法退回暂存；请用取消")
            t["released"] = False
            t["updated_at"] = _now()
            self._touch(flush=True)
            return t

    # ------------------------------------------------------------ 写入
    def create(self, kind: str, prompt: str, params: dict, refs: Optional[list] = None,
               ref_index: Optional[int] = None, title: str = "",
               origin: Optional[str] = None, source: str = "manual",
               released: Optional[bool] = None) -> dict:
        with self.lock:
            seq = int(self.state["next_seq"])
            self.state["next_seq"] = seq + 1
            tid = uuid.uuid4().hex[:12]
            now = _now()
            if released is None:
                released = bool(self.state.get("auto_start", True))
            task = {
                "id": tid,
                "seq": seq,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "finished_at": None,
                "status": STATUS_PENDING,
                "kind": kind,                 # generation | edit
                "prompt": prompt,
                "negative_prompt": params.get("negative_prompt") or None,
                "refs": list(refs or []),     # 参考图文件名, 存放于 <tasks>/refs/<tid>/
                "ref_index": ref_index,
                "params": params,             # 归一化后的生成参数(可直接执行)
                "title": title or prompt.strip().replace("\n", " ")[:80],
                "source": source,             # manual | api | retry
                "origin": origin,             # 由哪条任务重新生成而来
                "released": bool(released),   # False = 暂存, 等「开始队列」
                "progress": {"step": 0, "total": int(params.get("steps") or 0)},
                "outputs": [],
                "usage": None,
                "warnings": [],
                "error": None,
                "attempts": 0,
                "cancel_requested": False,
            }
            self.state["tasks"].append(task)
            self._touch(flush=True)
            self._prune()
            return task

    def update_params(self, task_id: str, changes: dict,
                      refs: Optional[list] = None,
                      ref_index: Optional[int] = None) -> dict:
        """编辑未开始的任务(运行时不可改)。"""
        with self.lock:
            t = self.get(task_id)
            if t["status"] != STATUS_PENDING:
                raise TaskConflict("只能编辑未开始的任务；正在运行或已结束的任务请用「重新生成」")
            params = dict(t["params"])
            params.update(changes)
            t["params"] = params
            if refs is not None:
                t["refs"] = list(refs)
            if ref_index is not None:
                t["ref_index"] = ref_index
            if "prompt" in changes and changes["prompt"]:
                t["prompt"] = changes["prompt"]
            if "negative_prompt" in changes:
                t["negative_prompt"] = changes["negative_prompt"] or None
            t["progress"] = {"step": 0, "total": int(params.get("steps") or 0)}
            t["title"] = (changes.get("title") or t.get("title")
                          or t["prompt"].strip().replace("\n", " ")[:80])
            t["updated_at"] = _now()
            self._touch(flush=True)
            return t

    def cancel(self, task_id: str) -> dict:
        """取消任务: 未开始 -> 立即取消; 运行中 -> 置取消标记, 到采样步边界生效。"""
        with self.lock:
            t = self.get(task_id)
            if t["status"] == STATUS_PENDING:
                t["status"] = STATUS_CANCELED
                t["cancel_requested"] = True
                t["finished_at"] = _now()
                t["updated_at"] = _now()
                t["error"] = "已取消（任务尚未开始）"
            elif t["status"] == STATUS_RUNNING:
                t["status"] = STATUS_CANCELING
                t["cancel_requested"] = True
                t["updated_at"] = _now()
            else:
                return t
            self._touch(flush=True)
            return t

    def mark_done(self, task_id: str, outputs: list, usage: dict, warnings: list) -> dict:
        with self.lock:
            t = self.get(task_id)
            t["outputs"] = outputs
            t["usage"] = usage
            t["warnings"] = warnings or []
            t["status"] = STATUS_DONE
            t["error"] = None
            t["finished_at"] = _now()
            t["updated_at"] = _now()
            self.state["worker"]["done"] = self.state["worker"].get("done", 0) + 1
            self._touch(flush=True)
            return t

    def mark_canceled(self, task_id: str, error: str = "已取消") -> dict:
        with self.lock:
            t = self.get(task_id)
            t["status"] = STATUS_CANCELED
            t["error"] = error
            t["finished_at"] = _now()
            t["updated_at"] = _now()
            self.state["worker"]["canceled"] = self.state["worker"].get("canceled", 0) + 1
            self._touch(flush=True)
            return t

    def mark_failed(self, task_id: str, error: str) -> dict:
        with self.lock:
            t = self.get(task_id)
            t["status"] = STATUS_FAILED
            t["error"] = error
            t["finished_at"] = _now()
            t["updated_at"] = _now()
            self.state["worker"]["failed"] = self.state["worker"].get("failed", 0) + 1
            self._touch(flush=True)
            return t

    def mark_started(self, task_id: str) -> dict:
        with self.lock:
            t = self.get(task_id)
            t["status"] = STATUS_RUNNING
            t["started_at"] = _now()
            t["updated_at"] = _now()
            t["attempts"] = int(t.get("attempts", 0)) + 1
            t["error"] = None
            t["progress"] = {"step": 0, "total": int(t["params"].get("steps") or 0)}
            self.state["worker"]["current"] = task_id
            self._touch(flush=True)
            return t

    def set_progress(self, task_id: str, step: int, total: int, flush: bool = False) -> None:
        with self.lock:
            try:
                t = self.get(task_id)
            except TaskNotFound:
                return
            t["progress"] = {"step": int(step), "total": int(total)}
            self._touch(flush=flush)

    def is_cancel_requested(self, task_id: str) -> bool:
        with self.lock:
            try:
                return bool(self.get(task_id).get("cancel_requested"))
            except TaskNotFound:
                return True                      # 记录没了就当取消, 别再占 GPU

    def delete(self, task_id: str) -> dict:
        """删除任务记录 + 参考图目录 + 独占的产物文件。"""
        with self.lock:
            t = self.get(task_id)
            if t["status"] in ACTIVE_STATUSES:
                raise TaskConflict("任务正在运行，请先取消再删除")
            self.state["tasks"] = [x for x in self.state["tasks"] if x["id"] != task_id]
            keep = self._referenced_outputs()
            self._touch(flush=True)
            self._prune(force=True, keep=keep)
        self._remove_refs(task_id)
        for o in t.get("outputs", []):
            _unlink_output(o, keep=keep)
        return t

    def delete_many(self, ids: list) -> dict:
        deleted, skipped = [], []
        for tid in ids:
            try:
                self.delete(str(tid))
                deleted.append(str(tid))
            except (TaskNotFound, TaskConflict) as e:
                skipped.append({"id": str(tid), "reason": str(e)})
        return {"deleted": deleted, "skipped": skipped}

    # ------------------------------------------------------------ 产物/参考图
    def ref_dir(self, task_id: str, create: bool = False) -> Path:
        d = self.refs_dir / task_id
        if create:
            d.mkdir(parents=True, exist_ok=True)
        return d

    def _remove_refs(self, task_id: str) -> None:
        d = self.refs_dir / task_id
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    def _referenced_outputs(self) -> set:
        """当前所有任务仍引用的产物文件(删除时要避开)。"""
        keep = set()
        for t in self.state["tasks"]:
            for o in t.get("outputs", []):
                p = _output_path(o)
                if p:
                    keep.add(str(p.resolve()))
        return keep

    def _prune(self, force: bool = False, keep: Optional[set] = None) -> None:
        """已结束任务超过上限时, 从最旧的开始清理(含产物文件)。"""
        if not force and not self._dirty:
            return
        settled = [t for t in self.state["tasks"] if t["status"] in SETTLED_STATUSES]
        if len(settled) <= MAX_TASKS:
            return
        settled.sort(key=lambda t: t.get("finished_at") or t["created_at"])
        for t in settled[: len(settled) - MAX_TASKS]:
            self.state["tasks"] = [x for x in self.state["tasks"] if x["id"] != t["id"]]
            self._remove_refs(t["id"])
            for o in t.get("outputs", []):
                _unlink_output(o, keep=keep)
            log.info("队列超出保留上限, 已清理任务 %s", t["id"])
        self._flush()


def _output_path(output: dict) -> Optional[Path]:
    """从产物记录里取出磁盘路径。"""
    p = output.get("path")
    return Path(p) if p else None


def _unlink_output(output: dict, keep: Optional[set] = None) -> None:
    """删除产物文件; keep 为仍被引用、必须保留的路径集合。"""
    p = _output_path(output)
    if p and p.is_file() and (keep is None or str(p.resolve()) not in keep):
        try:
            p.unlink()
        except OSError:
            pass


class QueueWorker:
    """单线程队列执行器: 串行消费待开始任务。"""

    def __init__(self, store: TaskStore,
                 run: Callable[[dict, threading.Event, Callable], dict],
                 can_run: Optional[Callable[[], bool]] = None):
        self.store = store
        self.run = run
        # can_run: 模型是否可用。为 None 表示不检查。
        # 有它才能让「服务刚起、模型还在加载/加载失败」期间的排队任务原地等待,
        # 而不是一拥而上全部失败。
        self.can_run = can_run
        self.thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._blocked_logged = False

    def status(self) -> dict:
        return {"running": bool(self.thread and self.thread.is_alive()),
                "blocked": bool(self.can_run and not self.can_run())}

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="task-queue")
        self.thread.start()
        log.info("任务队列工作线程已启动")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self.thread:
            self.thread.join(timeout=timeout)

    def wake(self) -> None:
        """有新任务/状态变化时叫醒工作线程, 避免空转等待。"""
        self._wake.set()

    # ------------------------------------------------------------ 主循环
    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.can_run is not None and not self.can_run():
                if not self._blocked_logged:
                    log.info("模型尚未就绪, 排队任务原地等待(不会失败)")
                    self._blocked_logged = True
                self._wake.wait(2.0)
                self._wake.clear()
                continue
            task = self._next_pending()
            if task is None:
                self._wake.wait(POLL_SEC)
                self._wake.clear()
                continue
            self._blocked_logged = False
            self._run_one(task["id"])

    def _next_pending(self) -> Optional[dict]:
        with self.store.lock:
            pend = [t for t in self.store.state["tasks"]
                    if t["status"] == STATUS_PENDING and _released(t)]
        if not pend:
            return None
        pend.sort(key=lambda t: t["seq"])
        return pend[0]

    def _run_one(self, task_id: str) -> None:
        try:
            task = self.store.get(task_id)
        except TaskNotFound:
            return
        if task["status"] != STATUS_PENDING:
            return
        self.store.mark_started(task_id)
        with self.store.lock:
            self.store.state["worker"]["started_at"] = _now()
        self.store.flush()
        started = _now()
        last_flush = [0.0]

        def progress(step: int, total: int) -> None:
            # 取消检查就放在采样步边界上: 抛出后由 pipeline 栈一路传回这里
            if self.store.is_cancel_requested(task_id):
                raise Canceled()
            now = _now()
            if now - last_flush[0] >= 1.5:
                last_flush[0] = now
                self.store.set_progress(task_id, step, total, flush=True)
            else:
                self.store.set_progress(task_id, step, total)

        try:
            result = self.run(task, threading.Event(), progress)
        except Canceled:
            log.info("任务 %s 已取消(用时 %.1fs)", task_id, _now() - started)
            self.store.mark_canceled(task_id, "已取消，未完成本次生成")
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            log.exception("任务 %s 失败", task_id)
            self.store.mark_failed(task_id, msg)
        else:
            outputs = result.get("outputs") or []
            usage = result.get("usage") or {}
            usage.setdefault("elapsed_sec", round(_now() - started, 1))
            if self.store.is_cancel_requested(task_id):
                self.store.mark_canceled(task_id, "已取消，未保存本次结果")
                for o in outputs:
                    _unlink_output(o)
            else:
                task = self.store.mark_done(task_id, outputs, usage,
                                            result.get("warnings") or [])
                log.info("任务 %s 完成: %s 用时 %ss",
                         task_id, ", ".join(o.get("url", "?") for o in outputs),
                         usage.get("elapsed_sec"))
        finally:
            with self.store.lock:
                self.store.state["worker"]["current"] = None
            self.store.flush()


def cleanup_orphan_outputs(store: TaskStore, output_dir: Path,
                           min_age_sec: float = 120.0) -> int:
    """删除既不被任何任务引用、又已过期的产物文件。

    只有在显式开启(``QWEN_TASK_CLEAN_ORPHANS=1``)时才由调用方调用 —— 默认关闭,
    因为 outputs/ 里可能有用户自己留下的图片或队列接管之前生成的老图, 自动删除
    会误伤。开启后也只动「比队列都新」的文件: 队列开始接管之前的产物一律保留。

    典型用途: 清理「生成到一半被强杀」留下的 PNG —— 未入队、未被记录, 否则会一直堆着。
    """
    referenced = set()
    started = None
    with store.lock:
        for t in store.state["tasks"]:
            for o in t.get("outputs", []):
                p = _output_path(o)
                if p:
                    referenced.add(str(p.resolve()))
            created = t.get("created_at")
            if created and (started is None or created < started):
                started = created
    removed = 0
    now = _now()
    for f in Path(output_dir).glob("*.png"):
        try:
            if str(f.resolve()) in referenced:
                continue
            stat = f.stat()
            if now - stat.st_mtime < min_age_sec:
                continue
            # 队列接管之前就存在的文件不碰(用户的老图 / 手工放进来的图)
            if started is None or stat.st_mtime < started:
                continue
            f.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        log.info("清理了 %d 个无主产物文件", removed)
    return removed
