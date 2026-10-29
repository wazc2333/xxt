# 播放请求测试工具

## 真实接口模式（本次新增，默认后端）

已加入 `chaoxing_client.py`。`simulator.py` 默认选择 `chaoxing` 后端，真实模式不启动本地 HTTP 服务。它从原分析目录读取当前样本视频的卡片/媒体/学习页上下文，使用卡片下发的真实 `reportUrl`，并实现真实 GET query 和 MD5 签名。

默认会向真实服务器发送请求：

```powershell
Set-Location D:\xxt\playback_simulator
python simulator.py
```

只预览目标与字段、不读取 Cookie、不连接网络时，必须显式使用 `--dry-run`：

```powershell
python simulator.py --dry-run
```

默认真实模式会读取 `D:\xxt\cookie.txt`，先刷新卡片和媒体状态以取得新令牌，然后按卡片配置的间隔、1 倍实际经过时间发送播放/周期/暂停/结束事件。`--send` 保留为兼容参数，与默认行为相同，不能和 `--dry-run` 同时使用。默认真实运行状态目录是 `chaoxing-state`。可分别通过 `--cookie-file`、`--capture-dir`、`--output` 指定路径；这些路径参数不会改变服务器 host。

暂停退出及恢复示例（单位秒）：

```powershell
python simulator.py --stop-at 120 --output .\course-run
python simulator.py --resume --output .\course-run
```

真实目标为本次已分析的单个视频，不会自动遍历整个账号的课程。更换视频需要一组相互匹配的卡片、媒体状态和学习页上下文，放入新的捕获目录。原始凭证和令牌不会复制进运行日志。

实现中的几个具体差异：

- 使用站点真实签名和扁平 query，不使用本机 HMAC、`payload` 包装或 `/mock/` 路由。`otherInfo` 内的 `&courseId=...` 按前端拼接语义保留为独立 query 字段。
- 每次上报读取 JSON 响应中的布尔 `isPassed`，并检查 `hasJobLimit`、`videoTimeLimit`。遇到登录重定向、权限/学习限制、未支持的验证/心跳流程或异常响应停止，不执行页面返回的 JavaScript。
- 控制台的 `local-position` 只是本地播放位置；`server-isPassed` 才是本次响应声明的通过状态。真实接口没有本机 `/progress` 查询，不能沿用本机模拟服务的“确认了多少秒”结论；`serverCreditedSeconds` 始终为未知。
- 真实模式固定 1 倍计时，时长来自媒体状态，间隔不得短于卡片配置，不接受本机倍率和故障注入参数。它仍是**事件/计时模拟，不会解码或渲染真实视频**。
- 真实进度 GET 会改变服务端状态，其幂等性未验证，因此不会自动重试。发生超时/中断时保留 `pendingEvent`；`--resume` 从最近收到有效响应的位置重新计时，不直接跳到未确认的位置。只读卡片/媒体请求遇到网络故障或 5xx 最多尝试 3 次。
- `remote-checkpoint.json`、`remote-events.jsonl` 只保存脱敏身份指纹、位置、事件和必要响应标志。到达总时长但 `isPassed=false` 时不会声称任务通过，也不会自动循环刷报。

最初的适配器验证使用离线拦截测试和现有捕获数据预览。后续另行授权的真实服务端测试记录见 `../backend/VERIFICATION.md`；完成状态与累计学时分别记录，不能用 HTTP 200 或本地位置代替服务端完成确认。Cookie 或页面上下文失效时需要重新取得有效上下文。

## 本机模拟模式（保留用于独立测试）

Python 3.11 或更新版本，无需安装依赖。入口是 `simulator.py`，同时启动本机模拟服务和客户端，结束时自动停止服务。

本机模式模拟的是**播放器事件与计时**，不会解码、渲染或播放真实视频。按实际经过时间 1 倍速推进虚构视频，覆盖播放、周期上报、暂停、恢复、结束、进度核对和断点恢复。本机服务只监听随机端口的 `127.0.0.1`；本机客户端禁用代理和重定向，只允许该字面地址。本机模式不会读取 Cookie、账号或抓包私有数据。

## 运行

在 PowerShell 中：

```powershell
Set-Location D:\xxt\playback_simulator
python simulator.py --backend local --duration 20 --interval 5 --pause-at 8 --pause-for 2 --fail-first
```

约 22 秒完成：8 秒处暂停 2 秒；`--fail-first` 让本机服务在记录第一条事件后返回一次 503，客户端使用相同序号和请求内容重试。

参数对应本次分析样本的长时测试（约 18 分钟，仍然全部在本机运行）：

```powershell
python simulator.py --backend local --duration 1093 --interval 60 --output .\long-test
```

快速验证暂停退出和跨进程恢复：

```powershell
python simulator.py --backend local --duration 12 --interval 2 --stop-at 4 --output .\resume-demo
python simulator.py --backend local --duration 12 --interval 2 --resume --output .\resume-demo
```

同一个输出目录再次运行时必须使用 `--resume`，并保持 `--duration` 一致；新测试请换一个输出目录。已完成的任务恢复后仅报告完成状态。也可以按 Ctrl+C：程序先向服务核对已确认的进度，再暂停并保存，未确认的播放片段不计入断点。再次按 Ctrl+C 可能中断清理。

`--backend local --time-scale 20` 同时把客户端和模拟服务的本机时钟加速 20 倍，便于快速测试。这个倍率只用于本地实验，默认是 1。本机后端不允许远程服务器配置。

## 与请求分析的对应关系

真实站点的只读分析结果在 `D:\xxt\analysis\请求分析.md`。本机模拟采用相同的事件语义：`playing=0`、`pause=2`、`play=3`、`ended=4`；当前媒体位置为 `playingTime`，总时长为 `duration`。周期上报的间隔与播放/暂停/结束事件分开处理。

模拟协议是独立测试协议：

| 路由 | 用途 |
| --- | --- |
| `GET /status` | 获取虚构视频标识、时长和时钟倍率 |
| `POST /session` | 创建本机临时令牌并重置请求序号 |
| `GET /mock/multimedia/log/{token}?payload=...` | 接收带签名的测试事件 |
| `GET /progress` | 查询服务端确认的位置、累计有效推进量及完成状态 |

`payload` 是 JSON，包含虚构的班级/用户/任务/视频标识、`seq`、事件、位置、时长等。`enc` 使用独立的测试 HMAC-SHA256；它与真实站点的签名、参数编码和令牌均不兼容。本程序不能证明真实站点会接受怎样的时长或完成条件。

模拟服务的校验规则由本项目定义，**不是通过实测真实服务端得出的规则**：

- 每次位置前进不得超过两次事件之间服务端实际经过的有效播放时间；暂停、退出和离线时间不增加进度。
- 不支持拖动、倒退和变速；在这个限制下累计有效媒体推进量与当前位置相同。
- 只有完整播放到总时长，再收到 `ended` 才标记完成；HTTP 200 本身不代表完成。
- 序号连续；完全相同的重试返回原确认结果，不重复计时；修改后的重复序号会被拒绝。
- 每次客户端收到确认后，再查询 `/progress` 比较位置、时长、序号、播放状态等，然后写客户端断点。
- 只有网络故障和 HTTP 5xx 会重试，最多额外 3 次，采用短暂指数退避；协议校验失败直接报错。

为了避免网络与文件写入延迟导致本地时钟误差，每个周期从上次确认后重新计时。因此墙上总耗时可能略长于媒体时长。

## 结果文件

输出目录默认是程序旁的 `run-state`，可用 `--output` 指定：

| 文件 | 内容 |
| --- | --- |
| `server-progress.json` | 模拟服务的持久进度；恢复的依据 |
| `client-checkpoint.json` | 客户端最近一次核对通过的断点 |
| `events.jsonl` | 本机接收的测试事件及确认结果；重试事件可能重复出现 |
| `summary.json` | 本次执行结果、重试次数、核对次数及完成状态 |

这些文件均使用虚构测试标识。服务状态和客户端断点采用临时文件替换方式保存；异常结束后，恢复仍以服务端已保存的位置为准。无需删除或修改原分析数据。

已保留 `verified-demo` 和 `verified-resume` 两组实际运行结果，便于直接查看。

## 测试

```powershell
python -m unittest -v
```

覆盖暂停不计时、跳进度拒绝、签名/身份/序号校验、幂等重试、提前结束拒绝、离线恢复、外部地址和重定向拒绝，以及“服务已记录但客户端被中断”的恢复路径。

真实适配器的测试使用内存拦截器，不建立真实 HTTP 连接；覆盖真实 host/路径/签名/query、`otherInfo` 嵌入参数、新令牌绑定、1 倍计时、断点恢复、未知结果保留、学习限制停止和响应通过状态。
