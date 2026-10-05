# -*- coding: utf-8 -*-
"""任务队列 HTTP 冒烟测试: 真的起一个服务实例(临时输出目录, 不加载模型)跑一遍 API。

覆盖: 入队(暂存/自动)、取消未开始任务、编辑未开始任务、重新生成、删除、
      落盘持久化与重启后仍在、参考图落盘、非法参数拒绝。

用法:
    venv\\Scripts\\python.exe scripts\\test_server_queue.py
"""
import base64
import io
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


def request(port, method, path, body=None, timeout=30):
    url = f"http://127.0.0.1:{port}{path}"
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
            try:
                return res.status, json.loads(raw.decode("utf-8"))
            except Exception:
                return res.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8"))
        except Exception:
            return e.code, raw


def wait_ready(port, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            status, _ = request(port, "GET", "/health", timeout=3)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


class Server:
    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.port = free_port()
        self.proc = None

    def start(self):
        env = dict(os.environ)
        env["QWEN_OUTPUT_DIR"] = str(self.out_dir)
        env["PYTHONIOENCODING"] = "utf-8"
        env["QWEN_QUEUE_AUTOSTART"] = "1"
        self.log = open(self.out_dir / "server.log", "ab")
        self.proc = subprocess.Popen(
            [str(PY), "-u", "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--lifespan", "off", "--log-level", "warning"],
            cwd=str(BASE), env=env, stdout=self.log, stderr=subprocess.STDOUT)
        if not wait_ready(self.port):
            self.stop()
            raise RuntimeError("服务未能在超时内就绪, 见 " + str(self.out_dir / "server.log"))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        try:
            self.log.close()
        except Exception:
            pass


def png_b64(text="ref"):
    """造一张 8x8 PNG 并转 base64 data URL。"""
    sys.path.insert(0, str(BASE))
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (30, 60, 90)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), buf.getvalue()


def main():
    root = Path(tempfile.mkdtemp(prefix="qwen-http-test-"))
    out_dir = root / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    server = Server(out_dir)
    try:
        server.start()
        port = server.port
        print(f"服务已启动: 端口 {port}, 输出目录 {out_dir}")

        print("1) 队列初始状态")
        status, queue = request(port, "GET", "/api/queue")
        check(status == 200 and queue["counts"]["total"] == 0, "队列为空", queue)
        check("max_steps" in queue and "max_ref_images" in queue, "队列参数上限已返回")

        print("2) 关闭自动开始 -> 新任务应暂存")
        status, res = request(port, "POST", "/api/queue/auto-start", {"auto_start": False})
        check(status == 200 and res["auto_start"] is False, "auto_start 已关闭")

        status, res = request(port, "POST", "/v1/images/generations",
                              {"prompt": "暂存的猫", "steps": 4, "n": 1, "queue": True})
        check(status == 200 and res["queued"] == 1, "入队成功", res)
        tid = res["data"][0]["id"]
        status, task = request(port, "GET", f"/api/tasks/{tid}")
        check(task["status"] == "pending" and task["staged"] is True, "任务处于暂存态", task)
        check(task["queue_position"] == 1, "排队位置为 1")

        print("3) 取消未开始的任务")
        status, task = request(port, "POST", f"/api/tasks/{tid}/cancel", {})
        check(status == 200 and task["status"] == "canceled", "未开始任务被取消", task)
        status, task = request(port, "POST", f"/api/tasks/{tid}/release", {})
        check(status == 409, "已取消任务不能再释放", f"status={status}")

        print("4) 编辑未开始的任务")
        status, res = request(port, "POST", "/v1/images/generations",
                              {"prompt": "原始", "steps": 4, "n": 1, "queue": True})
        tid2 = res["data"][0]["id"]
        status, task = request(port, "PATCH", f"/api/tasks/{tid2}",
                               {"prompt": "改过的提示词", "steps": 9, "aspect_ratio": "16:9"})
        check(status == 200 and task["prompt"] == "改过的提示词", "提示词已更新", task)
        check(task["params"]["steps"] == 9, "步数已更新")
        check(task["params"]["width"] == 1024 and task["params"]["height"] == 576,
              f"长宽比已重算 {task['params']['width']}x{task['params']['height']}")
        orig_seed = task["params"]["seed"]
        status, err = request(port, "PATCH", f"/api/tasks/{tid2}", {"steps": 999})
        check(status == 400, "越界参数被拒绝(400)", err)
        status, task = request(port, "POST", f"/api/tasks/{tid2}/cancel", {})
        status, err = request(port, "PATCH", f"/api/tasks/{tid2}", {"prompt": "x"})
        check(status == 409, "已结束任务拒绝编辑(409)", err)

        print("5) 重新生成(新建任务 + 保留原有记录)")
        status, res = request(port, "POST", f"/api/tasks/{tid2}/retry",
                              {"prompt": "重跑用提示词", "keep_seed": True})
        check(status == 200 and res["origin"] == tid2, "重跑任务记录来源", res)
        check(res["prompt"] == "重跑用提示词" and res["status"] == "pending", "重跑参数已覆盖")
        check(res["params"]["steps"] == 9, "重跑沿用原步数")
        status, old = request(port, "GET", f"/api/tasks/{tid2}")
        check(old["status"] == "canceled", "原任务记录未被改动")

        print("6) 批量提交(prompts) 与过滤")
        status, res = request(port, "POST", "/v1/images/generations",
                              {"prompts": ["批量A", "批量B", "批量C"], "steps": 4})
        check(status == 200 and res["queued"] == 3, "prompts 拆成 3 条任务", res)
        status, listing = request(port, "GET", "/api/tasks?status=pending")
        # 此刻待开始 = 重跑任务 1 条 + 批量 3 条 (前面两条已取消)
        check(len(listing["tasks"]) == 4, f"筛选待开始任务数 4, 实际 {len(listing['tasks'])}")
        check(all(t["status"] == "pending" for t in listing["tasks"]), "筛选结果状态正确")

        print("6b) 重新生成不能再丢 seed(回归: 任务执行时 KeyError('seed'))")
        # 队列行里的 ↻ 按钮就是 POST {} —— 不传 keep_seed。旧版本这里把 seed 从计划里删掉,
        # 任务一进 worker 就抛 KeyError('seed'), 进度列显示红色报错。
        status, res = request(port, "POST", f"/api/tasks/{tid2}/retry", {})
        check(status == 200, "重新生成(默认换随机种子)成功", res)
        check(isinstance(res["params"].get("seed"), int),
              "新任务计划里一定带 seed(执行时才不会 KeyError)", res["params"].get("seed"))
        check(res["params"]["seed"] != orig_seed, "默认重新生成换了一颗种子")
        check((res["params"]["width"], res["params"]["height"]) == (1024, 576),
              f"重新生成沿用原画布 {res['params']['width']}x{res['params']['height']}")

        print("6c) 自定义分辨率(size / long_side / 单边上限)")
        status, res = request(port, "POST", "/v1/images/generations",
                              {"prompt": "自定义尺寸", "size": "1280x720", "steps": 4,
                               "queue": True})
        check(status == 200 and res["queued"] == 1, "带 size 的任务入队", res)
        ctid = res["data"][0]["id"]
        status, ctask = request(port, "GET", f"/api/tasks/{ctid}")
        check((ctask["params"]["width"], ctask["params"]["height"]) == (1280, 720),
              f"size=1280x720 生效 {ctask['params']['width']}x{ctask['params']['height']}")
        check(ctask["params"].get("size") == "1280x720", "任务参数里记下实际画布")
        status, ctask = request(port, "PATCH", f"/api/tasks/{ctid}", {"prompt": "只改提示词"})
        check((ctask["params"]["width"], ctask["params"]["height"]) == (1280, 720),
              "只改提示词时画布原样保住")
        status, ctask = request(port, "PATCH", f"/api/tasks/{ctid}", {"size": "768x512"})
        check((ctask["params"]["width"], ctask["params"]["height"]) == (768, 512),
              f"PATCH 显式改 size 生效(不再被静默丢掉) "
              f"{ctask['params']['width']}x{ctask['params']['height']}")
        status, ctask = request(port, "PATCH", f"/api/tasks/{ctid}",
                                {"aspect_ratio": "1:1", "long_side": 1024})
        check((ctask["params"]["width"], ctask["params"]["height"]) == (1024, 1024),
              "改长宽比后画布重算(旧 size 让位)")
        status, res = request(port, "POST", "/v1/images/generations",
                              {"prompt": "超上限", "size": "4096x4096", "steps": 4,
                               "queue": True})
        big = request(port, "GET", f"/api/tasks/{res['data'][0]['id']}")[1]
        check((big["params"]["width"], big["params"]["height"])
              == (queue["max_side"], queue["max_side"]),
              f"超过单边上限的 size=4096x4096 被缩到 "
              f"{big['params']['width']}x{big['params']['height']}"
              f"(网页会在提交前就拦下并报错)")
        check(big["params"].get("size") == f"{queue['max_side']}x{queue['max_side']}",
              "计划里的 size 与实际画布一致")
        check(all(k in queue for k in ("max_side", "default_long_side", "min_side",
                                       "side_step")),
              "队列接口下发分辨率上限/默认长边/最小边/步长(网页据此校验)",
              {k: queue.get(k) for k in ("max_side", "default_long_side", "min_side", "side_step")})

        print("7) 编辑任务(图生图) 参考图落盘")
        data_url, png_bytes = png_b64()
        status, res = request(port, "POST", "/v1/images/edits/json",
                              {"prompt": "把背景换成雪山", "images": [data_url],
                               "steps": 4, "queue": True})
        check(status == 200 and res["queued"] == 1, "编辑任务入队", res)
        etid = res["data"][0]["id"]
        status, etask = request(port, "GET", f"/api/tasks/{etid}")
        check(etask["kind"] == "edit" and etask["refs"] == ["ref1.png"],
              "参考图已落盘为 ref1.png", etask["refs"])
        check("base64" not in json.dumps(etask), "任务记录内不存 base64")
        ref_file = out_dir / "tasks" / "refs" / etid / "ref1.png"
        check(ref_file.is_file() and ref_file.read_bytes() == png_bytes, "参考图文件内容一致")
        status, raw = request(port, "GET", f"/api/tasks/{etid}/refs/ref1.png")
        check(status == 200 and raw == png_bytes, "参考图可通过 API 取回")

        print("8) 删除任务会清掉参考图与记录")
        status, res = request(port, "DELETE", f"/api/tasks/{etid}")
        check(status == 200 and res["deleted"] == etid, "任务已删除", res)
        check(not ref_file.exists(), "参考图目录已清理")
        status, err = request(port, "GET", f"/api/tasks/{etid}")
        check(status == 404, "删除后详情返回 404")
        status, res = request(port, "POST", "/api/queue/delete", {"ids": [tid2]})
        check(status == 200 and res["deleted"] == [tid2], "批量删除接口可用", res)

        print("8b) 批量路由不被参数化路由遮蔽(回归)")
        status, res = request(port, "POST", "/api/queue/release-all", {})
        check(status == 200 and res["auto_start"] is True, "release-all 可用", res)
        status, res = request(port, "POST", "/api/queue/delete", {"ids": []})
        check(status == 400, "空 ids 被拒绝(400)", res)

        print("9) 落盘 + 重启后仍在")
        before = request(port, "GET", "/api/tasks")[1]["tasks"]
        pending_before = [t["id"] for t in before if t["status"] == "pending"]
        tasks_json = out_dir / "tasks" / "tasks.json"
        check(tasks_json.is_file(), "tasks.json 已落盘")
        server.stop()
        server2 = Server(out_dir)
        server2.start()
        port = server2.port
        after = request(port, "GET", "/api/tasks")[1]["tasks"]
        pending_after = [t["id"] for t in after if t["status"] == "pending"]
        check(pending_after == pending_before,
              f"重启后待开始任务一致 ({len(pending_after)} 条)")
        check(len(after) == len(before), f"重启后任务总数一致 ({len(after)})")
        status, task = request(port, "GET", f"/api/tasks/{pending_before[0]}")
        check(task["prompt"], "重启后仍能编辑(取回提示词)")

        print("10) 队列页与静态页可用")
        status, _ = request(port, "GET", "/")
        check(status == 200, "GET / 返回页面")
        status, info = request(port, "GET", "/api")
        check(status == 200 and "GET /api/tasks" in info["endpoints"], "/api 已列出队列接口")

        print()
        if FAILED:
            print(f"失败 {len(FAILED)} 项:")
            for item in FAILED:
                print("   - " + item)
            return 1
        print("全部通过")
        return 0
    finally:
        server.stop()
        try:
            server2.stop()
        except Exception:
            pass
        log = out_dir / "server.log"
        if FAILED and log.is_file():
            print("\n--- server.log 末尾 ---")
            print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]))
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
