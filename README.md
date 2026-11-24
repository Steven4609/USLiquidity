# USLiquidity · 美股流动性与机构仓位报告

美元流动性（美联储资产负债表、财政部 TGA、隔夜逆回购）、信用与波动率风险指标（高收益债利差、VIX、NFCI），
以及机构在标普500 / 纳指100 股指期货上的仓位（CFTC），对照标普500（SPY）和纳指100（QQQ）走势。
**每周六自动取数更新**，不需要任何人操作。

- 报告网页：`https://steven4609.github.io/USLiquidity/`
- 数据文件：本仓库的 [`data/`](data/) 文件夹（CSV 可直接用 Excel 打开，也可用 Power Query 连接原始文件网址）

## 文件说明

| 文件 | 内容 |
|---|---|
| `index.html` | 报告网页（中英文可切换） |
| `bg-market.jpg` | 背景照片（Nicholas Cappello / Unsplash） |
| `data/liquidity_weekly.csv` | 周度：美联储总资产、TGA、逆回购、净流动性（十亿美元）、HY 利差、NFCI、VIX、标普500、纳指100 |
| `data/cot_equity_index.csv` | 周度：资产管理人、杠杆基金在标普 / 纳指期货的净多头占未平仓量（%） |
| `data/latest.json` | 最近一次更新的日期和数值 |
| `data/dashboard_data.json` | 网页读取的合并数据（由脚本自动生成，不用手动改） |
| `scripts/update_weekly.py` | 每周取数与计算脚本 |
| `.github/workflows/weekly-update.yml` | 每周定时任务 |

## 口径

- **净流动性** = 美联储总资产（WALCL）− TGA（WTREGEN）− 隔夜逆回购（RRPONTSYD），单位十亿美元。
- **时间对齐**：以每周三为一行。美联储资产、TGA 取周三值；逆回购、HY 利差取当周日均；VIX、指数取当周最后一个交易日收盘；
  NFCI 取周三当天或之前最近一期；CFTC 持仓（周二）对齐到同一周的周三。
- **指数**：用 FRED 的标普500、纳指100 指数点位代表 SPY、QQQ，涨跌幅基本一致但不含分红。
- **HY 利差**：FRED 只提供 ICE 指数最近 3 年数据。
- **重取**：每次从 2018-01-01 起整段重取，跟随源头修订（NFCI 每周修订历史）；旧版本留在 git 历史里。
- 任何一个数据源取不到或数据异常，本次**不写任何文件**，Actions 显示红叉；不插值、不补数。

## 日常怎么用

- **看更新有没有成功**：仓库顶部 **Actions** 标签，绿色对勾 = 成功，红叉 = 失败（点进去看原因）。
- **手动立即更新一次**：Actions → 左侧 **Weekly update** → 右侧 **Run workflow**（勾选 check 只测试数据源，不写数据）。
- **把数据下载到电脑**：点进任意 CSV → 右上角下载按钮。

数据来源：FRED（圣路易斯联储）、CFTC。仅供个人参考，不构成投资建议。
