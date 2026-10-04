#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_gric_key.py —— GRIC X-SIGNATURE 密钥提取与回归验证（脱敏版）。

实现 GRIC X-SIGNATURE 密钥提取与回归校验流程，
两个子命令（均不硬编码任何密钥）：

  scan  在线提取：dump 设备 Java 堆 -> 扫 STS 现场 -> 找候选密钥 -> HMAC 自验证
        python3 tools/extract_gric_key.py scan [--serial <SN>] [--pid <PID>]
                    [--max-mb 1200] [--write] [--mask]

        --write   验证命中后自动写入 ../geely_env.json 的 gricSecret 字段
        --mask    输出时打码密钥（默认全显，仅本地终端使用）

  har   离线回归：用已知密钥复算 HAR 里的全部签名请求，要求 100% 一致
        python3 tools/extract_gric_key.py har <capture.har> [--secret <KEY>]

        密钥来源优先级：--secret > 环境变量 GEELY_GRIC_SECRET > geely_env.json

  vehicle  在线/离线提取车辆头：dump 堆 -> 扫 x-vehicle-identifier/series 现场
        python3 tools/extract_gric_key.py vehicle [--serial <SN>] [--pid <PID>]
                    [--max-mb 1200] [--write] [--mask]
        python3 tools/extract_gric_key.py vehicle --dump <heap.bin>   # 离线分析已有 dump

        --write  命中后写入 ../geely_env.json 的 vehicle 字段

        背景：x-vehicle-identifier 是 AES 加密的 VIN（密文 32 字节/44 字符 Base64，
        服务端会解密校验，传 VIN 明文会被拒），x-vehicle-series 是 Base64(车型代码)
        如 Base64("DCY11-A2")。二者均为登录/绑定后由 App 生成、长期稳定的值，
        在签名瞬间随 STS 一起以明文出现在堆中，可直接扫描提取。
        VIN/车型代码明文也可经 favorite-vehicles 接口（无需车辆头）获取，
        但明文无法在本地反推密文（AES key 为 App 内另一常量）。

前置条件（scan）：
  * root 且可直读 /proc/<pid>/mem 的环境（userdebug 模拟器）
  * App 已登录并挂前台（心跳 app/hb 持续重签，现场必然存在）
  * adb 在 PATH 或用 --serial 指定设备

原理（详见逆向文档）：
  签名计算瞬间，(完整 STS, HMAC 密钥, 签名结果) 同时以明文存在于堆中。
  以 STS 第一行 accept-language: 为锚点提取，锚点后依次为
  签名 raw digest(byte[32]) -> 签名 Base64 -> 密钥(32 可打印字符)。
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, "..", "geely_env.json")

STS_ANCHOR = b"accept-language:"
# STS 内必须出现的行（自检锚点截取完整性）
STS_MUST_HAVE = (b"\nauthorization:", b"\nx-api-signature-nonce:", b"\nx-app-id:")
B64_RE = re.compile(rb"[A-Za-z0-9+/]{43}=")
# 密钥候选：32 个可打印字符（经验上为十六进制小写）
KEY_RE = re.compile(rb"[!-~]{32}")
SCAN_WINDOW_AFTER_STS = 4096  # STS 结束后向后扫描多远找密钥/签名

# 车辆头提取：STS/header 现场中的 "x-vehicle-identifier:<value>" 行。
# 密文形态 = 32 字节 AES 密文的 Base64（43 字符 + '='）；
# series = Base64(车型代码)（如 DCY11-A2 -> RENZMTEtQTI=，长度不固定）。
VEH_IDENTIFIER_RE = re.compile(rb"x-vehicle-identifier:([A-Za-z0-9+/]{43}=)")
VEH_SERIES_RE = re.compile(rb"x-vehicle-series:([A-Za-z0-9+/]{8,42}={1,2})")
# 顺带收集 VIN 明文（17 位车架号，排除 I/O/Q）用于对照展示；
# 排除已知 17 字符静态常量（如 appId GEELYCNCH001M0001）免得误报
VIN_RE = re.compile(rb"\b[A-HJ-NPR-Z0-9]{17}\b")
VIN_EXCLUDE = {b"GEELYCNCH001M0001"}

PKG = os.environ.get("GEELY_PKG", "com.lynkco.customer")


def adb(*args, serial=None, binary=False, timeout=600):
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    out = subprocess.run(cmd, capture_output=True, timeout=timeout)
    data = out.stdout if binary else out.stdout.decode("utf-8", "replace")
    if out.returncode != 0 and not binary:
        print(f"[错误] {' '.join(cmd)} 失败: {out.stderr.decode('utf-8', 'replace')[:300]}")
    return data


# ---------------------------------------------------------------- scan ----

def get_pid(serial):
    pids = adb("shell", "pidof", PKG, serial=serial).strip().split()
    if not pids:
        print(f"[错误] 未找到进程 {PKG}，请确认 App 已在前台运行")
        sys.exit(1)
    return pids[0]


def find_heap_regions(serial, pid, max_mb):
    """从 /proc/<pid>/maps 找 dalvik 主堆 RW 区域。"""
    maps = adb("shell", "cat", f"/proc/{pid}/maps", serial=serial)
    regions = []
    for line in maps.splitlines():
        m = re.match(r"([0-9a-f]+)-([0-9a-f]+) (rw\S*) \S+ \S+ \S+\s*(.*)", line)
        if not m:
            continue
        start, end, perms, name = int(m.group(1), 16), int(m.group(2), 16), m.group(3), m.group(4).strip()
        if "dalvik-main space" in name and perms.startswith("rw"):
            regions.append((start, end, name))
    if not regions:
        print("[错误] maps 中未找到 dalvik-main space RW 区域")
        sys.exit(1)
    total = sum(e - s for s, e, _ in regions)
    print(f"[*] 找到 {len(regions)} 个堆区域，共 {total / 1048576:.0f} MB：")
    for s, e, n in regions:
        print(f"    {s:#x}-{e:#x} ({(e - s) / 1048576:.0f} MB) {n}")
    if total / 1048576 > max_mb:
        print(f"[警告] 超过 --max-mb {max_mb}，按区域倒序只取前若干个（大区域优先）")
        kept, acc = [], 0
        for s, e, n in sorted(regions, key=lambda r: r[1] - r[0], reverse=True):
            if acc / 1048576 >= max_mb:
                break
            kept.append((s, e, n))
            acc += e - s
        regions = kept
    return regions


def dump_region(serial, pid, start, end):
    """流式拉取一段内存（exec-out 避免 shell tty 污染二进制）。"""
    size = end - start
    skip, cnt = start // 4096, size // 4096
    data = adb("exec-out", "su", "-c",
               f"dd if=/proc/{pid}/mem bs=4096 skip={skip} count={cnt}",
               serial=serial, binary=True, timeout=1800)
    return data[:cnt * 4096]


def printable_run(data, off):
    """从 off 读连续可打印+\n 的段，返回 (bytes, end)。"""
    end = off
    n = len(data)
    while end < n and (32 <= data[end] < 127 or data[end] == 10):
        end += 1
    return data[off:end], end


def extract_sts_candidates(blob):
    """返回 [(sts_bytes, blob_offset)]。锚点=accept-language:，含完整性自检。"""
    out = []
    pos = 0
    while True:
        off = blob.find(STS_ANCHOR, pos)
        if off < 0:
            break
        pos = off + 1
        run, end = printable_run(blob, off)
        # 自检：必须是完整 STS（含 JWT 行、nonce、method、path）
        if all(must in run for must in STS_MUST_HAVE):
            lines = run.split(b"\n")
            if len(lines) >= 6 and lines[-2] in (b"POST", b"GET", b"PUT", b"DELETE"):
                out.append((run, off))
    return out


def find_candidates_after(blob, end):
    """STS 结束位置之后的小窗口内找 (b64 签名候选, 32 字符密钥候选)。"""
    window = blob[end:end + SCAN_WINDOW_AFTER_STS]
    sigs = [m.group(0) for m in B64_RE.finditer(window)]
    keys = []
    for m in KEY_RE.finditer(window):
        cand = m.group(0)
        # 排除误命中：纯 hex / 含 x- 头 / base64 段本身由调用方验证
        if cand.startswith(b"x-") or b":" in cand:
            continue
        keys.append(cand)
    return sigs, keys


def try_verify(sts, sig_b64, key):
    digest = hmac.new(key, sts, hashlib.sha256).digest()
    return base64.b64encode(digest) == sig_b64


def cmd_scan(args):
    pid = args.pid or get_pid(args.serial)
    print(f"[*] PID = {pid}（{PKG}），App 需保持前台（心跳持续重签）")
    regions = find_heap_regions(args.serial, pid, args.max_mb)

    sts_hits, pairs = [], []
    for start, end, name in regions:
        print(f"[*] dump {start:#x}-{end:#x} ...")
        try:
            blob = dump_region(args.serial, pid, start, end)
        except subprocess.TimeoutExpired:
            print(f"[警告] 该区域 dump 超时，跳过")
            continue
        if not blob:
            print(f"[警告] 该区域读取为空（可能已被回收/反保护），跳过")
            continue
        for sts, off in extract_sts_candidates(blob):
            sts_hits.append((sts, start + off, blob, off + len(sts)))
        print(f"    {len(blob) / 1048576:.0f} MB，累计 STS 现场 {len(sts_hits)} 处")

    if not sts_hits:
        print("[错误] 未找到任何完整 STS 现场。排查：1) App 是否在前台且已登录；"
              "2) 心跳接口是否在跑（等 30s 后重试）；3) 堆区域是否被 --max-mb 截掉")
        sys.exit(2)

    # 第一轮：现场内自验证（STS 后窗口内的 b64 签名 × 32 字符候选）
    verified = set()
    for sts, addr, blob, end in sts_hits:
        sigs, keys = find_candidates_after(blob, end)
        for sig in sigs:
            for key in keys:
                if try_verify(sts, sig, key):
                    verified.add(key)
    # 第二轮：跨现场交叉验证（同一候选能对上多个现场的签名，防巧合）
    if verified:
        cross = {}
        for key in verified:
            cross[key] = 0
        for sts, addr, blob, end in sts_hits:
            sigs, keys = find_candidates_after(blob, end)
            for sig in sigs:
                for key in verified:
                    if try_verify(sts, sig, key):
                        cross[key] += 1
        best = max(cross.items(), key=lambda kv: kv[1])
        print(f"\n[★] 密钥命中（跨 {best[1]}/{len(sts_hits)} 个签名现场验证通过）：")
        shown = f"{best[0].decode()} (验证 {best[1]} 处)" if not args.mask else \
            f"{best[0][:6].decode()}******{best[0][-4:].decode()} (验证 {best[1]} 处)"
        print(f"    {shown}")
        if args.write:
            write_env_secret(best[0].decode())
        return
    print(f"\n[未命中] {len(sts_hits)} 个 STS 现场均未验证成功。")
    print("可能原因：密钥形态不是 32 可打印字符（v2.2+ 算法升级？），或密钥不在 STS 后 4KB 窗口。")
    print("可人工检查：打印任一 STS 现场后 4KB 内容，观察 byte[32] 对象分布。")
    sys.exit(3)


def write_env_secret(secret):
    data = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    data["gricSecret"] = secret
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[*] 已写入 {ENV_FILE} 的 gricSecret 字段（该文件在 .gitignore 中，不会提交）")


# ------------------------------------------------------------- vehicle ----

def _decode_series(series_b64: bytes) -> str:
    """series 是 Base64(车型代码)，解码失败返回 '?'。"""
    try:
        raw = base64.b64decode(series_b64, validate=True)
        return raw.decode("ascii")
    except Exception:
        return "?"


def cmd_vehicle(args):
    """dump 堆（或读 --dump 离线文件）扫描车辆头现场。"""
    ident_count, series_count, vin_count = {}, {}, {}

    def scan_blob(blob):
        for m in VEH_IDENTIFIER_RE.finditer(blob):
            ident_count[m.group(1)] = ident_count.get(m.group(1), 0) + 1
        for m in VEH_SERIES_RE.finditer(blob):
            series_count[m.group(1)] = series_count.get(m.group(1), 0) + 1
        for m in VIN_RE.finditer(blob):
            if m.group(0) not in VIN_EXCLUDE:
                vin_count[m.group(0)] = vin_count.get(m.group(0), 0) + 1

    if args.dump:
        print(f"[*] 离线模式：读取 {args.dump} ...")
        with open(args.dump, "rb") as f:
            scan_blob(f.read())
    else:
        pid = args.pid or get_pid(args.serial)
        print(f"[*] PID = {pid}（{PKG}），App 保持前台（心跳持续重签，现场必然存在）")
        regions = find_heap_regions(args.serial, pid, args.max_mb)
        for start, end, name in regions:
            print(f"[*] dump {start:#x}-{end:#x} ...")
            try:
                blob = dump_region(args.serial, pid, start, end)
            except subprocess.TimeoutExpired:
                print("[警告] 该区域 dump 超时，跳过")
                continue
            if not blob:
                print("[警告] 该区域读取为空（可能已被回收/反保护），跳过")
                continue
            scan_blob(blob)
            print(f"    {len(blob) / 1048576:.0f} MB 扫描完成")

    if not ident_count:
        print("[错误] 未找到 x-vehicle-identifier 密文现场。排查：1) App 是否前台且已登录；"
              "2) 心跳/车辆接口是否在跑（等 30s 重试）；3) 堆区域是否被 --max-mb 截掉")
        sys.exit(2)

    best_ident = max(ident_count.items(), key=lambda kv: kv[1])
    best_series = max(series_count.items(), key=lambda kv: kv[1]) if series_count else (None, 0)

    def mask(s):
        return f"{s[:6]}******{s[-4:]}" if args.mask else s

    print(f"\n[★] x-vehicle-identifier（AES 密文，出现 {best_ident[1]} 次）：")
    print(f"    {mask(best_ident[0].decode())}")
    if best_series[0]:
        decoded = _decode_series(best_series[0])
        print(f"[★] x-vehicle-series（Base64(车型代码)，出现 {best_series[1]} 次）：")
        print(f"    {mask(best_series[0].decode())}  ->  车型代码 {decoded}")
    if vin_count:
        top_vin = max(vin_count.items(), key=lambda kv: kv[1])
        print(f"[i] 堆中 VIN 明文（对照用，出现 {top_vin[1]} 次）：{top_vin[0].decode()}")
    if len(ident_count) > 1:
        print(f"[警告] identifier 候选有 {len(ident_count)} 个（多车账号？），按频次取最高，")
        print("       其余候选：")
        for cand, n in sorted(ident_count.items(), key=lambda kv: -kv[1])[1:]:
            print(f"         {mask(cand.decode())} ({n} 次)")

    if args.write:
        data = {}
        if os.path.exists(ENV_FILE):
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        veh = data.get("vehicle", {})
        veh["identifier"] = best_ident[0].decode()
        if best_series[0]:
            veh["series"] = best_series[0].decode()
        veh.setdefault("brand", "LYNKCO")
        data["vehicle"] = veh
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[*] 已写入 {ENV_FILE} 的 vehicle 字段（该文件在 .gitignore 中，不会提交）")


# ----------------------------------------------------------------- har ----

def load_secret(cli_secret):
    if cli_secret:
        return cli_secret
    if os.environ.get("GEELY_GRIC_SECRET"):
        return os.environ["GEELY_GRIC_SECRET"]
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            v = json.load(f).get("gricSecret")
            if v:
                return v
    print("[错误] 未提供密钥：用 --secret、环境变量 GEELY_GRIC_SECRET 或 geely_env.json")
    sys.exit(1)


def build_sts_from_har(entry):
    """按算法规格从 HAR 条目重建 STS；返回 (sts, method, path) 或 None。"""
    req = entry.get("request", {})
    headers = {h["name"].lower(): h["value"] for h in req.get("headers", [])}
    if "x-signature" not in headers:
        return None
    url = req.get("url", "")
    after_scheme = url.partition("://")[2]
    path_and_q = after_scheme.split("/", 1)[1] if "/" in after_scheme else ""
    path = "/" + path_and_q
    path, _, query = path.partition("?")
    method = req.get("method", "GET").upper()

    lines = []
    for name in ("accept-language", "authorization"):
        if headers.get(name):
            lines.append(f"{name}:{headers[name]}")
    for k in sorted(k for k in headers if k.startswith("x-") and k != "x-signature"):
        lines.append(f"{k}:{headers[k]}")
    body = (req.get("postData") or {}).get("text", "")
    if body:
        lines.append(base64.b64encode(hashlib.md5(body.encode()).digest()).decode())
    if query:
        lines.append(query)
    lines.append(method)
    lines.append(path)
    return "\n".join(lines), headers["x-signature"], method, path


def cmd_har(args):
    secret = load_secret(args.secret)
    with open(args.har, "r", encoding="utf-8") as f:
        har = json.load(f)
    entries = har.get("log", {}).get("entries", [])
    total = ok = 0
    fails = []
    for i, entry in enumerate(entries):
        built = build_sts_from_har(entry)
        if not built:
            continue
        sts, expect, method, path = built
        total += 1
        got = base64.b64encode(hmac.new(secret.encode(), sts.encode(), hashlib.sha256).digest()).decode()
        if got == expect:
            ok += 1
        else:
            fails.append((i, method, path))
    print(f"[*] 签名请求共 {total} 个，复算一致 {ok} 个")
    for i, m, p in fails:
        print(f"    ✗ entry#{i} {m} {p}")
    if total == 0:
        print("[错误] HAR 中没有带 x-signature 的请求")
        sys.exit(2)
    if ok == total:
        print("[✓] 100% 一致，密钥与 STS 规则确认")
        return
    print(f"[✗] {total - ok} 个不一致：密钥错误或 STS 规则已变化（不可采用该候选）")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="GRIC X-SIGNATURE 密钥提取与回归验证（脱敏）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="在线提取：dump 堆 -> 扫 STS -> 验证密钥")
    p_scan.add_argument("--serial", default=None, help="adb 设备序列号（默认取唯一设备）")
    p_scan.add_argument("--pid", type=int, default=None, help="目标进程 PID（默认 pidof 自动获取）")
    p_scan.add_argument("--max-mb", type=int, default=1200, help="最多 dump 多少 MB 堆")
    p_scan.add_argument("--write", action="store_true", help="命中后写入 geely_env.json")
    p_scan.add_argument("--mask", action="store_true", help="输出打码密钥")
    p_scan.set_defaults(func=cmd_scan)

    p_har = sub.add_parser("har", help="离线回归：复算 HAR 全部签名（要求 100%%）")
    p_har.add_argument("har", help="HAR 文件路径")
    p_har.add_argument("--secret", default=None, help="HMAC 密钥（默认读环境变量/geely_env.json）")
    p_har.set_defaults(func=cmd_har)

    p_veh = sub.add_parser("vehicle", help="提取车辆头 identifier/series（在线 dump 或离线 dump 文件）")
    p_veh.add_argument("--serial", default=None, help="adb 设备序列号（默认取唯一设备）")
    p_veh.add_argument("--pid", type=int, default=None, help="目标进程 PID（默认 pidof 自动获取）")
    p_veh.add_argument("--max-mb", type=int, default=1200, help="最多 dump 多少 MB 堆")
    p_veh.add_argument("--dump", default=None, help="离线模式：分析已有的堆 dump 文件")
    p_veh.add_argument("--write", action="store_true", help="命中后写入 geely_env.json 的 vehicle 字段")
    p_veh.add_argument("--mask", action="store_true", help="输出打码")
    p_veh.set_defaults(func=cmd_vehicle)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
