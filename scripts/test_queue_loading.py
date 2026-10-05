# -*- coding: utf-8 -*-
"""验证「模型加载中排队任务不会失败, 就绪后自动开始」(需要 GPU, 约 1~2 分钟)。

不设 QWEN_TASK_DIR 时默认写到 outputs/tasks, 与真实使用一致; 用 QWEN_OUTPUT_DIR
指到临时目录可避免污染。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY = BASE / "venv" / "Scripts" / "python.exe"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(port, method, path, body=None, timeout=60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def main():
    root = Path(tempfile.mkdtemp(prefix="qwen-race-test-"))
    out_dir = root / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    env = dict(os.environ)
    env["QWEN_OUTPUT_DIR"] = str(out_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(out_dir / "server.log", "ab")
    proc = subprocess.Popen(
        [str(PY), "-u", "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
         "--port", str(port), "--lifespan", "on", "--log-level", "warning"],
        cwd=str(BASE), env=env, stdout=log, stderr=subprocess.STDOUT)
    failures = []
    try:
        # 等 HTTP 起来但不等模型就绪
        t0 = time.time()
        while time.time() - t0 < 60:
            try:
                status, health = request(port, "GET", "/health", timeout=3)
                if status == 200:
                    break
            except Exception:
                pass
            time.sleep(0.3)
        status, health = request(port, "GET", "/health", timeout=5)
        print(f"模型加载状态: {health.get('load')}")

        # 立刻入队: 此刻模型多半还在 loading
        status, res = request(port, "POST", "/v1/images/generations",
                              {"prompt": "加载中就排队的任务", "steps": 4,
                               "size": "512x512", "queue": True})
        tid = res["data"][0]["id"]
        print(f"已入队任务 {tid} (load={health.get('load')})")

        status, queue = request(port, "GET", "/api/queue")
        if health.get("load") == "loading":
            # 「加载中」队列仍会接单, 任务是卡在 ensure_pipe() 里等模型,
            # 于是等待时间会计入 usage.queue_sec; 这里只要求它别失败。
            status, task = request(port, "GET", f"/api/tasks/{tid}")
            if task["status"] not in ("pending", "running"):
                failures.append(f"加载中任务不该进入 {task['status']}")
            else:
                print(f"[ok] 加载中任务状态 {task['status']}(没有因模型未就绪而失败)")

        # 等结果
        t0 = time.time()
        task = None
        while time.time() - t0 < 420:
            status, task = request(port, "GET", f"/api/tasks/{tid}", timeout=30)
            if task["status"] in ("done", "failed"):
                break
            time.sleep(1.0)
        print(f"最终状态: {task['status']}, error={task.get('error')}")
        if task["status"] != "done" or not task.get("outputs"):
            failures.append(f"任务未能完成: {task['status']} {task.get('error')}")
        else:
            print(f"[ok] 模型就绪后自动开始并出图 ({task['usage'].get('elapsed_sec')}s, "
                  f"排队 {task['usage'].get('queue_sec')}s)")
            print(f"[ok] 产物: {Path(task['outputs'][0]['path']).name}")
            if health.get("load") == "loading" and (task["usage"].get("queue_sec") or 0) <= 0:
                failures.append("加载中入队的任务应记录到排队等待时间")
        return 1 if failures else 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            log.close()
        except Exception:
            pass
        if failures:
            print("\n失败:")
            for f in failures:
                print("   - " + f)
            p = out_dir / "server.log"
            if p.is_file():
                print("\n--- server.log 末尾 ---")
                print("\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]))
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
