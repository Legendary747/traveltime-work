# Traveltime Work

采集家 ↔ a.s.r. 的汽车预计通勤时间。默认工作日荷兰时间 06:00–09:00 家到公司，
15:00–18:00 公司回家，每 **3 分钟**一次。窗口末端不包含 09:00/18:00：
早晚各 60 次、每天共 120 次。自动处理 Europe/Amsterdam 冬夏令时。

## 首次使用：还需要什么

1. Google Cloud 项目启用 **Routes API** 和账单，创建仅允许 Routes API 的 API Key。
2. 填写家附近起点及公司实际停车入口；支持地址、纬度经度或 place_id。
3. 选择本地运行，或 **Google Cloud 采集 + GitHub Actions 每日出图**。

API Key 和地址写入本机 .env 或 Google Secret Manager，**不要放进 README、代码或聊天**。
推送代码不会自动部署或开始收费查询；首次云端部署按下面的说明完成。

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

## Google Cloud + GitHub Actions：不用保持电脑开机

已移除原来 Actions 早晚各等待三小时的采集任务，解决每月约 8,000 多分钟的问题。
本地模式和云端模式共用查询、事故匹配和报表逻辑。

| 组件 | 工作 |
|---|---|
| Cloud Scheduler | 荷兰工作日 06:00–08:57、15:00–17:57，每 3 分钟触发一次 |
| Cloud Run | 每次只查一次车程及 NDW 事件；无请求时可缩容到零 |
| Firestore | 即时保存每个样本，事务去重、预留月度查询额度 |
| Actions：Daily commute report | 工作日荷兰时间 18:15 导出数据，生成图表并备份 |
| Actions：Deploy Google Cloud | 手动部署/更新 Cloud Run 和两条 Scheduler 任务 |
| 本地 Python | 保留独立采集、SQLite 保存、出图，也可下载云端数据后出图 |

**[首次部署步骤、变量配置、暂停和故障排查](deploy/gcp/README.md)**。

Actions 每日只运行出图任务，设置 10 分钟超时；按每次约 2–5 分钟估算，
22 个工作日约 **44–110 分钟/月**，再加代码测试和偶尔部署。实际以 Actions 用量为准，
不再为整个早晚高峰的等待时间消耗运行分钟；账户内其他仓库仍共用你的 3,000 分钟额度。
Google Cloud 费用与 GitHub 分钟分别计算，部署说明列出了相关免费额度及成本来源。

Firestore 是云端采集的主存储，报告任务失败不影响后续采集。
每日把 SQLite 副本和图表提交到私有 commute-data 分支，并生成 14 天有效的下载 artifact。
下载后打开 reports/index.html；手动运行 Daily commute report 可随时更新。
已有相同路线/间隔的 SQLite 记录会保留；换实验不允许静默混入同一备份。

旧变量 ENABLE_COMMUTE_SCHEDULE 已不再使用。停用 ENABLE_CLOUD_REPORTS **只停止出图**，
暂停采集需要暂停两条 Scheduler 任务，具体命令见部署说明。
本地和云端采样额度各自保存，切勿同时开着两套采集器重复查询同一路线。

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
云端计数由 Firestore 事务保存，在同一项目内的实验之间共享；本地由 SQLite 保存。
不自动重试，不以新的当前结果补填历史。不删除数据库才能保留限额的记忆。
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
python -m pip install -r requirements.txt -r requirements-cloud.txt
python -m unittest discover -s tests -v
python -m traveltime --db demo-output/demo.sqlite3 --output demo-output/report demo --days 10
~~~

演示为固定随机种子生成的模拟数据，每张图都标为 SIMULATED；没有真实 API 调用。
演示数据库必须是不存在的新文件，不能覆盖或混入实际观察。
测试覆盖冬夏令时、时段边界、迟到不补采、并发去重、失败额度、路线变更、事故方向和去重、
缺测分母、按日期分位数、GitHub 新 runner 的数据恢复，以及云端重发/迟到、跨实验限额、
Firestore 数据导入的原子性、部署身份和定时任务配置。
测试使用合成数据、HTTP 测试客户端和存储/部署替身，不访问真实 Google Routes 或生产 Firestore。

## 官方参考

- [Routes 流量偏好](https://developers.google.com/maps/documentation/routes/config_trade_offs)
- [Routes 计费](https://developers.google.com/maps/documentation/routes/usage-and-billing)
- [当前 Google 价目表](https://developers.google.com/maps/billing-and-pricing/pricing)
- [NDW 情况报告](https://docs.ndw.nu/producten/situatieberichten-v3/)
- [GitHub 定时任务](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [Cloud Run 与 Scheduler](https://docs.cloud.google.com/run/docs/triggering/using-scheduler)
- [GitHub → Google 短期身份验证](https://github.com/google-github-actions/auth)
