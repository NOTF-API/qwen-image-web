# -*- coding: utf-8 -*-
"""任务队列 + 真模型集成测试(需要 GPU/模型, 8GB 卡上约 3~6 分钟)。

验证三件只有真管线才能证明的事:
  1. 队列任务能真的出图, 产物落盘且任务转为 done;
  2. 运行中的任务取消真的在采样步边界生效(用 callback_on_step_end), 且不留下半成品;
  3. 任务重启后仍可编辑并「重新生成」(参数与参考图落盘可复用)。

为控时间使用 512x512 / 4 步 —— steps>N 的取消用例需要多跑 1 个任务。
用法:
    venv\\Scripts\\python.exe scripts\\test_queue_gpu.py
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
FAILED = []


def check(cond, label, extra=""):
    print(("  [ok] " if cond else "  [FAIL] ") + label + ("" if cond else f"  {extra}"))
    if not cond:
        FAILED.append(label)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(port, method, path, body=None, timeout=900):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
            kind = res.headers.get("Content-Type", "")
            if "json" in kind:
                return res.status, json.loads(raw.decode("utf-8"))
            return res.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8"))
        except Exception:
            return e.code, raw


def wait_ready(port, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            status, health = request(port, "GET", "/health", timeout=5)
            if status == 200 and health.get("load") in ("ready", "error"):
                return health
        except Exception:
            pass
        time.sleep(1.0)
    return None


def wait_task(port, tid, statuses, timeout=600):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        status, task = request(port, "GET", f"/api/tasks/{tid}", timeout=20)
        last = task
        if task.get("status") in statuses:
            return task
        time.sleep(1.0)
    return last


def main():
    root = Path(tempfile.mkdtemp(prefix="qwen-gpu-test-"))
    out_dir = root / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    env = dict(os.environ)
    env["QWEN_OUTPUT_DIR"] = str(out_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    log_path = out_dir / "server.log"
    log = open(log_path, "ab")
    proc = subprocess.Popen(
        [str(PY), "-u", "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
         "--port", str(port), "--lifespan", "on", "--log-level", "info"],
        cwd=str(BASE), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        health = wait_ready(port)
        check(health is not None and health.get("load") == "ready",
              f"模型就绪 ({health.get('load') if health else '无响应'})",
              f"error={(health or {}).get('error')}")
        if not health or health.get("load") != "ready":
            return 1

        print("1) 队列任务真出图 (512x512 / 4 步)")
        t0 = time.time()
        status, res = request(port, "POST", "/v1/images/generations",
                              {"prompt": "a red cube on white background",
                               "steps": 4, "size": "512x512", "queue": True, "n": 1})
        check(status == 200 and res["queued"] == 1, "任务已入队", res)
        tid = res["data"][0]["id"]
        task = wait_task(port, tid, ("done", "failed"))
        check(task["status"] == "done", f"任务完成 (status={task['status']} err={task.get('error')})")
        outputs = task.get("outputs") or []
        check(len(outputs) == 1, f"产出 1 张图 ({len(outputs)})")
        if outputs:
            path = Path(outputs[0]["path"])
            check(path.is_file() and path.stat().st_size > 1000,
                  f"图片已落盘 {path.name} ({path.stat().st_size if path.is_file() else 0} 字节)")
            check(outputs[0]["width"] == 512 and outputs[0]["height"] == 512,
                  f"尺寸正确 {outputs[0]['width']}x{outputs[0]['height']}")
            status, raw = request(port, "GET", f"/outputs/{path.name}", timeout=30)
            check(status == 200 and isinstance(raw, bytes) and len(raw) > 1000,
                  f"图片可经 /outputs/ 访问 ({len(raw) if isinstance(raw, bytes) else 'json'} 字节)")
        check((task.get("usage") or {}).get("elapsed_sec") is not None,
              f"耗时已记录 {task.get('usage', {}).get('elapsed_sec')}s (墙钟 {time.time() - t0:.0f}s)")
        check((task.get("usage") or {}).get("vram_peak_mb", 0) > 0,
              f"显存峰值已记录 {task.get('usage', {}).get('vram_peak_mb')}MB")

        print("2) 运行中取消 (30 步, 采样中途发取消)")
        status, res = request(port, "POST", "/v1/images/generations",
                              {"prompt": "a busy street at night, many details",
                               "steps": 30, "size": "512x512", "queue": True, "n": 1})
        tid2 = res["data"][0]["id"]
        seen_progress = 0
        t_cancel = None
        t0 = time.time()
        while time.time() - t0 < 300:
            status, task = request(port, "GET", f"/api/tasks/{tid2}", timeout=20)
            step = (task.get("progress") or {}).get("step") or 0
            seen_progress = max(seen_progress, step)
            if task["status"] in ("running", "canceling") and step >= 2 and t_cancel is None:
                t_cancel = time.time()
                request(port, "POST", f"/api/tasks/{tid2}/cancel", {})
            if task["status"] in ("canceled", "done", "failed"):
                break
            time.sleep(0.3)
        check(seen_progress >= 2, f"取消前已看到采样进度 (step={seen_progress})")
        task2 = wait_task(port, tid2, ("canceled", "done", "failed"), timeout=300)
        check(task2["status"] == "canceled", f"运行中任务被取消 (status={task2['status']})")
        check(not task2.get("outputs"), "取消后没有产出文件")
        if t_cancel:
            print(f"      取消在采样 step={seen_progress}/30 生效, 距发出约 "
                  f"{time.time() - t_cancel:.1f}s")
        leftover = [p.name for p in out_dir.glob("*.png")
                    if p.name not in {Path(o["path"]).name for o in outputs}]
        check(not leftover, f"取消未留下无主图片 {leftover}")

        print("3) 重新生成: 复用原任务参数再出一张")
        status, res = request(port, "POST", f"/api/tasks/{tid}/retry", {"keep_seed": True})
        check(status == 200, "重跑任务已创建", res)
        tid3 = res["id"]
        task3 = wait_task(port, tid3, ("done", "failed"))
        check(task3["status"] == "done", f"重跑完成 (status={task3['status']})")
        if outputs and task3.get("outputs"):
            check(task3["outputs"][0]["seed"] == outputs[0]["seed"], "沿用原 seed")
        status, old = request(port, "GET", f"/api/tasks/{tid}")
        check(old["status"] == "done" and old["outputs"], "原任务结果仍保留")

        print("4) 重启后仍可重新生成 (参数/产物落盘)")
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        proc = subprocess.Popen(
            [str(PY), "-u", "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
             "--port", str(port), "--lifespan", "on", "--log-level", "warning"],
            cwd=str(BASE), env=env, stdout=log, stderr=subprocess.STDOUT)
        health2 = wait_ready(port)
        check(health2 is not None and health2.get("load") == "ready", "重启后模型再次就绪")
        status, after = request(port, "GET", "/api/tasks")
        check(len(after["tasks"]) == 3, f"重启后 3 条任务都在 (实际 {len(after['tasks'])})")
        status, res = request(port, "POST", f"/api/tasks/{tid}/retry", {})
        check(status == 200, "重启后仍能重新生成", res)
        check(res.get("params", {}).get("steps") == 4, "重跑沿用落盘的步数参数")

        print("5) 删除任务会清掉它自己的图片")
        status, res = request(port, "DELETE", f"/api/tasks/{tid3}")
        check(status == 200, "删除成功", res)
        removed = Path(task3["outputs"][0]["path"])
        check(not removed.exists(), "产物文件已删除")
        kept = Path(outputs[0]["path"])
        check(kept.exists(), "其它任务的图片未被误删")

        print()
        if FAILED:
            print(f"失败 {len(FAILED)} 项:")
            for item in FAILED:
                print("   - " + item)
            return 1
        print("全部通过")
        return 0
    finally:
        for p in (proc,):
            if p.poll() is None:
                p.terminate()
        try:
            log.close()
        except Exception:
            pass
        if FAILED:
            print("\n--- server.log 末尾 ---")
            print("\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]))
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
