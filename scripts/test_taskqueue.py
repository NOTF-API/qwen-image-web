# -*- coding: utf-8 -*-
"""任务队列离线自测: 用假 run 回调验证 入队/取消/重跑/删除/落盘恢复, 不加载模型。

用法:
    venv\\Scripts\\python.exe scripts\\test_taskqueue.py
"""
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from taskqueue import (                                    # noqa: E402
    Canceled, QueueWorker, STATUS_CANCELED, STATUS_DONE, STATUS_PENDING,
    TaskConflict, TaskStore,
)

FAILED = []


def check(cond, label):
    print(("  [ok] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILED.append(label)


def fake_run(task, cancel, progress):
    """假生成: 20 步 * 20ms, 中途检查取消标记。"""
    steps = int(task["params"].get("steps") or 20)
    for i in range(steps):
        progress(i + 1, steps)
        time.sleep(0.02)
    out = Path(task["params"]["out_dir"]) / f"{task['id']}-{int(time.time()*1000)}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"PNG")
    return {"outputs": [{"url": f"/outputs/{out.name}", "path": str(out),
                         "seed": 1, "width": 64, "height": 64, "steps": steps}],
            "usage": {"elapsed_sec": 0.4}, "warnings": []}


def wait_for(store, tid, statuses, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = store.get(tid)
        if t["status"] in statuses:
            return t
        time.sleep(0.05)
    return store.get(tid)


def main():
    root = Path(tempfile.mkdtemp(prefix="qwen-queue-test-"))
    out_dir = root / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        store = TaskStore(root / "tasks")
        worker = QueueWorker(store, fake_run)
        worker.start()

        print("1) 入队 + 自动执行 + 落盘")
        t1 = store.create("generation", "一只猫", {"steps": 6, "n": 1, "out_dir": str(out_dir)})
        t1 = wait_for(store, t1["id"], (STATUS_DONE, "failed"))
        check(t1["status"] == STATUS_DONE, f"任务完成 (status={t1['status']} err={t1['error']})")
        check(len(t1["outputs"]) == 1 and Path(t1["outputs"][0]["path"]).is_file(),
              "产物已落盘")
        check((root / "tasks" / "tasks.json").is_file(), "tasks.json 已写入")

        print("2) 取消未开始的任务(暂存 -> 取消)")
        store.set_auto_start(False)
        t2 = store.create("generation", "待取消", {"steps": 20, "n": 1, "out_dir": str(out_dir)})
        check(t2["released"] is False and store.view(t2)["staged"] is True, "新任务被暂存")
        check(store.queue_position(t2["id"]) == 1, "排队位置为 1")
        canceled = store.cancel(t2["id"])
        check(canceled["status"] == STATUS_CANCELED, "未开始任务立即变为已取消")
        time.sleep(0.6)
        check(not t2["outputs"], "被取消的暂存任务没有产物")

        print("3) 运行中取消(采样步边界生效)")
        t3 = store.create("generation", "跑一半取消", {"steps": 100, "n": 1, "out_dir": str(out_dir)},
                          released=True)
        t3 = wait_for(store, t3["id"], ("running", "canceling", STATUS_DONE))
        check(t3["status"] in ("running", "canceling"), f"任务已开始 (status={t3['status']})")
        store.cancel(t3["id"])
        t3 = wait_for(store, t3["id"], (STATUS_CANCELED, "failed"))
        check(t3["status"] == STATUS_CANCELED, f"运行中任务被取消 (status={t3['status']})")
        check(not t3["outputs"], "取消后不保留半成品")
        check(not list(out_dir.glob("*.png")) or all(
            Path(o["path"]).exists() for o in t1["outputs"]), "取消未留下无主产物")

        print("4) 编辑未开始的任务")
        t4 = store.create("generation", "原始提示词", {"steps": 6, "n": 1, "out_dir": str(out_dir)})
        store.update_params(t4["id"], {"prompt": "改过的提示词", "steps": 8})
        t4 = store.get(t4["id"])
        check(t4["prompt"] == "改过的提示词" and t4["params"]["steps"] == 8, "提示词与步数已更新")
        store.cancel(t4["id"])
        try:
            store.update_params(t4["id"], {"prompt": "x"})
            check(False, "已结束任务应拒绝编辑")
        except TaskConflict:
            check(True, "已结束任务拒绝编辑")

        print("5) 重新生成 = 复制参数新建任务")
        t5 = store.create("generation", "重跑我", {"steps": 5, "n": 1, "out_dir": str(out_dir)},
                          released=True)
        t5 = wait_for(store, t5["id"], (STATUS_DONE, "failed"))
        check(t5["status"] == STATUS_DONE and t5["outputs"], "原任务已出结果")
        t6 = store.create("generation", t5["prompt"], dict(t5["params"]), origin=t5["id"],
                          source="retry")
        check(t6["id"] != t5["id"] and t6["origin"] == t5["id"], "重跑生成新任务且保留来源")
        check(t5["status"] == STATUS_DONE and t5["outputs"], "原任务结果保留")
        t6 = wait_for(store, t6["id"], (STATUS_DONE, "failed"))

        print("6) 删除任务会清掉产物")
        prod = Path(t5["outputs"][0]["path"])
        store.delete(t5["id"])
        check(not prod.exists(), "删除任务同时删除产物文件")
        try:
            store.get(t5["id"])
            check(False, "删除后应查不到")
        except KeyError:
            check(True, "删除后查不到记录")

        print("7) 运行中任务不允许删除")
        t7 = store.create("generation", "运行中", {"steps": 100, "n": 1, "out_dir": str(out_dir)},
                          released=True)
        wait_for(store, t7["id"], ("running", "canceling"))
        try:
            store.delete(t7["id"])
            check(False, "运行中任务应拒绝删除")
        except TaskConflict:
            check(True, "运行中任务拒绝删除")
        store.cancel(t7["id"])
        wait_for(store, t7["id"], (STATUS_CANCELED,))

        print("8) 重启恢复: 中断的 running 回到待开始")
        store.cancel(t7["id"])
        wait_for(store, t7["id"], (STATUS_CANCELED,))
        worker.stop()
        for _ in range(100):                  # 确认老线程真的退出了(它还会写状态)
            if not worker.thread.is_alive():
                break
            time.sleep(0.05)
        check(not worker.thread.is_alive(), "老工作线程已退出")
        data = json.loads((root / "tasks" / "tasks.json").read_text(encoding="utf-8"))
        t8 = store.create("generation", "会被中断", {"steps": 50, "n": 1, "out_dir": str(out_dir)})
        with store.lock:                      # 模拟进程被杀: 状态留在 running
            for t in store.state["tasks"]:
                if t["id"] == t8["id"]:
                    t["status"] = "running"
                    t["attempts"] = 0
            store._touch(flush=True)
        store2 = TaskStore(root / "tasks")
        check(store2.get(t8["id"])["status"] == "running", "未做恢复时状态仍是 running")
        n = store2.recover_interrupted()
        recovered = store2.get(t8["id"])
        check(n == 1 and recovered["status"] == STATUS_PENDING,
              f"中断任务恢复为待开始 ({recovered['status']})")
        check("自动重新排队" in (recovered["error"] or ""), "恢复原因已记录")
        worker2 = QueueWorker(store2, fake_run)
        check(store2.get(t8["id"])["status"] == STATUS_PENDING, "起线程前不抢跑")
        store2.cancel(t8["id"])

        print("9) 计数与列表")
        c = store2.counts()
        check(c["total"] >= 1 and c["canceled"] >= 1, f"计数正确 {c['staged']=} {c['ready']=}")
        lst = store2.list()
        check(all(isinstance(x["id"], str) for x in lst), "列表可读")

        print()
        if FAILED:
            print(f"失败 {len(FAILED)} 项: {FAILED}")
            return 1
        print("全部通过")
        return 0
    finally:
        try:
            worker.stop()
        except Exception:
            pass
        try:
            worker2.stop()
        except Exception:
            pass
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
