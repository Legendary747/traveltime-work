# Google Cloud 部署

保留本地运行；云端使用 Cloud Run + Cloud Scheduler + Firestore，GitHub Actions 每晚出图。
默认地区 europe-west4（荷兰），默认项目内资源前缀 traveltime，实验名 commute-v1。
代码推送不会启动采集，首次部署需要下面三步。

## 1. 在 Cloud Shell 初始化一次

准备一个已关联账单的 Google Cloud 项目，建议单独用于通勤监测。
使用对该项目有管理权限的账号打开 [Cloud Shell](https://shell.cloud.google.com/)。
将私有仓库克隆到 Cloud Shell；也可以从 GitHub 下载仓库 ZIP，上传解压后进入仓库根目录。
私有仓库克隆需要你自己的 GitHub 登录授权。

在仓库根目录：

~~~sh
cp .env.example .env
cloudshell edit .env
~~~

填写 GOOGLE_MAPS_API_KEY、COMMUTE_HOME、COMMUTE_WORK。API Key 在 Google Cloud
控制台创建并限制为 Routes API；这是服务器调用，不要给它设置浏览器 HTTP referrer 限制。
起终点建议使用实际停车入口附近的坐标，格式：纬度,经度。不要在终端命令里直接粘贴密钥。

~~~sh
python3 -m deploy.gcp.bootstrap --project YOUR_PROJECT_ID
~~~

将 YOUR_PROJECT_ID 换成项目 ID（不是显示名称或项目编号）。
此初始化脚本只需 Python 标准库和 Cloud Shell 自带的 gcloud：

- 启用所需 API，创建默认 Native 模式 Firestore（新库启用删除保护）。
- 创建 runtime、scheduler、builder、reporter、deployer 五个用途独立的服务账号。
- 把 .env 的三个敏感值经标准输入写入 Secret Manager，值不进入命令参数或代码。
- runtime 可读这三个 secret、读写 Firestore；reporter 只有 Firestore 读取权限。
- 配置 GitHub Workload Identity Federation：限定本仓库不可变数字 ID、所有者 ID、
  main 分支，以及两个指定工作流；部署身份与报表身份分开。
- 输出下一步要填的 GitHub Variables，不创建长期服务账号密钥。

重复运行会复用资源，默认不覆盖已存在的 secret。需要轮换时先修改 .env，再加
--refresh-secrets 运行初始化；之后重新部署会固定到最新的已启用数字版本。
脚本不会创建新的 Google Cloud 项目、关联账单、创建 Maps API Key，或发起导航查询。
如果是 fork，需要显式提供 --repository、--repository-id、--owner-id，不能保留本仓库的身份条件。

## 2. 填写 GitHub Actions Variables

仓库 Settings → Secrets and variables → Actions → **Variables**。
将初始化脚本最后打印的八个变量逐项填入：

| 变量 | 用途 |
|---|---|
| GCP_PROJECT_ID | 你的 Cloud 项目 ID |
| GCP_REGION | 默认 europe-west4 |
| GCP_SERVICE | 默认 traveltime |
| CLOUD_EXPERIMENT | 默认 commute-v1 |
| GCP_WORKLOAD_IDENTITY_PROVIDER | 初始化脚本打印的 projects/…/providers/github |
| GCP_REPORT_SERVICE_ACCOUNT | …-reporter@….iam.gserviceaccount.com |
| GCP_DEPLOY_SERVICE_ACCOUNT | …-deployer@….iam.gserviceaccount.com |
| ENABLE_CLOUD_REPORTS | true：工作日 18:15 自动出图 |

另外建议设置 MONITOR_END_DATE，例如 2026-11-30（仅示例）。
可选 MONITOR_START_DATE、EXCLUDE_DATES（逗号分隔日期）。留空表示不限制。
**云端日期设置来自这些 Actions Variables**；初始化时 .env 中的日期不会自动复制到 GitHub。
修改日期后重新运行 Deploy Google Cloud，才会更新云端采集配置。

原来的 GOOGLE_MAPS_API_KEY / COMMUTE_HOME / COMMUTE_WORK GitHub Secrets 不再被工作流读取；
敏感值现在保存在 Google Secret Manager。旧 ENABLE_COMMUTE_SCHEDULE 变量也不再起作用。
IAM / 身份联合首次设置可能需要几分钟传播，刚配置后认证失败可稍后重新运行。

## 3. 手动部署并开始采集

Actions → **Deploy Google Cloud** → Run workflow：

1. 分支选择 main。
2. 保持 enable_schedule 勾选，运行。
3. 成功后，运行摘要显示私有 Cloud Run 服务 URL 和 ENABLED。
4. 下一个采样时隙开始采集；Firestore 的 commute_experiments/commute-v1/samples 中应出现记录。
5. 有第一条数据后，可运行 **Daily commute report**；以后工作日荷兰时间 18:15 自动运行。

部署使用 Dockerfile，只上传 allowlist 中的 Python 源码和云端依赖。
.env、临时 GitHub 身份文件、SQLite、历史报告不会上传到镜像构建。
服务需要 Cloud Run IAM 验证，Scheduler 使用 OIDC 调用；直接在浏览器打开 URL 得到 403 是正常的。
服务最小实例为 0，最大实例为 1，按请求分配 CPU，每次请求只采一个点，不在云端 sleep 等待。

两条 Scheduler 任务：

| 名称 | cron | 时区 |
|---|---|---|
| traveltime-am | */3 6-8 * * 1-5 | Europe/Amsterdam |
| traveltime-pm | */3 15-17 * * 1-5 | Europe/Amsterdam |

每个时隙由 Scheduler 原始调度时间标识，重发去重。迟到超过默认 60 秒跳过，未来、周末、
排除日期、窗口外也跳过；不能补采过去路况。关闭失败重试，已经失败的 API 调用不再次付费重查。
进程在预留额度后崩溃会留下 pending 缺测样本，下次不会偷偷重查。
不要用 Scheduler 的 Force run 在任意时间测试采集：不在合法的 3 分钟时隙会被正常跳过。

如偏好从 Cloud Shell 直接部署：

~~~sh
python3 -m deploy.gcp.deploy --project YOUR_PROJECT_ID --enable-schedule
~~~

此命令不自动读取 .env，日期等普通配置从进程环境读取；密钥仍使用 Secret Manager。
不带 --enable-schedule 则部署为暂停状态。默认值与 GitHub Variables 一致；
自定义资源名/实验时同时传 --region、--service、--experiment。

## 查看报告、备份及保留本地

Daily commute report → 运行记录 → 下载 commute-运行编号 artifact。
解压后打开 reports/index.html，里面包含每天早晚曲线、星期对比、热力图、CSV。
SQLite 和报表也保存在私有 commute-data 分支。源码仍在 main。
出图任务失败或 GitHub 排队只会推迟报告，Firestore 中的采集结果仍在。
首次采样前运行报表会明确报 No cloud observations yet，而不会覆盖历史备份为空文件。

本地独立采集仍按根目录 README 操作：

~~~sh
python -m pip install -r requirements.txt
python -m traveltime run
~~~

也可以用本地工具分析云端数据，不必在本地重复采集：

~~~sh
python -m pip install -r requirements.txt -r requirements-cloud.txt
gcloud auth application-default login
python -m traveltime.cloud_export --project YOUR_PROJECT_ID --db data/cloud-commute.sqlite3
python -m traveltime --db data/cloud-commute.sqlite3 --output reports/cloud report
~~~

需要登录对 Firestore 有读取权限的账号。自定义实验时给 cloud_export 加 --experiment。
也可以直接下载 Actions 的 SQLite artifact，在本地运行 report，无需 Cloud 认证。
同一路线的旧 SQLite 观察会保留，云端重复导出不会重复增加样本。
切换实验会拒绝合并到旧备份：请先下载旧 artifact，在新备份路径/数据分支保存新实验。
不要同时开启本地 run 和云端 Scheduler，二者没有跨平台共享的查询去重或限额。

## 暂停、恢复、更新

立即暂停两条调度（已有请求可能继续完成）：

~~~sh
gcloud scheduler jobs pause traveltime-am --location=europe-west4 --project=YOUR_PROJECT_ID
gcloud scheduler jobs pause traveltime-pm --location=europe-west4 --project=YOUR_PROJECT_ID
~~~

恢复同样把 pause 换成 resume。如果曾使用未勾选 enable_schedule 的方式部署，
服务内也会关闭采集，此时应重新运行部署并勾选 enable_schedule。
ENABLE_CLOUD_REPORTS=false 只停止每日出图，**不停止 Google Cloud 采样**。
MONITOR_END_DATE 到期后不会继续调用 Routes API；也可暂停 Scheduler 以停止空触发。
再次部署会先暂停旧 Scheduler；构建失败时保持暂停，修复后重新部署即可恢复。
建议在高峰窗口外部署，避免部署期间缺测。

更新代码后手动运行 Deploy Google Cloud 即可，普通 push 只触发测试。
Firestore 保留数据，部署不清空数据或月度额度。改变路线时使用新 CLOUD_EXPERIMENT，
不要在同一实验中混入不同的通勤起终点；同项目的所有实验共用月度次数计数。

## 用量与故障排查

- Google Routes：22 工作日 × 120 次 = 2,640 次/月，手动测试和其他应用调用另计。
  本程序默认限制为 4,500 次/月尝试；额度月份按 America/Los_Angeles。
- GitHub：采集不消耗 Actions 等待分钟。每日出图预估 2–5 分钟，单次超时 10 分钟；
  另外只消耗测试和手动部署时间。实际时长可在 Actions 查看。
- Cloud Run 使用按请求计费及零最小实例；Scheduler 仅两条任务，Firestore 数据量很小。
  通常可覆盖在相关免费用量内，但构建、镜像存储、网络、Secret Manager、
  同账号其他使用也会影响账单，不能保证整套服务绝对零费用。
- 403/认证失败：检查项目 ID、WIF provider 路径、服务账号变量和 main 分支；
  Cloud Scheduler 的服务代理角色不能移除，调用账号需有 Cloud Run Invoker。
- Cloud Run 启动失败：检查三个 Secret 的启用版本和 runtime 的访问权限。
- Firestore permission denied：检查 Native 默认数据库和 runtime/reporter 对应角色。
- status=configuration_error：起终点/间隔与已有实验不匹配，需要新实验。
- 图表缺口：检查 Cloud Run / Scheduler 日志、pending/error、延迟及 NDW 状态。
  missing/stale/unavailable 不当作“没事故”或“零延误”。
- 每日备份推送失败：检查 Actions 的 Contents write 权限、数据分支规则、
  是否有人同时修改 commute-data；当次 report artifact 可用于恢复。

官方说明：[Cloud Run 计费](https://cloud.google.com/run/pricing)、
[Scheduler 计费](https://cloud.google.com/scheduler/pricing)、
[Firestore 配额](https://docs.cloud.google.com/firestore/quotas)、
[Scheduler 认证](https://docs.cloud.google.com/scheduler/docs/http-target-auth)、
[Secret Manager](https://docs.cloud.google.com/run/docs/configuring/services/secrets)、
[GitHub 身份联合](https://github.com/google-github-actions/auth)。
