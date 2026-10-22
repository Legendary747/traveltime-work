# Travel Time Analyser · 通勤时间分析

[English](../README.md) · [完整配置说明](configuration.md) · [Google 配置与数据政策](google-setup.md) · [可选云部署](../deploy/gcp/README.md)

**出门早一点，通勤是否会更稳定？**

这个 Python 项目从实际通勤问题出发，按设定时间观察两个地点之间的驾车时间估计，生成每日曲线、按星期对比、热力图和 CSV。你可以使用自己的 Google Routes API 项目，自行配置地点、时区、星期、往返时间窗口和采样频率；荷兰路线还可以叠加 NDW 附近交通事件作为参考。

它分析的是“现在出发”的预计驾车时间，不是实际行车记录，不包含停车或步行时间。附近出现事故也不等于它造成了你的延误。程序会保存实时 API 结果，启用之前请先阅读 [Google 的内容存储限制及适用条款](google-setup.md#data-and-service-policies)；自己的密钥和私有存储并不自动获得长期保存数据的许可。MIT 许可仅适用于本项目代码。

## 先运行不需要密钥的演示

需要 Python 3.11+，CI 使用 Python 3.12。在项目根目录执行：

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m traveltime --db demo-output/demo.sqlite3 --output demo-output/report demo --days 10
```

Windows PowerShell 使用 `.venv\Scripts\Activate.ps1` 激活环境。打开 `demo-output/report/index.html` 即可查看。演示数据明确标记为模拟，不访问网络，也不产生 API 费用。再次生成时请换一个新的数据库路径。

## 使用自己的地点与 Google 配置

先在自己的 Google Cloud 项目中关联账单、启用 Routes API、创建并限制 API key，详见 [Google 配置指南](google-setup.md)。本地运行只需要 Maps API key，不需要创建 Cloud Run、Firestore 或服务账号密钥。

```sh
cp .env.example .env
cp config.example.json config.local.json
```

在 `.env` 填写 `GOOGLE_MAPS_API_KEY`。在 `config.local.json` 填写 `home` 和 `work`，可使用地址、`纬度,经度` 或 `place_id:...`，尽量选择合适的车辆入口。地点、时间和频率都放在这个本地 JSON 中；不要把密钥放进去。示例中的两个地点为空，需要自己补充。

```sh
python -m traveltime --config config.local.json doctor
python -m traveltime --config config.local.json run
```

`doctor` 只检查配置，不联网。`run` 才会按时发起可能收费的查询；请保持电脑唤醒和网络连接，Ctrl+C 停止。默认行为保持原样：

| 设置 | 默认值 |
|---|---|
| 时区 | `Europe/Amsterdam` |
| 星期 | 周一至周五，JSON 中为 `[0,1,2,3,4]`；周一是 0 |
| 去程 `home → work` | `06:00`–`09:00` |
| 返程 `work → home` | `15:00`–`18:00` |
| 间隔 | 3 分钟，可设为 1–60 的整数 |
| 月度请求尝试上限 | 4,500 次；这是程序限制，不是免费额度或总账单上限 |

结束时间不包含在窗口内，两个窗口不能重叠，也不支持跨午夜。可以设置开始/结束日期和排除日期；节假日不会自动排除。JSON 中其余参数、时钟变化及兼容性说明见 [配置文档](configuration.md)。荷兰以外建议设 `ndw_enabled` 为 `false`。

**环境变量优先于 JSON。** 使用 JSON 时，建议 `.env` 只保留 API key；原有仅使用 `.env` 的方式仍然支持。`--config`、`--db`、`--output` 等全局参数放在子命令前面。

## 报告与常用命令

默认数据库为 `data/commute.sqlite3`，报告为 `reports/index.html`。采样窗口结束及程序退出时会更新报告，也可以离线生成：

```sh
python -m traveltime --config config.local.json --db data/commute.sqlite3 --output reports report
```

报告包含每日往返曲线、同一出发时刻跨日期的中位数/P90、热力图，以及 `samples.csv`、`daily_summary.csv`、`nearby_events.csv`、`summary.json`。P90 至少需要五个已观察日期；缺测会明确显示，不会补造历史路况。

| 命令 | 用途 |
|---|---|
| `once` | 当前正好有合法时隙才查询 |
| `once --test-direction am` | 立即进行一次真实去程测试，可能收费；不计入定时统计；返程用 `pm` |
| `peak` | 只运行当前窗口，或十分钟内开始的窗口 |
| `run` | 持续按配置采样 |
| `report` | 读取已有数据，不调用 API |
| `demo --days 10` | 在新数据库中创建模拟数据 |

每次查询前会先占用程序额度；失败也保守计数，不自动重试收费请求。月度计数按 `America/Los_Angeles` 划分，采样时区独立配置。默认每天 120 次；22 个采样日约 2,640 次。实际收费取决于 Google 当前价格、账户及其他用量，请另外设置合适的 API 配额并监控账单。

## 数据、云部署与开发

真实数据库和报告应保持私有。时间、距离、路线描述和交通事件位置都可能透露出行习惯。`.env`、`*.local.json`、数据库和报告目录已被 Git 忽略，但忽略规则不能清除历史提交。公开源码前检查全部分支、历史、release 附件和 Actions artifacts；数据分支没有独立的私有可见性。

可选的 [Cloud Run + Scheduler + Firestore 部署](../deploy/gcp/README.md) 使用你自己的项目和凭据，需要手动启用采样。真实报告工作流只允许运行在私有仓库，不上传真实报告 artifact；请先合并这些保护再公开源码仓库。公开源码后仍可在本地导出、分析云端数据。

修改地点、时区、星期、窗口或采样间隔时，使用新的本地数据库或云端实验名，保留旧数据以免混合。不要同时开启同一路线的本地和云端采样，两者不共享去重和本地额度。

```sh
python -m pip install -r requirements.txt -r requirements-cloud.txt
python -m unittest discover -s tests -v
```

测试和模拟演示不需要真实 Google 凭据。欢迎通过 [贡献指南](../CONTRIBUTING.md) 提交改进；敏感问题见 [SECURITY.md](../SECURITY.md)。

作者：[Legendary747](https://github.com/Legendary747)。另一个项目：[H2S 找房辅助插件](https://github.com/Legendary747/H2S)。
