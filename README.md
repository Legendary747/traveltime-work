# Traveltime Work

采集家 ↔ a.s.r. 的汽车预计通勤时间。默认工作日荷兰时间 06:00–09:00 家到公司，
15:00–18:00 公司回家，每 **3 分钟**一次。窗口末端不包含 09:00/18:00：
早晚各 60 次、每天共 120 次。自动处理 Europe/Amsterdam 冬夏令时。

## 首次使用：还需要什么

1. Google Cloud 项目启用 **Routes API** 和账单，创建仅允许 Routes API 的 API Key。
2. 填写家附近起点及公司实际停车入口；支持地址、纬度经度或 place_id。
3. 选择常驻电脑/服务器，或下面的 GitHub Actions 运行方式。

API Key 和地址写入本机 .env 或 GitHub Secrets，**不要放进 README、代码或聊天**。
仓库刚初始化时不会自动开始收费查询；GitHub 定时运行需要单独启用变量。

## 本机运行

Python 3.11+，推荐 3.12。只依赖 matplotlib 和 Windows 等平台所需的 tzdata。

Windows PowerShell：

~~~powershell
git clone https://github.com/Legendary747/traveltime-work.git
cd traveltime-work
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
.\.venv\Scripts\python -m traveltime doctor
.\.venv\Scripts\python -m traveltime once --test-direction am
.\.venv\Scripts\python -m traveltime run
~~~

Linux/macOS：

~~~sh
git clone https://github.com/Legendary747/traveltime-work.git
cd traveltime-work
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
# Edit .env, then:
.venv/bin/python -m traveltime doctor
.venv/bin/python -m traveltime once --test-direction am
.venv/bin/python -m traveltime run
~~~

doctor 不发请求。test-direction 会真实调用一次 Google，作为手动测试保存，
但不混入定时采样统计。run 常驻，其他时段不查 API，在每次高峰结束时刷新报告。
机器需要在采集时间保持唤醒和联网；休眠期间的路况不能事后补抓。
Linux 常驻服务示例见 deploy/traveltime.service，安装前替换用户和目录。

.env 示例（值在本机填写）：

~~~dotenv
GOOGLE_MAPS_API_KEY=your-key
COMMUTE_HOME=latitude,longitude
COMMUTE_WORK=latitude,longitude
MONITOR_END_DATE=2026-11-30
~~~

结束日期只是示例，可以留空持续采集。可用 EXCLUDE_DATES 排除假期、异常日期；
默认星期一至五全部记录，不自动剔除学校假期或国定假日。坐标顺序是纬度、经度。
不要修改已有数据集的起终点或采样间隔；改动后使用新的 --db 路径。
电脑本机时区不影响调度。

## GitHub Actions：不用保持个人电脑开机

此仓库是私有仓库。GitHub cron 最短 5 分钟且可能排队；本项目每天启动两个约三小时的任务，
任务内部按 180 秒时隙采样。不是每 3 分钟新建一个 workflow。

**资源用量：22 个工作日约 8,000 多个 GitHub 托管运行分钟/月（包含等待），
另有安装和报表开销。可能超出账户包含的私库分钟数。Google API 免费额度与 GitHub 运行费用是两回事。**
本机或自己的服务器运行不消耗 GitHub 托管分钟。

### 设置

仓库 Settings → Secrets and variables → Actions → Secrets，新建：

| Secret | 内容 |
|---|---|
| GOOGLE_MAPS_API_KEY | Google Routes API Key |
| COMMUTE_HOME | 家附近起点地址/纬度经度 |
| COMMUTE_WORK | 公司停车入口地址/纬度经度 |

然后：

1. Actions → Collect commute → Run workflow → test_am，验证一次真实查询。
2. 查看运行结果；测试样本不计入定时图表。
3. 同一设置页的 Variables 中设置 MONITOR_END_DATE（建议先观察 6–8 周）。
4. 要启动云端定时任务，添加 **ENABLE_COMMUTE_SCHEDULE = true**。
5. 要暂停，删掉或改为 false；已经在运行的任务需在 Actions 中取消。

可选 Variables：MONITOR_START_DATE、MONITOR_END_DATE、EXCLUDE_DATES。
两次启动时间为本地 05:55、14:55；程序在窗口前等待。GitHub 排队仍可能漏掉最初几次，
程序只采实际当前时刻，报告保留缺口。任务结束后下载 commute-运行编号 artifact，
解压并打开 reports/index.html；也可查看私有 commute-data 分支。
workflow 需要 Contents write 权限；如组织策略限制，先修改允许的工作流权限。

### 数据持久化

每 15 分钟把 SQLite 状态提交到单独的 commute-data 分支；高峰结束后保存报告。
每次 runner 从这个分支恢复，统计跨天累计。源码保持在 main。
单次定时任务并发被锁定，保存不用 force push。人工同时修改数据分支可能导致推送失败，
此时本地数据和运行 artifact 仍可用于恢复。强制取消/runner 故障可能损失最近尚未推送的数据。
不要同时用多台机器各自采样同一条路线，否则账户调用额度可能叠加。

## 输出

默认数据库 data/commute.sqlite3，默认报告 reports/index.html。

~~~sh
python -m traveltime report
python -m traveltime --db data/commute.sqlite3 --output reports report
~~~

- 每天一张包含早晚两幅曲线的图：实时预计车程、同一路线基准、75 分钟阈值、附近事故报告。
- 周一到周五早晚共十个面板：同一出发时刻跨日期的中位数和 P90，少于 5 天不显示 P90。
- 日期 × 出发时间热力图：缺测为灰色，最多显示最近 40 个工作日。
- samples.csv：每次状态、实际查询时间、预计时长、距离、路线标识。手动测试单独标记。
- daily_summary.csv：有效样本/完整窗口应采数量、当日中位数/P90、延误及超时样本占比。
- nearby_events.csv：NDW 附近事件，按事件 ID 和通勤方向去重、保留首次/最后观察时间。
- summary.json：机器可读的每日汇总。

当日 P90 跨不同出发时刻计算；星期面板 P90 跨不同日期的同一时刻计算，两者不可互换。
相邻 3 分钟样本相关，不意味着取得了很多独立通勤天数。建议观察至少 6–8 周，
再与几次实际驾驶时间、停车和步行耗时一起判断。采集的是“此刻出发预计要多久”，不是实测驾驶时长。

## Google 查询与费用控制

使用 Compute Routes、DRIVE、TRAFFIC_AWARE_OPTIMAL；不指定 departureTime，代表查询时刻出发。
一次请求可返回默认和备选路线，以 DEFAULT_ROUTE 为主线。路线可以自动改道；
每条路线的 duration 与 staticDuration 始终来自同一次响应。
记录路线形状的哈希供对照，原始形状只在内存中用于 NDW 匹配，不保存家庭坐标。
路线哈希可能因几何细节变化而改变，不能单独作为换路的确定证据。

不请求 TRAFFIC_ON_POLYLINE 或收费道路计算等 Enterprise 功能。当前 Pro 每月免费 5,000 次，
22 个工作日 × 120 = 2,640 次，另加手动测试或其他程序的调用。免费额度按账单账户共享，
不是每个 API Key 各一份。价格请以官方价目表为准。

本程序默认每月最多 4,500 次尝试，在发送前写入数据库预留一次；超时/失败也保守计入。
不自动重试，不以新的当前结果补填历史。不删除数据库才能保留这项本地限额的记忆。
这个上限不是 Google 账户级别的硬性费用上限，控制台仍应设置合适配额和提醒。
账单月份按 America/Los_Angeles 划分。

## NDW 事故标记

默认读取公开 actueel_beeld.xml.gz（支持 DATEX II v2/v3 名称空间）：

- 排除未来、已结束、暂停、非真实信息记录。
- 超过 10 分钟的陈旧 feed 标记 stale，失败标记 unavailable，不当成没有事故。
- 利用公开坐标或 WGS 84 posList 与当前默认路线匹配，默认 250 米范围。
- 有 bearing 且明显反向的记录排除；未提供方向则明确标为 unknown。
- 以 situation ID 去重，事故连续出现十次仍是一件被报告事件。

这是**附近报告候选**，不是确认本车道发生事故，亦不能估算个人事故概率或把全部延误归因于事故。
平行道路、几何误差、无坐标事件、线段端点匹配、未覆盖道路和导航绕行均影响检测。
NDW 失败不影响保存 Google 的车程。可用 NDW_ENABLED=false 关闭这一层。

## 验证与演示

~~~sh
python -m unittest discover -s tests -v
python -m traveltime --db demo-output/demo.sqlite3 --output demo-output/report demo --days 10
~~~

演示为固定随机种子生成的模拟数据，每张图都标为 SIMULATED；没有真实 API 调用。
演示数据库必须是不存在的新文件，不能覆盖或混入实际观察。
测试覆盖冬夏令时、时段边界、迟到不补采、并发去重、失败额度、路线变更、事故方向和去重、
缺测分母、按日期分位数、GitHub 新 runner 的数据恢复。

## 官方参考

- [Routes 流量偏好](https://developers.google.com/maps/documentation/routes/config_trade_offs)
- [Routes 计费](https://developers.google.com/maps/documentation/routes/usage-and-billing)
- [当前 Google 价目表](https://developers.google.com/maps/billing-and-pricing/pricing)
- [NDW 情况报告](https://docs.ndw.nu/producten/situatieberichten-v3/)
- [GitHub 定时任务](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
