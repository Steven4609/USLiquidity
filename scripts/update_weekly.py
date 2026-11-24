#!/usr/bin/env python3
"""USLiquidity · 美股流动性与机构仓位报告：每周取数脚本

数据源（全部公开、免 key）：
  FRED  fredgraph.csv  WALCL / WTREGEN / RRPONTSYD / BAMLH0A0HYM2 / VIXCLS / SP500 / NASDAQ100 / NFCI
  CFTC  publicreporting.cftc.gov  金融期货持仓 TFF（E-mini S&P 500 13874A、Nasdaq Mini 209742）
  FRED  SOFR / IORB / IOER（日度）→ 资金市场利差 SOFR − IORB
  FINRA cdn.finra.org  每日卖空成交量（CNMS 合并文件）→ SPY / QQQ 场外成交卖空占比（类 DIX）

FINRA 的日度文件很多（2018 年 8 月起约 2,000 个），首次运行一次性下载后保存在
data/finra_short_daily.csv，之后每次只补最近缺的交易日（并重查最近 5 个交易日）。

做法：每次从 2018-01-01 起整段重取、重算。FRED 会修订历史（NFCI 每周都会修订），
整段重取保证与源头最新版本一致；旧版本留在 git 历史里可回溯。
任何一个数据源取不到或数据异常，本次不写任何文件，并以非零状态退出（Actions 显示红叉）。

用法：
  python scripts/update_weekly.py           正常更新
  python scripts/update_weekly.py --check   只测试数据源能否连通，不写文件
"""
from __future__ import annotations

import csv
import io
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
F_LIQ = DATA / "liquidity_weekly.csv"
F_COT = DATA / "cot_equity_index.csv"
F_DASH = DATA / "dashboard_data.json"
F_LATEST = DATA / "latest.json"
F_FINRA = DATA / "finra_short_daily.csv"
FINRA_START = date(2018, 8, 1)
FINRA_SYMS = ["SPY", "QQQ"]

START = "2018-01-01"
UA = {"User-Agent": "Mozilla/5.0 (USLiquidity weekly report; GitHub Actions)"}
WEEKLY_WED = "Weekly, Ending Wednesday"

# (列名, FRED 代码, 聚合方式)  avg = 当周均值, eop = 当周最后一个值
FRED_SERIES = [
    ("fed", "WALCL", "avg"),        # 美联储总资产，百万美元，周三
    ("tga", "WTREGEN", "avg"),      # 财政部一般账户 TGA，百万美元，周三
    ("rrp", "RRPONTSYD", "avg"),    # 隔夜逆回购，十亿美元，日度 → 周均
    ("hy", "BAMLH0A0HYM2", "avg"),  # 高收益债 OAS，%，日度 → 周均（FRED 只给近 3 年）
    ("vix", "VIXCLS", "eop"),       # VIX 收盘
    ("spx", "SP500", "eop"),        # 标普500 指数收盘
    ("ndx", "NASDAQ100", "eop"),    # 纳斯达克100 指数收盘
]
COT_CODES = {"es": "13874A", "nq": "209742"}


def log(*a):
    print(*a, flush=True)


def get(url: str, params: dict | None = None, tries: int = 4) -> requests.Response:
    err = None
    for k in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=60)
            if r.status_code == 200:
                return r
            err = f"HTTP {r.status_code}"
        except requests.RequestException as e:  # 网络错误
            err = str(e)
        time.sleep(5 * (k + 1))
    raise RuntimeError(f"{url} 取数失败：{err}")


def parse_fred(text: str) -> list[tuple[str, float]]:
    rows = list(csv.reader(io.StringIO(text.strip())))
    if not rows or len(rows[0]) < 2:
        raise RuntimeError("FRED 返回格式异常")
    out = []
    for r in rows[1:]:
        if len(r) < 2 or r[1] in ("", "."):
            continue
        out.append((r[0], float(r[1])))
    return out


def fred(code: str, fam: str | None, start: str = START) -> list[tuple[str, float]]:
    params = {"id": code, "cosd": start}
    if fam:
        params.update({"fq": WEEKLY_WED, "fam": fam})
    s = parse_fred(get("https://fred.stlouisfed.org/graph/fredgraph.csv", params).text)
    if len(s) < 100:
        raise RuntimeError(f"FRED {code} 只有 {len(s)} 行，疑似异常")
    return s


def cftc(code: str) -> list[dict]:
    params = {
        "$select": "report_date_as_yyyy_mm_dd,open_interest_all,asset_mgr_positions_long,asset_mgr_positions_short,"
                   "lev_money_positions_long,lev_money_positions_short",
        "cftc_contract_market_code": code,
        "$where": f"report_date_as_yyyy_mm_dd>='{START}'",
        "$order": "report_date_as_yyyy_mm_dd",
        "$limit": "5000",
    }
    rows = get("https://publicreporting.cftc.gov/resource/gpe5-46if.json", params).json()
    if len(rows) < 100:
        raise RuntimeError(f"CFTC {code} 只有 {len(rows)} 行，疑似异常")
    return rows


def f(x, p):
    if x is None:
        return ""
    s = f"{x:.{p}f}"
    return s[1:] if s.startswith("-") and float(s) == 0 else s  # 不写 "-0.0"


def build_liquidity() -> list[list[str]]:
    s = {k: fred(code, fam) for k, code, fam in FRED_SERIES}
    nfci = fred("NFCI", None, "2017-12-01")
    m = {k: dict(v) for k, v in s.items()}
    out = []
    for d, fed in s["fed"]:
        tga, rrp = m["tga"].get(d), m["rrp"].get(d)
        if tga is None or rrp is None:
            continue
        nf = None
        for nd, nv in nfci:  # 取周三当天或之前最近一次 NFCI（NFCI 是周五日期）
            if nd <= d:
                nf = nv
            else:
                break
        fed_bn, tga_bn = fed / 1000, tga / 1000
        out.append([d, f(fed_bn, 0), f(tga_bn, 0), f(rrp, 0), f(fed_bn - tga_bn - rrp, 0),
                    f(m["hy"].get(d), 2), f(nf, 3), f(m["vix"].get(d), 1), f(m["spx"].get(d), 0), f(m["ndx"].get(d), 0)])
    return out


def build_cot() -> list[list[str]]:
    by = {k: {r["report_date_as_yyyy_mm_dd"][:10]: r for r in cftc(c)} for k, c in COT_CODES.items()}

    def pct(r, who):
        if not r:
            return ""
        oi = float(r["open_interest_all"])
        return f(100 * (float(r[who + "_positions_long"]) - float(r[who + "_positions_short"])) / oi, 1)

    dates = sorted(set(by["es"]) | set(by["nq"]))
    return [[d, pct(by["es"].get(d), "asset_mgr"), pct(by["es"].get(d), "lev_money"),
             pct(by["nq"].get(d), "asset_mgr"), pct(by["nq"].get(d), "lev_money")] for d in dates]


def fred_daily(code: str, start: str = "2018-03-01") -> dict[str, float]:
    return dict(parse_fred(get("https://fred.stlouisfed.org/graph/fredgraph.csv", {"id": code, "cosd": start}).text))


def funding_spread() -> dict[str, float]:
    """每日 SOFR − 准备金利率（2021-07-29 起 IORB，之前 IOER），单位基点。"""
    sofr, iorb, ioer = fred_daily("SOFR"), fred_daily("IORB", "2021-07-01"), fred_daily("IOER")
    if len(sofr) < 1000 or len(iorb) < 500:
        raise RuntimeError(f"SOFR/IORB 行数异常：SOFR {len(sofr)}，IORB {len(iorb)}")
    out = {}
    for d, v in sofr.items():
        ref = iorb.get(d) if d >= "2021-07-29" else ioer.get(d)
        if ref is not None:
            out[d] = round((v - ref) * 100, 2)
    return out


def finra_day(d: date):
    """返回 {symbol: (short, total)}；非交易日（文件不存在）返回 None。"""
    url = f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d:%Y%m%d}.txt"
    err = None
    for k in range(5):
        try:
            r = requests.get(url, headers=UA, timeout=60)
            if r.status_code in (403, 404):
                return None
            if r.status_code == 200:
                out = {}
                for line in r.text.splitlines():
                    p = line.split("|")
                    if len(p) >= 5 and p[1] in FINRA_SYMS:
                        out[p[1]] = (float(p[2]), float(p[4]))
                if set(out) != set(FINRA_SYMS):
                    raise RuntimeError(f"FINRA {d} 缺少 {set(FINRA_SYMS) - set(out)}")
                return out
            err = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            err = str(e)
        time.sleep(3 * (k + 1))
    raise RuntimeError(f"FINRA {d} 取数失败：{err}")


def update_finra() -> list[list[str]]:
    """增量更新 FINRA 日度表：只下载缺失的交易日，并重查最近 5 个交易日。"""
    have = {r[0]: r for r in read_csv(F_FINRA)}
    today = datetime.now(timezone.utc).date()
    last = max(have) if have else None
    recheck_from = (date.fromisoformat(last) - timedelta(days=8)) if last else FINRA_START
    days, d = [], FINRA_START
    while d <= today:
        if d.weekday() < 5 and (d.isoformat() not in have or d >= recheck_from):
            days.append(d)
        d += timedelta(days=1)
    log(f"FINRA：需要检查 {len(days)} 个交易日")
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(finra_day, days))
    for d, v in zip(days, res):
        if v:
            have[d.isoformat()] = [d.isoformat()] + [f"{v[s][i]:.0f}" for s in FINRA_SYMS for i in (0, 1)]
    rows = [have[k] for k in sorted(have)]
    if len(rows) < 1500:
        raise RuntimeError(f"FINRA 日度数据只有 {len(rows)} 行，疑似异常")
    return rows


def weekly_join(liq: list[list[str]], spread: dict[str, float], finra: list[list[str]]) -> list[list[str]]:
    """把日度数据按“截至周三的一周”（周四至周三）汇总，接到流动性表后面。"""
    fin = {r[0]: [float(x) for x in r[1:]] for r in finra}
    out = []
    for r in liq:
        end = date.fromisoformat(r[0]); start = end - timedelta(days=6)
        days = [(start + timedelta(days=k)).isoformat() for k in range(7)]
        sp = [spread[d] for d in days if d in spread]
        fs = [fin[d] for d in days if d in fin]
        def dpi(i):
            sh, tot = sum(x[i] for x in fs), sum(x[i + 1] for x in fs)
            return f(100 * sh / tot, 1) if tot > 0 else ""
        out.append(r + [f(sum(sp) / len(sp), 1) if sp else "", f(max(sp), 1) if sp else "", dpi(0) if fs else "", dpi(2) if fs else ""])
    return out


def sanity(liq: list[list[str]], cot: list[list[str]]):
    problems = []
    if len(liq) < 400:
        problems.append(f"流动性表只有 {len(liq)} 行")
    if len(cot) < 400:
        problems.append(f"COT 表只有 {len(cot)} 行")
    for r in liq[-8:]:
        net = float(r[4])
        if not 2000 < net < 15000:
            problems.append(f"{r[0]} 净流动性 {net} 十亿美元，超出合理范围（单位可能变了）")
    old = read_csv(F_LIQ)
    if old and len(liq) < len(old) - 2:
        problems.append(f"流动性表行数从 {len(old)} 降到 {len(liq)}")
    if old and liq[-1][0] < old[-1][0]:
        problems.append(f"最新日期 {liq[-1][0]} 早于已有数据 {old[-1][0]}")
    oc = read_csv(F_COT)
    if oc and cot[-1][0] < oc[-1][0]:
        problems.append(f"COT 最新日期 {cot[-1][0]} 早于已有数据 {oc[-1][0]}")
    for col, name in [(5, "HY 利差"), (6, "NFCI"), (7, "VIX"), (8, "标普500"), (9, "纳指100")]:
        if all(r[col] == "" for r in liq[-4:]):
            problems.append(f"{name} 最近 4 周全部为空")
    if problems:
        raise RuntimeError("数据检查未通过：" + "；".join(problems))


def read_csv(p: Path) -> list[list[str]]:
    if not p.exists():
        return []
    with p.open() as fh:
        return list(csv.reader(fh))[1:]


def write_csv(p: Path, header: list[str], rows: list[list[str]]):
    with p.open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


LIQ_HEADER = ["date", "fed_assets_bn", "tga_bn", "rrp_bn", "net_liq_bn", "hy_oas", "nfci", "vix", "spx", "ndx",
              "sofr_iorb_bp", "sofr_iorb_max_bp", "spy_offex_short_pct", "qqq_offex_short_pct"]
FINRA_HEADER = ["date", "spy_short", "spy_total", "qqq_short", "qqq_total"]
COT_HEADER = ["date", "es_am_pct", "es_lev_pct", "nq_am_pct", "nq_lev_pct"]


def build_outputs(updated: str):
    """由两个 CSV 生成网页读取的 dashboard_data.json 和 latest.json（只做对齐，不改数字）。"""
    liq, cot = read_csv(F_LIQ), read_csv(F_COT)
    num = lambda v: None if v == "" else float(v)
    D = {k: [] for k in ["d", "fed", "tga", "rrp", "net", "hy", "nfci", "vix", "spx", "ndx",
                         "sofr", "sofr_max", "spy_dpi", "qqq_dpi",
                         "cd", "es_am", "es_lev", "nq_am", "nq_lev"]}
    for r in liq:
        D["d"].append(r[0])
        for k, v in zip(["fed", "tga", "rrp", "net", "hy", "nfci", "vix", "spx", "ndx", "sofr", "sofr_max", "spy_dpi", "qqq_dpi"], r[1:]):
            D[k].append(num(v))
    # COT 报告日（通常周二）对齐到当周周三：取 >= 报告日的第一个周三，且相差不超过 6 天
    n = len(D["d"])
    for k in ["cd", "es_am", "es_lev", "nq_am", "nq_lev"]:
        D[k] = [None] * n
    j = 0
    for r in cot:
        while j < n and D["d"][j] < r[0]:
            j += 1
        if j >= n:
            break
        gap = (datetime.fromisoformat(D["d"][j]) - datetime.fromisoformat(r[0])).days
        if 0 <= gap <= 6:
            D["cd"][j] = r[0]
            for k, v in zip(["es_am", "es_lev", "nq_am", "nq_lev"], r[1:]):
                D[k][j] = num(v)
    D["updated"] = updated
    D["cot_last"] = cot[-1][0] if cot else None
    F_DASH.write_text(json.dumps(D, ensure_ascii=False, separators=(",", ":")))
    last = liq[-1]
    latest = {"date": last[0], "cot_date": cot[-1][0] if cot else None, "updated": updated,
              **{k: num(v) for k, v in zip(LIQ_HEADER[1:], last[1:])},
              **({k: num(v) for k, v in zip(COT_HEADER[1:], cot[-1][1:])} if cot else {})}
    F_LATEST.write_text(json.dumps(latest, ensure_ascii=False, indent=1))


def check_sources():
    ok = True
    for k, code, fam in FRED_SERIES + [("nfci", "NFCI", None)]:
        try:
            s = fred(code, fam)
            log(f"[检查] FRED {code}: {len(s)} 行，最新 {s[-1][0]} = {s[-1][1]}")
        except Exception as e:
            ok = False
            log(f"[检查] FRED {code}: 失败 {e}")
    for code in ["SOFR", "IORB", "IOER"]:
        try:
            s = fred_daily(code)
            last = max(s)
            log(f"[检查] FRED {code}: {len(s)} 行，最新 {last} = {s[last]}")
        except Exception as e:
            ok = False
            log(f"[检查] FRED {code}: 失败 {e}")
    for d in [date(2018, 8, 1), date(2021, 3, 1), datetime.now(timezone.utc).date() - timedelta(days=3)]:
        try:
            v = finra_day(d)
            log(f"[检查] FINRA {d}: {v if v else '无文件（非交易日或尚未发布）'}")
        except Exception as e:
            ok = False
            log(f"[检查] FINRA {d}: 失败 {e}")
    for k, c in COT_CODES.items():
        try:
            rows = cftc(c)
            log(f"[检查] CFTC {c}: {len(rows)} 行，最新 {rows[-1]['report_date_as_yyyy_mm_dd'][:10]}")
        except Exception as e:
            ok = False
            log(f"[检查] CFTC {c}: 失败 {e}")
    return ok


def main():
    if "--check" in sys.argv:
        sys.exit(0 if check_sources() else 1)
    if "--rebuild" in sys.argv:  # 只用已有 CSV 重新生成网页数据，不联网
        build_outputs(json.loads(F_LATEST.read_text())["updated"] if F_LATEST.exists() else "")
        return
    DATA.mkdir(exist_ok=True)
    try:
        liq, cot = build_liquidity(), build_cot()
        sanity(liq, cot)
        finra = update_finra()
        liq = weekly_join(liq, funding_spread(), finra)
    except Exception as e:
        log(f"本次不更新：{e}")
        sys.exit(1)
    write_csv(F_FINRA, FINRA_HEADER, finra)  # 原始日度数据总是保存（只增不改历史）
    if liq == read_csv(F_LIQ) and cot == read_csv(F_COT) and F_DASH.exists():
        log(f"没有新数据（最新仍是 {liq[-1][0]} / COT {cot[-1][0]}），不改动文件")
        return
    write_csv(F_LIQ, LIQ_HEADER, liq)
    write_csv(F_COT, COT_HEADER, cot)
    build_outputs(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    log(f"已更新：流动性 {len(liq)} 行，最新 {liq[-1][0]}，净流动性 {liq[-1][4]} 十亿美元；COT {len(cot)} 行，最新 {cot[-1][0]}")


if __name__ == "__main__":
    main()
